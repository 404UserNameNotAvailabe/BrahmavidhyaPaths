# Brahmavidya Path Checker — Backend Setup

Hybrid duplication engine for the Dhanurmas message archive. Combines three
signals on normalized Gujarati text:

- **semantic** — Gemini embedding cosine similarity (meaning-level)
- **trigram** — `pg_trgm` character similarity (reworded / typo level)
- **token** — Jaccard word overlap (literal shared words)

Semantic is optional: without `GEMINI_API_KEY` the engine runs on trigram +
token only and stays fully functional.

## 1. Install dependencies

```bash
cd Backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure environment

```bash
cp .env.example .env
# Fill DB_* (managed Postgres → DB_SSLMODE=require) and, optionally, GEMINI_API_KEY.
```

## 3. Create the database

```bash
# Authoritative schema (extensions, messages table, indexes, trigger):
psql "$DATABASE_URL" -f sql/schema.sql

# Real Dhanurmas corpus (16 Dec 2016 – 14 Jan 2017, 30 messages):
psql "$DATABASE_URL" -f sql/seed_dhanurmas_2016-2017.sql
```

`schema.sql` is the single source of truth — `normalized_text` and `year` are
generated columns (the DB derives them; the app never sets them).

> If your managed Postgres does not expose `pgvector`, drop the
> `CREATE EXTENSION vector;` line, the `embedding` column, and its index from
> `schema.sql`. The engine degrades to trigram + token automatically.

## 4. Backfill embeddings (only if using Gemini)

```bash
python -m scripts.backfill_embeddings
```

## 5. Run

```bash
python -m uvicorn app:app --reload   # http://localhost:8000
```

`GET /health` reports `{ "status": "ok", "semantic": true|false }`.

## API contract (consumed by the React frontend)

**Duplication** (text-only — no categorization here by design):

| Method | Path     | Body / Query                         | Returns |
|--------|----------|--------------------------------------|---------|
| POST   | `/check` | `{ "text": "…" }` (legacy `path` ok) | `{ status, data: { is_unique, matches: [{ id, year, date, matched_snippet, confidence_score }] } }` |

**Curation** (add / import / edit — used by the Saint's Review console):

| Method | Path                       | Body / Query | Returns |
|--------|----------------------------|--------------|---------|
| POST   | `/add`                     | `{ text, message_date?, category?, tags? }` | `{ status, message, id? }` |
| PATCH  | `/messages/{id}`           | partial `{ text?, message_date?, category?, tags? }` | `{ status, message, id }` |
| DELETE | `/messages/{id}`           | — | `{ status, id }` |
| POST   | `/import`                  | `{ rows: [{ message, date?, category?, tags? }] }` | `{ status, data: { added, skipped, errors } }` |
| GET    | `/stats`                   | — | `{ status, data: { total, pending_embeddings, semantic_enabled, backfill } }` |
| POST   | `/embeddings/backfill`     | — | starts the paced background embedder |

**Archive + taxonomy** (editorial metadata):

| Method | Path               | Body / Query | Returns |
|--------|--------------------|--------------|---------|
| GET    | `/archive`         | `?year=&q=&category=&tag=&limit=` | `{ status, data: { entries: [{ id, message, date, year, category, tags }], years } }` |
| GET    | `/categories`      | — | `{ status, data: { categories: [{ name, count }] } }` |
| POST   | `/categories`      | `{ name }` | `{ status, name }` |
| DELETE | `/categories/{name}` | — | `{ status, name }` |
| GET    | `/tags`            | — | `{ status, data: { tags: [{ name, count }] } }` |

- `confidence_score` is the blended 0–100 hybrid score; `matched_snippet` has `<mark>` around overlapping words. Match floor 18, unique threshold 35 (tune in `matching.py`).
- **Categorization is archive-only** — `category` (managed vocabulary, FK) + `tags` (free `text[]`). The duplication engine never reads them.
- Editing a message's text re-embeds it inline; bulk import leaves embeddings NULL and the console auto-triggers the background backfill.
