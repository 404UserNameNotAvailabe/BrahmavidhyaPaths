import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import audit
import auth
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
    LoginRequest,
    UpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)

ROLES = ("viewer", "editor", "admin")


logger = logging.getLogger("brahmavidya.app")

SCHEMA_SQL = Path(__file__).resolve().parent / "sql" / "schema.sql"


def fail(message: str = "The request could not be completed.", **extra) -> dict:
    """Log the active exception server-side and return a generic error to the
    client — internal details (DB errors, stack) never leak out."""
    logger.warning("Request failed (%s)", message, exc_info=True)
    return {"status": "error", "message": message, **extra}


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
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
async def root():
    # Minimal, public, no data — for uptime checks. The React frontend (CF
    # Worker) is the UI; the legacy server-rendered page was removed.
    return {"service": "brahmavidya", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "semantic": embeddings.is_enabled()}


# --- Auth -----------------------------------------------------------------


@app.post("/auth/login")
async def login(payload: LoginRequest):
    username = payload.username.strip().lower()
    if auth.login_locked(username):
        audit.record(username, "login.locked")
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, password_hash, role FROM users WHERE username = %s", (username,)
        )
        row = cur.fetchone()
        # Always run a hash check (real or dummy) so a missing username and a
        # wrong password take the same time — no user enumeration via timing.
        ok = auth.verify_password(payload.password, row[1] if row else auth.DUMMY_HASH)
        if row is None or not ok:
            auth.record_login_fail(username)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        auth.clear_login_fails(username)
        token, expires = auth.create_session(cur, row[0])

    audit.record(username, "login")
    return {
        "status": "success",
        "token": token,
        "user": {"username": username, "role": row[2]},
        "expires_at": expires.isoformat(),
    }


@app.post("/auth/logout")
async def logout(authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if token:
        auth.delete_session(token)
    return {"status": "success"}


@app.get("/auth/me")
async def me(user: dict = Depends(auth.get_current_user)):
    return {"status": "success", "user": {"username": user["username"], "role": user["role"]}}


# --- User management (admin only) -----------------------------------------


@app.get("/users")
async def list_users(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    return {
        "status": "success",
        "data": {
            "users": [
                {
                    "id": str(r[0]),
                    "username": r[1],
                    "role": r[2],
                    "created_at": r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ]
        },
    }


@app.post("/users")
async def create_user_endpoint(
    payload: UserCreateRequest, user: dict = Depends(auth.require_admin)
):
    username = payload.username.strip().lower()
    role = payload.role.strip().lower()
    if role not in ROLES:
        return {"status": "error", "message": "Role must be viewer, editor, or admin"}
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
                """,
                (username, auth.hash_password(payload.password), role),
            )
            row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": "That username already exists"}
        audit.record(user["username"], "user.create", f"{username} ({role})")
        return {"status": "success", "id": str(row[0])}
    except Exception as exc:
        return fail()


@app.patch("/users/{user_id}")
async def update_user_endpoint(
    user_id: int, payload: UserUpdateRequest, user: dict = Depends(auth.require_admin)
):
    sets: list[str] = []
    params: list = []
    new_role = payload.role.strip().lower() if payload.role is not None else None
    if new_role is not None:
        if new_role not in ROLES:
            return {"status": "error", "message": "Role must be viewer, editor, or admin"}
        sets.append("role = %s")
        params.append(new_role)
    if payload.password is not None:
        sets.append("password_hash = %s")
        params.append(auth.hash_password(payload.password))
    if not sets:
        return {"status": "error", "message": "Nothing to update"}

    with db_cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": "User not found"}
        # Don't let the last admin be demoted.
        if row[0] == "admin" and new_role is not None and new_role != "admin":
            cur.execute("SELECT count(*) FROM users WHERE role = 'admin'")
            if cur.fetchone()[0] <= 1:
                return {"status": "error", "message": "Cannot demote the last admin"}
        params.append(user_id)
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", params)
    changed = "role+password" if (new_role and payload.password) else ("role" if new_role else "password")
    audit.record(user["username"], "user.update", f"id={user_id} ({changed})")
    return {"status": "success"}


@app.delete("/users/{user_id}")
async def delete_user_endpoint(user_id: int, user: dict = Depends(auth.require_admin)):
    if user_id == int(user["id"]):
        return {"status": "error", "message": "You cannot delete your own account"}
    with db_cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            return {"status": "error", "message": "User not found"}
        if row[0] == "admin":
            cur.execute("SELECT count(*) FROM users WHERE role = 'admin'")
            if cur.fetchone()[0] <= 1:
                return {"status": "error", "message": "Cannot delete the last admin"}
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    audit.record(user["username"], "user.delete", f"id={user_id}")
    return {"status": "success"}


@app.get("/audit")
async def list_audit(limit: int = 100, user: dict = Depends(auth.require_admin)):
    limit = max(1, min(limit, 500))
    with db_cursor() as cur:
        cur.execute(
            "SELECT at, username, action, detail FROM audit_log ORDER BY at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return {
        "status": "success",
        "data": {
            "entries": [
                {
                    "at": r[0].isoformat() if r[0] else None,
                    "username": r[1],
                    "action": r[2],
                    "detail": r[3],
                }
                for r in rows
            ]
        },
    }


@app.get("/archive")
async def list_archive(
    year: int | None = None,
    season: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
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
    user: dict = Depends(auth.get_current_user),
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
    if season is not None:
        clauses.append("season = %s")
        params.append(season)
    if date_from:
        clauses.append("message_date >= %s::date")
        params.append(date_from)
    if date_to:
        clauses.append("message_date <= %s::date")
        params.append(date_to)
    if q and q.strip():
        # Escape LIKE wildcards (\ % _) so the query is a literal substring,
        # not a pattern — '_' or '%' should match themselves, not everything.
        like = (
            q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        clauses.append(
            "(message_text ILIKE %s ESCAPE '\\' OR source ILIKE %s ESCAPE '\\')"
        )
        params.append(f"%{like}%")
        params.append(f"%{like}%")
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
                       (embedding IS NULL) AS pending, source, is_favorite, season
                FROM messages
                {where}
                ORDER BY message_date {direction} NULLS LAST, id {direction}
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

            # All distinct years + seasons (independent of filters) for pickers.
            cur.execute(
                "SELECT DISTINCT year FROM messages WHERE year IS NOT NULL ORDER BY year DESC"
            )
            years = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT season FROM messages WHERE season IS NOT NULL ORDER BY season DESC"
            )
            seasons = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        return fail(
            "Could not load the archive.",
            data={"entries": [], "years": [], "seasons": [], "total": 0},
        )

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
            "season": r[9],
        }
        for r in rows
    ]

    return {
        "status": "success",
        "data": {"entries": entries, "years": years, "seasons": seasons, "total": total},
    }


@app.post("/check")
async def check_path(
    payload: CheckRequest, semantic: bool = False, user: dict = Depends(auth.get_current_user)
):
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
    query_seq = matching.token_list(query)

    # Semantic adds a Gemini embedding call — opt-in (Deep check) for reworded
    # duplicates. The default check is purely literal text overlap.
    qvec = embeddings.to_pgvector(embeddings.embed_query(query)) if semantic else None

    if qvec is not None:
        sem_expr = "1 - (embedding <=> %(qvec)s::vector)"
        sem_order = "COALESCE(1 - (embedding <=> %(qvec)s::vector), 0)"
    else:
        sem_expr = "NULL"
        sem_order = "0"

    # Prefilter candidates by trigram (and semantic when deep-checking) so we
    # only score a handful in Python; exact matches rank top via trigram anyway.
    sql = f"""
        SELECT
            id, message_text, normalized_text, message_date, year,
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
    except Exception:
        return fail(data={"verdict": "new", "matches": []})

    KIND_RANK = {"exact": 5, "strong": 4, "some": 3, "reworded": 1}
    matches = []
    for msg_id, msg_text, row_norm, msg_date, year, sem in rows:
        row_tokens = matching.tokenize(msg_text)
        cov = matching.coverage(query_tokens, row_tokens)
        phrase = matching.longest_shared_phrase(query_seq, matching.token_list(msg_text))
        kind = matching.check_kind(query_norm, row_norm or "", cov, phrase)

        sem_pct = round(float(sem) * 100) if sem is not None else None
        # Incidental (weak / none) literal overlap isn't shown — too noisy with
        # the corpus's shared vocabulary. Deep check can still surface a
        # meaning-level match via the embedding even with little literal overlap.
        if kind in ("none", "weak"):
            if sem is not None and float(sem) >= 0.80:
                kind = "reworded"
            else:
                continue

        matches.append({
            "id": str(msg_id),
            "year": year,
            "date": msg_date.isoformat() if msg_date else None,
            "overlap_pct": round(cov * 100),
            "shared_phrase_words": phrase,
            "kind": kind,
            "semantic_pct": sem_pct,
            "matched_snippet": matching.highlight_overlap(msg_text, query_tokens & row_tokens),
        })

    matches.sort(key=lambda m: (KIND_RANK[m["kind"]], m["overlap_pct"]), reverse=True)
    matches = matches[:20]

    # 3-state verdict: identical exists / worth reviewing / new.
    if matches and matches[0]["kind"] == "exact":
        verdict = "exact"
    elif matches:
        verdict = "review"
    else:
        verdict = "new"

    return {
        "status": "success",
        "data": {"verdict": verdict, "matches": matches},
    }


@app.post("/add")
async def add_path(payload: AddRequest, user: dict = Depends(auth.require_editor)):
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
        return fail()


@app.patch("/messages/{message_id}")
async def update_message(
    message_id: int, payload: UpdateRequest, user: dict = Depends(auth.require_editor)
):
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
        return fail()


@app.delete("/messages/{message_id}")
async def delete_message(message_id: int, user: dict = Depends(auth.require_admin)):
    """Delete one message."""
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM messages WHERE id = %s RETURNING id", (message_id,))
            found = cur.fetchone()
        if found is None:
            return {"status": "error", "message": "Message not found"}
        audit.record(user["username"], "message.delete", f"id={message_id}")
        return {"status": "success", "message": "Message deleted", "id": message_id}
    except Exception as exc:
        return fail()


@app.post("/import")
async def import_messages(payload: ImportRequest, user: dict = Depends(auth.require_editor)):
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
                logger.warning("Import row %d failed", i + 1, exc_info=True)
                errors.append({"row": i + 1, "message": "Could not import this row"})

    audit.record(user["username"], "import", f"added={added} skipped={skipped}")
    return {
        "status": "success",
        "data": {"added": added, "skipped": skipped, "errors": errors},
    }


@app.get("/stats")
async def stats(user: dict = Depends(auth.get_current_user)):
    """Archive counts + embedding backfill progress (for the console UI)."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM messages"
            )
            total, pending = cur.fetchone()
    except Exception as exc:
        return fail()

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
async def start_backfill(user: dict = Depends(auth.require_editor)):
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
async def list_categories(user: dict = Depends(auth.get_current_user)):
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
        return fail(data={"categories": []})

    return {
        "status": "success",
        "data": {"categories": [{"name": r[0], "count": r[1]} for r in rows]},
    }


@app.post("/categories")
async def create_category(payload: CategoryRequest, user: dict = Depends(auth.require_editor)):
    """Add a category to the managed vocabulary."""
    try:
        with db_cursor() as cur:
            name = taxonomy.ensure_category(cur, payload.name)
        return {"status": "success", "name": name}
    except Exception as exc:
        return fail()


@app.delete("/categories/{name}")
async def delete_category(name: str, user: dict = Depends(auth.require_admin)):
    """Remove a category (messages keep their data, category cleared to NULL)."""
    try:
        with db_cursor() as cur:
            cur.execute("DELETE FROM categories WHERE name = %s RETURNING name", (name,))
            if cur.fetchone() is None:
                return {"status": "error", "message": "Category not found"}
            # No FK, so detach the category from any messages that used it.
            cur.execute("UPDATE messages SET category = NULL WHERE category = %s", (name,))
        audit.record(user["username"], "category.delete", name)
        return {"status": "success", "name": name}
    except Exception as exc:
        return fail()


@app.get("/tags")
async def list_tags(user: dict = Depends(auth.get_current_user)):
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
        return fail(data={"tags": []})

    return {
        "status": "success",
        "data": {"tags": [{"name": r[0], "count": r[1]} for r in rows]},
    }


@app.get("/sources")
async def list_sources(user: dict = Depends(auth.get_current_user)):
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
        return fail(data={"sources": []})

    return {
        "status": "success",
        "data": {"sources": [{"name": r[0], "count": r[1]} for r in rows]},
    }


# --- Archive insights -----------------------------------------------------


@app.get("/overview")
async def overview(user: dict = Depends(auth.get_current_user)):
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
                "SELECT season, count(*) FROM messages WHERE season IS NOT NULL "
                "GROUP BY season ORDER BY season"
            )
            per_season = [{"season": r[0], "count": r[1]} for r in cur.fetchall()]
    except Exception as exc:
        return fail()

    return {
        "status": "success",
        "data": {
            "total": total,
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "favorites": favorites,
            "per_season": per_season,
        },
    }


@app.get("/onthisday")
async def on_this_day(user: dict = Depends(auth.get_current_user)):
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
        return fail(data={"entries": []})

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
async def duplicates(
    threshold: float = 0.55, limit: int = 50, user: dict = Depends(auth.require_editor)
):
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
        return fail(data={"pairs": []})

    pairs = [
        {
            "a": {"id": str(r[0]), "message": r[1], "date": r[2].isoformat() if r[2] else None},
            "b": {"id": str(r[3]), "message": r[4], "date": r[5].isoformat() if r[5] else None},
            "similarity": round(float(r[6]) * 100, 1),
        }
        for r in rows
    ]
    return {"status": "success", "data": {"pairs": pairs}}
