"""
The AI judge for the Candidates check (build spec, step 4).

One generative call per candidate: the candidate text, its shortlist of
archive paths (id, date, text) and the topic list go in; a label + one-line
reason per shortlisted path and 1–2 topics come back as JSON.

Privacy: only the texts above are sent (archive paths are public on
baps.org). No usernames, notes or ids other than message ids. Never log the
prompt or the reply text.

The model name comes from GEMINI_JUDGE_MODEL; nothing is hard-coded. The
client is the same google-genai client embeddings.py builds (Vertex).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from candidate_rules import JUDGE_LABELS, MAX_TOPICS

logger = logging.getLogger("brahmavidya.judge")

JUDGE_MODEL = os.getenv("GEMINI_JUDGE_MODEL", "")
REASON_MAX_WORDS = 25

SYSTEM = """You compare a proposed spiritual message (a "candidate") with messages already given in past years ("paths"). The texts are short teachings from BAPS Swaminarayan sadhus, mostly in Gujarati, some in Hindi or English. Judge meaning, not just wording.

Label every listed path with exactly one of:
- same_sentence: the same wording apart from spacing, punctuation, spelling, or an added exclamation or attribution (e.g. "અહો!" added at the start).
- same_message: a listener would take away the same single teaching, though the words differ. This applies across languages.
- similar: the same subject and a close point, or the same sentence frame with a different key word. A person should read both.
- different: shares only a topic or common words.

For each path give a reason: one plain-English sentence of 25 words or fewer naming what is the same or what differs. Do not start it with the label (it is shown separately). You may quote Gujarati words.

Also choose 1 or 2 topics for the candidate, most fitting first, from the topic list only.

Reply with JSON only, matching the schema. Include every listed path exactly once."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "matches": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "message_id": {"type": "STRING"},
                    "label": {"type": "STRING", "enum": list(JUDGE_LABELS)},
                    "reason": {"type": "STRING"},
                },
                "required": ["message_id", "label", "reason"],
            },
        },
        "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["matches", "topics"],
}


@dataclass(frozen=True)
class PathRow:
    message_id: int
    date: str  # ISO date
    text: str


@dataclass(frozen=True)
class Verdict:
    labels: dict[int, tuple[str, str]]  # message_id → (label, reason)
    topics: list[str]


class JudgeError(Exception):
    """The judge could not produce a valid answer (after one retry)."""


def build_prompt(candidate: str, shortlist: list[PathRow], topics: list[str]) -> str:
    paths = "\n".join(f"- id {p.message_id} ({p.date}): {p.text}" for p in shortlist)
    topic_list = "\n".join(f"- {t}" for t in topics)
    return f"Candidate:\n{candidate}\n\nPaths:\n{paths or '(none)'}\n\nTopic list:\n{topic_list}"


def validate(raw: str, shortlist: list[PathRow], topics: list[str]) -> Verdict:
    """Parse and check a reply. Raises JudgeError on anything off-spec:
    every listed path labelled once, labels from the four, reasons short,
    1–2 topics from the list."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise JudgeError("reply is not JSON") from exc
    if not isinstance(data, dict):
        raise JudgeError("reply is not an object")

    wanted = {p.message_id for p in shortlist}
    labels: dict[int, tuple[str, str]] = {}
    for m in data.get("matches") or []:
        try:
            mid = int(str(m["message_id"]).strip())
            label, reason = str(m["label"]), " ".join(str(m["reason"]).split())
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeError("malformed match") from exc
        if mid not in wanted:
            raise JudgeError("message_id not in the shortlist")
        if mid in labels:
            raise JudgeError("message_id labelled twice")
        if label not in JUDGE_LABELS:
            raise JudgeError("unknown label")
        if not reason or len(reason.split()) > REASON_MAX_WORDS:
            raise JudgeError("reason missing or too long")
        labels[mid] = (label, reason)
    if set(labels) != wanted:
        raise JudgeError("not every shortlisted path was labelled")

    allowed = set(topics)
    picked: list[str] = []
    for t in data.get("topics") or []:
        t = str(t).strip()
        if t not in allowed:
            raise JudgeError("topic not in the list")
        if t not in picked:
            picked.append(t)
    if not 1 <= len(picked) <= MAX_TOPICS:
        raise JudgeError("need 1 or 2 topics")
    return Verdict(labels, picked)


def _vertex_generate(prompt: str) -> str:
    import embeddings  # lazy: pulls in config/.env and the SDK
    from google.genai import types

    client = embeddings._get_client()
    if client is None or not JUDGE_MODEL:
        raise JudgeError("judge model not configured")
    resp = client.models.generate_content(
        model=JUDGE_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    return resp.text


def judge(candidate: str, shortlist: list[PathRow], topics: list[str], generate=None) -> Verdict:
    """One judge call with one retry. Raises JudgeError when the model isn't
    configured, the call fails, or the reply fails validation twice.
    `generate(prompt) -> str` is injectable for tests; default is Vertex."""
    generate = generate or _vertex_generate
    prompt = build_prompt(candidate, shortlist, topics)
    last = "unknown"
    for attempt in (1, 2):
        try:
            return validate(generate(prompt), shortlist, topics)
        except JudgeError as exc:
            last = str(exc)
            if last == "judge model not configured":
                break
        except Exception as exc:  # network / quota / SDK — never log the text
            last = type(exc).__name__
        logger.warning("judge attempt %d failed: %s", attempt, last)
    raise JudgeError(last)
