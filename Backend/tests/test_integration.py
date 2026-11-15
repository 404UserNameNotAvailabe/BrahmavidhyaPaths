"""End-to-end Candidates tests against a THROWAWAY local Postgres (never Neon).

Skipped unless BV_TEST_DB=1. Expects DB_HOST/DB_PORT/DB_NAME/DB_USER/
DB_PASSWORD/DB_SSLMODE pointing at an empty pgvector database, and
BV_ARCHIVE_CSV = an archive export (id,date,season,message,tags,link,starred).
The AI is faked: no network calls. Run from Backend/ with the venv:

    BV_TEST_DB=1 CANDIDATE_WORKER=off DB_HOST=127.0.0.1 DB_PORT=55432 ... \
      python -m unittest tests.test_integration -v
"""

import csv
from datetime import date
import hashlib
import os
import subprocess
import unittest

if os.getenv("BV_TEST_DB") != "1":
    raise unittest.SkipTest("integration tests need BV_TEST_DB=1 and a throwaway database")

os.environ.setdefault("CANDIDATE_WORKER", "off")
os.environ["GEMINI_API_KEY"] = ""
os.environ["GOOGLE_CLOUD_PROJECT"] = ""

import config  # noqa: E402  (loads Backend/.env — which on the server is Neon)

if config.DB_HOST not in ("127.0.0.1", "localhost", "::1") or not config.DB_NAME.startswith("bvtest"):
    raise SystemExit(
        f"REFUSING: integration tests DROP the whole schema. DB_HOST must be local and DB_NAME must "
        f"start with 'bvtest' (got host={config.DB_HOST!r} db={config.DB_NAME!r})."
    )

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import candidate_worker  # noqa: E402
import candidates  # noqa: E402
import judge  # noqa: E402
from database import db_cursor  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vec_for(text: str) -> list[float]:
    """Deterministic fake 768-d unit-ish vector (not semantic)."""
    h = hashlib.sha256(text.encode()).digest()
    return [((h[i % 32] + i) % 97) / 97.0 - 0.5 for i in range(768)]


def pgvec(v):
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


class FakeJudge:
    """Labels from a per-candidate script; records calls."""

    def __init__(self):
        self.script = {}  # candidate text → {message_id: label}
        self.topics = {}  # candidate text → [topics]
        self.fail = set()
        self.calls = []

    def __call__(self, text, shortlist, topics):
        self.calls.append((text, [p.message_id for p in shortlist]))
        if text in self.fail:
            raise judge.JudgeError("reply is not JSON")
        want = self.script.get(text, {})
        labels = {p.message_id: (want.get(p.message_id, "different"), f"Fake reason for {p.message_id}.")
                  for p in shortlist}
        return judge.Verdict(labels, self.topics.get(text, [topics[0]]))


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Old (pre-fix) schema + the 270 public archive paths at their real ids.
        old_schema = subprocess.run(["git", "show", "bc3c35f:Backend/sql/schema.sql"], cwd=HERE,
                                    capture_output=True, text=True, check=True,
                                    env={**os.environ, "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}).stdout
        cls.archive = list(csv.DictReader(open(os.environ["BV_ARCHIVE_CSV"], encoding="utf-8")))
        with db_cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            cur.execute(old_schema)
            for r in cls.archive:
                cur.execute(
                    """INSERT INTO messages (id, message_text, message_date, link, embedding)
                       OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s::vector)""",
                    (int(r["id"]), r["message"], r["date"], r["link"] or None, pgvec(vec_for(r["message"]))),
                )
        # 2. App start applies the NEW schema (v2 key upgrade + candidates).
        import app as app_module

        cls.app_module = app_module
        cls.client = TestClient(app_module.app)
        cls.client.__enter__()
        with db_cursor() as cur:
            cur.execute(open(os.path.join(HERE, "sql", "seed_topics.sql"), encoding="utf-8").read())
            cls.tokens = {}
            for name, role in (("t_admin", "admin"), ("t_editor", "editor"), ("t_viewer", "viewer")):
                cur.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, 'x', %s) RETURNING id",
                            (name, role))
                uid = cur.fetchone()[0]
                tok, _ = auth.create_session(cur, uid)
                cls.tokens[role] = tok
        cls.fake = FakeJudge()
        cls._orig = (judge.judge, candidates._embed_query, judge.JUDGE_MODEL)
        judge.JUDGE_MODEL = "fake-judge"
        judge.judge = cls.fake
        candidates.jg.judge = cls.fake
        candidates._embed_query = lambda text: vec_for(text)
        cls.by_date = {r["date"]: r for r in cls.archive}

    @classmethod
    def tearDownClass(cls):
        judge.judge, candidates._embed_query, judge.JUDGE_MODEL = cls._orig
        cls.client.__exit__(None, None, None)

    # --- helpers ---
    def h(self, role):
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def api(self, method, path, role="admin", **kw):
        r = self.client.request(method, path, headers=self.h(role), **kw)
        return r.status_code, (r.json() if r.content else None)

    def drain(self, limit=500):
        """Run the worker loop body synchronously until the queue is empty."""
        n = 0
        while n < limit:
            cid = candidate_worker._claim()
            if cid is None:
                return n
            try:
                candidates.run_check(cid)
            except candidates.CheckFailed as exc:
                candidates.mark_failed(cid, str(exc))
            n += 1
        raise AssertionError("queue did not drain")

    def cand(self, cid):
        return self.api("GET", f"/candidates/{cid}")[1]["data"]

    def find(self, text):
        with db_cursor() as cur:
            cur.execute("SELECT id FROM candidates WHERE text = %s", (text,))
            return cur.fetchone()[0]

    # --- tests (run in name order) ---
    def test_01_schema_upgraded_and_idempotent(self):
        with db_cursor() as cur:
            cur.execute("""SELECT pg_get_expr(d.adbin, d.adrelid) LIKE '%।॥%' FROM pg_attrdef d
                           JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
                           WHERE d.adrelid='messages'::regclass AND a.attname='normalized_text'""")
            self.assertTrue(cur.fetchone()[0])
            cur.execute("SELECT count(*), count(DISTINCT normalized_text) FROM messages")
            self.assertEqual(cur.fetchone(), (270, 270))
            cur.execute("SELECT pg_relation_filenode('messages')")
            before = cur.fetchone()[0]
        self.app_module.init_schema()
        self.app_module.init_schema()
        with db_cursor() as cur:
            cur.execute("SELECT pg_relation_filenode('messages')")
            self.assertEqual(cur.fetchone()[0], before, "schema.sql rewrote messages again")
            cur.execute("SELECT count(*) FROM topics")
            self.assertEqual(cur.fetchone()[0], 25)

    def test_02_preview_rules_only(self):
        p84 = self.by_date["2024-01-12"]["message"]
        rows = [
            {"sheet_row": 84, "text": "અહો! " + p84, "topics": "પ્રાપ્તિનો વિચાર"},
            {"sheet_row": 5, "text": "આ નવું વાક્ય છે અને અને સેવા કરવી.", "topics": "સેવા"},
            {"sheet_row": 6, "text": "આ નવું વાક્ય છે અને સેવા કરવી", "topics": "ધ્યાન"},
            {"sheet_row": 7, "text": "Let Go = Peace", "topics": "સમજણ / સંપ"},
        ]
        code, body = self.api("POST", "/candidates/preview", "editor", json={"collection": "Test", "rows": rows})
        self.assertEqual(code, 200)
        out, summary = body["data"]["rows"], body["data"]["summary"]
        self.assertEqual(out[0]["archive"]["kind"], "contained")
        self.assertEqual(out[1]["doubled_word"], "અને")
        self.assertEqual(out[2]["repeat_of_index"], 1)
        self.assertIn("Unknown topic: ધ્યાન", out[2]["errors"])
        self.assertEqual(out[3]["topics"], ["સમજણ", "સંપ"])
        self.assertEqual((summary["already_given"], summary["repeats"], summary["errors"]), (1, 1, 1))
        code, _ = self.api("POST", "/candidates/preview", "viewer", json={"collection": "T", "rows": rows})
        self.assertEqual(code, 403)

    def test_03_import_and_check(self):
        p84 = self.by_date["2024-01-12"]["message"]
        same_target = self.by_date["2020-01-14"]  # spec: row 139 ↔ 14 Jan 2020
        self.__class__.t_given = "અહો! " + p84
        self.__class__.t_same = "ભગવાનને અર્પણ કરીએ તે મોક્ષમાં પરિવર્તિત થાય"
        self.__class__.t_new = "Let Go = Peace"
        self.__class__.t_fail = "આ વાક્ય ચકાસણીમાં નિષ્ફળ જશે એવું ધારીએ"
        self.fake.script[self.t_same] = {int(same_target["id"]): "same_message"}
        self.fake.topics[self.t_same] = ["સમર્પણ"]
        self.fake.topics[self.t_new] = ["સમજણ"]
        self.fake.fail.add(self.t_fail)
        # Make sure the target is on the shortlist: give it the candidate's vector.
        with db_cursor() as cur:
            cur.execute("UPDATE messages SET embedding = %s::vector WHERE id = %s",
                        (pgvec(vec_for(self.t_same)), int(same_target["id"])))
        rows = [
            {"sheet_row": 84, "text": self.t_given, "topics": "પ્રાપ્તિનો વિચાર"},
            {"sheet_row": 139, "text": self.t_same, "topics": "પ્રાપ્તિનો વિચાર / સમર્પણ"},
            {"sheet_row": 99, "text": self.t_new, "topics": "સમજણ"},
            {"sheet_row": 150, "text": self.t_fail, "topics": ""},
            {"sheet_row": 151, "text": "ભૂલ", "topics": "ધ્યાન"},
        ]
        code, body = self.api("POST", "/candidates/import", "editor", json={"collection": "Unused", "rows": rows})
        self.assertEqual((code, body["data"]["added"], len(body["data"]["errors"])), (200, 4, 1))
        calls_before = len(self.fake.calls)
        self.assertEqual(self.drain(), 4)

        g = self.cand(self.find(self.t_given))
        self.assertEqual((g["verdict"], g["status"]), ("given", "rejected"))
        self.assertEqual(g["status_note"], "Given on 2024-01-12")
        self.assertIn("12 Jan 2024", g["verdict_reason"])
        self.assertEqual(g["matches"][0]["word_kind"], "contained")
        self.assertNotIn(self.t_given, [c[0] for c in self.fake.calls[calls_before:]], "given must not call the AI")

        s = self.cand(self.find(self.t_same))
        self.assertEqual(s["verdict"], "same")
        self.assertEqual(s["matches"][0]["judge"], "same_message")
        self.assertEqual(s["matches"][0]["date"], "2020-01-14")
        self.assertNotIn("semantic_sim", s["matches"][0])
        self.assertEqual(s["topics"], ["પ્રાપ્તિનો વિચાર", "સમર્પણ"])  # sheet kept, AI differs
        self.assertFalse(s["topics_confirmed"])

        n = self.cand(self.find(self.t_new))
        self.assertEqual((n["verdict"], n["topics_confirmed"]), ("new", True))

        f = self.cand(self.find(self.t_fail))
        self.assertEqual((f["check_state"], f["verdict"]), ("failed", None))
        self.assertIn("Meaning check failed", f["check_error"])

    def test_04_list_counts_and_filters(self):
        code, body = self.api("GET", "/candidates", "viewer")
        d = body["data"]
        self.assertEqual(d["counts"]["given"], 1)
        self.assertEqual(d["counts"]["same"], 1)
        self.assertEqual(d["counts"]["new"], 1)
        self.assertEqual(d["counts"]["unchecked"], 1)  # the failed one
        self.assertEqual([e["sheet_row"] for e in d["entries"]], [84, 99, 139, 150])
        code, body = self.api("GET", "/candidates?topics_unconfirmed=true", "viewer")
        self.assertEqual([e["sheet_row"] for e in body["data"]["entries"]], [139])
        code, body = self.api("GET", "/candidates?verdict=same", "viewer")
        self.assertEqual(body["data"]["total"], 1)

    def test_05_status_roles(self):
        cid = self.find(self.t_new)
        self.assertEqual(self.api("POST", f"/candidates/{cid}/status", "viewer", json={"status": "shortlisted"})[0], 403)
        self.assertEqual(self.api("POST", f"/candidates/{cid}/status", "editor", json={"status": "shortlisted"})[1]["status"], "success")
        self.assertEqual(self.api("POST", f"/candidates/{cid}/status", "editor", json={"status": "approved"})[1]["status"], "error")
        self.assertEqual(self.api("POST", f"/candidates/{cid}/status", "admin", json={"status": "rejected"})[1]["status"], "error")
        _, body = self.api("POST", f"/candidates/{cid}/status", "admin", json={"status": "approved"})
        self.assertEqual(body["data"]["target_season"], 2026)

    def test_06_give_and_duplicate_link(self):
        cid = self.find(self.t_new)
        # A future day is refused: nothing is written down before the day (leak risk).
        future = f"{date.today().year + 1}-12-20"
        _, body = self.api("POST", f"/candidates/{cid}/give", "editor",
                           json={"message_date": future, "link": "https://www.baps.org/x.jpg"})
        self.assertIn("hasn't come yet", body["message"])
        code, body = self.api("POST", f"/candidates/{cid}/give", "editor",
                              json={"message_date": "2025-12-16", "link": "https://www.baps.org/x.jpg"})
        self.assertEqual(body["status"], "success", body)
        mid = int(body["data"]["message_id"])
        with db_cursor() as cur:
            cur.execute("SELECT message_text, tags, tags_reviewed, season FROM messages WHERE id = %s", (mid,))
            self.assertEqual(cur.fetchone(), (self.t_new, ["સમજણ"], True, 2025))
        self.assertEqual(self.cand(cid)["status"], "given")
        # Bad date window
        _, body = self.api("POST", f"/candidates/{self.find(self.t_same)}/give", "editor",
                           json={"message_date": "2026-11-01", "link": "https://x.org/a.jpg"})
        self.assertNotEqual(body.get("status"), "success")
        # Giving a text that already exists links to the existing row.
        sid = self.find(self.t_same)
        with db_cursor() as cur:
            cur.execute("UPDATE candidates SET status='approved' WHERE id=%s", (sid,))
            cur.execute("INSERT INTO messages (message_text, message_date) VALUES (%s, '2025-12-20') RETURNING id",
                        (self.t_same + ".",))
            existing = cur.fetchone()[0]
        _, body = self.api("POST", f"/candidates/{sid}/give", "editor",
                           json={"message_date": "2025-12-17", "link": "https://www.baps.org/y.jpg"})
        self.assertEqual(int(body["data"]["message_id"]), existing)

    def test_07_restart_and_stale(self):
        cid = self.find(self.t_fail)
        self.fake.fail.discard(self.t_fail)
        self.assertEqual(self.api("POST", "/candidates/recheck", "editor", json={"ids": [cid]})[1]["data"]["queued"], 1)
        self.assertEqual(candidate_worker._claim(), cid)  # now running…
        self.assertEqual(candidate_worker.reset_running(), 1)  # …restart
        self.assertEqual(self.cand(cid)["check_state"], "queued")
        # Edit while running → the old result is discarded, row re-queued.
        self.assertEqual(candidate_worker._claim(), cid)
        self.api("PATCH", f"/candidates/{cid}", "editor", json={"text": "આ વાક્ય હવે બદલાઈ ગયું છે"})
        self.assertEqual(self.cand(cid)["check_state"], "queued")  # the edit re-queued it
        # The worker now finishes on the OLD text: its result must be discarded.
        self.assertEqual(candidates.save_result(cid, self.t_fail, "new", "x", [], [], [], False, None, None), "stale")
        self.drain()
        c = self.cand(cid)
        self.assertEqual((c["check_state"], c["text"]), ("done", "આ વાક્ય હવે બદલાઈ ગયું છે"))

    def test_08_topic_rename_and_audit(self):
        tid = next(t["id"] for t in self.api("GET", "/topics", "viewer")[1]["data"]["topics"] if t["name"] == "સમજણ")
        self.assertEqual(self.api("PATCH", f"/topics/{tid}", "editor", json={"name": "સમજ"})[0], 403)
        self.assertEqual(self.api("PATCH", f"/topics/{tid}", "admin", json={"name": "સમજણ (નવું)"})[1]["status"], "success")
        with db_cursor() as cur:
            cur.execute("SELECT count(*) FROM messages WHERE 'સમજણ (નવું)' = ANY(tags)")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM candidates WHERE 'સમજણ' = ANY(topics || sheet_topics || ai_topics)")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT DISTINCT action FROM audit_log WHERE action LIKE 'candidate.%' OR action LIKE 'topic.%'")
            actions = {r[0] for r in cur.fetchall()}
        self.assertTrue({"candidate.import", "candidate.status", "candidate.give", "candidate.edit",
                         "candidate.recheck", "topic.update"} <= actions, actions)

    def test_09_stats_and_archive_hook(self):
        _, body = self.api("GET", "/stats", "viewer")
        cc = body["data"]["candidate_checks"]
        self.assertEqual((cc["queued"], cc["running"]), (0, 0))
        # Adding an archive path identical to an open candidate re-queues it.
        with db_cursor() as cur:
            cur.execute("INSERT INTO candidates (text, match_key) VALUES ('સાવ નવું ઉમેદવાર વાક્ય અહીં', 'x') RETURNING id")
            cid = cur.fetchone()[0]
        self.drain()
        self.api("POST", "/add", "editor", json={"text": "સાવ નવું ઉમેદવાર વાક્ય અહીં.", "message_date": "2026-12-20"})
        self.assertEqual(self.cand(cid)["check_state"], "queued")
        self.drain()
        self.assertEqual(self.cand(cid)["verdict"], "given")

    def test_10_delete_admin_only(self):
        cid = self.find(self.t_given)
        self.assertEqual(self.api("DELETE", f"/candidates/{cid}", "editor")[0], 403)
        self.assertEqual(self.api("DELETE", f"/candidates/{cid}", "admin")[1]["status"], "success")

    def test_10b_role_rules(self):
        with db_cursor() as cur:
            cur.execute("INSERT INTO candidates (text, match_key, status) VALUES ('ભૂમિકા પરીક્ષણ માટેનું વાક્ય', 'r', 'rejected') RETURNING id")
            cid = cur.fetchone()[0]
        # An editor cannot reopen what an admin rejected.
        self.assertEqual(self.api("POST", f"/candidates/{cid}/status", "editor", json={"status": "shortlisted"})[1]["status"], "error")
        # Approving needs a finished check.
        self.assertIn("Wait for the check", self.api("POST", f"/candidates/{cid}/status", "admin", json={"status": "approved"})[1]["message"])

    def test_12_no_credentials_real_path(self):
        """Spec: with model credentials removed, rows end failed, the list says
        Check failed, and only a word-rule given gets a result. Uses the REAL
        embedding + judge code (no fakes), with no Google credentials."""
        import time

        fake_judge, fake_embed, model = judge.judge, candidates._embed_query, judge.JUDGE_MODEL
        judge.judge = candidates.jg.judge = self._orig[0]
        candidates._embed_query = self._orig[1]
        try:
            given_text = "અહો! " + self.by_date["2023-01-02"]["message"]
            with db_cursor() as cur:
                cur.execute("INSERT INTO candidates (text, match_key) VALUES ('ક્રેડેન્શિયલ વગરનું પરીક્ષણ વાક્ય', 'z') RETURNING id")
                a = cur.fetchone()[0]
                cur.execute("INSERT INTO candidates (text, match_key) VALUES (%s, 'g') RETURNING id", (given_text,))
                g = cur.fetchone()[0]
            for model_name, expect in (("", "GEMINI_JUDGE_MODEL"), ("some-model", "Meaning search is not configured")):
                judge.JUDGE_MODEL = model_name
                candidates.queue([a])
                t0 = time.monotonic()
                self.drain()
                self.assertLess(time.monotonic() - t0, 3, "must fail fast, not wait on retries")
                c = self.cand(a)
                self.assertEqual((c["check_state"], c["verdict"]), ("failed", None))
                self.assertIn(expect, c["check_error"])
            self.assertEqual(self.cand(g)["verdict"], "given")  # words need no AI
            listed = {e["id"]: e for e in self.api("GET", "/candidates?limit=200", "viewer")[1]["data"]["entries"]}
            self.assertEqual(listed[str(a)]["check_state"], "failed")
        finally:
            judge.judge = candidates.jg.judge = fake_judge
            candidates._embed_query, judge.JUDGE_MODEL = fake_embed, model

    def test_13_season_plan(self):
        with db_cursor() as cur:
            ids = []
            for i, t in enumerate(["યોજના માટેનું પહેલું વાક્ય", "યોજના માટેનું બીજું વાક્ય", "યોજના માટેનું ત્રીજું વાક્ય"]):
                cur.execute("""INSERT INTO candidates (text, match_key, status, verdict, check_state, target_season, sheet_row)
                               VALUES (%s, %s, 'approved', 'new', 'done', 2026, %s) RETURNING id""", (t, f"p{i}", 900 + i))
                ids.append(cur.fetchone()[0])
        a, b, c = ids
        plan = lambda cid, d, role="editor", **kw: self.api("POST", f"/candidates/{cid}/plan", role, json={"date": d, **kw})[1]
        self.assertEqual(plan(a, "2026-12-26", role="viewer"), {"detail": "Insufficient permissions"})
        r0 = plan(a, "2026-12-26"); self.assertEqual(r0["status"], "success", r0)
        # one path per day: a second one is refused, unless replace
        r = plan(b, "2026-12-26")
        self.assertEqual((r["status"], r["conflict"]), ("error", str(a)))
        self.assertEqual(plan(b, "2026-12-26", replace=True)["status"], "success")
        # wrong season / outside the window / a day already given in the archive
        self.assertIn("approved for 2026", plan(c, "2025-12-20")["message"])
        self.assertNotEqual(self.api("POST", f"/candidates/{c}/plan", "editor", json={"date": "2026-11-30"})[0], 200)
        with db_cursor() as cur:
            cur.execute("INSERT INTO messages (message_text, message_date) VALUES ('પહેલેથી અપાયેલો પાઠ યોજનામાં', '2026-12-28')")
        self.assertIn("already given", plan(c, "2026-12-28")["message"])
        self.assertEqual(plan(c, "2026-12-27")["status"], "success")
        d = self.api("GET", "/plan?season=2026", "viewer")[1]["data"]
        days = {x["date"]: x for x in d["days"]}
        self.assertEqual((len(d["days"]), d["days"][0]["date"], d["days"][-1]["date"]), (30, "2026-12-16", "2027-01-14"))
        self.assertEqual(days["2026-12-26"]["candidate"]["id"], str(b))
        self.assertEqual(days["2026-12-27"]["candidate"]["id"], str(c))
        self.assertIsNotNone(days["2026-12-28"]["given"])
        self.assertIn(str(a), [w["id"] for w in d["waiting"]])  # replaced → back to waiting
        self.assertGreaterEqual(d["filled"], 3)
        # leaving "approved" frees the day; unplanning works
        self.api("POST", f"/candidates/{b}/status", "admin", json={"status": "rejected", "note": "test"})
        self.assertIsNone({x["date"]: x for x in self.api("GET", "/plan?season=2026", "viewer")[1]["data"]["days"]}["2026-12-26"]["candidate"])
        self.assertEqual(plan(c, None)["status"], "success")
        self.assertEqual(self.cand(c)["plan_date"], None)
        # rejected can't be planned
        self.assertIn("Only an approved", plan(b, "2026-12-29")["message"])

    def test_10c_check_page_one_path(self):
        """Check page = the same check as the pool; Add reuses it (no 2nd AI call)."""
        text = "સ્વભાવ ટાળવા સંતનો સંગ કરવો એ જ ઉપાય છે ખરો"
        self.fake.topics[text] = ["સમજણ"]
        before = len(self.fake.calls)
        code, body = self.api("POST", "/candidates/check", "viewer", json={"text": text})
        self.assertEqual(code, 200)
        d = body["data"]
        self.assertIsNone(d["in_pool"])
        self.assertEqual((d["verdict"], d["topics"]), ("new", ["સમજણ"]))
        self.assertEqual(len(self.fake.calls), before + 1)
        # Viewers may check but not add.
        self.assertEqual(self.api("POST", "/candidates/add", "viewer", json={"text": text})[0], 403)
        code, body = self.api("POST", "/candidates/add", "editor", json={"text": text})
        cid = body["data"]["id"]
        self.assertFalse(body["data"]["existed"])
        self.assertEqual(len(self.fake.calls), before + 1, "Add must reuse the Check page result")
        c = self.cand(cid)
        self.assertEqual((c["verdict"], c["check_state"], c["collection"], c["status"]),
                         ("new", "done", "Added from Check", "pending"))
        self.assertEqual(self.drain(), 0)
        # Now in the pool: Check points at the row, Add doesn't duplicate it —
        # also with different punctuation and spacing.
        again = text.replace(" ", "  ") + "."
        self.assertEqual(self.api("POST", "/candidates/check", "viewer", json={"text": again})[1]["data"]["in_pool"]["id"], cid)
        self.assertEqual(self.api("POST", "/candidates/add", "editor", json={"text": again})[1]["data"],
                         {"id": cid, "existed": True})
        self.assertEqual(len(self.fake.calls), before + 1)

    def test_10d_check_page_given_and_unchecked_add(self):
        p = self.by_date["2024-01-12"]["message"]
        given = "ખરેખર! " + p
        body = self.api("POST", "/candidates/check", "viewer", json={"text": given})[1]["data"]
        self.assertEqual(body["verdict"], "given")
        self.assertEqual(body["matches"][0]["date"], "2024-01-12")
        # Added without a fresh Check result → queued for the worker.
        fresh = "વિશ્વાસ રાખી સેવા કરવી એ સાચી ભક્તિ ગણાય"
        cid = self.api("POST", "/candidates/add", "editor", json={"text": fresh})[1]["data"]["id"]
        self.assertEqual(self.cand(cid)["check_state"], "queued")
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.cand(cid)["check_state"], "done")

    def test_10e_undo_given(self):
        """Undo Mark as given: back to approved; the archive row goes only if the give created it."""
        def approved(text):
            with db_cursor() as cur:
                cur.execute("""INSERT INTO candidates (text, match_key, status, verdict, check_state)
                               VALUES (%s, %s, 'approved', 'new', 'done') RETURNING id""", (text, text))
                return cur.fetchone()[0]
        give = lambda cid, d: self.api("POST", f"/candidates/{cid}/give", "editor",
                                       json={"message_date": d, "link": "https://www.baps.org/u.jpg"})[1]
        a = approved("ભૂલથી અપાયેલો પાઠ પાછો લેવાનો છે")
        mid = int(give(a, "2025-12-21")["data"]["message_id"])
        # Editors can't undo; admins can.
        self.assertEqual(self.api("POST", f"/candidates/{a}/ungive", "editor")[0], 403)
        body = self.api("POST", f"/candidates/{a}/ungive", "admin")[1]
        self.assertEqual(body["data"], {"archive_removed": True})
        c = self.cand(a)
        self.assertEqual((c["status"], c["message_id"]), ("approved", None))
        with db_cursor() as cur:
            cur.execute("SELECT count(*) FROM messages WHERE id = %s", (mid,))
            self.assertEqual(cur.fetchone()[0], 0)
        # Not given any more → a second undo is refused.
        self.assertNotEqual(self.api("POST", f"/candidates/{a}/ungive", "admin")[1]["status"], "success")
        # Give again works after the undo.
        self.assertEqual(give(a, "2025-12-21")["status"], "success")

        # A give that linked to an existing archive row: undo keeps that row.
        text = "આ પાઠ પહેલેથી આર્કાઇવમાં હતો જ"
        with db_cursor() as cur:
            cur.execute("INSERT INTO messages (message_text, message_date) VALUES (%s, '2024-12-22') RETURNING id",
                        (text + ".",))
            existing = cur.fetchone()[0]
        b = approved(text)
        self.assertEqual(int(give(b, "2025-12-22")["data"]["message_id"]), existing)
        self.assertEqual(self.api("POST", f"/candidates/{b}/ungive", "admin")[1]["data"], {"archive_removed": False})
        with db_cursor() as cur:
            cur.execute("SELECT count(*) FROM messages WHERE id = %s", (existing,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_10f_undo_approve_and_reject(self):
        with db_cursor() as cur:
            cur.execute("""INSERT INTO candidates (text, match_key, status, verdict, check_state)
                           VALUES ('પાછું વાળવાનું ઉદાહરણ વાક્ય', 'z', 'approved', 'new', 'done') RETURNING id""")
            cid = cur.fetchone()[0]
        st = lambda role, status, note=None: self.api("POST", f"/candidates/{cid}/status", role,
                                                      json={"status": status, "note": note})[1]
        self.assertNotEqual(st("editor", "shortlisted")["status"], "success")  # only an admin undoes
        self.assertEqual(st("admin", "shortlisted", "Approval undone")["status"], "success")
        self.assertEqual(st("admin", "rejected", "duplicate idea")["status"], "success")
        self.assertEqual(st("admin", "pending", "Rejection undone")["status"], "success")
        self.assertEqual(self.cand(cid)["status"], "pending")

    def test_11_background_thread(self):
        import time

        with db_cursor() as cur:
            cur.execute("INSERT INTO candidates (text, match_key) VALUES ('થ્રેડ માટેનું પરીક્ષણ વાક્ય', 'y') RETURNING id")
            cid = cur.fetchone()[0]
        candidate_worker.PACE_SECONDS = 0.05
        os.environ["CANDIDATE_WORKER"] = "on"
        candidate_worker.start()
        candidate_worker.wake()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.cand(cid)["check_state"] != "done":
            time.sleep(0.2)
        self.assertEqual(self.cand(cid)["check_state"], "done")


if __name__ == "__main__":
    unittest.main()
