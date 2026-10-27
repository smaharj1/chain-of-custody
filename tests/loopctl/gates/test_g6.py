"""G6: verify, wrapped, plus what verify alone does not prove."""
from __future__ import annotations

import json
import os
import sys

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, GateCase, g3_item


MIG = {"name": "0001_widgets", "up": "db/0001.up.sql", "down": "db/0001.down.sql", "dataLossClass": "none", "stage": "expand"}
LOSSY = {"name": "0002_drop", "up": "db/0002.up.sql", "dataLossClass": "lossy", "noDownReason": "the column is gone", "stage": "contract"}
MIGRATE_SCRIPT = '''import os, sys
mode = open("mode.txt").read().strip() if os.path.exists("mode.txt") else "ok"
open("migrate.log", "a").write(os.environ["LOOPCTL_MIGRATION"] + "|" + os.environ.get("LOOPCTL_MIGRATION_DOWN", "") + "\\n")
if mode == "fail":
    print("down failed"); sys.exit(1)
if mode != "nodialect":
    print("dialect: " + ("postgres" if mode == "postgres" else "sqlite"))
'''
PY = '"%s"' % sys.executable


def state_for(head, g3="pass", g6=None, ac=None):
    st = {"status": "verifying", "attempt": 1, "gates": {}, "ac": {}}
    if g3:
        st["gates"]["g3"] = {"status": g3, "head": head}
    if g6:
        st["gates"]["g6"] = {"status": g6, "head": head}
    for a, cells in (ac or {}).items():
        st["ac"][a] = cells
    return st


def config(extra=""):
    return "verify_tests: \"%s -m unittest -v tests.acceptance.test_a tests.unit.test_calc\"\nverify_require:\n  - tests\ndb_local: sqlite\n%s" % (sys.executable, extra)


UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n\n    def test_two(self):\n        self.assertTrue(len('ab') == 2)\n"


class G6(GateCase):
    ITEMS = [g3_item("A"), item_doc("M", acs=1, migrations=[MIG], ext={"g3": {"exempt": "schema only"}}),
             item_doc("L", acs=1, migrations=[MIG, LOSSY], ext={"g3": {"exempt": "schema only"}}),
             item_doc("U", acs=2, ext={"g3": {"visible": ["tests/unit/**"], "cmd": "true"}}, verify={"tests": "%s -m unittest -v tests.unit.test_calc" % sys.executable})]
    CFG = config("migrate_check_cmd: \"%s scripts/migrate_check.py\"\n" % sys.executable)
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK, "tests/unit/__init__.py": "", "tests/unit/test_calc.py": UNIT,
                              "scripts/migrate_check.py": MIGRATE_SCRIPT})

    def pass_state(self):
        return state_for(self.head(), g3="pass", ac={"AC-A-1": {"g3": {"result": "pass"}}, "AC-A-2": {"g3": {"result": "pass"}}})

    def test_green_build_passes_and_every_criterion_is_executed(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        res = self.run_gate("g6")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-1"]["result"], "pass")
        self.assertIn("2/2 criteria executed and passed", res["evidence"]["summary"])

    def test_a_failing_suite_is_an_item_failure(self):
        self.commit({"src/calc.py": IMPL.replace("a + b", "a - b")}, "break add")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.verify-failed", "g6.ac-failed")
        self.assertEqual(res["failure_class"], "item")

    def test_the_branch_cannot_weaken_the_verify_definition(self):
        self.commit({"src/calc.py": IMPL.replace("a + b", "a - b"),
                     ".claude/loop.config.md": self.lr.cfg_text.replace("verify_tests:", "verify_tests: true\n#")}, "weaken verify and break")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.verify-failed")           # the BASE's definition ran, not `true`

    def test_a_test_that_disappeared_is_a_finding(self):
        self.commit({"tests/unit/test_calc.py": UNIT.replace("    def test_two", "    def renamed_two")}, "drop a test")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.test-removed")
        self.assertIn("T.test_two", json.dumps(res["findings"]))

    def test_a_test_that_asserts_less_than_before_is_a_finding(self):
        weaker = UNIT.replace("        self.assertEqual(1, 1 + 0)", "        self.assertTrue(True)")
        self.commit({"tests/unit/test_calc.py": weaker}, "loosen")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.assertion-weakened")
        self.assertIn("T.test_one", json.dumps(res["findings"]))
        self.setUp()
        self.commit({"tests/unit/test_calc.py": UNIT.replace("        self.assertEqual(1, 1 + 0)", "        pass")}, "delete the assertion")
        self.assertFails(self.run_gate("g6"), "g6.assertion-weakened")
        self.setUp()
        self.commit({"tests/unit/test_calc.py": UNIT.replace("        self.assertEqual(1, 1 + 0)", "        self.assertEqual(1, 1 + 0)\n        self.assertEqual(len('ab'), 2)")}, "more asserts")
        self.assertNotIn("g6.assertion-weakened", self.ids(self.run_gate("g6")))
        self.setUp()
        self.commit({"tests/unit/test_calc.py": UNIT.replace("        self.assertEqual(1, 1 + 0)", "        self.assertEqual(1, 1 + 0)\n        self.assertEqual(2, 2)")}, "padding")
        self.assertFails(self.run_gate("g6"), "g6.assertion-weakened")      # a constant assertion added is not an assertion

    def test_deleting_a_whole_test_file_is_a_finding(self):
        self.remove("tests/unit/test_calc.py")
        self.assertFails(self.run_gate("g6"), "g6.test-removed")

    def test_a_renamed_test_file_keeps_its_names(self):
        self.lr.repo.git("mv", "tests/unit/test_calc.py", "tests/unit/test_calc2.py")
        self.lr.commit_all("rename the file")
        self.assertNotIn("g6.test-removed", self.ids(self.run_gate("g6")))

    def test_skip_markers_may_not_be_added(self):
        self.commit({"tests/unit/test_calc.py": UNIT.replace("    def test_two", "    @unittest.skip('later')\n    def test_two")}, "skip")
        self.assertFails(self.run_gate("g6"), "g6.skipped-increased")

    def test_a_skipped_criterion_test_is_refused(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("    def test_ac_a_2_subtracts", "    @unittest.skip('later')\n    def test_ac_a_2_subtracts")}, "skip ac")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.skipped-ac-test")
        self.assertTrue({"g6.ac-skipped", "g6.skipped-increased"} & set(self.ids(res)))

    def test_a_criterion_with_no_executed_test_line_is_not_a_pass(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts")}, "unnamed")
        self.assertFails(self.run_gate("g6"), "g6.ac-not-executed")

    def test_a_g3_pass_for_this_head_stands_in_for_the_missing_line(self):
        # item U's own test command prints no criterion line at all
        self.assertFails(self.run_gate("g6", "U"), "g6.ac-not-executed")
        st = state_for(self.head(), ac={"AC-U-1": {"g3": {"result": "pass"}}, "AC-U-2": {"g3": {"result": "pass"}}})
        self.assertPass(self.run_gate("g6", "U", state=st))
        stale = state_for("a" * 40, ac={"AC-U-1": {"g3": {"result": "pass"}}, "AC-U-2": {"g3": {"result": "pass"}}})
        self.assertFails(self.run_gate("g6", "U", state=stale), "g6.ac-not-executed")

    def test_runtime_skipped_count_may_not_rise_over_the_baseline(self):
        from loopctl.gates import _lib
        ctx = self.ctx()
        d = os.path.join(_lib.runstate_of(ctx).root, "baseline")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "A.json"), "w") as fh:
            json.dump({"base": self.base, "skipped": 0, "collected": 4}, fh)
        self.commit({"tests/unit/test_calc.py": UNIT.replace("        self.assertTrue(len('ab') == 2)", "        self.skipTest('flaky')\n        self.assertTrue(len('ab') == 2)")}, "skip at runtime")
        res = self.run_gate("g6")
        self.assertFails(res, "g6.skipped-increased")

    def test_migrations_up_down_up_pass_with_the_declared_dialect(self):
        res = self.run_gate("g6", "M")
        self.assertNotIn("g6.migration-failed", self.ids(res))
        self.assertNotIn("g6.dialect-mismatch", self.ids(res))

    def test_a_failing_migration_check_fails(self):
        with open(os.path.join(self.lr.path, "mode.txt"), "w") as fh:
            fh.write("fail")
        self.assertFails(self.run_gate("g6", "M"), "g6.migration-failed")

    def test_the_wrong_dialect_is_refused(self):
        with open(os.path.join(self.lr.path, "mode.txt"), "w") as fh:
            fh.write("postgres")
        res = self.run_gate("g6", "M")
        self.assertFails(res, "g6.dialect-mismatch")
        self.assertEqual(res["failure_class"], "env")

    def test_an_unnamed_dialect_is_refused(self):
        with open(os.path.join(self.lr.path, "mode.txt"), "w") as fh:
            fh.write("nodialect")
        self.assertFails(self.run_gate("g6", "M"), "g6.dialect-unknown")

    def test_a_lossy_migrations_down_is_never_handed_to_the_command(self):
        self.run_gate("g6", "L")
        with open(os.path.join(self.lr.path, "migrate.log")) as fh:
            log = fh.read().splitlines()
        self.assertIn("0001_widgets|db/0001.down.sql", log)
        self.assertIn("0002_drop|", log)
        os.unlink(os.path.join(self.lr.path, "migrate.log"))


class G6NoMigrateCmd(GateCase):
    ITEMS = [g3_item("A"), item_doc("M", acs=1, migrations=[MIG], ext={"g3": {"exempt": "schema only"}})]
    CFG = config()
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK, "tests/unit/__init__.py": "", "tests/unit/test_calc.py": UNIT})

    def test_no_prod_dialect_database_is_an_env_failure(self):
        res = self.run_gate("g6", "M")
        self.assertFails(res, "g6.no-prod-dialect-db")
        self.assertEqual(res["failure_class"], "env")


class G6Unconfigured(GateCase):
    ITEMS = [g3_item("A")]

    def test_a_missing_verify_command_is_a_definition_error_never_a_pass(self):
        res = self.run_gate("g6")
        self.assertFails(res, "g6.verify-error")
        self.assertEqual(res["failure_class"], "env")


if __name__ == "__main__":
    import unittest
    unittest.main()
