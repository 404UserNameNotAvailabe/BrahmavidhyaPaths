"""judge.py — prompt, reply validation and retry, with a fake model.

No network: every test injects `generate`. Run from Backend/:
    python3 -m unittest discover -s tests -t .
"""

import json
import unittest

import judge as j

SHORT = [j.PathRow(480, "2020-01-14", "ભગવાનને જે અર્પણ કરીએ તે મોક્ષરૂપ થાય."),
         j.PathRow(568, "2024-01-12", "આપણને અક્ષર અને પુરુષોત્તમ બંને અહીં જ મળ્યા છે.")]
TOPICS = ["સમર્પણ", "પ્રાપ્તિનો વિચાર", "સંપ"]


def reply(**over):
    data = {
        "matches": [
            {"message_id": "480", "label": "same_message", "reason": "Same message: what is offered to God turns into moksha."},
            {"message_id": "568", "label": "different", "reason": "Different subject: the attainment of Akshar and Purushottam."},
        ],
        "topics": ["સમર્પણ"],
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


class Validate(unittest.TestCase):
    def test_good(self):
        v = j.validate(reply(), SHORT, TOPICS)
        self.assertEqual(v.labels[480][0], "same_message")
        self.assertEqual(v.topics, ["સમર્પણ"])

    def bad(self, raw):
        with self.assertRaises(j.JudgeError):
            j.validate(raw, SHORT, TOPICS)

    def test_not_json(self):
        self.bad("Sure! Here is the answer")

    def test_id_not_in_shortlist(self):
        m = json.loads(reply())["matches"]; m[0]["message_id"] = "999"
        self.bad(reply(matches=m))

    def test_missing_path(self):
        self.bad(reply(matches=json.loads(reply())["matches"][:1]))

    def test_duplicate_id(self):
        m = json.loads(reply())["matches"]; m[1]["message_id"] = "480"
        self.bad(reply(matches=m))

    def test_unknown_label(self):
        m = json.loads(reply())["matches"]; m[0]["label"] = "duplicate"
        self.bad(reply(matches=m))

    def test_long_reason(self):
        m = json.loads(reply())["matches"]; m[0]["reason"] = "word " * 26
        self.bad(reply(matches=m))

    def test_topic_not_in_list(self):
        self.bad(reply(topics=["ધ્યાન"]))

    def test_topic_count(self):
        self.bad(reply(topics=[]))
        self.bad(reply(topics=["સમર્પણ", "સંપ", "પ્રાપ્તિનો વિચાર"]))


class Judge(unittest.TestCase):
    def test_retry_once_then_succeeds(self):
        calls = iter(["not json", reply()])
        v = j.judge("ભગવાનને અર્પણ કરીએ તે મોક્ષમાં પરિવર્તિત થાય", SHORT, TOPICS, generate=lambda p: next(calls))
        self.assertEqual(v.labels[480][0], "same_message")

    def test_fails_after_two(self):
        with self.assertRaises(j.JudgeError):
            j.judge("x", SHORT, TOPICS, generate=lambda p: "nope")

    def test_network_error_is_judge_error(self):
        def boom(p):
            raise ConnectionError("down")
        with self.assertRaises(j.JudgeError) as ctx:
            j.judge("x", SHORT, TOPICS, generate=boom)
        self.assertEqual(str(ctx.exception), "ConnectionError")

    def test_prompt_contains_only_texts(self):
        p = j.build_prompt("CAND", SHORT, TOPICS)
        self.assertIn("id 480 (2020-01-14)", p)
        self.assertIn("સમર્પણ", p)
        self.assertTrue(p.startswith("Candidate:\nCAND"))


if __name__ == "__main__":
    unittest.main()
