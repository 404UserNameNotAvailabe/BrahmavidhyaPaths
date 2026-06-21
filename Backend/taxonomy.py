"""
Helpers for archive categorization (editorial metadata).

Categories are a managed vocabulary (a row in `categories`); tags are free.
Neither is used by the duplication engine — this is archive organization only.
"""

from __future__ import annotations

import re

# A trailing attribution: "… - શ્રીજી મહારાજ" / "… – શ્રીજી". Kept short and
# name-like so we don't grab sentence fragments.
_SOURCE_RE = re.compile(r"[-–—]\s*([^-–—\n]{2,30})\s*$")


def extract_source(text: str | None) -> str | None:
    """Best-effort detect a trailing attribution as the source name. Does NOT
    modify the text. Returns None when there's no clear trailer."""
    if not text:
        return None
    m = _SOURCE_RE.search(text.strip())
    if not m:
        return None
    name = m.group(1).strip().rstrip(".।")
    return name if 2 <= len(name) <= 30 else None


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
