"""G5: test integrity. The glob check (FULL) and the AST lint (OPT-IN, evidence-gated)."""
from __future__ import annotations

import json

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, STUB, GateCase, g3_item

PYPROJECT = '[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
PKG_JSON = json.dumps({"name": "x", "scripts": {"test": "jest"}, "dependencies": {"a": "1"}, "jest": {"testMatch": ["**/*.test.js"]}}, indent=1)
EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})


class Glob(GateCase):
    ITEMS = [g3_item("A"), item_doc("B", acs=2, touches={"domains": ["backend"], "paths": ["src/**", "tests/unit/**"], "schema": False, "deps_change": False},
                                    ext={"g5": {"allow": ["package.json"]}})]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK, "tests/unit/test_calc.py": "import unittest\n",
                              "conftest.py": "", "pyproject.toml": PYPROJECT, "package.json": PKG_JSON, ".github/workflows/ci.yml": "name: ci\n"})

    def test_a_plain_source_change_passes(self):
        self.commit({"src/calc.py": IMPL + "\n# more\n"}, "work")
        res = self.run_gate("g5")
        self.assertPass(res)
        self.assertIn("integrity flags 0", res["evidence"]["summary"])

    def test_a_new_test_inside_the_plan_footprint_is_expected(self):
        self.commit({"tests/unit/test_new.py": "import unittest\n"}, "own test")
        res = self.run_gate("g5")
        self.assertPass(res)
        self.assertIn("g5.expected-change", self.ids(res))

    def test_a_new_test_outside_the_footprint_is_flagged(self):
        self.commit({"tests/other/test_new.py": "import unittest\n"}, "stray test")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_conftest_ci_and_lint_config_are_flagged_at_any_depth(self):
        for path, text in (("conftest.py", "import os\n"), ("pkg/conftest.py", "x=1\n"), (".github/workflows/ci.yml", "name: weaker\n"),
                           ("web/jest.config.js", "module.exports = {}\n"), (".eslintrc.json", "{}\n"), (".env.local", "A=1\n"),
                           ("web/__snapshots__/a.snap", "snap\n"), ("Makefile", "test:\n\ttrue\n")):
            with self.subTest(path):
                self.setUp()
                self.commit({path: text}, "edit " + path)
                res = self.run_gate("g5")
                self.assertFails(res, "g5.protected-path")
                self.assertIn(path, json.dumps(res["findings"]))

    def test_a_deleted_test_is_always_a_finding_even_inside_the_footprint(self):
        self.remove("tests/unit/test_calc.py")
        self.assertFails(self.run_gate("g5"), "g5.test-deleted")

    def test_an_uncommitted_new_test_file_counts(self):
        import os
        with open(os.path.join(self.lr.path, "tests", "stray_test.py"), "w") as fh:
            fh.write("x = 1\n")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_package_json_is_judged_on_scripts_and_jest_not_on_dependencies(self):
        doc = json.loads(PKG_JSON)
        doc["dependencies"]["b"] = "2"
        self.commit({"package.json": json.dumps(doc, indent=1)}, "dependency")
        self.assertPass(self.run_gate("g5"))
        self.setUp()
        doc["scripts"]["test"] = "true"
        self.commit({"package.json": json.dumps(doc, indent=1)}, "weaker test script")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.protected-path")
        self.assertIn("scripts", json.dumps(res["findings"]))
        self.setUp()
        doc2 = json.loads(PKG_JSON)
        doc2["jest"] = {"testMatch": []}
        self.commit({"package.json": json.dumps(doc2, indent=1)}, "jest config")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_the_plan_can_allow_a_package_json_edit(self):
        doc = json.loads(PKG_JSON)
        doc["scripts"]["lint"] = "eslint ."
        self.commit({"package.json": json.dumps(doc, indent=1)}, "lint script")
        res = self.run_gate("g5", "B")
        self.assertPass(res)
        self.assertIn("g5.expected-change", self.ids(res))

    def test_pyproject_is_judged_on_the_test_and_lint_tables_only(self):
        self.commit({"pyproject.toml": PYPROJECT.replace('["requests"]', '["requests", "idna"]')}, "dependency")
        self.assertPass(self.run_gate("g5"))
        self.setUp()
        self.commit({"pyproject.toml": PYPROJECT.replace('addopts = "-q"', 'addopts = "-q -k not_slow"')}, "pytest options")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")
        self.setUp()
        self.commit({"pyproject.toml": PYPROJECT + "\n[tool.ruff]\nline-length = 300\n"}, "lint")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_an_unparsable_package_json_is_flagged_not_ignored(self):
        self.commit({"package.json": "{ not json"}, "break it")
        self.assertFails(self.run_gate("g5"), "g5.protected-path")


class Pinned(GateCase):
    ITEMS = [g3_item("A")]

    def staged(self):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.assertPass(self.run_gate("g2"))

    def test_the_acceptance_authors_pinned_tests_are_not_the_engineers_edit(self):
        self.staged()
        self.commit({"src/calc.py": IMPL}, "implementation")
        self.assertPass(self.run_gate("g5"))

    def test_editing_a_pinned_test_is_a_reward_hack_signature(self):
        self.staged()
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("3)", "None)")}, "hack")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.pinned-test-edited")
        self.assertTrue(any(f.get("reward_hack") for f in res["findings"]))

    def test_deleting_a_pinned_test_is_caught_once(self):
        self.staged()
        self.remove("tests/acceptance/test_a.py")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.pinned-test-edited")
        self.assertNotIn("g5.test-deleted", self.ids(res))


class Lint(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})
    CFG = "g5_ast_lint:\n  - python\n  - javascript\n"

    def with_evidence(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)

    def run_lint(self):
        self.with_evidence()
        return self.run_gate("g5")

    def test_the_lint_refuses_without_the_evidence_gate(self):
        res = self.run_gate("g5")
        self.assertFails(res, "g5.ast-evidence-required")
        self.assertEqual(res["failure_class"], "env")

    def test_skip_markers_added_to_python_tests(self):
        self.commit({"tests/acceptance/test_b.py": "import unittest\n@unittest.skip('x')\ndef test_b(): pass\n"}, "skip")
        self.assertIn("g5.lint.skip-added", self.ids(self.run_lint()))

    def test_test_environment_detection_in_production_code(self):
        self.commit({"src/calc.py": IMPL + "\nimport os\nif os.environ.get('PYTEST_CURRENT_TEST'):\n    pass\n"}, "sniff")
        res = self.run_lint()
        self.assertFails(res, "g5.lint.test-env-detection")

    def test_a_mock_of_the_items_own_footprint(self):
        self.commit({"tests/unit/test_mock.py": "from unittest.mock import patch\n\ndef test_x():\n    with patch('src.calc.add'):\n        pass\n"}, "mock")
        self.assertIn("g5.lint.mock-of-footprint", self.ids(self.run_lint()))

    def test_a_snapshot_created_with_the_code_it_snapshots(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n", "web/__snapshots__/calc.snap": "out\n"}, "snap")
        self.assertIn("g5.lint.snapshot-with-code", self.ids(self.run_lint()))

    def test_update_snapshot_flags(self):
        self.commit({"scripts/test.sh": "jest -u\n"}, "update")
        self.assertIn("g5.lint.update-snapshots", self.ids(self.run_lint()))

    def test_production_code_importing_the_test_framework(self):
        self.commit({"src/helpers.py": "import unittest\nunittest.TestCase.assertEqual = lambda *a, **k: None\n"}, "patch the framework")
        self.assertFails(self.run_lint(), "g5.lint.test-framework-in-production")

    def test_javascript_findings_are_warnings_with_low_confidence(self):
        self.commit({"web/a.test.js": "describe.skip('x', () => {});\n"}, "js skip")
        res = self.run_lint()
        hit = [f for f in res["findings"] if f["id"] == "g5.lint.skip-added"]
        self.assertTrue(hit)
        self.assertEqual((hit[0]["severity"], hit[0].get("confidence")), ("warn", "low"))

    def test_clean_changes_pass_the_lint(self):
        self.commit({"src/calc.py": IMPL + "\n# fine\n"}, "fine")
        res = self.run_lint()
        self.assertPass(res)
        self.assertIn("AST lint python+javascript", res["evidence"]["summary"])


class LintOff(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_without_the_config_key_no_lint_runs(self):
        self.commit({"src/calc.py": IMPL + "\nimport os\nos.environ.get('PYTEST_CURRENT_TEST')\n"}, "sniff")
        self.assertPass(self.run_gate("g5"))


if __name__ == "__main__":
    import unittest
    unittest.main()
