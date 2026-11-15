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

try:  # read Backend/.env even when judge is imported before config (scripts, one-liners)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # unit tests run without python-dotenv
    pass

logger = logging.getLogger("brahmavidya.judge")

JUDGE_MODEL = os.getenv("GEMINI_JUDGE_MODEL", "")
# Region for the judge only (embeddings keep GOOGLE_CLOUD_LOCATION). Blank = same.
JUDGE_LOCATION = os.getenv("GEMINI_JUDGE_LOCATION", "").strip()
_judge_client = None
REASON_MAX_WORDS = 25

SYSTEM = """You compare a proposed spiritual message (a "candidate") with messages already given in past years ("paths"). The texts are short teachings from BAPS Swaminarayan sadhus, mostly in Gujarati, some in Hindi or English. Judge meaning, not just wording.

Label every listed path with exactly one of:
- same_sentence: the same wording apart from spacing, punctuation, spelling, or an added exclamation or attribution (e.g. "અહો!" added at the start).
- same_message: a listener would take away the same single teaching, though the words differ. This applies across languages.
- similar: the same SPECIFIC point said differently, or the same sentence frame with one key word swapped, so that a person choosing paths might count one as a repeat of the other. A person should read both.
- different: everything else. Sharing a theme (satsang, seva, unity, the guru's glory, moksha), a metaphor, an image or a general spiritual idea is NOT similar — that is different.

Most shortlisted paths are different: the shortlist is built to be broad. Use similar sparingly and same_message only when the single teaching truly matches.

For each path give a reason: one plain-English sentence of 25 words or fewer naming what is the same or what differs. Do not start it with the label (it is shown separately). You may quote Gujarati words.

Also choose 1 or 2 topics for the candidate from the topic list only, the most SPECIFIC fitting topic first. Broad topics (સમજણ, મહિમા, સત્સંગ) come first only when no more specific topic fits the sentence's main point.

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
        if not reason:
            raise JudgeError("reason missing")
        words = reason.split()
        if len(words) > REASON_MAX_WORDS:  # keep it, trimmed — not worth a retry
            reason = " ".join(words[:REASON_MAX_WORDS]).rstrip(",;:") + "…"
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
    if not picked:
        raise JudgeError("no topic chosen")
    return Verdict(labels, picked[:MAX_TOPICS])  # a third topic is dropped, not an error


def _vertex_generate(prompt: str) -> str:
    import embeddings  # lazy: pulls in config/.env and the SDK
    from google.genai import types

    global _judge_client
    client = embeddings._get_client()
    if client is not None and JUDGE_LOCATION and not embeddings.GEMINI_API_KEY:
        if _judge_client is None:  # ADC client pinned to the judge's region
            from google import genai

            _judge_client = genai.Client(vertexai=True, project=embeddings.GEMINI_PROJECT, location=JUDGE_LOCATION)
        client = _judge_client
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
