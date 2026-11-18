"""
Google Cloud (Vertex AI) settings that a superadmin can change from the UI.

Values start from Backend/.env (GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION,
GEMINI_EMBED_MODEL, GEMINI_JUDGE_MODEL, GEMINI_JUDGE_LOCATION,
GOOGLE_APPLICATION_CREDENTIALS). A row in app_settings (key 'gcp') overrides
them, so a change made in the UI survives restarts. Applying swaps the live
clients in embeddings.py and judge.py — no restart needed.

The service-account key is only ever written to the key file on disk and is
never returned by any endpoint: key_info() exposes the email, project and
key id only.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from psycopg.types.json import Jsonb

import config
from database import db_cursor

logger = logging.getLogger("brahmavidya.gcp_settings")

FIELDS = ("project", "location", "embed_model", "judge_model", "judge_location")
MAX_KEY_BYTES = 16_000
_lock = threading.Lock()
_state: dict = {"overrides": {}, "last_test": None}


def env_defaults() -> dict:
    return {
        "project": config.GEMINI_PROJECT,
        "location": config.GEMINI_LOCATION,
        "embed_model": config.GEMINI_EMBED_MODEL,
        "judge_model": os.getenv("GEMINI_JUDGE_MODEL", ""),
        "judge_location": os.getenv("GEMINI_JUDGE_LOCATION", "").strip(),
    }


def key_path() -> str:
    return os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()


def current() -> dict:
    return {**env_defaults(), **_state["overrides"]}


def api_key_mode() -> bool:
    """An API key in .env bypasses ADC; the UI then only shows status."""
    return bool(config.GEMINI_API_KEY)


# --- key file ---------------------------------------------------------------


class SettingsError(ValueError):
    pass


def parse_key(raw: str) -> dict:
    if len(raw.encode()) > MAX_KEY_BYTES:
        raise SettingsError("That file is too big to be a service-account key")
    try:
        info = json.loads(raw)
    except ValueError:
        raise SettingsError("That file is not valid JSON") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SettingsError("That is not a service-account key (type must be service_account)")
    for f in ("client_email", "private_key", "project_id", "private_key_id"):
        if not info.get(f):
            raise SettingsError(f"The key is missing {f}")
    return info


def _summary(info: dict) -> dict:
    return {
        "client_email": info.get("client_email"),
        "project_id": info.get("project_id"),
        "key_id": (info.get("private_key_id") or "")[:8],
    }


def key_info() -> dict:
    """What the key file says — never the private key itself."""
    path = key_path()
    out: dict = {"path": path or None, "exists": False}
    if not path:
        return out
    p = Path(path)
    try:
        st = p.stat()
        out.update(exists=True, modified=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                   mode=oct(st.st_mode & 0o777))
        out.update(_summary(json.loads(p.read_text())))
    except FileNotFoundError:
        pass
    except Exception as exc:
        out["error"] = f"Can't read the key file ({type(exc).__name__})"
    return out


def _write_key(raw: str) -> None:
    """Replace the key file atomically (temp file + rename); if the folder is
    not writable, overwrite the existing file in place. Always mode 600."""
    path = key_path()
    if not path:
        raise SettingsError("GOOGLE_APPLICATION_CREDENTIALS is not set in the server .env")
    target = Path(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".gcp-sa-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except PermissionError:
        if not target.exists():
            raise SettingsError(f"The server can't write {target.parent}") from None
        with open(target, "w") as f:  # owned by the service user, so writable
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(target, 0o600)


# --- clients ----------------------------------------------------------------


def _credentials(key: dict | None):
    from google.oauth2 import service_account

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if key is not None:
        return service_account.Credentials.from_service_account_info(key, scopes=scopes)
    path = key_path()
    if path and Path(path).exists():
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)
    return None  # fall back to google-auth's own ADC search


def make_client(settings: dict, location: str | None = None, key: dict | None = None):
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings["project"],
        location=location or settings["location"],
        credentials=_credentials(key),
    )


def apply() -> None:
    """Point embeddings.py and judge.py at the current settings."""
    import embeddings
    import judge

    s = current()
    with _lock:
        embeddings.GEMINI_PROJECT = s["project"]
        embeddings.GEMINI_LOCATION = s["location"]
        embeddings.GEMINI_EMBED_MODEL = s["embed_model"]
        embeddings._client = None
        embeddings._client_ready = False
        judge.JUDGE_MODEL = s["judge_model"]
        judge.JUDGE_LOCATION = s["judge_location"]
        judge._judge_client = None


def load() -> None:
    """At startup: read the saved overrides (if any) and apply them."""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'gcp'")
            row = cur.fetchone()
        _state["overrides"] = {k: v for k, v in (row[0] if row else {}).items() if k in FIELDS}
    except Exception:
        logger.warning("Could not read saved Google Cloud settings; using .env", exc_info=True)
        _state["overrides"] = {}
    apply()


def clean(payload: dict) -> dict:
    out = {}
    for f in FIELDS:
        v = str(payload.get(f) or "").strip()
        if len(v) > 120 or any(c.isspace() for c in v):
            raise SettingsError(f"{f.replace('_', ' ')} looks wrong")
        out[f] = v
    for f in ("project", "location", "embed_model", "judge_model"):
        if not out[f]:
            raise SettingsError(f"{f.replace('_', ' ').capitalize()} is required")
    return out


# --- test -------------------------------------------------------------------


def _short_error(exc: Exception) -> str:
    """A plain reason for the screen; the full error goes to the server log
    only (never send exception text to the client)."""
    logger.warning("Google Cloud test call failed", exc_info=True)
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    if "invalid_grant" in text or "invalid jwt" in text or code == 401:
        return "Google rejected the key (deleted, disabled or wrong)."
    if code == 403 or "permission" in text:
        return "Permission denied: the service account needs Vertex AI User on this project, and the Vertex AI API must be on."
    if code == 404 or "not found" in text:
        return "Model not found in this region, or this project can't use it."
    if code == 429 or "quota" in text or "exhausted" in text:
        return "Quota or rate limit reached. Try again in a minute."
    if "billing" in text:
        return "Billing is off or the credits ran out for this project."
    if isinstance(exc, (SettingsError, ValueError)):
        return "The settings or key are not valid."
    return "The call failed. The server log has the details."


def test(settings: dict, key: dict | None = None) -> dict:
    """One embedding call and one judge call with these settings. Sends only
    a fixed word and a fixed prompt — no archive or user text."""
    from google.genai import types

    result: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "settings": settings}
    if key is not None:
        result["key"] = _summary(key)

    t = time.monotonic()
    try:
        client = make_client(settings, key=key)
        r = client.models.embed_content(
            model=settings["embed_model"],
            contents="સમજણ",
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=config.EMBED_DIM),
        )
        dims = len(r.embeddings[0].values)
        ok = dims == config.EMBED_DIM
        result["embed"] = {"ok": ok, "ms": int((time.monotonic() - t) * 1000), "dims": dims,
                           **({} if ok else {"error": f"Expected {config.EMBED_DIM} numbers, got {dims}"})}
    except Exception as exc:
        result["embed"] = {"ok": False, "ms": int((time.monotonic() - t) * 1000), "error": _short_error(exc)}

    t = time.monotonic()
    try:
        client = make_client(settings, location=settings["judge_location"] or None, key=key)
        r = client.models.generate_content(
            model=settings["judge_model"],
            contents='Reply with the JSON {"ok": true}',
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
        )
        ok = bool((r.text or "").strip())
        result["judge"] = {"ok": ok, "ms": int((time.monotonic() - t) * 1000),
                           **({} if ok else {"error": "Empty reply"})}
    except Exception as exc:
        result["judge"] = {"ok": False, "ms": int((time.monotonic() - t) * 1000), "error": _short_error(exc)}

    result["ok"] = result["embed"]["ok"] and result["judge"]["ok"]
    _state["last_test"] = result
    return result


def last_test() -> dict | None:
    return _state["last_test"]


# --- save -------------------------------------------------------------------


def save(settings: dict, key_raw: str | None, username: str) -> dict:
    """Test first; only a passing test is saved and applied."""
    key = parse_key(key_raw) if key_raw else None
    result = test(settings, key)
    if not result["ok"]:
        return {"saved": False, "test": result}
    if key_raw:
        _write_key(key_raw)
    overrides = {f: v for f, v in settings.items() if v != env_defaults().get(f)}
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value, updated_by) VALUES ('gcp', %s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                   updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (Jsonb(overrides), username),
        )
    _state["overrides"] = overrides
    apply()
    return {"saved": True, "test": result}


def reset() -> None:
    """Forget the UI overrides; go back to the .env values (key file stays)."""
    with db_cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = 'gcp'")
    _state["overrides"] = {}
    apply()


def overrides() -> dict:
    return dict(_state["overrides"])
