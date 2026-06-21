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

# Route through Vertex AI (express mode) vs the Gemini Developer API.
# Vertex express-mode keys start with "AQ."; Developer API keys with "AIza".
# Auto-detected from the key prefix, overridable via GEMINI_USE_VERTEX.
GEMINI_USE_VERTEX = os.getenv(
    "GEMINI_USE_VERTEX",
    "true" if GEMINI_API_KEY.startswith("AQ.") else "false",
).strip().lower() in ("1", "true", "yes")

# Embedding dimensionality. Must match the vector(N) column in the migration.
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

# Allowed CORS origins (comma-separated). Defaults to the local dev frontends.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
