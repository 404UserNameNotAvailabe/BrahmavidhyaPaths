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
| POST   | `/check?semantic=bool` | `{ "text": "…" }` | `{ status, data: { verdict: "exact"\|"contains"\|"review"\|"new", matches: [{ id, year, date, link, overlap_pct, shared_phrase_words, kind, semantic_pct, matched_snippet }], unembedded_count, semantic_used } }` |

**Curation** (add / import / edit — used by the Saint's Review console):

| Method | Path                       | Body / Query | Returns |
|--------|----------------------------|--------------|---------|
| POST   | `/add`                     | `{ text, message_date?, tags?, source?, link? }` | `{ status, message, id? }` |
| PATCH  | `/messages/{id}`           | partial `{ text?, message_date?, tags?, source?, link?, is_favorite? }` | `{ status, message, id }` |
| DELETE | `/messages/{id}`           | — | `{ status, id }` |
| POST   | `/import`                  | `{ rows: [{ message, date?, tags?, source?, link? }] }` | `{ status, data: { added, skipped, errors } }` |
| GET    | `/stats`                   | — | `{ status, data: { total, pending_embeddings, semantic_enabled, backfill } }` |
| POST   | `/embeddings/backfill`     | — | starts the paced background embedder |

**Candidates** (see `CANDIDATES.md`; AI work happens in the background worker, never in a request):

| Method | Path | Role | Body / Query | Returns `data` |
|--------|------|------|--------------|----------------|
| GET | `/topics` | any | — | `topics: [{id, name, sort_order, is_active, archive_count, candidate_count}]` |
| POST | `/topics` | admin | `{name}` | `{id}` |
| PATCH | `/topics/{id}` | admin | `{name?, sort_order?, is_active?}` (rename rewrites every tag) | — |
| POST | `/candidates/preview` | editor | `{collection, rows: [{text, topics?, ref?, sheet_row?}]}` | per row `errors, archive, in_pool, repeat_of_index, doubled_word, topics` + `summary` (rules only, no AI) |
| POST | `/candidates/import` | editor | same as preview | `{added, skipped, errors}`; already-given rows saved as rejected |
| GET | `/candidates` | any | `verdict, topic, status, collection, q, topics_unconfirmed, order, limit≤200, offset` | `entries, total, counts, waiting, pool_total, collections` |
| GET | `/candidates/{id}` | any | — | entry + `matches, topic_history, duplicate` |
| PATCH | `/candidates/{id}` | editor | `{text?, topics?, ref?, collection?}` | text re-queues; topics → confirmed |
| POST | `/candidates/{id}/status` | editor / admin | `{status, note?, target_season?}` | `{status, target_season}` |
| POST | `/candidates/{id}/give` | editor | `{message_date, link}` | `{message_id}` |
| POST | `/candidates/recheck` | editor | `{ids?}` | `{queued}` |
| DELETE | `/candidates/{id}` | admin | — | — |

`GET /stats` adds `candidate_checks: {queued, running, failed, done, daily_limit, used_today, limit_reached}`. `GET /archive` adds `topics_unreviewed` and returns `tags_reviewed`.

**Archive + taxonomy** (editorial metadata):

| Method | Path               | Body / Query | Returns |
|--------|--------------------|--------------|---------|
| GET    | `/archive`         | `?season=&q=&tag=&limit=` | `{ status, data: { entries: [{ id, message, date, year, season, link, tags }], seasons } }` |
| GET    | `/tags`            | — | `{ status, data: { tags: [{ name, count }] } }` |

- `confidence_score` is the blended 0–100 hybrid score; `matched_snippet` has `<mark>` around overlapping words. Match floor 18, unique threshold 35 (tune in `matching.py`).
- **Tags are archive-only** — free `text[]` (category was merged into tags). The duplication engine never reads them.
- **Dhanurmas dates** — every `message_date` must fall in 16 Dec – 14 Jan (API validation + DB check `ck_messages_dhanurmas`).
- **Original link** — `link` is an optional http(s) URL to where the vachan was first published (DB check `ck_messages_link`).
- Editing a message's text re-embeds it inline; bulk import leaves embeddings NULL and the console auto-triggers the background backfill.
