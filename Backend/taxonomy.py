"""
Helpers for archive metadata: Dhanurmas dates, original links, tags, source.

Tags are the single free-form grouping (category was merged into tags). None
of this is used by the duplication engine — it is archive organization only.
"""

from __future__ import annotations

import re
from datetime import date

# Every Dhanurmas season runs 16 Dec → 14 Jan (30 vachans, one per day).
SEASON_START = (12, 16)
SEASON_END = (1, 14)
SEASON_LABEL = "16 Dec – 14 Jan"

LINK_MAX = 500
_LINK_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def in_season(d: date) -> bool:
    """True when the date falls inside the Dhanurmas window."""
    return (d.month, d.day) >= SEASON_START or (d.month, d.day) <= SEASON_END


def check_season_date(d: date | None) -> date | None:
    """Pass a Dhanurmas date (or None) through; raise ValueError otherwise."""
    if d is not None and not in_season(d):
        raise ValueError(f"Date must fall in Dhanurmas ({SEASON_LABEL})")
    return d


def clean_link(link: str | None) -> str | None:
    """Trim the original-source URL; blank → None. Raises ValueError when it
    isn't an http(s) URL or is too long."""
    clean = (link or "").strip()
    if not clean:
        return None
    if len(clean) > LINK_MAX or not _LINK_RE.match(clean):
        raise ValueError("Link must be a full http:// or https:// URL")
    return clean

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

