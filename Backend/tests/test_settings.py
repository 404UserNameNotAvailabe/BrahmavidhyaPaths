"""Unit tests for the superadmin settings (no DB, no network)."""

import json
import os
import stat
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("DB_HOST", "127.0.0.1")

try:
    import gcp_settings as g
except Exception:  # pragma: no cover - deps missing (psycopg)
    g = None

KEY = {
    "type": "service_account",
    "project_id": "p-1",
    "private_key_id": "abcdef1234567890",
    "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    "client_email": "sa@p-1.iam.gserviceaccount.com",
}
GOOD = {"project": "p-1", "location": "asia-south1", "embed_model": "gemini-embedding-001",
        "judge_model": "gemini-3.1-pro-preview", "judge_location": "global"}


@unittest.skipIf(g is None, "backend deps not installed")
class KeyTests(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(g.parse_key(json.dumps(KEY))["client_email"], KEY["client_email"])

    def test_parse_rejects(self):
        for raw in ("not json", json.dumps({"type": "user"}), json.dumps({**KEY, "private_key": ""}),
                    "x" * (g.MAX_KEY_BYTES + 1)):
            with self.assertRaises(g.SettingsError):
                g.parse_key(raw)

    def test_summary_never_has_private_key(self):
        self.assertNotIn("private_key", json.dumps(g._summary(KEY)))
        self.assertEqual(g._summary(KEY)["key_id"], "abcdef12")

    def test_write_key_atomic_600(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gcp-sa.json")
            with mock.patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": path}):
                g._write_key(json.dumps(KEY))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                info = g.key_info()
            self.assertEqual(info["client_email"], KEY["client_email"])
            self.assertNotIn("private_key", info)
            self.assertEqual(os.listdir(d), ["gcp-sa.json"])  # no temp file left


@unittest.skipIf(g is None, "backend deps not installed")
class CleanTests(unittest.TestCase):
    def test_ok_and_trim(self):
        self.assertEqual(g.clean({**GOOD, "project": " p-1 "})["project"], "p-1")

    def test_required_and_spaces(self):
        for bad in ({**GOOD, "project": ""}, {**GOOD, "judge_model": "a b"}):
            with self.assertRaises(g.SettingsError):
                g.clean(bad)

    def test_judge_location_optional(self):
        self.assertEqual(g.clean({**GOOD, "judge_location": ""})["judge_location"], "")


class _Err(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code


@unittest.skipIf(g is None, "backend deps not installed")
class TestCallTests(unittest.TestCase):
    def _client(self, dims=768, judge_exc=None):
        emb = types.SimpleNamespace(embeddings=[types.SimpleNamespace(values=[0.0] * dims)])

        def gen(**kw):
            if judge_exc:
                raise judge_exc
            return types.SimpleNamespace(text='{"ok": true}')

        models = types.SimpleNamespace(embed_content=lambda **kw: emb, generate_content=gen)
        return types.SimpleNamespace(models=models)

    def test_pass(self):
        with mock.patch.object(g, "make_client", lambda *a, **k: self._client()):
            r = g.test(GOOD)
        self.assertTrue(r["ok"])
        self.assertEqual(r["embed"]["dims"], 768)

    def test_wrong_dims_fails(self):
        with mock.patch.object(g, "make_client", lambda *a, **k: self._client(dims=3072)):
            self.assertFalse(g.test(GOOD)["embed"]["ok"])

    def test_errors_are_plain_words(self):
        cases = {403: "Permission denied", 404: "Model not found", 429: "Quota", 401: "rejected the key"}
        for code, words in cases.items():
            with mock.patch.object(g, "make_client", lambda *a, **k: self._client(judge_exc=_Err(code, "secret detail"))), \
                 self.assertLogs("brahmavidya.gcp_settings", "WARNING"):
                r = g.test(GOOD)
            self.assertFalse(r["ok"])
            self.assertIn(words, r["judge"]["error"])
            self.assertNotIn("secret detail", r["judge"]["error"])


try:
    import admin_api as a
except Exception:  # pragma: no cover
    a = None


@unittest.skipIf(a is None, "backend deps not installed")
class MenuTests(unittest.TestCase):
    def test_defaults_follow_lowest_role(self):
        d = a.default_menu()
        self.assertEqual(d["/"], ["viewer", "editor", "admin"])
        self.assertEqual(d["/review"], ["editor", "admin"])
        self.assertEqual(d["/users"], ["admin"])

    def test_allowed_drops_roles_below_the_page(self):
        self.assertEqual(a.allowed("/users", ["viewer", "editor", "admin"]), ["admin"])
        self.assertEqual(a.allowed("/archive", ["admin", "bogus"]), ["admin"])
        self.assertEqual(a.allowed("/archive", []), [])


if __name__ == "__main__":
    unittest.main()
