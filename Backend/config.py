"""
Configuration loaded from environment variables.

Copy .env.example to .env and fill in real values, then load it before
starting the app (uvicorn picks up a .env automatically when python-dotenv
is installed, or export the vars in your shell / process manager).
"""

import os

from dotenv import load_dotenv

# Load a local .env if present (no-op in production where vars are injected).
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# sslmode: "require" for managed Postgres, "disable" for a local dev DB.
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")

# Gemini embeddings (semantic match layer). If unset, the backend silently
# falls back to trigram + word-overlap matching only.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# Vertex AI project + location — required for ADC mode (no API key).
# When GEMINI_API_KEY is blank, the SDK authenticates via ADC (service account
# key at GOOGLE_APPLICATION_CREDENTIALS, or workload identity on GCP).
GEMINI_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
GEMINI_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

# Route through Vertex AI vs the Gemini Developer API.
# ADC mode: always Vertex (no key). Express-mode key "AQ." → Vertex.
# Developer API key "AIza" → non-Vertex. Overridable via GEMINI_USE_VERTEX.
_default_use_vertex = "true" if (not GEMINI_API_KEY or GEMINI_API_KEY.startswith("AQ.")) else "false"
GEMINI_USE_VERTEX = os.getenv("GEMINI_USE_VERTEX", _default_use_vertex).strip().lower() in ("1", "true", "yes")

# Embedding dimensionality. Must match the vector(N) column in the migration.
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

# How long a login session stays valid (hours). Default 7 days.
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", str(24 * 7)))

# Allowed CORS origins (comma-separated). Defaults to the local dev frontends.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
