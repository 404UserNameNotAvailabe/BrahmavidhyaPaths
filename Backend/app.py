import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import embeddings
import matching
import taxonomy
import worker
from config import CORS_ORIGINS
from database import close_pool, db_cursor
from models import (
    AddRequest,
    CategoryRequest,
    CheckRequest,
    ImportRequest,
    UpdateRequest,
)


logger = logging.getLogger("brahmavidya.app")

SCHEMA_SQL = Path(__file__).resolve().parent / "sql" / "schema.sql"


def init_schema() -> None:
    """Apply the idempotent schema on startup so the DB always matches the
    code (extensions, tables, new columns, indexes). Safe to run every time —
    every statement is CREATE/ALTER ... IF NOT EXISTS. Logs and continues on
    failure rather than blocking the app."""
    try:
        script = SCHEMA_SQL.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read schema.sql (%s) — skipping auto-init.", exc)
        return

    try:
        # No parameters → psycopg uses the simple protocol, which runs the
        # whole multi-statement script in one call.
        with db_cursor() as cur:
            cur.execute(script)
        logger.info("Schema ensured.")
    except Exception as exc:
        logger.warning("Schema auto-init failed (%s) — continuing.", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    yield
    # Close the connection pool cleanly on shutdown.
    close_pool()


app = FastAPI(title="Brahmavidya Path Checker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok", "semantic": embeddings.is_enabled()}


@app.get("/archive")
async def list_archive(
    year: int | None = None,
    q: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    favorite: bool = False,
    uncategorized: bool = False,
    untagged: bool = False,
    order: str = "desc",
    limit: int = 25,
    offset: int = 0,
):
    """
    Paginated ledger of archived messages for the Archive page.
    Filters: `year`, `q` (text search), `category`, `tag`, `uncategorized`,
    `untagged`. `order` = "asc" | "desc" by date. Returns `total` (filtered
    count, for pagination) and `years` (all distinct years, for the picker).
    Each entry carries a `pending` flag (embedding not yet computed).
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    direction = "ASC" if order.lower() == "asc" else "DESC"

    clauses = []
    params: list = []
    if year is not None:
        clauses.append("year = %s")
        params.append(year)
    if q:
        clauses.append("message_text ILIKE %s")
        params.append(f"%{q.strip()}%")
    if category:
        clauses.append("category = %s")
        params.append(category.strip())
    if tag:
        clauses.append("%s = ANY(tags)")
        params.append(tag.strip())
    if source:
        clauses.append("source = %s")
        params.append(source.strip())
    if favorite:
        clauses.append("is_favorite")
    if uncategorized:
        clauses.append("category IS NULL")
    if untagged:
        clauses.append("(tags IS NULL OR cardinality(tags) = 0)")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    try:
        with db_cursor() as cur:
            cur.execute(f"SELECT count(*) FROM messages {where}", params)
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT id, message_text, message_date, year, category, tags,
                       (embedding IS NULL) AS pending, source, is_favorite
                FROM messages
                {where}
                ORDER BY message_date {direction} NULLS LAST, id {direction}
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

            # All distinct years (independent of filters / page) for the picker.
            cur.execute(
                "SELECT DISTINCT year FROM messages WHERE year IS NOT NULL ORDER BY year DESC"
            )
            years = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "data": {"entries": [], "years": [], "total": 0},
        }

    entries = [
        {
            "id": str(r[0]),
            "message": r[1],
            "date": r[2].isoformat() if r[2] else None,
            "year": r[3],
            "category": r[4] or "",
            "tags": r[5] or [],
            "pending": bool(r[6]),
            "source": r[7] or "",
            "is_favorite": bool(r[8]),
        }
        for r in rows
    ]

    return {"status": "success", "data": {"entries": entries, "years": years, "total": total}}


@app.post("/check")
async def check_path(payload: CheckRequest, semantic: bool = False):
    """
    Find archive entries that overlap the proposed message.

    Default (fast, sub-second): trigram + word-overlap on normalized text.
    semantic=true: also embeds the query via Gemini for meaning-level matches.
    The embedding is a network round-trip (~0.5s warm), so it is opt-in to keep
    the everyday check instant.
    """
    query = payload.text.strip()
    query_norm = matching.normalize_text(query)
    query_tokens = matching.tokenize(query)

    # Semantic adds a Gemini embedding call — opt-in so the default stays fast.
    qvec = embeddings.to_pgvector(embeddings.embed_query(query)) if semantic else None

    if qvec is not None:
        sem_expr = "1 - (embedding <=> %(qvec)s::vector)"
        sem_order = "COALESCE(1 - (embedding <=> %(qvec)s::vector), 0)"
    else:
        sem_expr = "NULL"
        sem_order = "0"

    # Duplication is purely text-based — no category/tags here by design.
    sql = f"""
        SELECT
            id, message_text, message_date, year,
            similarity(normalized_text, %(q)s) AS trgm,
            {sem_expr} AS sem
        FROM messages
        ORDER BY GREATEST(similarity(normalized_text, %(q)s), {sem_order}) DESC
        LIMIT 50
    """
    params = {"q": query_norm}
    if qvec is not None:
        params["qvec"] = qvec

    try:
        with db_cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"is_unique": True, "matches": []}}

    matches = []
    for row in rows:
        msg_id, msg_text, msg_date, year, trgm, sem = row

        row_tokens = matching.tokenize(msg_text)
        matched_tokens = query_tokens & row_tokens
        token_sim = matching.token_overlap(query_tokens, row_tokens)

        score = matching.combine_score(
            trigram=float(trgm or 0.0),
            token=token_sim,
            semantic=float(sem) if sem is not None else None,
        )

        if score < matching.MATCH_FLOOR:
            continue

        matches.append({
            "id": str(msg_id),
            "year": year,
            "date": msg_date.isoformat() if msg_date else None,
            "matched_snippet": matching.highlight_overlap(msg_text, matched_tokens),
            "confidence_score": score,
        })

    matches.sort(key=lambda m: m["confidence_score"], reverse=True)
    matches = matches[:20]

    best = matches[0]["confidence_score"] if matches else 0.0
    is_unique = best < matching.UNIQUE_THRESHOLD

    return {
        "status": "success",
        "data": {"is_unique": is_unique, "matches": matches},
    }


@app.post("/add")
async def add_path(payload: AddRequest):
    """Add a new archive entry, rejecting exact normalized duplicates.

    normalized_text and year are DB-generated, so we only supply text, date,
    theme, and embedding. ON CONFLICT makes the insert atomic against the
    unique normalized_text index.
    """
    message_text = payload.text.strip()
    tags = taxonomy.clean_tags(payload.tags)
    source = (payload.source or "").strip() or taxonomy.extract_source(message_text)
    embedding = embeddings.to_pgvector(embeddings.embed_document(message_text))

    try:
        with db_cursor() as cur:
            category = taxonomy.ensure_category(cur, payload.category)
            cur.execute(
                """
                INSERT INTO messages
                    (message_text, message_date, category, tags, source, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT (normalized_text) DO NOTHING
                RETURNING id
                """,
                (
                    message_text,
                    payload.message_date,
                    category,
                    tags,
                    source,
                    embedding,
                ),
            )
            row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": "Message already exists"}

        return {"status": "success", "message": "Message added successfully", "id": row[0]}

    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.patch("/messages/{message_id}")
async def update_message(message_id: int, payload: UpdateRequest):
    """Partial update of one message. Changing the text re-embeds it."""
    provided = payload.model_fields_set
    if not provided:
        return {"status": "error", "message": "No fields to update"}

    try:
        with db_cursor() as cur:
            sets: list[str] = []
            params: list = []

            if "text" in provided:
                text = (payload.text or "").strip()
                if len(text) < 2:
                    return {"status": "error", "message": "Message text too short"}
                sets.append("message_text = %s")
                params.append(text)
                # Text changed → the old embedding is stale. Recompute inline.
                sets.append("embedding = %s::vector")
                params.append(embeddings.to_pgvector(embeddings.embed_document(text)))

            if "message_date" in provided:
                sets.append("message_date = %s")
                params.append(payload.message_date)

            if "category" in provided:
                # Empty string clears the category.
                category = taxonomy.ensure_category(cur, payload.category)
                sets.append("category = %s")
                params.append(category)

            if "tags" in provided:
                sets.append("tags = %s")
                params.append(taxonomy.clean_tags(payload.tags))

            if "source" in provided:
                sets.append("source = %s")
                params.append((payload.source or "").strip() or None)

            if "is_favorite" in provided:
                sets.append("is_favorite = %s")
                params.append(bool(payload.is_favorite))

            if not sets:
                return {"status": "error", "message": "No fields to update"}

            params.append(message_id)
            cur.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = %s RETURNING id",
                params,
            )
            found = cur.fetchone()

        if found is None:
            return {"status": "error", "message": "Message not found"}
        return {"status": "success", "message": "Message updated", "id": message_id}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.delete("/messages/{message_id}")
async def delete_message(message_id: int):
    """Delete one message."""
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM messages WHERE id = %s RETURNING id", (message_id,))
            found = cur.fetchone()
        if found is None:
            return {"status": "error", "message": "Message not found"}
        return {"status": "success", "message": "Message deleted", "id": message_id}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.post("/import")
async def import_messages(payload: ImportRequest):
    """
    Bulk insert rows (parsed client-side from CSV or pasted text). Embeddings
    are left NULL — trigger POST /embeddings/backfill afterwards to fill them.
    Returns a per-row summary.
    """
    added = 0
    skipped = 0
    errors: list[dict] = []

    with db_cursor() as cur:
        for i, row in enumerate(payload.rows):
            text = row.message.strip()
            try:
                category = taxonomy.ensure_category(cur, row.category)
                source = (row.source or "").strip() or taxonomy.extract_source(text)
                cur.execute(
                    """
                    INSERT INTO messages (message_text, message_date, category, tags, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (normalized_text) DO NOTHING
                    RETURNING id
                    """,
                    (text, row.message_date, category, taxonomy.clean_tags(row.tags), source),
                )
                if cur.fetchone() is None:
                    skipped += 1
                else:
                    added += 1
            except Exception as exc:
                errors.append({"row": i + 1, "message": str(exc)})

    return {
        "status": "success",
        "data": {"added": added, "skipped": skipped, "errors": errors},
    }


@app.get("/stats")
async def stats():
    """Archive counts + embedding backfill progress (for the console UI)."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM messages"
            )
            total, pending = cur.fetchone()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "success",
        "data": {
            "total": total,
            "pending_embeddings": pending,
            "semantic_enabled": embeddings.is_enabled(),
            "backfill": worker.status(),
        },
    }


@app.post("/embeddings/backfill")
async def start_backfill():
    """Kick off the background embedder for rows missing an embedding."""
    if not embeddings.is_enabled():
        return {"status": "error", "message": "Semantic embeddings are not configured"}
    started = worker.start()
    return {
        "status": "success",
        "started": started,
        "message": "Backfill running" if started else "Backfill already running",
        "backfill": worker.status(),
    }


# --- Archive taxonomy (categories + tags) ---------------------------------


@app.get("/categories")
async def list_categories():
    """Managed categories with how many messages use each."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT c.name, count(m.id) AS uses
                FROM categories c
                LEFT JOIN messages m ON m.category = c.name
                GROUP BY c.name
                ORDER BY c.name
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"categories": []}}

    return {
        "status": "success",
        "data": {"categories": [{"name": r[0], "count": r[1]} for r in rows]},
    }


@app.post("/categories")
async def create_category(payload: CategoryRequest):
    """Add a category to the managed vocabulary."""
    try:
        with db_cursor() as cur:
            name = taxonomy.ensure_category(cur, payload.name)
        return {"status": "success", "name": name}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.delete("/categories/{name}")
async def delete_category(name: str):
    """Remove a category (messages keep their data, category cleared to NULL)."""
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM categories WHERE name = %s RETURNING name", (name,))
            if cur.fetchone() is None:
                return {"status": "error", "message": "Category not found"}
            # No FK, so detach the category from any messages that used it.
            cur.execute("UPDATE messages SET category = NULL WHERE category = %s", (name,))
        return {"status": "success", "name": name}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/tags")
async def list_tags():
    """Distinct tags in use, most-used first (for autocomplete + filters)."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT tag, count(*) AS uses
                FROM messages, unnest(tags) AS tag
                GROUP BY tag
                ORDER BY uses DESC, tag
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"tags": []}}

    return {
        "status": "success",
        "data": {"tags": [{"name": r[0], "count": r[1]} for r in rows]},
    }


@app.get("/sources")
async def list_sources():
    """Distinct speaker/source attributions in use, most-used first."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT source, count(*) AS uses
                FROM messages
                WHERE source IS NOT NULL AND btrim(source) <> ''
                GROUP BY source
                ORDER BY uses DESC, source
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"sources": []}}

    return {
        "status": "success",
        "data": {"sources": [{"name": r[0], "count": r[1]} for r in rows]},
    }


# --- Archive insights -----------------------------------------------------


@app.get("/overview")
async def overview():
    """Headline stats for the archive: total, date span, per-year counts."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT count(*), min(message_date), max(message_date),
                       count(*) FILTER (WHERE is_favorite)
                FROM messages
                """
            )
            total, earliest, latest, favorites = cur.fetchone()
            cur.execute(
                "SELECT year, count(*) FROM messages WHERE year IS NOT NULL "
                "GROUP BY year ORDER BY year"
            )
            per_year = [{"year": r[0], "count": r[1]} for r in cur.fetchall()]
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "success",
        "data": {
            "total": total,
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "favorites": favorites,
            "per_year": per_year,
        },
    }


@app.get("/onthisday")
async def on_this_day():
    """Vachans given on today's month+day in past years."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT id, message_text, message_date, year, source
                FROM messages
                WHERE message_date IS NOT NULL
                  AND extract(month FROM message_date) = extract(month FROM CURRENT_DATE)
                  AND extract(day   FROM message_date) = extract(day   FROM CURRENT_DATE)
                ORDER BY message_date DESC
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"entries": []}}

    return {
        "status": "success",
        "data": {
            "entries": [
                {
                    "id": str(r[0]),
                    "message": r[1],
                    "date": r[2].isoformat() if r[2] else None,
                    "year": r[3],
                    "source": r[4] or "",
                }
                for r in rows
            ]
        },
    }


@app.get("/duplicates")
async def duplicates(threshold: float = 0.55, limit: int = 50):
    """
    Near-duplicate vachan PAIRS already in the archive (trigram similarity),
    so admins can merge/delete. O(n²) self-join — fine for a small corpus.
    """
    threshold = min(max(threshold, 0.3), 0.95)
    limit = max(1, min(limit, 200))
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.message_text, a.message_date,
                       b.id, b.message_text, b.message_date,
                       similarity(a.normalized_text, b.normalized_text) AS sim
                FROM messages a
                JOIN messages b ON a.id < b.id
                WHERE similarity(a.normalized_text, b.normalized_text) >= %s
                ORDER BY sim DESC
                LIMIT %s
                """,
                (threshold, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"pairs": []}}

    pairs = [
        {
            "a": {"id": str(r[0]), "message": r[1], "date": r[2].isoformat() if r[2] else None},
            "b": {"id": str(r[3]), "message": r[4], "date": r[5].isoformat() if r[5] else None},
            "similarity": round(float(r[6]) * 100, 1),
        }
        for r in rows
    ]
    return {"status": "success", "data": {"pairs": pairs}}
