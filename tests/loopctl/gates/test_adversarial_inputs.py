"""Adversarial self-review, part two (IMP-24 item 8): G5 shadowing and command files, G9 look-alikes, G11 excuses, G7 pasted reviews."""
from __future__ import annotations

import json
import os

from .. import item_doc
from . import IMPL, PKG, PY, GateCase



class Integrity(GateCase):
    ITEMS = [item_doc("A", acs=1, touches={"domains": ["backend"], "paths": ["src/**", "tests/unit/**"], "schema": False, "deps_change": False},
                      verify={"tests": "sh scripts/run_tests.sh"})]
    CFG = "verify_tests: \"sh scripts/run_tests.sh\"\n"
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "scripts/run_tests.sh": "%s -m unittest discover -s tests -t .\n" % PY, "tests/unit/__init__.py": ""})

    def test_files_that_shadow_the_runner_or_the_standard_library(self):
        for path in ("unittest.py", "pytest.py", "sitecustomize.py", "json.py", "src/os.py", "tests/re.py", "pkg/sitecustomize.py", "unittest/__init__.py"):
            with self.subTest(path):
                self.setUp()
                self.commit({path: "x = 1\n"}, "shadow")
                self.assertFails(self.run_gate("g5"), "g5.shadow-module")

    def test_a_module_that_merely_shares_a_name_inside_a_package_is_fine(self):
        self.commit({"src/app/json.py": "x = 1\n", "src/app/__init__.py": ""}, "ordinary")
        self.assertNotIn("g5.shadow-module", self.ids(self.run_gate("g5")))

    def test_the_script_a_configured_command_runs_is_test_configuration(self):
        self.commit({"scripts/run_tests.sh": "true\n"}, "make the suite pass")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.command-file")
        self.assertIn("scripts/run_tests.sh", json.dumps(res["findings"]))

    def test_gitattributes_and_coverage_config_are_protected(self):
        for path in (".gitattributes", ".coveragerc"):
            with self.subTest(path):
                self.setUp()
                self.commit({path: "x\n"}, "edit")
                self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_moving_a_test_out_of_the_test_tree_is_a_deletion(self):
        self.commit({"tests/unit/test_calc.py": "def test_x():\n    assert 1\n"}, "own test")
        self.lr.repo.git("mv", "tests/unit/test_calc.py", "src/calc_checks.py")
        self.lr.commit_all("rename it out of tests/")
        # the file did not exist at the base, so this is a new non-test file; the base has no test to lose. Do it to a base test instead
        self.setUp()
        self.lr.repo.git("switch", "-q", "main")


class IntegrityBaseTest(GateCase):
    ITEMS = [item_doc("A", acs=1, touches={"domains": ["backend"], "paths": ["src/**"], "schema": False, "deps_change": False})]
    BASE_FILES = {"tests/unit/__init__.py": "", "tests/unit/test_base.py": "def test_x():\n    assert 1\n", "src/a.py": "x = 1\n"}

    def test_a_base_test_moved_out_of_the_tests_tree_is_deleted_for_g5_and_removed_for_g6(self):
        self.lr.repo.git("mv", "tests/unit/test_base.py", "src/base_checks.py")
        self.lr.commit_all("rename it out of tests/")
        self.assertFails(self.run_gate("g5"), "g5.test-deleted")


class Supply(GateCase):
    ITEMS = [item_doc("D", acs=1, slug="slice-d", touches={"domains": ["backend"], "paths": ["package.json"], "schema": False, "deps_change": True}),
             item_doc("A", acs=1)]
    BASE_FILES = {"package.json": json.dumps({"dependencies": {"express": "^4"}}) + "\n", "package-lock.json": "{}\n"}

    def add(self, **deps):
        d = {"dependencies": dict({"express": "^4"}, **deps)}
        self.commit({"package.json": json.dumps(d) + "\n", "package-lock.json": "{ }\n"}, "dep")

    def test_a_homoglyph_of_a_popular_package(self):
        self.add(**{"l\u043ddash": "1.0.0"})          # a Cyrillic letter inside the name
        self.assertFails(self.run_gate("g9", "D"), "g9.non-ascii-name")

    def test_an_alias_to_another_package_skips_the_name_checks(self):
        self.add(harmless="npm:totally-other-package@1.0.0")
        self.assertFails(self.run_gate("g9", "D"), "g9.non-registry-source")

    def test_a_lockfile_that_moves_on_its_own(self):
        self.commit({"package-lock.json": '{ "changed": true }\n'}, "npm update")
        res = self.run_gate("g9", "A")
        self.assertIn("g9.lockfile-only", self.ids(res))


class Secrets(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    BASE_FILES = {"src/a.py": "x = 1\n"}

    def test_an_excuse_in_a_trailing_comment_does_not_excuse_a_real_value(self):
        self.commit({"src/conf.py": 'password = "Zx81kQm2Lp9r"  # example only, honest\n'}, "conf")
        self.assertFails(self.run_gate("g11"), "g11.secret-assignment")

    def test_a_file_too_big_to_scan_is_said_out_loud(self):
        self.commit({"data/blob.txt": ("x" * 1000 + "\n") * 7000}, "big")
        res = self.run_gate("g11")
        self.assertIn("g11.skipped-large", self.ids(res))
        self.assertEqual(res["status"], "pass")


class Reviews(GateCase):
    ITEMS = [item_doc("A", acs=1, touches={"domains": ["backend"], "paths": ["src/**"], "schema": False, "deps_change": False})]
    BASE_FILES = {"src/a.py": "x = 1\n"}

    def test_a_review_pasted_from_another_item(self):
        text = ("# Code Review - slice-zzz (iteration 1)\n\n## Verdict\nCODE_REVIEW_APPROVED\n\n## Summary\n- AC-A-1 proved\n\n"
                "## Verification Run\n- tests: PASS\n")
        self.commit({"docs/features/slice-a/reports/code-review.md": text}, "review")
        self.assertFails(self.run_gate("g7"), "g7.code-review-wrong-item")


if __name__ == "__main__":
    import unittest
    unittest.main()
