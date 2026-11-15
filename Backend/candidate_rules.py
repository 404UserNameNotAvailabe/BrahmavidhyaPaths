"""
Rules-only part of the Candidates check: no database, no AI.

Pipeline steps from the build spec that live here:
  1. match_key       — a looser comparison key (spaces + punctuation removed)
  2. word_compare    — candidate vs one archive path: exact / contained /
                       strong / some / weak / none (no ≤6-word "contains"
                       relabel; that rule is for searching)
  5. decide          — the four results from word kinds + judge labels
  plus: shortlist, rank_matches, find_repeats, doubled_words,
        parse_sheet_topics, detect_lang, given_reason.

Tokenising stays in matching.py (token_list / coverage / phrase).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import matching

# --- 1. match key -----------------------------------------------------------

# Letters, digits and combining marks of Gujarati, Devanagari and Latin. The
# blocks' punctuation/symbols stay out: Gujarati abbreviation sign + rupee
# (U+0AF0–0AF1), Devanagari dandas (U+0964–65) and abbreviation sign (U+0970).
_KEEP_RE = re.compile(r"[0-9a-zઁ-૯ૹ-૿ऀ-ॣ०-९ॱ-ॿ]")


def match_key(text: str) -> str:
    """Comparing-only key: NFC, lowercase, and nothing but script letters,
    digits and marks — so "સત્સંગદીક્ષા શાસ્ત્ર" == "સત્સંગ દીક્ષા શાસ્ત્ર".
    The stored text is never changed."""
    t = unicodedata.normalize("NFC", text or "").lower()
    return "".join(_KEEP_RE.findall(t))


# --- 2. word comparison -----------------------------------------------------

CONTAINED_MIN_WORDS = 3  # the shorter text must have at least this many words

# Strongest first; used for ranking.
WORD_KINDS = ("exact", "contained", "strong", "some", "weak", "none")


@dataclass(frozen=True)
class WordResult:
    kind: str  # one of WORD_KINDS
    overlap_pct: int  # share of the candidate's words found in the path
    shared_phrase_words: int  # longest run of shared consecutive words


@dataclass(frozen=True)
class Prepared:
    """A text tokenised once, so 163 × 270 comparisons stay fast."""
    words: tuple[str, ...]
    word_set: frozenset[str]
    key: str


def prepare(text: str) -> Prepared:
    words = tuple(matching.token_list(text))
    return Prepared(words, frozenset(words), match_key(text))


def compare(c: Prepared, p: Prepared) -> WordResult:
    """word_compare on prepared texts (candidate c vs archive path p)."""
    if c.key and c.key == p.key:
        return WordResult("exact", 100 if c.words else 0, len(c.words))
    contained = bool(c.key and p.key) and (
        (c.key in p.key and len(c.words) >= CONTAINED_MIN_WORDS)
        or (p.key in c.key and len(p.words) >= CONTAINED_MIN_WORDS)
    )
    shared = c.word_set & p.word_set
    if not shared and not contained:
        return WordResult("none", 0, 0)  # no shared word: skip the phrase DP
    pct = round(len(shared) / len(c.word_set) * 100) if c.word_set else 0
    phrase = matching.longest_shared_phrase(list(c.words), list(p.words))
    if contained:
        return WordResult("contained", pct, phrase)
    cov = len(shared) / len(c.word_set)
    return WordResult(matching.overlap_kind(cov, phrase, len(c.words)), pct, phrase)


def word_compare(candidate: str, path: str) -> WordResult:
    """Compare one candidate with one archive path by words only."""
    return compare(prepare(candidate), prepare(path))


# --- 3 (rules part). shortlist for the judge --------------------------------

# Measured on the spec's 13 known same-message pairs: the true past path is
# within the 12 nearest for 13/13 (within 8 for 12/13).
NEAREST_BY_MEANING = 12
SHORTLIST_CAP = 16


def shortlist(word_results: dict[int, WordResult], nearest_ids: list[int]) -> list[int]:
    """Rows sent to the judge: every row with a word result other than none
    (strongest first), then the nearest rows by meaning, capped at 12.
    No similarity threshold decides anything."""
    hits = sorted(
        (mid for mid, r in word_results.items() if r.kind != "none"),
        key=lambda mid: (WORD_KINDS.index(word_results[mid].kind), -word_results[mid].overlap_pct, mid),
    )
    out: list[int] = []
    for mid in hits + list(nearest_ids[:NEAREST_BY_MEANING]):
        if mid not in out:
            out.append(mid)
        if len(out) == SHORTLIST_CAP:
            break
    return out


# --- 5. result rules --------------------------------------------------------

JUDGE_LABELS = ("same_sentence", "same_message", "similar", "different")
RESULTS = ("given", "same", "similar", "new")


def decide(word_kinds: list[str], judge_labels: list[str] | None) -> str | None:
    """The candidate's result, first rule wins. The judge can raise a result
    but never lower one set by the word rules.

    judge_labels=None means the judge did not run. Only a word-rule `given`
    may be decided without it; anything else returns None (no silent
    fallback to "new")."""
    if any(k in ("exact", "contained") for k in word_kinds):
        return "given"
    if judge_labels is None:
        return None
    if "same_sentence" in judge_labels:
        return "given"
    if "same_message" in judge_labels:
        return "same"
    if "similar" in judge_labels or any(k in ("strong", "some") for k in word_kinds):
        return "similar"
    return "new"


# --- 6. which matches to keep -----------------------------------------------

MATCHES_KEPT = 5
_JUDGE_RANK = {"same_sentence": 0, "same_message": 1, "similar": 2, "different": 3, None: 4}


def rank_matches(rows: list[dict]) -> list[dict]:
    """Keep up to 5 rows. Each row: {message_id, word_kind, overlap_pct,
    judge}. Rows that are both word `none` and judged `different` (or not
    judged) are dropped. Order: exact/contained, then same_sentence,
    same_message, similar, then overlap."""
    kept = [r for r in rows if not (r["word_kind"] == "none" and r.get("judge") in ("different", None))]
    kept.sort(
        key=lambda r: (
            0 if r["word_kind"] in ("exact", "contained") else 1,
            _JUDGE_RANK.get(r.get("judge"), 4),
            -r["overlap_pct"],
            r["message_id"],
        )
    )
    return kept[:MATCHES_KEPT]


def given_reason(kind: str, date_label: str) -> str:
    """verdict_reason for a `given` decided by the word rules."""
    if kind == "exact":
        return f"This sentence was given on {date_label}."
    return f"This sentence and the path given on {date_label} contain one another word for word."


# --- 7. repeats inside the pool ---------------------------------------------

# A "strong" word match alone is not a repeat between two pool rows: in short
# sentences a shared 4-word run of common words ("એ એક જ આપણા") reaches it.
# It counts only when the shared run is most of the shorter sentence.
REPEAT_PHRASE_SHARE = 0.6


def is_repeat(a: Prepared, b: Prepared) -> bool:
    """Two pool rows say the same words: equal, one inside the other, or a
    strong match whose longest shared run covers ≥60% of the shorter one."""
    ab, ba = compare(a, b), compare(b, a)
    kinds = {ab.kind, ba.kind}
    if kinds & {"exact", "contained"}:
        return True
    shorter = min(len(a.words), len(b.words))
    phrase = max(ab.shared_phrase_words, ba.shared_phrase_words)
    return "strong" in kinds and shorter > 0 and phrase >= REPEAT_PHRASE_SHARE * shorter


def find_repeats(rows: list[tuple[int, str]]) -> dict[int, int]:
    """rows = [(sheet_row, text)]. Returns {sheet_row: duplicate_of}, where
    duplicate_of is the lower sheet_row of a pair is_repeat() accepts.
    Shown, never blocking."""
    out: dict[int, int] = {}
    ordered = [(row, prepare(text)) for row, text in sorted(rows)]
    for i, (row_a, prep_a) in enumerate(ordered):
        for row_b, prep_b in ordered[i + 1:]:
            if row_b not in out and is_repeat(prep_a, prep_b):
                out[row_b] = out.get(row_a, row_a)  # point at the first of a chain
    return out


# --- import preview helpers -------------------------------------------------

def doubled_words(text: str) -> list[str]:
    """Words typed twice in a row ("અને અને")."""
    words = matching.token_list(text)
    return [w for a, w in zip(words, words[1:]) if a == w]


_SPLIT = " / "
MAX_TOPICS = 2


def parse_sheet_topics(cell: str, topic_names: list[str]) -> tuple[list[str], list[str]]:
    """Parse a Vishay cell into (topics, errors). One topic name itself
    contains " / " (ગુણગ્રહણ / અભાવ-અવગુણ), so whole names are matched before
    splitting. A name not in the list is an error."""
    nfc = lambda s: unicodedata.normalize("NFC", " ".join(s.split()))
    names = {nfc(n): n for n in topic_names}
    cell = nfc(cell or "")
    if not cell:
        return [], []
    if cell in names:
        return [names[cell]], []
    parts = [p.strip() for p in cell.split(_SPLIT.strip())]
    parts = [nfc(p) for p in parts if p.strip()]
    topics: list[str] = []
    errors: list[str] = []
    i = 0
    while i < len(parts):
        joined = f"{parts[i]}{_SPLIT}{parts[i + 1]}" if i + 1 < len(parts) else None
        if joined and joined in names:
            name, i = names[joined], i + 2
        elif parts[i] in names:
            name, i = names[parts[i]], i + 1
        else:
            errors.append(f"Unknown topic: {parts[i]}")
            i += 1
            continue
        if name not in topics:
            topics.append(name)
    if len(topics) > MAX_TOPICS:
        errors.append(f"More than {MAX_TOPICS} topics")
    return topics, errors


_GU = re.compile(r"[઀-૿]")
_HI = re.compile(r"[ऀ-ॿ]")
_EN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """gu | hi | en | mixed, from the scripts present."""
    found = [code for code, rx in (("gu", _GU), ("hi", _HI), ("en", _EN)) if rx.search(text or "")]
    return found[0] if len(found) == 1 else ("mixed" if found else "en")


def resolve_topics(sheet: list[str], ai: list[str], current: list[str], confirmed: bool) -> tuple[list[str], bool]:
    """Current topics + confirmed flag after the judge returns ai topics.
    A person's choice (confirmed) is never overwritten. Sheet and AI agree
    (same set) → sheet order, confirmed. No sheet topic → the AI's, to
    confirm. They differ → keep the sheet's, a person chooses."""
    if confirmed:
        return current, True
    if not ai:
        return (current or sheet), False
    if not sheet:
        return list(ai), False
    if set(sheet) == set(ai):
        return list(sheet), True
    return list(sheet), False
