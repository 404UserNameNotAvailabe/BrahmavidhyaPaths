"""
One-off backfill: compute Gemini embeddings for every archive row whose
Embedding column is still NULL.

Run after applying sql/migrate_v2.sql and setting GEMINI_API_KEY:

    cd Backend
    python -m scripts.backfill_embeddings

Safe to re-run — it only touches rows missing an embedding, so if a run is
interrupted or rate-limited, just run it again to finish the rest.

Vertex AI express mode has a low embeddings quota, so requests are paced and
retried with backoff. Tune via env:

    BACKFILL_PACE_SECONDS   gap between successful requests (default 2)
    BACKFILL_MAX_RETRIES    retries per row on failure / 429 (default 5)
"""

import os
import sys
import time
from pathlib import Path

# Allow running as a module from the Backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import embeddings  # noqa: E402
from database import close_pool, get_connection, return_connection  # noqa: E402

# Conservative defaults to stay under the Vertex express-mode quota.
PACE_SECONDS = float(os.getenv("BACKFILL_PACE_SECONDS", "10"))
MAX_RETRIES = int(os.getenv("BACKFILL_MAX_RETRIES", "5"))
BACKOFF_SECONDS = float(os.getenv("BACKFILL_BACKOFF_SECONDS", "30"))


def embed_with_retry(text: str) -> str | None:
    """Embed one row, retrying with backoff on failure (e.g. 429 quota)."""
    for attempt in range(1, MAX_RETRIES + 1):
        vec = embeddings.to_pgvector(embeddings.embed_document(text))
        if vec is not None:
            return vec
        if attempt < MAX_RETRIES:
            wait = min(300, BACKOFF_SECONDS * attempt)
            print(f"    retry in {wait:.0f}s (attempt {attempt}/{MAX_RETRIES})…")
            time.sleep(wait)
    return None


def main() -> int:
    if not embeddings.is_enabled():
        print("GEMINI_API_KEY not set or SDK unavailable — nothing to do.")
        return 1

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, message_text FROM messages WHERE embedding IS NULL"
        )
        rows = cur.fetchall()

        if not rows:
            print("All rows already have embeddings.")
            return 0

        print(f"Embedding {len(rows)} row(s)...")
        done = 0
        failed: list = []
        for msg_id, msg_text in rows:
            vec = embed_with_retry(msg_text)
            if vec is None:
                print(f"  ! row {msg_id}: giving up after {MAX_RETRIES} attempts")
                failed.append(msg_id)
                continue

            # autocommit pool → each UPDATE commits on its own.
            cur.execute(
                "UPDATE messages SET embedding = %s::vector WHERE id = %s",
                (vec, msg_id),
            )
            done += 1
            print(f"  ✓ row {msg_id}")
            time.sleep(PACE_SECONDS)

        print(f"Done. {done}/{len(rows)} embedded.")
        if failed:
            print(f"Still missing ({len(failed)}): {failed}")
            print("Re-run `python -m scripts.backfill_embeddings` to finish them.")
            return 1
        return 0

    except Exception as exc:
        print(f"Backfill failed: {exc}")
        return 1
    finally:
        return_connection(conn)
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
