"""
Candidates + topics endpoints (build spec, API section). Same style as
app.py: bearer auth, role dependencies from auth.py, pydantic bodies from
models.py, fail() for errors, audit.record() on every write. No endpoint
calls a model: AI work happens in candidate_worker.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

import audit
import auth
import candidate_rules as cr
import candidate_worker
import candidates as cands
import matching
import taxonomy
import worker as embed_worker
from database import db_cursor, db_transaction
from models import (
    CandidateGiveRequest,
    CandidateImportRequest,
    CandidateStatusRequest,
    CandidateUpdateRequest,
    RecheckRequest,
    TopicCreateRequest,
    TopicUpdateRequest,
)

logger = logging.getLogger("brahmavidya.candidates_api")
router = APIRouter()

RESULTS = ("given", "same", "similar", "new")
STATUSES = ("pending", "shortlisted", "approved", "rejected", "given")
# Who may move a candidate INTO each status (given only via /give).
STATUS_ROLE = {"pending": "editor", "shortlisted": "editor", "approved": "admin", "rejected": "admin"}


def fail(message: str = "The request could not be completed.", **extra) -> dict:
    logger.warning("Request failed (%s)", message, exc_info=True)
    return {"status": "error", "message": message, **extra}


def _has_role(user: dict, minimum: str) -> bool:
    role = user.get("role", "viewer")
    return (auth.ROLES.index(role) if role in auth.ROLES else 0) >= auth.ROLES.index(minimum)


# --- topics -----------------------------------------------------------------


@router.get("/topics")
async def list_topics(user: dict = Depends(auth.get_current_user)):
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT t.id, t.name, t.sort_order, t.is_active,
                          (SELECT count(*) FROM messages m WHERE t.name = ANY(m.tags)),
                          (SELECT count(*) FROM candidates c WHERE t.name = ANY(c.topics))
                   FROM topics t ORDER BY t.sort_order, t.id"""
            )
            rows = cur.fetchall()
    except Exception:
        return fail(data={"topics": []})
    return {
        "status": "success",
        "data": {"topics": [
            {"id": str(r[0]), "name": r[1], "sort_order": r[2], "is_active": r[3],
             "archive_count": r[4], "candidate_count": r[5]} for r in rows
        ]},
    }


@router.post("/topics")
async def create_topic(payload: TopicCreateRequest, user: dict = Depends(auth.require_admin)):
    name = " ".join(payload.name.split())
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO topics (name, sort_order)
               VALUES (%s, (SELECT coalesce(max(sort_order), 0) + 1 FROM topics))
               ON CONFLICT (name) DO NOTHING RETURNING id""",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return {"status": "error", "message": "That topic already exists"}
    audit.record(user["username"], "topic.create", name)
    return {"status": "success", "data": {"id": str(row[0])}}


@router.patch("/topics/{topic_id}")
async def update_topic(topic_id: int, payload: TopicUpdateRequest, user: dict = Depends(auth.require_admin)):
    """Rename / reorder / retire. A rename rewrites every tag and candidate
    topic in the same transaction."""
    try:
        with db_transaction() as cur:
            cur.execute("SELECT name FROM topics WHERE id = %s FOR UPDATE", (topic_id,))
            row = cur.fetchone()
            if row is None:
                return {"status": "error", "message": "Topic not found"}
            old = row[0]
            if payload.name is not None:
                new = " ".join(payload.name.split())
                if new != old:
                    cur.execute("SELECT 1 FROM topics WHERE name = %s", (new,))
                    if cur.fetchone():
                        return {"status": "error", "message": "A topic with that name already exists"}
                    cur.execute("UPDATE topics SET name = %s WHERE id = %s", (new, topic_id))
                    cur.execute("UPDATE messages SET tags = array_replace(tags, %s, %s) WHERE %s = ANY(tags)", (old, new, old))
                    for col in ("topics", "sheet_topics", "ai_topics"):
                        cur.execute(
                            f"UPDATE candidates SET {col} = array_replace({col}, %s, %s) WHERE %s = ANY({col})",
                            (old, new, old),
                        )
            if payload.sort_order is not None:
                cur.execute("UPDATE topics SET sort_order = %s WHERE id = %s", (payload.sort_order, topic_id))
            if payload.is_active is not None:
                cur.execute("UPDATE topics SET is_active = %s WHERE id = %s", (payload.is_active, topic_id))
    except Exception:
        return fail()
    audit.record(user["username"], "topic.update", f"id={topic_id}")
    return {"status": "success"}


# --- import -----------------------------------------------------------------


def _analyse(rows, topic_names: list[str]) -> tuple[list[dict], dict]:
    """Rules-only preview: no AI. Per row: errors, archive word hit, in_pool,
    repeat_of_index, doubled_word, parsed topics."""
    paths = cands.archive_paths()
    with db_cursor() as cur:
        cur.execute("SELECT id, normalized_text, match_key FROM candidates")
        pool = cur.fetchall()
    pool_norm = {n: cid for cid, n, _ in pool}
    pool_key = {k: cid for cid, _, k in pool}

    texts = [(r.text or "").strip() for r in rows]
    repeats = cr.find_repeats([(i, t) for i, t in enumerate(texts) if t])

    out = []
    for i, (r, text) in enumerate(zip(rows, texts)):
        errors: list[str] = []
        if len(text) < 2:
            errors.append("Text is empty")
        topics, topic_errors = cr.parse_sheet_topics(r.topics or "", topic_names)
        errors += topic_errors
        archive = None
        if text:
            hit = cands.best_word_hit(cr.prepare(text), paths)
            if hit:
                p, res = hit
                archive = {"kind": res.kind, "message_id": str(p.id),
                           "date": p.date.isoformat() if p.date else None,
                           "overlap_pct": res.overlap_pct, "shared_phrase_words": res.shared_phrase_words}
        in_pool = pool_norm.get(matching.normalize_text(text)) or pool_key.get(cr.match_key(text))
        doubled = cr.doubled_words(text)
        out.append({
            "index": i,
            "sheet_row": r.sheet_row,
            "text": text,
            "topics": topics,
            "ref": (r.ref or "").strip() or None,
            "lang": cr.detect_lang(text),
            "errors": errors,
            "archive": archive,
            "in_pool": str(in_pool) if in_pool else None,
            "repeat_of_index": repeats.get(i),
            "doubled_word": doubled[0] if doubled else None,
        })
    given = sum(1 for x in out if x["archive"] and x["archive"]["kind"] in ("exact", "contained"))
    summary = {
        "rows": len(out),
        "already_given": given,
        "close_to_archive": sum(1 for x in out if x["archive"] and x["archive"]["kind"] in ("strong", "some")),
        "repeats": sum(1 for x in out if x["repeat_of_index"] is not None),
        "in_pool": sum(1 for x in out if x["in_pool"]),
        "errors": sum(1 for x in out if x["errors"]),
        "doubled_words": sum(1 for x in out if x["doubled_word"]),
        "no_topic": sum(1 for x in out if not x["topics"]),
    }
    summary["ready"] = sum(1 for x in out if not x["errors"] and not x["in_pool"])
    return out, summary


@router.post("/candidates/preview")
async def preview_candidates(payload: CandidateImportRequest, user: dict = Depends(auth.require_editor)):
    try:
        rows, summary = _analyse(payload.rows, cands.active_topics())
    except Exception:
        return fail(data={"rows": [], "summary": {}})
    return {"status": "success", "data": {"rows": rows, "summary": summary}}


@router.post("/candidates/import")
async def import_candidates(payload: CandidateImportRequest, user: dict = Depends(auth.require_editor)):
    """Save rows to the pool and return at once; checks run in the worker.
    Rows already given (word rules) are saved as rejected with the date, so
    nobody proposes them again. Rows with errors are skipped and listed."""
    try:
        analysed, _ = _analyse(payload.rows, cands.active_topics())
    except Exception:
        return fail(data={"added": 0, "skipped": 0, "errors": []})
    collection = " ".join(payload.collection.split())
    added = skipped = 0
    errors: list[dict] = []
    with db_cursor() as cur:
        for a in analysed:
            if a["errors"]:
                errors.append({"row": a["sheet_row"] or a["index"] + 1, "message": "; ".join(a["errors"])})
                continue
            given = a["archive"] and a["archive"]["kind"] in ("exact", "contained")
            status, note = ("rejected", f"Given on {a['archive']['date']}") if given else ("pending", None)
            try:
                cur.execute(
                    """INSERT INTO candidates
                         (text, match_key, lang, topics, sheet_topics, ref, collection, sheet_row,
                          status, status_note, status_by, status_at, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               CASE WHEN %s THEN now() END, %s)
                       ON CONFLICT (normalized_text) DO NOTHING RETURNING id""",
                    (a["text"], cr.match_key(a["text"]), a["lang"], a["topics"], a["topics"], a["ref"],
                     collection, a["sheet_row"], status, note, "import" if given else None, bool(given),
                     user["username"]),
                )
                if cur.fetchone() is None:
                    skipped += 1
                else:
                    added += 1
            except Exception:
                logger.warning("candidate import row %s failed", a["index"] + 1, exc_info=True)
                errors.append({"row": a["sheet_row"] or a["index"] + 1, "message": "Could not save this row"})
    try:
        cands.refresh_repeats()
    except Exception:
        logger.warning("refresh_repeats failed after import", exc_info=True)
    candidate_worker.wake()
    audit.record(user["username"], "candidate.import", f"{collection}: added={added} skipped={skipped} errors={len(errors)}")
    return {"status": "success", "data": {"added": added, "skipped": skipped, "errors": errors}}


# --- list + detail ----------------------------------------------------------

_SORTS = {
    "sheet": "c.sheet_row ASC NULLS LAST, c.id ASC",
    "newest": "c.created_at DESC, c.id DESC",
    "checked": "c.checked_at DESC NULLS LAST, c.id DESC",
}


def _filters(verdict, topic, status, collection, q, topics_unconfirmed, include_verdict=True):
    clauses, params = [], []
    if include_verdict and verdict:
        if verdict == "unchecked":
            clauses.append("c.verdict IS NULL")
        elif verdict in RESULTS:
            clauses.append("c.verdict = %s")
            params.append(verdict)
    if topic:
        clauses.append("%s = ANY(c.topics)")
        params.append(topic)
    if status == "open":
        clauses.append("c.status IN ('pending','shortlisted','approved')")
    elif status in STATUSES:
        clauses.append("c.status = %s")
        params.append(status)
    if collection:
        clauses.append("c.collection = %s")
        params.append(collection)
    if q and q.strip():
        like = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(c.text ILIKE %s ESCAPE '\\' OR c.ref ILIKE %s ESCAPE '\\')")
        params += [f"%{like}%", f"%{like}%"]
    if topics_unconfirmed:
        clauses.append("NOT c.topics_confirmed AND cardinality(c.ai_topics) > 0")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


@router.get("/candidates")
async def list_candidates(
    verdict: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    collection: str | None = None,
    q: str | None = None,
    topics_unconfirmed: bool = False,
    order: str = "sheet",
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(auth.get_current_user),
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where, params = _filters(verdict, topic, status, collection, q, topics_unconfirmed)
    where_nv, params_nv = _filters(verdict, topic, status, collection, q, topics_unconfirmed, include_verdict=False)
    try:
        with db_cursor() as cur:
            cur.execute(f"SELECT count(*) FROM candidates c {where}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"""SELECT c.id, c.text, c.lang, c.topics, c.verdict, c.check_state, c.status,
                           c.sheet_row, c.duplicate_of, c.topics_confirmed,
                           cardinality(c.ai_topics) > 0, m.message_date, c.check_error
                    FROM candidates c
                    LEFT JOIN candidate_matches cm ON cm.candidate_id = c.id AND cm.rank = 1
                    LEFT JOIN messages m ON m.id = cm.message_id
                    {where}
                    ORDER BY {_SORTS.get(order, _SORTS['sheet'])}
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            rows = cur.fetchall()
            cur.execute(f"SELECT coalesce(c.verdict, 'unchecked'), count(*) FROM candidates c {where_nv} GROUP BY 1", params_nv)
            by_verdict = dict(cur.fetchall())
            cur.execute("SELECT count(*) FILTER (WHERE status IN ('pending','shortlisted','approved')), count(*) FROM candidates")
            waiting, pool_total = cur.fetchone()
            cur.execute("SELECT DISTINCT collection FROM candidates WHERE collection IS NOT NULL ORDER BY 1")
            collections = [r[0] for r in cur.fetchall()]
    except Exception:
        return fail(data={"entries": [], "total": 0, "counts": {}})
    counts = {k: by_verdict.get(k, 0) for k in (*RESULTS, "unchecked")}
    counts["all"] = sum(counts.values())
    return {
        "status": "success",
        "data": {
            "entries": [
                {
                    "id": str(r[0]), "text": r[1], "lang": r[2], "topics": r[3] or [], "verdict": r[4],
                    "check_state": r[5], "status": r[6], "sheet_row": r[7],
                    "duplicate_of": str(r[8]) if r[8] else None,
                    "topics_to_confirm": (not r[9]) and bool(r[10]),
                    "closest_date": r[11].isoformat() if r[11] else None,
                    "check_error": r[12],
                }
                for r in rows
            ],
            "total": total,
            "counts": counts,
            "waiting": waiting,
            "pool_total": pool_total,
            "collections": collections,
        },
    }


@router.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: int, user: dict = Depends(auth.get_current_user)):
    try:
        with db_cursor() as cur:
            cur.execute(
                """SELECT id, text, lang, topics, sheet_topics, ai_topics, topics_confirmed, ref, collection,
                          sheet_row, duplicate_of, status, status_note, status_by, status_at, target_season,
                          verdict, verdict_reason, check_state, check_error, checked_at, message_id, created_by
                   FROM candidates WHERE id = %s""",
                (candidate_id,),
            )
            c = cur.fetchone()
            if c is None:
                return {"status": "error", "message": "Candidate not found"}
            cur.execute(
                """SELECT cm.message_id, m.message_date, m.season, m.link, m.message_text, cm.word_kind,
                          cm.overlap_pct, cm.shared_phrase_words, cm.judge, cm.judge_reason
                   FROM candidate_matches cm JOIN messages m ON m.id = cm.message_id
                   WHERE cm.candidate_id = %s ORDER BY cm.rank""",
                (candidate_id,),
            )
            match_rows = cur.fetchall()
            history = []
            for t in c[3] or []:
                cur.execute(
                    """SELECT count(*) OVER (), message_date, message_text FROM messages
                       WHERE %s = ANY(tags) ORDER BY message_date DESC NULLS LAST LIMIT 1""",
                    (t,),
                )
                h = cur.fetchone()
                history.append({
                    "topic": t, "count": h[0] if h else 0,
                    "latest": {"date": h[1].isoformat() if h and h[1] else None, "text": h[2]} if h else None,
                })
            duplicate = None
            if c[10]:
                cur.execute("SELECT id, sheet_row, text FROM candidates WHERE id = %s", (c[10],))
                d = cur.fetchone()
                if d:
                    duplicate = {"id": str(d[0]), "sheet_row": d[1], "text": d[2]}
    except Exception:
        return fail()

    cand_tokens = matching.tokenize(c[1])
    matches = [
        {
            "message_id": str(m[0]),
            "date": m[1].isoformat() if m[1] else None,
            "season": m[2],
            "link": m[3] or "",
            "snippet_html": matching.highlight_overlap(m[4], cand_tokens & matching.tokenize(m[4])),
            "word_kind": m[5],
            "overlap_pct": m[6],
            "shared_phrase_words": m[7],
            "judge": m[8],
            "judge_reason": m[9],
        }
        for m in match_rows
    ]
    return {
        "status": "success",
        "data": {
            "id": str(c[0]), "text": c[1], "lang": c[2], "topics": c[3] or [], "sheet_topics": c[4] or [],
            "ai_topics": c[5] or [], "topics_confirmed": c[6], "ref": c[7], "collection": c[8],
            "sheet_row": c[9], "duplicate_of": str(c[10]) if c[10] else None, "status": c[11],
            "status_note": c[12], "status_by": c[13], "status_at": c[14].isoformat() if c[14] else None,
            "target_season": c[15], "verdict": c[16], "verdict_reason": c[17], "check_state": c[18],
            "check_error": c[19], "checked_at": c[20].isoformat() if c[20] else None,
            "message_id": str(c[21]) if c[21] else None, "created_by": c[22],
            "matches": matches, "topic_history": history, "duplicate": duplicate,
        },
    }


# --- writes -----------------------------------------------------------------


@router.patch("/candidates/{candidate_id}")
async def update_candidate(candidate_id: int, payload: CandidateUpdateRequest, user: dict = Depends(auth.require_editor)):
    provided = payload.model_fields_set
    if not provided:
        return {"status": "error", "message": "No fields to update"}
    sets, params = [], []
    text_changed = False
    if "text" in provided and payload.text is not None:
        text = payload.text.strip()
        sets += ["text = %s", "match_key = %s", "lang = %s", "embedding = NULL", "verdict = NULL",
                 "verdict_reason = NULL", "check_state = 'queued'", "check_error = NULL", "judge_model = NULL"]
        params += [text, cr.match_key(text), cr.detect_lang(text)]
        text_changed = True
    if "topics" in provided and payload.topics is not None:
        allowed = set(cands.active_topics())
        topics = list(dict.fromkeys(t.strip() for t in payload.topics if t.strip()))
        if any(t not in allowed for t in topics):
            return {"status": "error", "message": "Topics must come from the topic list"}
        sets += ["topics = %s", "topics_confirmed = true"]
        params.append(topics)
    if "ref" in provided:
        sets.append("ref = %s")
        params.append((payload.ref or "").strip() or None)
    if "collection" in provided:
        sets.append("collection = %s")
        params.append((payload.collection or "").strip() or None)
    if not sets:
        return {"status": "error", "message": "No fields to update"}
    try:
        with db_transaction() as cur:
            cur.execute("SELECT status FROM candidates WHERE id = %s FOR UPDATE", (candidate_id,))
            row = cur.fetchone()
            if row is None:
                return {"status": "error", "message": "Candidate not found"}
            if row[0] == "given":
                return {"status": "error", "message": "A given candidate can no longer be edited"}
            cur.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id = %s", params + [candidate_id])
            if text_changed:
                cur.execute("DELETE FROM candidate_matches WHERE candidate_id = %s", (candidate_id,))
    except Exception as exc:
        if "ux_candidates_normalized" in str(exc):
            return {"status": "error", "message": "That text is already in the pool"}
        return fail()
    if text_changed:
        try:
            cands.refresh_repeats()
        except Exception:
            logger.warning("refresh_repeats failed after edit", exc_info=True)
        candidate_worker.wake()
    audit.record(user["username"], "candidate.edit", f"id={candidate_id} ({', '.join(sorted(provided))})")
    return {"status": "success"}


@router.post("/candidates/{candidate_id}/status")
async def set_candidate_status(candidate_id: int, payload: CandidateStatusRequest, user: dict = Depends(auth.require_editor)):
    target = payload.status
    if target not in STATUS_ROLE:
        return {"status": "error", "message": "Status must be pending, shortlisted, approved or rejected"}
    if not _has_role(user, STATUS_ROLE[target]):
        return {"status": "error", "message": f"Only an admin can mark a candidate {target}"}
    note = (payload.note or "").strip() or None
    try:
        with db_transaction() as cur:
            cur.execute("SELECT status, verdict FROM candidates WHERE id = %s FOR UPDATE", (candidate_id,))
            row = cur.fetchone()
            if row is None:
                return {"status": "error", "message": "Candidate not found"}
            current, verdict = row
            if current == "given":
                return {"status": "error", "message": "This candidate has already been given"}
            if target == "rejected" and not note and verdict != "given":
                return {"status": "error", "message": "Add a note saying why it is rejected"}
            season = (payload.target_season or taxonomy.upcoming_season()) if target == "approved" else None
            cur.execute(
                """UPDATE candidates SET status = %s, status_note = %s, status_by = %s, status_at = now(),
                          target_season = %s WHERE id = %s""",
                (target, note, user["username"], season, candidate_id),
            )
    except Exception:
        return fail()
    audit.record(user["username"], "candidate.status", f"id={candidate_id} {current}→{target}")
    return {"status": "success", "data": {"status": target, "target_season": season}}


@router.post("/candidates/{candidate_id}/give")
async def give_candidate(candidate_id: int, payload: CandidateGiveRequest, user: dict = Depends(auth.require_editor)):
    """Promote an approved candidate into the archive in one transaction. If
    the text already exists there, link to that row instead of failing."""
    try:
        with db_transaction() as cur:
            cur.execute("SELECT status, text, topics FROM candidates WHERE id = %s FOR UPDATE", (candidate_id,))
            row = cur.fetchone()
            if row is None:
                return {"status": "error", "message": "Candidate not found"}
            status, text, topics = row
            if status != "approved":
                return {"status": "error", "message": "Only an approved candidate can be marked as given"}
            cur.execute(
                """INSERT INTO messages (message_text, message_date, tags, tags_reviewed, source, link)
                   VALUES (%s, %s, %s, true, %s, %s)
                   ON CONFLICT (normalized_text) DO NOTHING RETURNING id""",
                (text, payload.message_date, topics or [], taxonomy.extract_source(text), payload.link),
            )
            got = cur.fetchone()
            if got is None:
                cur.execute("SELECT id FROM messages WHERE normalized_text = %s", (matching.normalize_text(text),))
                got = cur.fetchone()
                if got is None:
                    return {"status": "error", "message": "Could not add or find this path in the archive"}
            message_id = got[0]
            cur.execute(
                """UPDATE candidates SET status = 'given', message_id = %s, status_by = %s, status_at = now(),
                          plan_date = %s WHERE id = %s""",
                (message_id, user["username"], payload.message_date, candidate_id),
            )
    except Exception:
        return fail()
    audit.record(user["username"], "candidate.give", f"id={candidate_id} → message {message_id} ({payload.message_date})")
    # Background: embed the new archive row, then other candidates near it are re-queued.
    embed_worker.start()
    try:
        if cands.requeue_for_archive([message_id]):
            candidate_worker.wake()
    except Exception:
        logger.warning("requeue after give failed", exc_info=True)
    return {"status": "success", "data": {"message_id": str(message_id)}}


@router.post("/candidates/recheck")
async def recheck_candidates(payload: RecheckRequest, user: dict = Depends(auth.require_editor)):
    try:
        n = cands.queue(payload.ids)
    except Exception:
        return fail()
    candidate_worker.wake()
    audit.record(user["username"], "candidate.recheck", f"queued={n}")
    return {"status": "success", "data": {"queued": n}}


@router.delete("/candidates/{candidate_id}")
async def delete_candidate(candidate_id: int, user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute("DELETE FROM candidates WHERE id = %s RETURNING id", (candidate_id,))
        found = cur.fetchone()
    if found is None:
        return {"status": "error", "message": "Candidate not found"}
    audit.record(user["username"], "candidate.delete", f"id={candidate_id}")
    return {"status": "success"}
