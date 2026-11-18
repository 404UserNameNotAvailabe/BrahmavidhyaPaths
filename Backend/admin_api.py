"""
Superadmin pages: which menu items each role sees, the Google Cloud (Vertex)
settings, and a status view of the server, the worker and the database.

GET /settings/menu is open to every signed-in user (the sidebar needs it);
everything else here needs the superadmin role.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import audit
import auth
import candidate_worker
import embeddings
import gcp_settings
from database import db_cursor, pool

logger = logging.getLogger("brahmavidya.admin_api")
router = APIRouter()
STARTED = time.time()
APP_DIR = Path(__file__).resolve().parent.parent

# Menu items whose visibility can be set, with the lowest role the page works
# for (the API still enforces that). Settings + Status are superadmin-only and
# never hideable, so a superadmin can't lock themself out.
MENU = {
    "/": "viewer",
    "/candidates": "viewer",
    "/ready": "viewer",
    "/archive": "viewer",
    "/review": "editor",
    "/topics": "editor",
    "/users": "admin",
}
MENU_ROLES = ("viewer", "editor", "admin")


def default_menu() -> dict[str, list[str]]:
    return {p: list(MENU_ROLES[MENU_ROLES.index(lo):]) for p, lo in MENU.items()}


def allowed(path: str, roles: list) -> list[str]:
    """Known roles only, never below the page's lowest working role."""
    lo = MENU_ROLES.index(MENU[path])
    return [r for r in MENU_ROLES if r in roles and MENU_ROLES.index(r) >= lo]


def read_menu() -> dict[str, list[str]]:
    menu = default_menu()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'menu'")
            row = cur.fetchone()
    except Exception:
        logger.warning("Could not read menu settings; using defaults", exc_info=True)
        return menu
    for path, roles in (row[0] if row else {}).items():
        if path in menu and isinstance(roles, list):
            menu[path] = allowed(path, roles)
    return menu


@router.get("/settings/menu")
def get_menu(user: dict = Depends(auth.get_current_user)):
    return {"status": "success", "data": {"menu": read_menu(), "min_role": MENU}}


class MenuUpdate(BaseModel):
    menu: dict[str, list[str]]


@router.post("/settings/menu")
def set_menu(payload: MenuUpdate, user: dict = Depends(auth.require_superadmin)):
    clean: dict[str, list[str]] = {}
    for path, roles in payload.menu.items():
        if path not in MENU:
            return {"status": "error", "message": f"Unknown menu item {path}"}
        bad = [r for r in roles if r not in MENU_ROLES]
        if bad:
            return {"status": "error", "message": f"Unknown role {bad[0]}"}
        clean[path] = allowed(path, roles)
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value, updated_by) VALUES ('menu', %s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                   updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (Jsonb(clean), user["username"]),
        )
    hidden = {p: [r for r in default_menu()[p] if r not in clean.get(p, default_menu()[p])] for p in MENU}
    audit.record(user["username"], "settings.menu",
                 "; ".join(f"{p} hidden for {','.join(r)}" for p, r in hidden.items() if r) or "all visible")
    return {"status": "success", "data": {"menu": read_menu(), "min_role": MENU}}


# --- Google Cloud ---------------------------------------------------------


class GcpPayload(BaseModel):
    project: str = Field(max_length=120)
    location: str = Field(max_length=60)
    embed_model: str = Field(max_length=120)
    judge_model: str = Field(max_length=120)
    judge_location: str = Field(default="", max_length=60)
    key_json: str | None = Field(default=None, max_length=gcp_settings.MAX_KEY_BYTES)


def _gcp_view() -> dict:
    return {
        "settings": gcp_settings.current(),
        "env": gcp_settings.env_defaults(),
        "overridden": sorted(gcp_settings.overrides()),
        "key": gcp_settings.key_info(),
        "api_key_mode": gcp_settings.api_key_mode(),
        "last_test": gcp_settings.last_test(),
    }


@router.get("/admin/gcp")
def get_gcp(user: dict = Depends(auth.require_superadmin)):
    return {"status": "success", "data": _gcp_view()}


def _parse(payload: GcpPayload) -> tuple[dict, dict | None]:
    settings = gcp_settings.clean(payload.model_dump())
    key = gcp_settings.parse_key(payload.key_json) if payload.key_json else None
    return settings, key


@router.post("/admin/gcp/test")
def test_gcp(payload: GcpPayload, user: dict = Depends(auth.require_superadmin)):
    try:
        settings, key = _parse(payload)
    except gcp_settings.SettingsError as exc:
        return {"status": "error", "message": str(exc)}
    result = gcp_settings.test(settings, key)
    audit.record(user["username"], "settings.gcp.test",
                 f"{settings['project']} {'ok' if result['ok'] else 'failed'}{' (new key)' if key else ''}")
    return {"status": "success", "data": result}


@router.post("/admin/gcp")
def save_gcp(payload: GcpPayload, user: dict = Depends(auth.require_superadmin)):
    if gcp_settings.api_key_mode():
        return {"status": "error", "message": "The server uses GEMINI_API_KEY; remove it from .env to manage the key here"}
    try:
        settings, _ = _parse(payload)
        r = gcp_settings.save(settings, payload.key_json, user["username"])
    except gcp_settings.SettingsError as exc:
        return {"status": "error", "message": str(exc)}
    if not r["saved"]:
        return {"status": "error", "message": "The test failed, so nothing was saved", "data": r["test"]}
    audit.record(user["username"], "settings.gcp.save",
                 f"project={settings['project']} judge={settings['judge_model']}{' + new key' if payload.key_json else ''}")
    return {"status": "success", "data": _gcp_view()}


@router.post("/admin/gcp/reset")
def reset_gcp(user: dict = Depends(auth.require_superadmin)):
    gcp_settings.reset()
    audit.record(user["username"], "settings.gcp.reset", "back to .env values")
    return {"status": "success", "data": _gcp_view()}


# --- Status ---------------------------------------------------------------


def _section(fn) -> dict:
    try:
        return {"ok": True, **fn()}
    except Exception:
        logger.warning("status section %s failed", fn.__name__, exc_info=True)
        return {"ok": False, "error": "Could not read this. The server log has the details."}


def _git_commit() -> str | None:
    head = APP_DIR / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            p = APP_DIR / ".git" / ref[5:]
            if p.exists():
                return p.read_text().strip()[:7]
            for line in (APP_DIR / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(ref[5:]):
                    return line[:7]
            return None
        return ref[:7]
    except OSError:
        return None


def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable"):
                out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    rss = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) // 1024
    except OSError:
        pass
    return {"mem_total_mb": out.get("MemTotal"), "mem_available_mb": out.get("MemAvailable"), "process_mb": rss}


def server() -> dict:
    disk = shutil.disk_usage(APP_DIR)
    return {
        "host": socket.gethostname(),
        "commit": _git_commit(),
        "python": sys.version.split()[0],
        "os": platform.platform(terse=True),
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED)),
        "uptime_s": int(time.time() - STARTED),
        "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        **_meminfo(),
    }


def worker() -> dict:
    return {
        "enabled": os.getenv("CANDIDATE_WORKER", "on").strip().lower() != "off",
        "running": candidate_worker._state.get("started", False),
        **candidate_worker.status(),
    }


def database() -> dict:
    t = time.monotonic()
    with db_cursor() as cur:
        cur.execute("SELECT 1")
        ping = int((time.monotonic() - t) * 1000)
        cur.execute("SHOW server_version")
        version = cur.fetchone()[0]
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'pg_trgm') ORDER BY 1")
        ext = dict(cur.fetchall())
        cur.execute(
            """SELECT (SELECT count(*) FROM messages),
                      (SELECT count(*) FROM messages WHERE embedding IS NULL),
                      (SELECT count(*) FROM candidates),
                      (SELECT count(*) FROM users),
                      (SELECT count(*) FROM sessions WHERE expires_at > now())"""
        )
        paths, unembedded, new_paths, users, sessions = cur.fetchone()
    stats = pool.get_stats()
    return {
        "ping_ms": ping,
        "version": version,
        "size_mb": round(size / 1e6, 1),
        "extensions": ext,
        "paths": paths,
        "paths_without_embedding": unembedded,
        "new_paths": new_paths,
        "users": users,
        "active_sessions": sessions,
        "pool": {"size": stats.get("pool_size"), "available": stats.get("pool_available"),
                 "max": pool.max_size, "waiting": stats.get("requests_waiting", 0)},
    }


def google() -> dict:
    s = gcp_settings.current()
    key = gcp_settings.key_info()
    return {
        "project": s["project"],
        "location": s["location"],
        "embed_model": s["embed_model"],
        "judge_model": s["judge_model"],
        "judge_location": s["judge_location"] or s["location"],
        "semantic_enabled": embeddings.is_enabled(),
        "key_email": key.get("client_email"),
        "key_project": key.get("project_id"),
        "key_exists": key.get("exists"),
        "last_test": gcp_settings.last_test(),
    }


@router.get("/admin/status")
def status(user: dict = Depends(auth.require_superadmin)):
    return {
        "status": "success",
        "data": {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "server": _section(server),
            "worker": _section(worker),
            "database": _section(database),
            "google": _section(google),
        },
    }
