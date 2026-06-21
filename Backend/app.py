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
from database import close_pool, get_connection, return_connection
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

    conn = get_connection()
    try:
        # No parameters → psycopg uses the simple protocol, which runs the
        # whole multi-statement script in one call.
        with conn.cursor() as cur:
            cur.execute(script)
        logger.info("Schema ensured.")
    except Exception as exc:
        logger.warning("Schema auto-init failed (%s) — continuing.", exc)
    finally:
        return_connection(conn)


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
    uncategorized: bool = False,
    untagged: bool = False,
    order: str = "desc",
    limit: int = 500,
):
    """
    Chronological ledger of archived messages for the Archive page.
    Filters: `year`, `q` (text search), `category`, `tag`, `uncategorized`,
    `untagged`. `order` = "asc" | "desc" by date. Each entry carries a
    `pending` flag (embedding not yet computed).
    """
    limit = max(1, min(limit, 1000))
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
    if uncategorized:
        clauses.append("category IS NULL")
    if untagged:
        clauses.append("(tags IS NULL OR cardinality(tags) = 0)")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, message_text, message_date, year, category, tags,
                   (embedding IS NULL) AS pending
            FROM messages
            {where}
            ORDER BY message_date {direction} NULLS LAST, id {direction}
            LIMIT {limit}
            """,
            params,
        )
        rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"entries": []}}
    finally:
        return_connection(conn)

    entries = [
        {
            "id": str(r[0]),
            "message": r[1],
            "date": r[2].isoformat() if r[2] else None,
            "year": r[3],
            "category": r[4] or "",
            "tags": r[5] or [],
            "pending": bool(r[6]),
        }
        for r in rows
    ]

    years = sorted({e["year"] for e in entries if e["year"] is not None}, reverse=True)

    return {"status": "success", "data": {"entries": entries, "years": years}}


@app.post("/check")
async def check_path(payload: CheckRequest):
    """
    Find archive entries that overlap the proposed message, ranked by a
    hybrid confidence score (semantic + trigram + token overlap).
    """
    query = payload.text.strip()
    query_norm = matching.normalize_text(query)
    query_tokens = matching.tokenize(query)

    # Semantic query vector (None when embeddings are disabled / fail).
    qvec = embeddings.to_pgvector(embeddings.embed_query(query))

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

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": {"is_unique": True, "matches": []}}
    finally:
        return_connection(conn)

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
    embedding = embeddings.to_pgvector(embeddings.embed_document(message_text))

    conn = get_connection()
    try:
        cur = conn.cursor()
        category = taxonomy.ensure_category(cur, payload.category)
        cur.execute(
            """
            INSERT INTO messages
                (message_text, message_date, category, tags, embedding)
            VALUES
                (%s, %s, %s, %s, %s::vector)
            ON CONFLICT (normalized_text) DO NOTHING
            RETURNING id
            """,
            (
                message_text,
                payload.message_date,
                category,
                tags,
                embedding,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": "Message already exists"}

        return {"status": "success", "message": "Message added successfully", "id": row[0]}

    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)


@app.patch("/messages/{message_id}")
async def update_message(message_id: int, payload: UpdateRequest):
    """Partial update of one message. Changing the text re-embeds it."""
    provided = payload.model_fields_set
    if not provided:
        return {"status": "error", "message": "No fields to update"}

    conn = get_connection()
    try:
        cur = conn.cursor()

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

        if not sets:
            return {"status": "error", "message": "No fields to update"}

        params.append(message_id)
        cur.execute(
            f"UPDATE messages SET {', '.join(sets)} WHERE id = %s RETURNING id",
            params,
        )
        if cur.fetchone() is None:
            return {"status": "error", "message": "Message not found"}
        return {"status": "success", "message": "Message updated", "id": message_id}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)


@app.delete("/messages/{message_id}")
async def delete_message(message_id: int):
    """Delete one message."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE id = %s RETURNING id", (message_id,))
        if cur.fetchone() is None:
            return {"status": "error", "message": "Message not found"}
        return {"status": "success", "message": "Message deleted", "id": message_id}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)


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

    conn = get_connection()
    try:
        cur = conn.cursor()
        for i, row in enumerate(payload.rows):
            text = row.message.strip()
            try:
                category = taxonomy.ensure_category(cur, row.category)
                cur.execute(
                    """
                    INSERT INTO messages (message_text, message_date, category, tags)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (normalized_text) DO NOTHING
                    RETURNING id
                    """,
                    (text, row.message_date, category, taxonomy.clean_tags(row.tags)),
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
    finally:
        return_connection(conn)


@app.get("/stats")
async def stats():
    """Archive counts + embedding backfill progress (for the console UI)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM messages"
        )
        total, pending = cur.fetchone()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)

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
    conn = get_connection()
    try:
        cur = conn.cursor()
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
    finally:
        return_connection(conn)

    return {
        "status": "success",
        "data": {"categories": [{"name": r[0], "count": r[1]} for r in rows]},
    }


@app.post("/categories")
async def create_category(payload: CategoryRequest):
    """Add a category to the managed vocabulary."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        name = taxonomy.ensure_category(cur, payload.name)
        return {"status": "success", "name": name}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)


@app.delete("/categories/{name}")
async def delete_category(name: str):
    """Remove a category (messages keep their data, category cleared to NULL)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM categories WHERE name = %s RETURNING name", (name,))
        if cur.fetchone() is None:
            return {"status": "error", "message": "Category not found"}
        # No FK, so detach the category from any messages that used it.
        cur.execute("UPDATE messages SET category = NULL WHERE category = %s", (name,))
        return {"status": "success", "name": name}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        return_connection(conn)


@app.get("/tags")
async def list_tags():
    """Distinct tags in use, most-used first (for autocomplete + filters)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
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
    finally:
        return_connection(conn)

    return {
        "status": "success",
        "data": {"tags": [{"name": r[0], "count": r[1]} for r in rows]},
    }
