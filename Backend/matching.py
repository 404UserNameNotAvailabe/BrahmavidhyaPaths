"""
Text normalization, tokenization, overlap highlighting, and hybrid scoring
for the duplication engine.

Hybrid score combines three signals on the normalized Gujarati text:
  - semantic  : Gemini embedding cosine similarity   (meaning-level)
  - trigram   : pg_trgm character similarity          (reworded / typo level)
  - token     : Jaccard word overlap                  (literal shared words)

The corpus is tiny (~300 short aphorisms) with heavy vocabulary repetition
across unrelated messages, so token overlap alone over-rewards a single
shared common word. Trigram + semantic correct for that.
"""

from __future__ import annotations

import re

# --- thresholds (combined score is 0–100) --------------------------------
# Below this a row is not reported as a match at all.
MATCH_FLOOR = 18.0
# If the best match is below this the input is considered unique.
UNIQUE_THRESHOLD = 35.0

# Keep only the Gujarati block, word chars, and whitespace.
_PUNCT_RE = re.compile(r"[^\w\s઀-૿]")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w઀-૿]+")


def normalize_text(text: str) -> str:
    """Trim, collapse whitespace, lowercase. Used for storage + trigram."""
    return _WS_RE.sub(" ", text.strip()).lower()


def tokenize(text: str) -> set[str]:
    """
    Unique words, with punctuation treated as a separator so that
    'સેવા-ભક્તિ' and 'સેવા ભક્તિ' tokenize identically.
    """
    cleaned = _PUNCT_RE.sub(" ", normalize_text(text))
    return {tok for tok in cleaned.split() if tok}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def highlight_overlap(path_text: str, matched_tokens: set[str]) -> str:
    """
    Wrap each overlapping word in <mark>…</mark>, preserving the original
    text (punctuation, spacing). Returns HTML-safe markup.
    """
    if not matched_tokens:
        return _escape(path_text)

    def repl(match: re.Match[str]) -> str:
        word = match.group(0)
        if word.lower() in matched_tokens:
            return f"<mark>{_escape(word)}</mark>"
        return _escape(word)

    # Escape the gaps between words, mark the words themselves.
    out: list[str] = []
    last = 0
    for m in _WORD_RE.finditer(path_text):
        out.append(_escape(path_text[last:m.start()]))
        out.append(repl(m))
        last = m.end()
    out.append(_escape(path_text[last:]))
    return "".join(out)


def combine_score(
    trigram: float,
    token: float,
    semantic: float | None,
) -> float:
    """
    Blend the available signals into a single 0–100 confidence.

    All inputs are 0–1. When semantic is unavailable (no embedding / no API
    key) the weights renormalize across trigram + token only.
    """
    trigram = max(0.0, min(1.0, trigram))
    token = max(0.0, min(1.0, token))

    if semantic is None:
        score = 0.64 * trigram + 0.36 * token
    else:
        semantic = max(0.0, min(1.0, semantic))
        score = 0.45 * semantic + 0.35 * trigram + 0.20 * token

    return round(score * 100, 2)


def token_overlap(query_tokens: set[str], row_tokens: set[str]) -> float:
    """Jaccard similarity of two token sets (0–1)."""
    if not query_tokens or not row_tokens:
        return 0.0
    inter = len(query_tokens & row_tokens)
    union = len(query_tokens | row_tokens)
    return inter / union if union else 0.0
