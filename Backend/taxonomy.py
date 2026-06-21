"""
Helpers for archive categorization (editorial metadata).

Categories are a managed vocabulary (a row in `categories`); tags are free.
Neither is used by the duplication engine — this is archive organization only.
"""

from __future__ import annotations


def clean_tags(tags: list[str] | None) -> list[str]:
    """Trim, drop blanks, de-duplicate (order-preserving)."""
    out: list[str] = []
    for raw in tags or []:
        t = (raw or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def ensure_category(cur, name: str | None) -> str | None:
    """Return the cleaned category name, inserting it into the managed list if
    new. Empty/blank → None (no category)."""
    clean = (name or "").strip()
    if not clean:
        return None
    cur.execute(
        "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (clean,),
    )
    return clean
