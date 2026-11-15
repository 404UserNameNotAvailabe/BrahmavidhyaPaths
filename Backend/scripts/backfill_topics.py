"""
Give archive paths their topics (build spec: "Giving the archive its
topics"). For each message with EMPTY tags: one topics-only judge call, the
result written to tags with tags_reviewed = false, so the team confirms them
in the Archive ("Topics not reviewed" filter). Rows that already have tags
are never touched. Paced; safe to stop and re-run.

    cd Backend
    GEMINI_JUDGE_MODEL=<model> .venv/bin/python -m scripts.backfill_topics [--dry-run] [--limit N]

Run only after the topic accuracy test (scripts.eval_candidates) passes the
85% bar and the team has agreed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import candidates
import judge
from database import db_cursor

PACE = float(os.getenv("BACKFILL_PACE_SECONDS", "3"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if not judge.JUDGE_MODEL:
        sys.exit("Set GEMINI_JUDGE_MODEL")

    topics = candidates.active_topics()
    if not topics:
        sys.exit("No topics: run sql/seed_topics.sql first")
    with db_cursor() as cur:
        cur.execute("SELECT id, message_text FROM messages WHERE cardinality(tags) = 0 ORDER BY message_date, id")
        rows = cur.fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} untagged paths")

    done = failed = 0
    for i, (mid, text) in enumerate(rows, 1):
        try:
            picked = judge.judge(text, [], topics).topics
        except judge.JudgeError as exc:
            failed += 1
            print(f"  {mid}: failed ({exc})")
            continue
        if args.dry_run:
            print(f"  {mid}: {' / '.join(picked)}")
        else:
            with db_cursor() as cur:
                # Only if still untagged: a person may have tagged it meanwhile.
                cur.execute(
                    "UPDATE messages SET tags = %s, tags_reviewed = false WHERE id = %s AND cardinality(tags) = 0",
                    (picked, mid),
                )
        done += 1
        print(f"\r{i}/{len(rows)}", end="", flush=True)
        time.sleep(PACE)
    print(f"\nTagged {done}, failed {failed}{' (dry run, nothing written)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
