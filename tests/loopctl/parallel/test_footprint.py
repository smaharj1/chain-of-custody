"""Footprints: which plan items may run together, and whether a finished item stayed in its lane (report 11.5 item 3, 19.7 L-35, D-29)."""
from __future__ import annotations

import unittest

from loopctl.parallel import footprint

from . import pitem

# (a, b, may the two globs match one file?)
OVERLAP = [
    ("src/api/**", "src/ui/**", False), ("src/**", "src/api/x.py", True), ("src/a/*.py", "src/*/x.py", True), ("docs/**", "src/**", False),
    ("*.md", "docs/readme.md", True), ("*.md", "src/**", True), ("docs/*.md", "src/**", False), ("**/*.md", "src/**", True),
    ("package.json", "package.json", True), ("package.json", "apps/web/package.json", True), ("packages/*/src/**", "packages/web/src/x", True),
    ("packages/*/src/**", "packages/web/test/**", False), ("src/api", "src/api/x/y.py", True), ("src/a*", "src/b*", False),
    ("src/ab*", "src/a*c", True), ("src/a*", "src/*", True), ("a/**/z", "a/b/c/**", True), ("a/b/*.py", "a/b/c/*.py", False),
    ("a/b/c.py", "a/b/c.py", True), ("a/b/c.py", "a/b/d.py", False), ("src/", "src/x", True), ("src/a?c", "src/abc", True), ("src/a?c", "src/abbc", False),
    ("src/a**b/x", "src/a/q/b/x", True),            # `**` inside a segment crosses directories: the glob is widened, never narrowed
    ("src/(a|b)/**", "src/a/x", False),             # regex metacharacters are literal characters, not syntax
    ("a/b/**", "a/b", True), ("**", "x", True), ("x/**/y/**/z", "x/q/y/r/z", True), ("x/**/y", "w/z", False), ("x/**/y", "x/z", True),   # x/z/y is under x/z/ (a directory pattern) and matches x/**/y
   
]


class Overlap(unittest.TestCase):
    def test_table_in_both_directions(self):
        for a, b, want in OVERLAP:
            with self.subTest(a=a, b=b):
                self.assertEqual(footprint.may_overlap(a, b), want)
                self.assertEqual(footprint.may_overlap(b, a), want)

    def test_a_very_long_glob_does_not_blow_up(self):
        a = "/".join(["**", "*"] * 60)
        b = "/".join(["x*y"] * 60)
        footprint.may_overlap(a, b)                  # returns; the point is that it neither hangs nor recurses to death
        self.assertTrue(footprint.may_overlap("**/" * 40 + "z", "z"))

    def test_first_overlap_names_the_pair(self):
        self.assertEqual(footprint.first_overlap(["a/**", "b/**"], ["c/**", "b/x"]), ("b/**", "b/x"))
        self.assertIsNone(footprint.first_overlap(["a/**"], ["b/**"]))


class Conflict(unittest.TestCase):
    def test_disjoint_footprints_may_run_together(self):
        self.assertIsNone(footprint.conflict(pitem("A"), pitem("B")))

    def test_overlapping_footprints_may_not(self):
        why = footprint.conflict(pitem("A", paths=["src/**"]), pitem("B", paths=["src/b/**"]))
        self.assertIn("footprints may meet", why)

    def test_an_item_that_declares_nothing_runs_alone(self):
        blank = pitem("A", paths=[])
        self.assertFalse(footprint.known(blank))
        self.assertIn("runs alone", footprint.conflict(blank, pitem("B")))
        self.assertIn("runs alone", footprint.conflict(pitem("B"), blank))

    def test_two_schema_items_share_a_lane_whatever_their_paths(self):
        a, b = pitem("A", schema=True), pitem("B", schema=True)
        self.assertEqual(footprint.lanes(a), frozenset(["schema"]))
        self.assertIn("schema", footprint.conflict(a, b))

    def test_a_migration_alone_puts_an_item_in_the_schema_lane(self):
        m = pitem("A", migrations=[{"name": "m1", "up": "db/migrations/001.sql", "down": "db/migrations/001.down.sql", "dataLossClass": "none"}])
        self.assertEqual(footprint.lanes(m), frozenset(["schema"]))

    def test_a_dependency_change_has_its_own_lane(self):
        a, b = pitem("A", deps_change=True), pitem("B", deps_change=True)
        self.assertIn("deps", footprint.conflict(a, b))
        self.assertIsNone(footprint.conflict(a, pitem("C", schema=True)))

    def test_hostile_globs_are_dropped_not_trusted(self):
        item = pitem("A", paths=["../escape/**", "/etc/**", "**", "*", "src/ok/**", "a/./b", 7])
        self.assertEqual(footprint.declared(item), ["src/ok/**"])
        self.assertFalse(footprint.known(pitem("A", paths=["**", "/x"])))


class Diff(unittest.TestCase):
    def test_what_the_item_declared_and_its_own_custody_trail_are_in_its_lane(self):
        item = pitem("A", paths=["src/a/**", "tests/test_a.py"], slug="slice-a")
        changed = ["src/a/m.py", "src/a/deep/er/x.py", "tests/test_a.py", "docs/features/slice-a/reports/backend.md"]
        self.assertEqual(footprint.undeclared(changed, item), [])

    def test_a_file_outside_it_is_a_lie(self):
        item = pitem("A", paths=["src/a/**"], slug="slice-a")
        self.assertEqual(footprint.undeclared(["src/a/m.py", "src/b/m.py", "README.md", "docs/features/other/x.md"], item),
                         ["README.md", "docs/features/other/x.md", "src/b/m.py"])

    def test_a_directory_pattern_covers_what_is_under_it_a_star_does_not_cross_directories(self):
        self.assertEqual(footprint.undeclared(["src/a/x/y.py"], pitem("A", paths=["src/a"])), [])
        self.assertEqual(footprint.undeclared(["src/a/x/y.py"], pitem("A", paths=["src/a/*.py"])), ["src/a/x/y.py"])

    def test_the_custody_trail_is_only_the_items_own(self):
        item = pitem("A", paths=["src/a/**"], slug="slice-a")
        self.assertEqual(footprint.custody_globs(item), ["docs/features/slice-a/**"])
        self.assertEqual(footprint.undeclared(["docs/features/slice-b/reports/x.md", "docs/features/slice-a-evil/x.md"], item),
                         ["docs/features/slice-a-evil/x.md", "docs/features/slice-b/reports/x.md"])

    def test_an_item_with_no_footprint_has_nothing_to_lie_about(self):
        self.assertEqual(footprint.undeclared(["anything/at/all"], pitem("A", paths=[])), [])


if __name__ == "__main__":
    unittest.main()
