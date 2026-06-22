"""
Authentication: PBKDF2 password hashing (stdlib, no native deps) + opaque
DB-backed session tokens (revocable). Used to gate write endpoints; reads stay
public.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException

from config import SESSION_TTL_HOURS
from database import db_cursor

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000


# --- passwords ------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


# Precomputed hash used to equalize login timing when a username doesn't
# exist — prevents user enumeration via response-time differences.
DUMMY_HASH = ""  # set below, after hash_password is defined


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


DUMMY_HASH = hash_password(secrets.token_hex(16))


# --- login throttle (in-memory, per username) -----------------------------
# Single-process deploy → an in-memory counter is enough; CF rate-limiting
# covers volumetric / per-IP abuse. Resets on restart (acceptable).

_LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
_LOGIN_WINDOW = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))  # 15 min
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def login_locked(username: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(username, []) if now - t < _LOGIN_WINDOW]
        _fails[username] = recent
        return len(recent) >= _LOGIN_MAX_FAILS


def record_login_fail(username: str) -> None:
    with _fails_lock:
        _fails.setdefault(username, []).append(time.time())


def clear_login_fails(username: str) -> None:
    with _fails_lock:
        _fails.pop(username, None)


# --- sessions -------------------------------------------------------------


def create_session(cur, user_id: int) -> tuple[str, datetime]:
    """Create a session row using the caller's cursor (so it shares the login
    transaction). Returns (token, expires_at)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    cur.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
        (token, user_id, expires),
    )
    return token, expires


def user_for_session(token: str) -> dict | None:
    if not token:
        return None
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = %s AND s.expires_at > now()
            """,
            (token,),
        )
        r = cur.fetchone()
    return {"id": r[0], "username": r[1], "role": r[2]} if r else None


def delete_session(token: str) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


# --- FastAPI dependency ---------------------------------------------------


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization[7:].strip()


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """Require a valid session (any role). Raises 401 otherwise."""
    user = user_for_session(_bearer(authorization))
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# Role hierarchy — higher roles include lower privileges.
ROLES = ("viewer", "editor", "admin")


def require_role(minimum: str):
    """Dependency factory: require at least `minimum` role."""
    floor = ROLES.index(minimum)

    def dep(user: dict = Depends(get_current_user)) -> dict:
        role = user.get("role", "viewer")
        rank = ROLES.index(role) if role in ROLES else 0
        if rank < floor:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return dep


require_editor = require_role("editor")  # editor or admin — curate
require_admin = require_role("admin")  # admin only — delete / manage
