"""
Background worker for candidate checks.

The queue IS the table: candidates.check_state (queued → running → done |
failed). Nothing lives only in memory, so a restart loses nothing — rows
left `running` are put back to `queued` on start. One daemon thread per API
process; it claims the oldest queued row atomically, checks it, paces, and
repeats. No request ever waits on a model.

Env:
    CANDIDATE_PACE_SECONDS  pause between checks          (default 3)
    JUDGE_DAILY_CALL_LIMIT  AI-judged checks per IST day   (default 300)
    CANDIDATE_WORKER        "off" pauses all candidate checks (rows stay queued)
"""

from __future__ import annotations

import logging
import os
import threading
import time

import candidates
from database import db_cursor

logger = logging.getLogger("brahmavidya.candidate_worker")

PACE_SECONDS = float(os.getenv("CANDIDATE_PACE_SECONDS", "3"))
DAILY_LIMIT = int(os.getenv("JUDGE_DAILY_CALL_LIMIT", "300"))
IDLE_WAIT = 30.0

_lock = threading.Lock()
_wake = threading.Event()
_state = {"started": False, "limit_reached": False}


def reset_running() -> int:
    """On API start: a row left `running` by a restart goes back to the queue."""
    with db_cursor() as cur:
        cur.execute("UPDATE candidates SET check_state = 'queued' WHERE check_state = 'running'")
        return cur.rowcount


def used_today() -> int:
    """AI-judged checks since midnight India time."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM candidates
               WHERE judge_model IS NOT NULL
                 AND checked_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')"""
        )
        return cur.fetchone()[0]


def _claim() -> int | None:
    """Atomically take the oldest queued row (safe with several workers)."""
    with db_cursor() as cur:
        cur.execute(
            """UPDATE candidates SET check_state = 'running', check_error = NULL
               WHERE id = (SELECT id FROM candidates WHERE check_state = 'queued'
                           ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING id"""
        )
        row = cur.fetchone()
    return row[0] if row else None


def _loop() -> None:
    while True:
        try:
            if used_today() >= DAILY_LIMIT:
                _state["limit_reached"] = True
                _wake.wait(300)
                _wake.clear()
                continue
            _state["limit_reached"] = False
            cid = _claim()
            if cid is None:
                _wake.wait(IDLE_WAIT)
                _wake.clear()
                continue
            t0 = time.monotonic()
            try:
                outcome = candidates.run_check(cid)
            except candidates.CheckFailed as exc:
                candidates.mark_failed(cid, str(exc))
                outcome = "failed"
            except Exception as exc:  # never log texts or credentials
                logger.warning("candidate %s check crashed (%s)", cid, type(exc).__name__, exc_info=True)
                candidates.mark_failed(cid, "Check failed unexpectedly; try again")
                outcome = "failed"
            logger.info("candidate %s checked in %.1fs: %s", cid, time.monotonic() - t0, outcome)
            time.sleep(PACE_SECONDS)
        except Exception:  # DB hiccup (Neon idle drop etc.): back off, keep the thread alive
            logger.warning("candidate worker loop error", exc_info=True)
            time.sleep(10)


def start() -> None:
    """Start the worker thread once per process (unless CANDIDATE_WORKER=off)."""
    if os.getenv("CANDIDATE_WORKER", "on").strip().lower() == "off":
        logger.info("Candidate worker is off (CANDIDATE_WORKER=off); checks stay queued.")
        return
    with _lock:
        if _state["started"]:
            return
        _state["started"] = True
    threading.Thread(target=_loop, name="candidate-worker", daemon=True).start()


def wake() -> None:
    """New work was queued: check now instead of after the idle wait."""
    _wake.set()


def status() -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT check_state, count(*) FROM candidates GROUP BY check_state")
        counts = dict(cur.fetchall())
    used = used_today()
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "done": counts.get("done", 0),
        "daily_limit": DAILY_LIMIT,
        "used_today": used,
        "limit_reached": used >= DAILY_LIMIT,
    }
