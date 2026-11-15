"""matching.py — the /check word rules, including the Nov 2026 fixes.

Run from Backend/:  python3 -m unittest discover -s tests -t .
"""

import unittest

import matching as m

V = "સંપ રાખે તે બુદ્ધિમાન છે. નહિ તો બુદ્ધુમાન."  # 16 Dec 2016


def kind(query: str, row: str) -> str:
    """The per-row classification exactly as /check applies it."""
    qs = m.token_list(query)
    cov = m.coverage(m.tokenize(query), m.tokenize(row))
    k = m.check_kind(m.normalize_text(query), m.normalize_text(row), cov,
                     m.longest_shared_phrase(qs, m.token_list(row)), len(qs))
    if len(qs) <= 6 and k in ("strong", "some", "weak"):  # app.py short relabel
        k = "contains"
    return k


class Tokeniser(unittest.TestCase):
    def test_gujarati(self):
        self.assertEqual(m.token_list(V), ["સંપ", "રાખે", "તે", "બુદ્ધિમાન", "છે", "નહિ", "તો", "બુદ્ધુમાન"])

    def test_hindi_whole_words(self):
        self.assertEqual(m.token_list("सर्वोच्च आनंद प्राप्ति के विचार में है।"),
                         ["सर्वोच्च", "आनंद", "प्राप्ति", "के", "विचार", "में", "है"])

    def test_english(self):
        self.assertEqual(m.token_list("Never Worry, We Have God!"), ["never", "worry", "we", "have", "god"])

    def test_hyphen_splits(self):
        self.assertEqual(m.token_list("સેવા-ભક્તિ"), m.token_list("સેવા ભક્તિ"))

    def test_hindi_highlight(self):
        self.assertEqual(m.highlight_overlap("नींव है।", {"नींव"}), "<mark>नींव</mark> है।")


class NormaliseText(unittest.TestCase):
    def test_punctuation_ignored(self):
        self.assertEqual(m.normalize_text(V), m.normalize_text("સંપ રાખે તે બુદ્ધિમાન છે નહિ તો બુદ્ધુમાન"))

    def test_quotes_and_dashes(self):
        self.assertEqual(m.normalize_text("ગુરુ કહે તે 'હાજી, હાજી'."), "ગુરુ કહે તે હાજી હાજી")
        self.assertEqual(m.normalize_text("ગુરુ-પૂજન."), "ગુરુ પૂજન")

    def test_danda(self):
        self.assertEqual(m.normalize_text("कथावार्ता सत्संग की नींव है।"), "कथावार्ता सत्संग की नींव है")

    def test_spaces_still_count(self):
        # The stored key keeps word boundaries; candidates use match_key for that.
        self.assertNotEqual(m.normalize_text("સત્સંગદીક્ષા શાસ્ત્ર"), m.normalize_text("સત્સંગ દીક્ષા શાસ્ત્ર"))


class CheckKind(unittest.TestCase):
    def test_exact_long(self):
        self.assertEqual(kind(V, V), "exact")

    def test_exact_short_not_relabelled(self):
        self.assertEqual(kind("રાજીપો એ જ મોક્ષ.", "રાજીપો એ જ મોક્ષ."), "exact")

    def test_exact_ignores_punctuation(self):
        self.assertEqual(kind("સંપ રાખે તે બુદ્ધિમાન છે નહિ તો બુદ્ધુમાન", V), "exact")

    def test_exact_hindi(self):
        t = "कथावार्ता सत्संग की नींव है।"
        self.assertEqual(kind(t, t), "exact")

    def test_contains(self):
        self.assertEqual(kind("રાજીપો", "રાજીપો એ જ મોક્ષ."), "contains")

    def test_punctuation_only_query_matches_nothing(self):
        self.assertEqual(m.overlap_kind(0.0, 0, 0), "none")

    def test_overlap_kind_matches_check_kind(self):
        for cov, phrase, qlen in [(0.9, 5, 10), (0.2, 4, 10), (0.7, 3, 10), (0.1, 3, 10), (0.1, 1, 10)]:
            self.assertEqual(m.check_kind("q", "different row", cov, phrase, qlen), m.overlap_kind(cov, phrase, qlen))


if __name__ == "__main__":
    unittest.main()
