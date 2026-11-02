"""levels: L0-L5 constants and the min-with-reasons computation (IMP-21)."""
from __future__ import annotations

import json
import unittest

from kitlib import levels, semver


class Parse(unittest.TestCase):
    def test_parse_level(self):
        for v, want in ((0, 0), (3, 3), ("3", 3), ("L4", 4), ("l5", 5), (" L2 ", 2)):
            self.assertEqual(levels.parse_level(v), want)
        for bad in (-1, 6, "L6", "x", "", None, 1.5, True, "L"):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValueError):
                    levels.parse_level(bad)

    def test_constants_and_names(self):
        self.assertEqual([levels.L0, levels.L1, levels.L2, levels.L3, levels.L4, levels.L5], [0, 1, 2, 3, 4, 5])
        self.assertEqual(levels.level_name(3), "L3")
        self.assertEqual(sorted(levels.LEVEL_NAMES), [0, 1, 2, 3, 4, 5])


class Effective(unittest.TestCase):
    def test_min_of_configured_and_caps(self):
        e = levels.effective_level("L4", [levels.Cap("prereq", 3, "no bot identity"), levels.Cap("platform", 5, "ok"),
                                          ("evidence", "L2", "no evidence.json")])
        self.assertEqual(e.effective, 2)
        self.assertEqual([c.name for c in e.binding], ["evidence", "prereq"])

    def test_no_caps_means_configured(self):
        self.assertEqual(levels.effective_level(3, []).effective, 3)

    def test_caps_above_configured_do_not_raise_the_level(self):
        e = levels.effective_level(1, [levels.Cap("x", 5, "")])
        self.assertEqual(e.effective, 1)
        self.assertEqual(e.binding, [])

    def test_dict_caps_and_json_shape(self):
        e = levels.effective_level("L3", [{"name": "b5", "level": 2, "reason": "ungated deploy on main"}])
        d = e.to_dict()
        self.assertEqual(d, {"configured": "L3", "effective": "L2",
                             "caps": [{"name": "b5", "level": "L2", "reason": "ungated deploy on main"}]})
        json.dumps(d)

    def test_explain_marks_binding_caps(self):
        text = levels.effective_level(3, [levels.Cap("a", 1, "why a"), levels.Cap("b", 5, "why b")]).explain()
        self.assertIn("effective L1", text)
        self.assertRegex(text, r"\* a\s+L1\s+why a")
        self.assertRegex(text, r"  b\s+L5\s+why b")

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            levels.effective_level("L9", [])
        with self.assertRaises(ValueError):
            levels.effective_level(2, [("x", "L7", "bad")])


class Imp21Out_Of_The_Box(unittest.TestCase):
    """IMP-21: with nothing attested, v2 is an L1 kit."""

    def test_evidence_cap(self):
        self.assertEqual(levels.evidence_cap(False).level, levels.L3)
        self.assertIn("evidence.json", levels.evidence_cap(False).reason)
        self.assertEqual(levels.evidence_cap(False, "custom").reason, "custom")
        self.assertEqual(levels.evidence_cap(True).level, levels.L5)

    def test_version_cap_table(self):
        want = {"unknown": 1, "untested": 2, "below_min": 2, "above_tested": 2, "ok": 5}
        for status, level in want.items():
            self.assertEqual(levels.version_cap(status).level, level, status)
        with self.assertRaises(ValueError):
            levels.version_cap("bogus")

    def test_unreadable_claude_version_caps_at_l1(self):
        status = semver.range_status(None, "2.1.302", "none")
        e = levels.effective_level("L4", [levels.evidence_cap(False), levels.version_cap(status)])
        self.assertEqual(e.effective, 1)

    def test_tested_max_none_caps_at_l2_even_with_a_readable_version(self):
        status = semver.range_status("2.1.302 (Claude Code)", "2.1.302", "none")
        e = levels.effective_level("L4", [levels.evidence_cap(True), levels.version_cap(status)])
        self.assertEqual(e.effective, 2)

    def test_all_clear(self):
        status = semver.range_status("2.1.305", "2.1.302", "2.1.310")
        e = levels.effective_level("L4", [levels.evidence_cap(True), levels.version_cap(status)])
        self.assertEqual(e.effective, 4)


if __name__ == "__main__":
    unittest.main()
