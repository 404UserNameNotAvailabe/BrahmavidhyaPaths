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

# Letters + combining marks of the scripts in the archive. Python's \w misses
# Indic vowel signs and the virama, so the blocks are listed explicitly:
# Gujarati (U+0A80–0AFF) and Devanagari (U+0900–097F) minus its dandas । ॥
# (U+0964–65, punctuation), plus ZWNJ/ZWJ, which sit inside words.
_INDIC = "઀-૿ऀ-ॣ०-ॿ‌‍"
_PUNCT_RE = re.compile(rf"[^\w\s{_INDIC}]")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(rf"[\w{_INDIC}]+")

# Punctuation that never changes what a vachan says. It is dropped from the
# exact-duplicate key, so "સંપ રાખે." and "સંપ રાખે" are the same vachan.
# MUST stay identical to the character class of `normalized_text` in
# sql/schema.sql — change both together.
NORM_PUNCT = "]\\.,!?;:'\"“”‘’(){}[|/=+*&%#@~`^<>_–—…।॥-"
_NORM_PUNCT_RE = re.compile("[" + re.escape(NORM_PUNCT) + "]")


def normalize_text(text: str) -> str:
    """The exact-duplicate key: punctuation → space, collapse whitespace, trim,
    lowercase. Mirrors the generated column `normalized_text` in schema.sql."""
    return _WS_RE.sub(" ", _NORM_PUNCT_RE.sub(" ", text)).strip().lower()


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


# --- literal duplicate check (deterministic, no fuzzy score) --------------
# A new message is judged purely on how much of *its own text* literally
# appears in an existing vachan, plus the longest shared word-for-word phrase.

PHRASE_STRONG = 4  # a shared run of ≥4 words is a strong duplicate signal
PHRASE_WEAK = 3  # a shared 3-word phrase is worth surfacing
COVERAGE_STRONG = 0.60  # ≥60% of the new message's words also in the vachan


def token_list(text: str) -> list[str]:
    """Ordered word list (keeps duplicates/order) for phrase matching."""
    cleaned = _PUNCT_RE.sub(" ", normalize_text(text))
    return [tok for tok in cleaned.split() if tok]


def coverage(query_tokens: set[str], row_tokens: set[str]) -> float:
    """Fraction of the new message's words that also appear in the vachan (0–1)."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & row_tokens) / len(query_tokens)


def longest_shared_phrase(a: list[str], b: list[str]) -> int:
    """Length (in words) of the longest contiguous run shared by a and b."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def check_kind(query_norm: str, row_norm: str, cov: float, phrase_len: int, query_len: int) -> str:
    """Classify one candidate. Order matters — strongest first.

    The phrase requirement adapts to how much was typed, so a 1–2 word lookup
    can match (the whole query appears) while a full pasted vachan still needs a
    real 4-word phrase — keeping common vocabulary from raising false alarms.

    exact    : same vachan (ignoring spacing, case and punctuation)
    contains : the typed text appears inside the vachan, word for word
    strong   : most words AND a long-enough shared phrase → likely duplicate
    some   : a long-enough shared phrase, or most words + a phrase → review
    weak   : a borderline phrase → incidental (only shown for short queries)
    none   : nothing notable
    """
    # Exact must be tested before containment: identical text is also its own
    # substring, and labelling it "contains" let an existing vachan be re-added.
    if query_norm and query_norm == row_norm:
        return "exact"
    if query_norm and query_norm in row_norm:
        return "contains"
    return overlap_kind(cov, phrase_len, query_len)


def overlap_kind(cov: float, phrase_len: int, query_len: int) -> str:
    """The word-overlap part of check_kind, without the exact/contains tests:
    strong / some / weak / none from coverage and the longest shared phrase.
    Used directly by the candidate pipeline, which has its own exact/contained
    rules (candidate_rules.py)."""
    if query_len == 0:
        # Nothing but punctuation was typed: with a 0-word phrase bar every
        # row would otherwise count as an overlap.
        return "none"
    strong_phrase = min(PHRASE_STRONG, query_len)  # e.g. 1-word query → 1
    weak_phrase = min(PHRASE_WEAK, query_len)
    if cov >= COVERAGE_STRONG and phrase_len >= strong_phrase:
        return "strong"
    if phrase_len >= strong_phrase:
        return "some"
    if cov >= COVERAGE_STRONG and phrase_len >= weak_phrase:
        return "some"
    if phrase_len >= weak_phrase:
        return "weak"
    return "none"
