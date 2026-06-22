# Brahmavidya — Deployment Runbook

```
Browser ──▶ Cloudflare ──▶ CF Worker (frontend, TanStack SSR)
                                 │  HTTPS fetch
                                 ▼
                         CF Tunnel ──▶ Proxmox LXC: FastAPI (uvicorn @127.0.0.1:8000, systemd)
                                 │
                                 ▼
                           Neon Postgres (managed, SSL)
```

- **Frontend**: Cloudflare Workers (`brahmavidya.<domain>`)
- **Backend**: FastAPI in a Proxmox LXC under systemd, exposed via Cloudflare Tunnel (`api.brahmavidya.<domain>`) — no inbound ports opened.
- **DB**: Neon (already provisioned).
- **Auth**: custom (users + sessions), roles viewer/editor/admin. Whole app is private.

Replace `<domain>`, `<repo-url>`, `<frontend-repo-url>`, `<TUNNEL_ID>` as you go.

---

## Prerequisites

- Cloudflare account with `<domain>` on it (Zero Trust not required).
- Neon connection details (host/db/user/password).
- A **GCP project with Vertex AI enabled** + a service-account JSON key
  (`roles/aiplatform.user`) for embeddings — or skip embeddings entirely (see §1.4).
- Proxmox host.

---

## Part 1 — Backend on a Proxmox LXC

### 1.1 Create the LXC

Create an **unprivileged LXC** (Proxmox UI or `pct create`):
- Template: **Ubuntu 24.04** (ships Python 3.12) or Debian 12 (Python 3.11). Avoid 3.14.
- 1 vCPU, 1 GB RAM, 8 GB disk is plenty.
- Networking: DHCP is fine — it only needs **outbound** internet (Neon + Cloudflare). No port forwarding.

Enter the container: `pct enter <vmid>`.

### 1.2 Install dependencies

```bash
apt update && apt install -y python3 python3-venv python3-pip git postgresql-client curl
```

### 1.3 Deploy the app

```bash
# dedicated, unprivileged service user
adduser --system --group --home /opt/brahmavidya brahmavidya

cd /opt/brahmavidya
git clone <repo-url> app
cd app/Backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chown -R brahmavidya:brahmavidya /opt/brahmavidya
```

### 1.4 Configure `.env` (production)

```bash
cp .env.example .env
nano .env
```

Set:

```ini
DB_HOST=...neon...        DB_NAME=neondb   DB_USER=...   DB_PASSWORD=...
DB_PORT=5432             DB_SSLMODE=require

# Embeddings via Vertex AI + ADC (paid tier → Google does NOT train on your data).
GEMINI_API_KEY=                              # blank ⇒ ADC mode
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1            # or asia-south1 (Mumbai); verify model availability
GOOGLE_APPLICATION_CREDENTIALS=/opt/brahmavidya/gcp-sa.json
GEMINI_EMBED_MODEL=gemini-embedding-001
EMBED_DIM=768

SESSION_TTL_HOURS=168            # 7-day logins
LOGIN_MAX_FAILS=5
LOGIN_WINDOW_SECONDS=900

# IMPORTANT — lock CORS to the production frontend origin only:
CORS_ORIGINS=https://brahmavidya.<domain>
```

```bash
chmod 600 .env && chown brahmavidya:brahmavidya .env
```

> **Vertex service-account key (for embeddings).** Do the one-time GCP setup
> (enable Vertex AI API, create a service account with `roles/aiplatform.user`,
> download its JSON key), then put the key on the box:
> ```bash
> install -o brahmavidya -g brahmavidya -m 600 brahmavidya-sa.json /opt/brahmavidya/gcp-sa.json
> ```
> ADC reads `GOOGLE_APPLICATION_CREDENTIALS` automatically — no systemd change.
> To run **without** any AI (fully self-contained), just leave `GEMINI_API_KEY`,
> `GOOGLE_CLOUD_PROJECT` blank — matching stays on trigram + word-overlap.

### 1.5 Apply the schema + create admin user(s)

```bash
DBURL=$(.venv/bin/python -c "from config import *; from psycopg.conninfo import make_conninfo; print(make_conninfo(host=DB_HOST,port=DB_PORT,dbname=DB_NAME,user=DB_USER,password=DB_PASSWORD,sslmode=DB_SSLMODE))")
psql "$DBURL" -f sql/schema.sql           # creates/updates all tables incl. users + sessions

# create accounts (prompts for username, role [admin/editor/viewer], password)
.venv/bin/python -m scripts.create_user   # make yourself admin
.venv/bin/python -m scripts.create_user   # helpers as editor / viewer
```

> The app also auto-applies `schema.sql` on every startup, so future schema changes need no manual step — just restart the service.

### 1.6 systemd service for the API

Copy the unit (provided at `Backend/deploy/brahmavidya-api.service`) and enable it:

```bash
cp /opt/brahmavidya/app/Backend/deploy/brahmavidya-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now brahmavidya-api
systemctl status brahmavidya-api          # should be active (running)
journalctl -u brahmavidya-api -f          # logs — look for "Schema ensured."
```

### 1.7 Verify locally

```bash
curl -s http://127.0.0.1:8000/health      # {"status":"ok","semantic":true}
curl -s http://127.0.0.1:8000/archive     # {"detail":"Authentication required"}  ← correct (private)
```

---

## Part 2 — Cloudflare Tunnel (expose the backend)

### 2.1 Install cloudflared (in the LXC)

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
dpkg -i /tmp/cf.deb
```

### 2.2 Authenticate + create the tunnel

```bash
cloudflared tunnel login                          # opens a URL; authorize <domain>
cloudflared tunnel create brahmavidya             # prints the TUNNEL_ID + creds json path
cloudflared tunnel route dns brahmavidya api.brahmavidya.<domain>
```

### 2.3 Config + run as a service

Create `/etc/cloudflared/config.yml` (template at `Backend/deploy/cloudflared-config.yml`):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: api.brahmavidya.<domain>
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
cloudflared service install        # installs + starts the systemd service using the config above
systemctl status cloudflared
```

### 2.4 Verify publicly

```bash
curl -s https://api.brahmavidya.<domain>/health   # {"status":"ok",...} over HTTPS
```

---

## Part 3 — Frontend on Cloudflare Workers

On your **dev machine**, in the frontend repo (`<frontend-repo-url>`):

### 3.1 Point it at the backend

```bash
echo "VITE_API_URL=https://api.brahmavidya.<domain>" > .env.production
```

### 3.2 Build + deploy

TanStack Start builds a Cloudflare Workers module by default — `dist/server/server.js`
exports `{ fetch(request, env, ctx) }` (the Workers signature). `wrangler.toml` already
points `main` at it with the `dist/client` assets binding.

```bash
bun install
bunx wrangler login          # once, authorizes your CF account
bun run build
bunx wrangler dev            # optional: smoke-test the Worker locally first
bunx wrangler deploy
```

### 3.3 Custom domain

In the Cloudflare dashboard → your Worker → **Settings → Domains & Routes**, add
`brahmavidya.<domain>`. (Must match `CORS_ORIGINS` in the backend `.env` exactly.)

Open `https://brahmavidya.<domain>` → you should hit the **login screen**. Sign in with the admin
account from step 1.5.

---

## Part 4 — Lock it down (Cloudflare dashboard)

1. **Rate-limit login + expensive reads** — Security → WAF → Rate limiting rules on the
   `api.brahmavidya.<domain>` zone:
   - `POST /auth/login` → e.g. 10 req / 10 min per IP (brute-force protection).
   - `POST /check`, `GET /duplicates` → e.g. 60 req / min per IP.
2. **CORS** is already restricted to the Worker origin via `CORS_ORIGINS`.
3. **Backend binds to 127.0.0.1 only** (systemd `--host 127.0.0.1`) — never publicly reachable
   except through the tunnel.
4. (Optional) Neon → restrict allowed IPs / use a dedicated role for the app.

---

## Part 5 — Maintenance

**Deploy a backend update:**
```bash
cd /opt/brahmavidya/app && git pull
cd Backend && .venv/bin/pip install -r requirements.txt   # if deps changed
systemctl restart brahmavidya-api                          # schema auto-applies on boot
```

**Deploy a frontend update:** `bun run build && bunx wrangler deploy`.

**Reset a password / add a user:** `.venv/bin/python -m scripts.create_user` (re-using a username updates it).

**Backfill embeddings after a big import:** the Curation "Generate embeddings" button, or
`.venv/bin/python -m scripts.backfill_embeddings`.

**Clean up expired sessions (optional cron):**
```bash
psql "$DBURL" -c "DELETE FROM sessions WHERE expires_at < now();"
```

**Logs:** `journalctl -u brahmavidya-api -f` and `journalctl -u cloudflared -f`.
