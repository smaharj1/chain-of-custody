"""semver: parse, precedence, the Claude Code range (report §14, IMP-8, IMP-21)."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import semver

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "semver_cases.json")


def cases():
    with open(DATA) as fh:
        return json.load(fh)


class Tables(unittest.TestCase):
    def test_valid_and_invalid(self):
        c = cases()
        for text in c["valid"]:
            with self.subTest("valid " + text):
                self.assertEqual(str(semver.parse(text)), text)
        for text in c["invalid"]:
            with self.subTest("invalid %r" % text):
                with self.assertRaises(ValueError):
                    semver.parse(text)
                self.assertIsNone(semver.try_parse(text))

    def test_compare(self):
        for a, b, want in cases()["compare"]:
            with self.subTest("%s vs %s" % (a, b)):
                self.assertEqual(semver.compare(a, b), want)
                self.assertEqual(semver.compare(b, a), -want)
                va, vb = semver.parse(a), semver.parse(b)
                self.assertEqual(va < vb, want < 0)
                self.assertEqual(va > vb, want > 0)
                self.assertEqual(va == vb, want == 0)
                self.assertEqual(va <= vb, want <= 0)
                self.assertEqual(va >= vb, want >= 0)

    def test_leading(self):
        for text, want in cases()["leading"]:
            with self.subTest(text):
                got = semver.parse_leading(text)
                self.assertEqual(None if got is None else str(got), want)

    def test_range_status(self):
        for version, lo, hi, want in cases()["range"]:
            with self.subTest("%r in [%s, %s]" % (version, lo, hi)):
                self.assertEqual(semver.range_status(version, lo, hi), want)


class Objects(unittest.TestCase):
    def test_sorting_the_spec_example(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                 "1.0.0-rc.1", "1.0.0"]
        shuffled = [order[i] for i in (5, 2, 7, 0, 3, 6, 1, 4)]
        self.assertEqual([str(v) for v in sorted(semver.parse(s) for s in shuffled)], order)

    def test_in_range(self):
        self.assertTrue(semver.in_range("2.1.302", "2.1.302", None))
        self.assertTrue(semver.in_range("2.1.302", None, None))
        self.assertFalse(semver.in_range("2.1.301", "2.1.302", None))
        self.assertFalse(semver.in_range("2.2.0", "2.1.302", "2.1.999"))
        self.assertTrue(semver.in_range("2.0.0-rc.1", "2.0.0-rc.1", "2.0.0"))

    def test_hashable_and_prerelease_flag(self):
        self.assertEqual(len({semver.parse("1.0.0"), semver.parse("1.0.0+b")}), 1)
        self.assertTrue(semver.parse("2.0.0-rc.1").is_prerelease)
        self.assertFalse(semver.parse("2.0.0").is_prerelease)

    def test_non_string_input(self):
        self.assertIsNone(semver.try_parse(None))
        self.assertIsNone(semver.parse_leading(None))


class KitVersionFile(unittest.TestCase):
    def test_v1_single_line(self):
        d = semver.parse_kit_version_file("1.2.0 (2026-07-30)\n")
        self.assertEqual(d, {"version": "1.2.0", "date": "2026-07-30", "claude_code_min": None,
                             "claude_code_tested_max": None})

    def test_v2_with_range(self):
        d = semver.parse_kit_version_file("2.0.0-rc.1 (2026-11-02)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: none\n")
        self.assertEqual(d["version"], "2.0.0-rc.1")
        self.assertEqual(d["claude_code_min"], "2.1.302")
        self.assertEqual(d["claude_code_tested_max"], "none")

    def test_comments_blank_lines_and_unknown_keys(self):
        d = semver.parse_kit_version_file("# kit\n\n2.0.0 (x)\nfoo: bar\nclaude_code_min: 2.1.302\n")
        self.assertEqual(d["version"], "2.0.0")
        self.assertEqual(d["claude_code_min"], "2.1.302")

    def test_bad_files(self):
        for text in ("", "\n\n", "not a version\n", "1.2 (x)\n"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    semver.parse_kit_version_file(text)

    def test_the_committed_kit_version_parses(self):
        from tests import REPO_ROOT
        with open(os.path.join(REPO_ROOT, ".claude", "KIT_VERSION")) as fh:
            self.assertIsNotNone(semver.parse_kit_version_file(fh.read())["version"])


if __name__ == "__main__":
    unittest.main()
