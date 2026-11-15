"""
Accuracy test for the Candidates check (build spec: Acceptance tests +
Appendix B). Runs the REAL pipeline (words → meaning → AI judge) on every
sheet row, in memory: nothing is written to the database.

    cd Backend
    GEMINI_JUDGE_MODEL=<model> .venv/bin/python -m scripts.eval_candidates sheet.csv [--out report.csv]

sheet.csv = the team's sheet exported as CSV, columns No, Vakya, Vishay, Ref
(Gujarati headers નં / વાક્ય / વિષય / સંદર્ભ also work). Run it once per
candidate model; keep the cheapest model that passes. Costs one embedding +
at most one judge call per row that the word rules don't already settle.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time

import candidate_rules as cr
import candidates
import judge

# Appendix B (sheet row → archive messages.id), from one manual reading.
MUST_GIVEN = {16: 627, 84: 568, 150: 413}
MUST_SAME = {10: 509, 15: 595, 42: 528, 44: 449, 57: 391, 70: 567, 79: 569, 82: 508,
             118: 402, 135: 382, 139: 480, 156: 572, 157: 475}
MUST_STAY_NEW = {40, 66, 95, 99, 147}
SIMILAR_ROWS = {6, 8, 12, 14, 18, 19, 22, 25, 27, 32, 35, 38, 41, 47, 50, 53, 87, 89, 93, 96,
                104, 110, 111, 113, 114, 116, 117, 140, 143, 153, 162, 163}

HEADERS = {
    "no": "no", "nō": "no", "નં": "no", "ક્રમ": "no", "sr": "no", "#": "no",
    "vakya": "text", "vakyo": "text", "વાક્ય": "text", "text": "text", "sentence": "text",
    "vishay": "topics", "વિષય": "topics", "topic": "topics", "topics": "topics",
    "ref": "ref", "સંદર્ભ": "ref", "reference": "ref",
}


def read_sheet(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    head = [HEADERS.get(re.sub(r"\(.*\)", "", h).strip().lower(), "") for h in rows[0]]  # "Vishay (Subject)" → vishay
    if "text" not in head:
        sys.exit("No Vakya/text column found in the header row")
    out = []
    for i, r in enumerate(rows[1:], 2):
        d = {k: (r[j].strip() if j < len(r) else "") for j, k in enumerate(head) if k}
        if not d.get("text"):
            continue
        no = d.get("no", "")
        out.append({"no": int(no) if no.isdigit() else i - 1, "text": d["text"],
                    "topics": d.get("topics", ""), "ref": d.get("ref", "")})
    return out


def use_snapshot(path: str) -> None:
    """Point the pipeline at an archive snapshot file instead of the DB:
    archive paths, nearest-by-meaning (in-memory cosine) and the topic list
    (sql/seed_topics.sql)."""
    import json
    import math
    import os
    from datetime import date

    snap = json.load(open(path, encoding="utf-8"))
    paths = [candidates.ArchivePath(r["id"], date.fromisoformat(r["date"]) if r["date"] else None, r["season"],
                                    r["link"] or "", r["text"], cr.prepare(r["text"])) for r in snap]
    vecs = {r["id"]: r["embedding"] for r in snap if r.get("embedding")}
    norms = {k: math.sqrt(sum(x * x for x in v)) for k, v in vecs.items()}

    def nearest(vec: str):
        q = [float(x) for x in vec.strip("[]").split(",")]
        qn = math.sqrt(sum(x * x for x in q))
        sims = sorted(((sum(a * b for a, b in zip(q, v)) / (qn * norms[k]), k) for k, v in vecs.items()), reverse=True)
        return [(k, s) for s, k in sims[: cr.NEAREST_BY_MEANING]]

    seed = open(os.path.join(os.path.dirname(__file__), "..", "sql", "seed_topics.sql"), encoding="utf-8").read()
    topics = re.findall(r"\('([^']+)', \d+\)", seed)
    candidates.archive_paths = lambda: paths
    candidates._nearest = nearest
    candidates.active_topics = lambda: topics
    print(f"snapshot: {len(paths)} paths, {len(vecs)} with embeddings, {len(topics)} topics")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("--out", default="eval-report.csv")
    ap.add_argument("--model", help="override GEMINI_JUDGE_MODEL for this run")
    ap.add_argument("--rows", help="only these sheet rows, e.g. 10,15,84")
    ap.add_argument("--snapshot", help="archive snapshot JSON (id, date, season, link, text, embedding): "
                    "run without a database — nothing is read from or written to any DB")
    args = ap.parse_args()
    if args.snapshot:
        use_snapshot(args.snapshot)
    if args.model:
        judge.JUDGE_MODEL = args.model
    if not judge.JUDGE_MODEL:
        sys.exit("Set GEMINI_JUDGE_MODEL or pass --model")

    topic_names = candidates.active_topics()
    rows = read_sheet(args.sheet)
    if args.rows:
        keep = {int(x) for x in args.rows.split(",")}
        rows = [r for r in rows if r["no"] in keep]

    results, t0 = {}, time.monotonic()
    for i, r in enumerate(rows, 1):
        sheet_topics, _ = cr.parse_sheet_topics(r["topics"], topic_names)
        try:
            a = candidates.assess(r["text"], sheet_topics, sheet_topics, False)
            results[r["no"]] = {"verdict": a.verdict, "top": a.matches[0]["message_id"] if a.matches else None,
                                "reason": a.reason, "sheet_topics": sheet_topics, "ai_topics": a.ai_topics or [],
                                "error": ""}
        except candidates.CheckFailed as exc:
            results[r["no"]] = {"verdict": "failed", "top": None, "reason": "", "sheet_topics": sheet_topics,
                                "ai_topics": [], "error": str(exc)}
        print(f"\r{i}/{len(rows)} checked", end="", flush=True)
    print(f"  ({time.monotonic() - t0:.0f}s, model {judge.JUDGE_MODEL})\n")

    def verdict(n):
        return results.get(n, {}).get("verdict")

    def at(n, mid):
        return results.get(n, {}).get("top") == mid

    checks = []
    g = [n for n, mid in MUST_GIVEN.items() if n in results]
    ok = sum(verdict(n) == "given" and at(n, MUST_GIVEN[n]) for n in g)
    checks.append(("Already given (all 3 given, right date)", f"{ok}/{len(g)}", ok == len(g)))
    s = [n for n in MUST_SAME if n in results]
    ok = sum(verdict(n) in ("same", "given") and at(n, MUST_SAME[n]) for n in s)
    none_new = all(verdict(n) != "new" for n in s)
    checks.append(("Same message (≥12 of 13 same/given at the date, none new)", f"{ok}/{len(s)}",
                   ok >= min(12, len(s)) and none_new))
    nn = [n for n in MUST_STAY_NEW if n in results]
    ok = sum(verdict(n) == "new" for n in nn)
    bad = sum(verdict(n) in ("given", "same") for n in nn)
    checks.append(("Must stay new (≥4 of 5 new, none given/same)", f"{ok}/{len(nn)}", ok >= min(4, len(nn)) and bad == 0))
    sim = [n for n in SIMILAR_ROWS if n in results]
    ok = sum(verdict(n) in ("similar", "same", "given") for n in sim)
    checks.append(("Similar (report only)", f"{ok}/{len(sim)} similar or stronger", None))

    with_sheet = [n for n, r in results.items() if r["sheet_topics"] and r["ai_topics"]]
    agree = [n for n in with_sheet if results[n]["ai_topics"][0] in results[n]["sheet_topics"]]
    rate = len(agree) / len(with_sheet) if with_sheet else 0
    checks.append(("Topics (AI first topic in sheet's; bar 85%)", f"{rate:.0%} of {len(with_sheet)}", rate >= 0.85))

    failed = [n for n, r in results.items() if r["verdict"] == "failed"]
    counts = {k: sum(r["verdict"] == k for r in results.values()) for k in ("given", "same", "similar", "new", "failed")}
    print("| Check | Result | Pass |\n|---|---|---|")
    for name, res, passed in checks:
        print(f"| {name} | {res} | {'—' if passed is None else ('yes' if passed else '**NO**')} |")
    print(f"\nSplit: {counts}  (spec's manual reading: 3 given, 13 same, 32 similar, 115 new)")
    if failed:
        print(f"Failed checks (rows): {sorted(failed)}")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sheet_row", "expected", "verdict", "top_message_id", "reason", "sheet_topics", "ai_topics", "error"])
        for n in sorted(results):
            r = results[n]
            exp = ("given" if n in MUST_GIVEN else "same" if n in MUST_SAME else
                   "new" if n in MUST_STAY_NEW else "similar" if n in SIMILAR_ROWS else "")
            w.writerow([n, exp, r["verdict"], r["top"] or "", r["reason"], " / ".join(r["sheet_topics"]),
                        " / ".join(r["ai_topics"]), r["error"]])
    print(f"\nPer-row report: {args.out}")


if __name__ == "__main__":
    main()
