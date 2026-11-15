"""
Candidates: the database side of the check pipeline.

run_check(id) is called by candidate_worker for one claimed row:
  1–2. words vs every archive path (candidate_rules, prepared once)
       → an exact/contained hit is `given`: saved with no AI call at all
  3.   meaning: candidate embedding (RETRIEVAL_QUERY) → 8 nearest paths,
       plus every word hit, capped at 12 = the shortlist
  4.   judge: one Gemini call labels the shortlist and picks topics
  5–6. result rules, keep the top 5 matches, resolve topics, save
A failure anywhere marks the row `failed` with a reason — never a silent
"new". Results are saved only if the text was not edited mid-check.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date

import candidate_rules as cr
import judge as jg
import matching
from database import db_cursor, db_transaction

logger = logging.getLogger("brahmavidya.candidates")

EMBED_RETRIES = int(os.getenv("EMBED_MAX_RETRIES", "5"))
EMBED_BACKOFF = float(os.getenv("EMBED_BACKOFF_SECONDS", "30"))
OPEN_STATUSES = ("pending", "shortlisted", "approved")  # not given / rejected


class CheckFailed(Exception):
    """A step could not complete; the message is shown to the team."""


# --- archive snapshot (prepared once, refreshed when the archive changes) ---

@dataclass(frozen=True)
class ArchivePath:
    id: int
    date: date | None
    season: int | None
    link: str
    text: str
    prep: cr.Prepared


_archive_lock = threading.Lock()
_archive: dict = {"stamp": None, "paths": []}


def archive_paths() -> list[ArchivePath]:
    """All archive paths, tokenised once; reloaded only when the archive
    changes (row count or latest update moved)."""
    with db_cursor() as cur:
        cur.execute("SELECT count(*), max(updated_at) FROM messages")
        stamp = cur.fetchone()
    with _archive_lock:
        if _archive["stamp"] != stamp:
            with db_cursor() as cur:
                cur.execute("SELECT id, message_date, season, link, message_text FROM messages ORDER BY id")
                rows = cur.fetchall()
            _archive["paths"] = [
                ArchivePath(r[0], r[1], r[2], r[3] or "", r[4], cr.prepare(r[4])) for r in rows
            ]
            _archive["stamp"] = stamp
        return _archive["paths"]


def active_topics() -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT name FROM topics WHERE is_active ORDER BY sort_order, id")
        return [r[0] for r in cur.fetchall()]


def date_label(d: date | None) -> str:
    return f"{d.day} {d.strftime('%b %Y')}" if d else "an unknown date"


def best_word_hit(prep: cr.Prepared, paths: list[ArchivePath]) -> tuple[ArchivePath, cr.WordResult] | None:
    """The strongest word match (exact > contained > strong > some), or None."""
    best = None
    for p in paths:
        r = cr.compare(prep, p.prep)
        if r.kind in ("exact", "contained", "strong", "some"):
            rank = (cr.WORD_KINDS.index(r.kind), -r.overlap_pct)
            if best is None or rank < best[0]:
                best = (rank, p, r)
    return (best[1], best[2]) if best else None


# --- embeddings -------------------------------------------------------------

def _embed_query(text: str) -> list[float]:
    import embeddings  # lazy: config + SDK

    for attempt in range(1, EMBED_RETRIES + 1):
        vec = embeddings.embed_query(text)
        if vec is not None:
            return vec
        if attempt < EMBED_RETRIES:
            time.sleep(min(300, EMBED_BACKOFF * attempt))
    raise CheckFailed("Meaning search failed (embedding service unavailable)")


def _to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# --- the check --------------------------------------------------------------

def run_check(candidate_id: int) -> str:
    """Check one claimed (`running`) candidate. Returns the outcome for the
    log: given/same/similar/new, 'stale' if edited mid-check."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT text, sheet_topics, topics, topics_confirmed, embedding::text
               FROM candidates WHERE id = %s""",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return "deleted"
    text, sheet_topics, topics_now, confirmed, emb_text = row
    a = assess(text, list(sheet_topics), list(topics_now), confirmed, emb_text)
    return _save(candidate_id, text, a.verdict, a.reason, a.matches, a.ai_topics, a.topics,
                 a.topics_confirmed, a.vector, a.model)


@dataclass
class Assessment:
    verdict: str
    reason: str
    matches: list[dict]  # kept rows (rank order), see _match_row
    ai_topics: list[str] | None  # None = judge not called
    topics: list[str]
    topics_confirmed: bool
    vector: str | None  # pgvector literal when newly embedded
    model: str | None  # judge model, None = words only


def assess(text: str, sheet_topics: list[str], topics_now: list[str], confirmed: bool,
           emb_text: str | None = None) -> Assessment:
    """The whole check for one text, without writing anything. Raises
    CheckFailed. Used by run_check and by scripts/eval_candidates.py."""
    paths = archive_paths()
    by_id = {p.id: p for p in paths}
    prep = cr.prepare(text)
    words = {p.id: cr.compare(prep, p.prep) for p in paths}

    # Steps 1–2: a word-rule `given` needs no AI.
    if any(r.kind in ("exact", "contained") for r in words.values()):
        rows = [_match_row(mid, r, None, None, None) for mid, r in words.items() if r.kind != "none"]
        kept = cr.rank_matches(rows)
        top = kept[0]
        reason = cr.given_reason(top["word_kind"], date_label(by_id[top["message_id"]].date))
        return Assessment("given", reason, kept, None, topics_now, confirmed, None, None)

    # Step 3: meaning shortlist.
    vec = emb_text or _to_pgvector(_embed_query(text))
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, 1 - (embedding <=> %s::vector) FROM messages
               WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vec, vec, cr.NEAREST_BY_MEANING),
        )
        nearest = cur.fetchall()
    sims = {mid: float(sim) for mid, sim in nearest}
    short_ids = cr.shortlist(words, [mid for mid, _ in nearest])

    # Step 4: judge.
    try:
        verdict = jg.judge(
            text,
            [jg.PathRow(mid, by_id[mid].date.isoformat() if by_id[mid].date else "", by_id[mid].text)
             for mid in short_ids],
            active_topics(),
        )
    except jg.JudgeError as exc:
        raise CheckFailed(f"AI check failed ({exc})") from exc

    # Steps 5–6: result + matches.
    labels = {mid: verdict.labels[mid] for mid in short_ids}
    result = cr.decide([r.kind for r in words.values()], [lab for lab, _ in labels.values()])
    kept = cr.rank_matches([_match_row(mid, words[mid], sims.get(mid), *labels[mid]) for mid in short_ids])
    if result == "new" and not kept:
        reason = "No past path carries this message."
    elif kept and kept[0].get("judge_reason"):
        reason = kept[0]["judge_reason"]
    else:
        reason = "Shares wording with a past path; read both."
    topics, topics_ok = cr.resolve_topics(sheet_topics, verdict.topics, topics_now, confirmed)
    return Assessment(result, reason, kept, verdict.topics, topics, topics_ok,
                      None if emb_text else vec, jg.JUDGE_MODEL)


def _match_row(mid, word: cr.WordResult, sim, judge_label, judge_reason) -> dict:
    return {
        "message_id": mid,
        "word_kind": word.kind,
        "overlap_pct": word.overlap_pct,
        "shared_phrase_words": word.shared_phrase_words,
        "semantic_sim": sim,
        "judge": judge_label,
        "judge_reason": judge_reason,
    }


def _save(cid, checked_text, verdict, reason, kept, ai_topics, topics, confirmed, vec, model) -> str:
    """Store the result in one transaction — only if the row is still the one
    we checked (still running, same text). Otherwise discard: it was edited
    or re-queued and will be checked again."""
    with db_transaction() as cur:
        cur.execute(
            """UPDATE candidates SET
                   verdict = %s, verdict_reason = %s, check_state = 'done', check_error = NULL,
                   checked_at = now(), judge_model = %s,
                   ai_topics = COALESCE(%s, ai_topics), topics = %s, topics_confirmed = %s,
                   embedding = COALESCE(%s::vector, embedding)
               WHERE id = %s AND check_state = 'running' AND text = %s
               RETURNING id""",
            (verdict, reason, model, ai_topics, topics, confirmed, vec, cid, checked_text),
        )
        if cur.fetchone() is None:
            return "stale"
        cur.execute("DELETE FROM candidate_matches WHERE candidate_id = %s", (cid,))
        for rank, m in enumerate(kept, 1):
            cur.execute(
                """INSERT INTO candidate_matches
                   (candidate_id, message_id, rank, word_kind, overlap_pct, shared_phrase_words,
                    semantic_sim, judge, judge_reason)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (cid, m["message_id"], rank, m["word_kind"], m["overlap_pct"], m["shared_phrase_words"],
                 m["semantic_sim"], m["judge"], m["judge_reason"]),
            )
    return verdict


def mark_failed(cid: int, reason: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """UPDATE candidates SET check_state = 'failed', check_error = %s, checked_at = now()
               WHERE id = %s AND check_state = 'running'""",
            (reason[:300], cid),
        )


# --- queueing ---------------------------------------------------------------

def queue(ids: list[int] | None = None) -> int:
    """Queue candidates for a (re)check. ids=None → every open candidate."""
    with db_cursor() as cur:
        if ids is None:
            cur.execute(
                "UPDATE candidates SET check_state = 'queued', check_error = NULL WHERE status = ANY(%s)",
                (list(OPEN_STATUSES),),
            )
        else:
            cur.execute(
                """UPDATE candidates SET check_state = 'queued', check_error = NULL
                   WHERE id = ANY(%s) AND status <> 'given'""",
                (ids,),
            )
        return cur.rowcount


def requeue_for_archive(message_ids: list[int]) -> int:
    """An archive path was added or its text changed: queue open candidates
    whose word result against it is not none, or for which it is now among
    the 8 nearest by meaning. Cheap — no AI here."""
    if not message_ids:
        return 0
    paths = {p.id: p for p in archive_paths()}
    changed = [paths[m] for m in message_ids if m in paths]
    with db_cursor() as cur:
        cur.execute("SELECT id, text FROM candidates WHERE status = ANY(%s)", (list(OPEN_STATUSES),))
        open_rows = cur.fetchall()
        hit = {cid for cid, text in open_rows
               if any(cr.compare(cr.prepare(text), p.prep).kind != "none" for p in changed)}
        cur.execute(
            """SELECT c.id FROM candidates c
               WHERE c.status = ANY(%s) AND c.embedding IS NOT NULL
                 AND EXISTS (
                   SELECT 1 FROM (SELECT m.id FROM messages m WHERE m.embedding IS NOT NULL
                                  ORDER BY m.embedding <=> c.embedding LIMIT %s) near
                   WHERE near.id = ANY(%s))""",
            (list(OPEN_STATUSES), cr.NEAREST_BY_MEANING, message_ids),
        )
        hit |= {r[0] for r in cur.fetchall()}
    return queue(sorted(hit)) if hit else 0


def candidates_matching_message(message_id: int) -> list[int]:
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT candidate_id FROM candidate_matches WHERE message_id = %s", (message_id,))
        return [r[0] for r in cur.fetchall()]


def refresh_repeats() -> None:
    """Recompute duplicate_of across the whole pool (word rules only)."""
    with db_cursor() as cur:
        cur.execute("SELECT id, sheet_row, text FROM candidates")
        rows = cur.fetchall()
    # Order by sheet row (then id) so a repeat points at the earlier row.
    order = {cid: (sheet_row if sheet_row is not None else 10**9, cid) for cid, sheet_row, _ in rows}
    keyed = sorted(rows, key=lambda r: order[r[0]])
    pos = {r[0]: i for i, r in enumerate(keyed)}
    found = cr.find_repeats([(pos[cid], text) for cid, _, text in keyed])
    dup = {keyed[b][0]: keyed[a][0] for b, a in found.items()}
    with db_transaction() as cur:
        cur.execute("UPDATE candidates SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL")
        for cid, of in dup.items():
            cur.execute("UPDATE candidates SET duplicate_of = %s WHERE id = %s", (of, cid))


def normalised(text: str) -> str:
    return matching.normalize_text(text)
