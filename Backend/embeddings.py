"""
Gemini embedding helpers for the semantic match layer.

If GEMINI_API_KEY is unset (or the SDK/network fails), every function returns
None and the caller skips semantic matching, falling back to trigram +
word-overlap. The tool stays fully functional without embeddings.
"""

from __future__ import annotations

import logging

from config import EMBED_DIM
from config import GEMINI_API_KEY
from config import GEMINI_EMBED_MODEL
from config import GEMINI_USE_VERTEX

logger = logging.getLogger("brahmavidya.embeddings")

# Lazily-initialised singleton client so an unset key never crashes import.
_client = None
_client_ready = False


def _get_client():
    global _client, _client_ready

    if _client_ready:
        return _client

    _client_ready = True

    if not GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY not set — semantic matching disabled.")
        _client = None
        return None

    try:
        from google import genai

        if GEMINI_USE_VERTEX:
            # Vertex AI express mode: key auths directly, no project/location.
            _client = genai.Client(vertexai=True, api_key=GEMINI_API_KEY)
        else:
            _client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:  # SDK missing or init failure
        logger.warning("Gemini client init failed (%s) — semantic disabled.", exc)
        _client = None

    return _client


def is_enabled() -> bool:
    """True when semantic matching is available."""
    return _get_client() is not None


def _embed(text: str, task_type: str) -> list[float] | None:
    client = _get_client()

    if client is None or not text.strip():
        return None

    try:
        from google.genai import types

        result = client.models.embed_content(
            model=GEMINI_EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )

        return list(result.embeddings[0].values)
    except Exception as exc:
        logger.warning("Embedding request failed (%s) — skipping semantic.", exc)
        return None


def embed_document(text: str) -> list[float] | None:
    """Embedding for a stored path (used at /add and during backfill)."""
    return _embed(text, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> list[float] | None:
    """Embedding for the user's query (used at /check)."""
    return _embed(text, "RETRIEVAL_QUERY")


def to_pgvector(embedding: list[float] | None) -> str | None:
    """Serialise a Python list to the pgvector literal '[1,2,3]'."""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
