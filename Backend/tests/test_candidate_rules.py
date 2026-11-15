"""candidate_rules.py — the rules-only part of the Candidates check.

Run from Backend/:  python3 -m unittest discover -s tests -t .
Archive texts are real paths (ids in comments); candidate texts are made up
for the rule being tested, except where the spec names a real pair.
"""

import unittest

import candidate_rules as cr
from candidate_rules import WordResult

P568 = "આપણને અક્ષર અને પુરુષોત્તમ બંને અહીં જ મળ્યા છે."  # 12 Jan 2024
P361 = "સંપ રાખે તે બુદ્ધિમાન છે. નહિ તો બુદ્ધુમાન."  # 16 Dec 2016

TOPICS = ["આત્મનિષ્ઠા", "ગુણગ્રહણ / અભાવ-અવગુણ", "સમજણ", "સેવા", "સંપ"]


class MatchKey(unittest.TestCase):
    def test_spaces_and_punctuation_removed(self):
        self.assertEqual(cr.match_key("સત્સંગદીક્ષા શાસ્ત્ર"), cr.match_key("સત્સંગ દીક્ષા શાસ્ત્ર"))
        self.assertEqual(cr.match_key("અહો! આપણને."), cr.match_key("અહો આપણને"))

    def test_marks_kept(self):
        # A vowel sign changes the word, so it must change the key.
        self.assertNotEqual(cr.match_key("સંપ"), cr.match_key("સપ"))
        self.assertNotEqual(cr.match_key("नींव"), cr.match_key("नव"))

    def test_dandas_and_latin_case(self):
        self.assertEqual(cr.match_key("है।"), cr.match_key("है"))
        self.assertEqual(cr.match_key("Let Go = Peace"), "letgopeace")

    def test_nfc(self):
        # क़ typed as one code point (U+0958) or as क + nukta: same key.
        self.assertEqual(cr.match_key("क़"), cr.match_key("क़"))


class WordCompare(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(cr.word_compare(P361, P361).kind, "exact")

    def test_exact_across_spacing(self):
        self.assertEqual(cr.word_compare("સંપ રાખે તે બુદ્ધિમાનછે નહિ તો બુદ્ધુમાન", P361).kind, "exact")

    def test_contained_with_prefix(self):
        # Spec: sheet row 84 = the 12 Jan 2024 path with "અહો!" added.
        r = cr.word_compare("અહો! " + P568, P568)
        self.assertEqual(r.kind, "contained")

    def test_contained_needs_three_words(self):
        self.assertNotEqual(cr.word_compare("સંપ રાખે", P361).kind, "contained")

    def test_no_short_relabel(self):
        # /check would call this "contains"; candidates must not.
        self.assertNotIn(cr.word_compare("બુદ્ધિમાન છે", P361).kind, ("contains", "contained"))

    def test_unrelated(self):
        self.assertEqual(cr.word_compare("Let Go = Peace", P361), WordResult("none", 0, 0))

    def test_overlap_numbers(self):
        r = cr.word_compare("અહો! " + P568, P568)
        self.assertEqual((r.overlap_pct, r.shared_phrase_words), (90, 9))


class Decide(unittest.TestCase):
    def test_word_given_needs_no_judge(self):
        self.assertEqual(cr.decide(["contained"], None), "given")

    def test_no_silent_fallback(self):
        self.assertIsNone(cr.decide(["none"], None))
        self.assertIsNone(cr.decide(["strong"], None))

    def test_judge_raises(self):
        self.assertEqual(cr.decide(["none"], ["same_sentence"]), "given")
        self.assertEqual(cr.decide(["weak"], ["same_message"]), "same")
        self.assertEqual(cr.decide(["none"], ["similar"]), "similar")

    def test_judge_cannot_lower(self):
        self.assertEqual(cr.decide(["exact"], ["different"]), "given")
        self.assertEqual(cr.decide(["strong"], ["different"]), "similar")

    def test_new(self):
        self.assertEqual(cr.decide(["weak", "none"], ["different", "different"]), "new")
        self.assertEqual(cr.decide([], []), "new")


class Shortlist(unittest.TestCase):
    def test_word_hits_first_then_nearest_capped(self):
        words = {1: WordResult("none", 0, 0), 2: WordResult("strong", 80, 5), 3: WordResult("weak", 30, 3)}
        nearest = list(range(10, 30))
        out = cr.shortlist(words, nearest)
        self.assertEqual(out[:2], [2, 3])
        self.assertEqual(out[2:], nearest[: cr.NEAREST_BY_MEANING])  # only the N nearest
        self.assertLessEqual(len(out), cr.SHORTLIST_CAP)

    def test_cap(self):
        words = {i: WordResult("some", 50, 4) for i in range(20)}
        self.assertEqual(len(cr.shortlist(words, [100, 101])), cr.SHORTLIST_CAP)

    def test_no_duplicates(self):
        self.assertEqual(cr.shortlist({5: WordResult("some", 50, 4)}, [5, 6]), [5, 6])


class RankMatches(unittest.TestCase):
    def test_order_and_drop(self):
        rows = [
            {"message_id": 1, "word_kind": "none", "overlap_pct": 0, "judge": "different"},  # dropped
            {"message_id": 2, "word_kind": "weak", "overlap_pct": 40, "judge": "similar"},
            {"message_id": 3, "word_kind": "none", "overlap_pct": 10, "judge": "same_message"},
            {"message_id": 4, "word_kind": "contained", "overlap_pct": 90, "judge": "same_sentence"},
            {"message_id": 5, "word_kind": "some", "overlap_pct": 60, "judge": "different"},
        ]
        self.assertEqual([r["message_id"] for r in cr.rank_matches(rows)], [4, 3, 2, 5])

    def test_keeps_five(self):
        rows = [{"message_id": i, "word_kind": "some", "overlap_pct": i, "judge": None} for i in range(9)]
        self.assertEqual(len(cr.rank_matches(rows)), 5)


class Repeats(unittest.TestCase):
    def test_points_at_lower_row(self):
        rows = [(39, "તેનાથી અંદર અક્ષરધામ રચાય."), (5, "સત્સંગ શિક્ષણ પરીક્ષા એ ગુરુપૂજન છે. તેનાથી અંદર અક્ષરધામ રચાય."),
                (7, "Let Go = Peace")]
        self.assertEqual(cr.find_repeats(rows), {39: 5})

    def test_sheet_pairs(self):
        """The four pairs flagged on the real sheet (Nov 2026): two are real
        repeats, two only share a run of common words."""
        rows = [
            (5, "ભગવાન ને સાધુના મહિમાની વાતુ નિરંતર કરવી ને સાંભળવી."),
            (37, "મહિમાની વાતું નિરંતર કરવી ને સાંભળવી."),
            (39, "પ્રાપ્તિના કેફથી સત્સંગના આનંદને માણવો."),
            (85, "તેમની પ્રાપ્તિના કેફથી સત્સંગના આનંદને સદાય માણવો."),
            (67, "અક્ષરાધિપતિ સ્વામિનારાયણ ભગવાન સાક્ષાત્ પરમાત્મા પરબ્રહ્મ પુરુષોત્તમ હરિ છે. એ એક જ આપણા સદા પરમ ઉપાસ્ય ઇષ્ટદેવ છે. તેમની જ અનન્ય ભાવે સદા ભક્તિ કરવી."),
            (69, "પ્રગટ અક્ષરબ્રહ્મ એ એક જ આપણા ગુરુ છે."),
            (84, "અહો! આપણને અક્ષર અને પુરુષોત્તમ બંને અહીં જ મળ્યા છે."),
            (88, "અક્ષર અને પુરુષોત્તમ બંને માયાથી પર છે, દિવ્ય છે."),
        ]
        self.assertEqual(cr.find_repeats(rows), {37: 5, 85: 39})

    def test_chain(self):
        t = "આપણે સૌ સાથે મળીને સેવા કરીએ"
        self.assertEqual(cr.find_repeats([(3, t), (1, t), (2, t + ".")]), {2: 1, 3: 1})


class ImportHelpers(unittest.TestCase):
    def test_doubled_word(self):
        self.assertEqual(cr.doubled_words("સંપ અને અને સુહૃદભાવ"), ["અને"])
        self.assertEqual(cr.doubled_words(P361), [])

    def test_topic_with_slash_in_name(self):
        self.assertEqual(cr.parse_sheet_topics("ગુણગ્રહણ / અભાવ-અવગુણ", TOPICS), (["ગુણગ્રહણ / અભાવ-અવગુણ"], []))

    def test_two_topics(self):
        self.assertEqual(cr.parse_sheet_topics("સમજણ / સેવા", TOPICS), (["સમજણ", "સેવા"], []))
        self.assertEqual(cr.parse_sheet_topics("ગુણગ્રહણ / અભાવ-અવગુણ / સંપ", TOPICS),
                         (["ગુણગ્રહણ / અભાવ-અવગુણ", "સંપ"], []))

    def test_unknown_and_too_many(self):
        topics, errors = cr.parse_sheet_topics("સમજણ / ધ્યાન", TOPICS)
        self.assertEqual(topics, ["સમજણ"])
        self.assertEqual(errors, ["Unknown topic: ધ્યાન"])
        self.assertIn("More than 2 topics", cr.parse_sheet_topics("સમજણ / સેવા / સંપ", TOPICS)[1])

    def test_blank(self):
        self.assertEqual(cr.parse_sheet_topics("  ", TOPICS), ([], []))

    def test_lang(self):
        self.assertEqual(cr.detect_lang(P361), "gu")
        self.assertEqual(cr.detect_lang("कथावार्ता सत्संग की नींव है।"), "hi")
        self.assertEqual(cr.detect_lang("Let Go = Peace"), "en")
        self.assertEqual(cr.detect_lang("સંપ = Peace"), "mixed")

    def test_given_reason(self):
        self.assertEqual(cr.given_reason("exact", "12 Jan 2024"), "This sentence was given on 12 Jan 2024.")


if __name__ == "__main__":
    unittest.main()


class ResolveTopics(unittest.TestCase):
    def test_agree(self):
        self.assertEqual(cr.resolve_topics(["સેવા", "સંપ"], ["સંપ", "સેવા"], ["સેવા", "સંપ"], False), (["સેવા", "સંપ"], True))

    def test_no_sheet(self):
        self.assertEqual(cr.resolve_topics([], ["સંપ"], [], False), (["સંપ"], False))

    def test_differ_keeps_sheet(self):
        self.assertEqual(cr.resolve_topics(["માન-અપમાન"], ["સમજણ"], ["માન-અપમાન"], False), (["માન-અપમાન"], False))

    def test_person_choice_kept(self):
        self.assertEqual(cr.resolve_topics(["માન-અપમાન"], ["સમજણ"], ["સમજણ"], True), (["સમજણ"], True))


class Speed(unittest.TestCase):
    def test_preview_scale(self):
        import time
        paths = [cr.prepare(f"સત્સંગ સેવા ભક્તિ મહિમા વાક્ય નંબર {i} અહીં લખેલું છે") for i in range(270)]
        cands = [cr.prepare(f"ઉમેદવાર વાક્ય {i} સત્સંગ અને સેવા વિશે છે") for i in range(163)]
        t = time.perf_counter()
        for c in cands:
            for p in paths:
                cr.compare(c, p)
        self.assertLess(time.perf_counter() - t, 5.0)
