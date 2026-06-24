"""Best-effort audit trail — records who did what. Never raises."""

from __future__ import annotations

import logging

from database import db_cursor

logger = logging.getLogger("brahmavidya.audit")


def record(username: str | None, action: str, detail: str = "") -> None:
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (username, action, detail) VALUES (%s, %s, %s)",
                (username or None, action, detail or None),
            )
    except Exception:
        logger.warning("audit write failed (%s)", action, exc_info=True)
