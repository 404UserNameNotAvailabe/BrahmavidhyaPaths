"""
Background embedding worker.

Embeds rows whose `embedding` is NULL in a daemon thread, paced conservatively
so the Vertex AI express-mode quota is not exhausted (which can get the key
temporarily blocked). The UI triggers it via POST /embeddings/backfill and
watches progress via GET /stats.

Pacing is intentionally slow. Tune via env:
    EMBED_PACE_SECONDS    gap between successful embeds   (default 10)
    EMBED_MAX_RETRIES     retries per row on 429/failure  (default 5)
    EMBED_BACKOFF_SECONDS base backoff, grows per attempt (default 30)
"""

from __future__ import annotations

import os
import threading
import time

import embeddings
from database import get_connection, return_connection

PACE_SECONDS = float(os.getenv("EMBED_PACE_SECONDS", "10"))
MAX_RETRIES = int(os.getenv("EMBED_MAX_RETRIES", "5"))
BACKOFF_SECONDS = float(os.getenv("EMBED_BACKOFF_SECONDS", "30"))

_lock = threading.Lock()
_state = {"running": False, "total": 0, "done": 0, "failed": 0}


def status() -> dict:
    with _lock:
        return dict(_state)


def _embed_one(text: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        vec = embeddings.to_pgvector(embeddings.embed_document(text))
        if vec is not None:
            return vec
        if attempt < MAX_RETRIES:
            time.sleep(min(300, BACKOFF_SECONDS * attempt))
    return None


def _run() -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, message_text FROM messages WHERE embedding IS NULL")
        rows = cur.fetchall()

        with _lock:
            _state.update(total=len(rows), done=0, failed=0)

        for msg_id, msg_text in rows:
            vec = _embed_one(msg_text)
            if vec is None:
                with _lock:
                    _state["failed"] += 1
                continue
            cur.execute(
                "UPDATE messages SET embedding = %s::vector WHERE id = %s",
                (vec, msg_id),
            )
            with _lock:
                _state["done"] += 1
            time.sleep(PACE_SECONDS)
    finally:
        return_connection(conn)
        with _lock:
            _state["running"] = False


def start() -> bool:
    """Launch the worker if idle and embeddings are configured. Returns True
    if a new run started, False if already running or semantic is disabled."""
    with _lock:
        if _state["running"]:
            return False
        if not embeddings.is_enabled():
            return False
        _state["running"] = True

    threading.Thread(target=_run, daemon=True).start()
    return True
