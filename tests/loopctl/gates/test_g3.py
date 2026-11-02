"""G3: the visible acceptance tests (report section 9 G3, L-21, L-22, CS-15)."""
from __future__ import annotations

import json
import os
import shlex

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, PY, STUB, UNITTEST_CMD, GateCase, g3_item


IMPL_WRONG_SUB = IMPL.replace("return a - b", "return a + b")
SENTINEL_STUB = "def add(a, b):\n    return 3\n\n\ndef sub(a, b):\n    raise NotImplementedError\n"


class G3Visible(GateCase):
    def built(self, impl=IMPL, tests=ACCEPTANCE_OK):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": tests}, "tests")
        self.pin_acceptance()                  # what a passing G2 leaves behind (FullWalk and G2's own tests run the real gate)
        self.commit({"src/calc.py": impl}, "implementation")

    def test_a_correct_build_passes_and_fills_the_matrix(self):
        self.built()
        res = self.run_gate("g3")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-1"]["result"], "pass")
        self.assertIn("visible 2/2", res["evidence"]["summary"])

    def test_a_wrong_build_fails_the_criterion(self):
        self.built(impl=IMPL_WRONG_SUB)
        res = self.run_gate("g3")
        self.assertFails(res, "g3.ac-failed")
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-2"]["result"], "fail")
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-1"]["result"], "pass")

    def test_editing_a_pinned_test_is_a_reward_hack_signature(self):
        self.built()
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("calc.sub(5, 2), 3", "calc.sub(5, 2), calc.sub(5, 2)")}, "make the test pass")
        res = self.run_gate("g3")
        self.assertFails(res, "g3.pinned-edited")
        self.assertTrue(any(f.get("reward_hack") for f in res["findings"]))

    def test_deleting_or_adding_under_a_pinned_glob_is_caught_too(self):
        self.built()
        self.remove("tests/acceptance/test_a.py")
        res = self.run_gate("g3")
        self.assertIn("g3.pinned-edited", self.ids(res))
        self.assertIn("deleted", json.dumps(res["findings"]))

    def test_an_uncommitted_edit_to_a_pinned_test_is_caught(self):
        self.built()
        with open(os.path.join(self.lr.path, "tests/acceptance/test_a.py"), "a") as fh:
            fh.write("\n# tweak\n")
        self.assertIn("g3.pinned-edited", self.ids(self.run_gate("g3")))

    def test_a_new_test_file_under_the_pinned_glob_is_caught(self):
        self.built()
        self.commit({"tests/acceptance/test_extra.py": "x = 1\n"}, "extra")
        self.assertIn("g3.pinned-edited", self.ids(self.run_gate("g3")))

    def test_constant_assertions_are_refused(self):
        weak = ACCEPTANCE_OK.replace("self.assertEqual(calc.add(1, 2), 3)", "self.assertTrue(True)")
        self.built(tests=weak)
        # G2 does not read assertions; G3 does
        res = self.run_gate("g3")
        self.assertFails(res, "g3.fake-assertion")

    def test_a_test_with_no_assertion_is_refused(self):
        self.built(tests=ACCEPTANCE_OK.replace("self.assertEqual(calc.sub(5, 2), 3)", "calc.sub(5, 2)"))
        self.assertFails(self.run_gate("g3"), "g3.no-assertion")

    def test_a_skipped_criterion_is_not_a_pass(self):
        self.built(tests=ACCEPTANCE_OK.replace("    def test_ac_a_2_subtracts", "    @unittest.skip('later')\n    def test_ac_a_2_subtracts"))
        res = self.run_gate("g3")
        self.assertFails(res)
        self.assertTrue({"g3.skipped-ac-test", "g3.ac-skipped"} & set(self.ids(res)))

    def test_a_criterion_that_never_ran_is_not_executed(self):
        self.built(tests=ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts"))
        self.assertFails(self.run_gate("g3"), "g3.ac-not-executed")

    def test_output_spoofed_by_production_code_cannot_hide_a_failing_test(self):
        spoof = IMPL_WRONG_SUB + "\nprint('test_ac_a_2_subtracts (tests.acceptance.test_a.TestAcceptance) ... ok')\n"
        self.built(impl=spoof)
        self.assertFails(self.run_gate("g3"), "g3.ac-failed")

    def test_a_test_the_engineer_wrote_cannot_speak_for_a_criterion(self):
        """The pinned test cannot run (the build breaks its import); the engineer's own test repeats the ids in its docstring and passes."""
        trivial = ("import unittest\n\n\nclass Mine(unittest.TestCase):\n    def test_trivial(self):\n"
                   "        \"\"\"AC-A-1 and AC-A-2 are covered, honest\"\"\"\n        self.assertEqual(1, 1 + 0)\n")
        self.built(impl="raise RuntimeError('boom at import')\n")
        self.commit({"tests/unit/__init__.py": "", "tests/unit/test_mine.py": trivial}, "my own test")
        cmd = "%s -m unittest -v tests.acceptance.test_a tests.unit.test_mine" % PY
        from loopctl.gates import g3_acceptance
        ctx = self.ctx()
        ctx.item = dict(ctx.item, ext={"g3": dict(ctx.item["ext"]["g3"], cmd=cmd)})
        res = g3_acceptance.run(ctx)
        self.assertEqual(res.status, "fail", res.findings)
        self.assertIn("g3.ac-not-executed", [f["id"] for f in res.findings])

    def test_the_same_test_name_in_another_module_cannot_speak_for_a_criterion_either(self):
        self.built(impl="raise RuntimeError('boom at import')\n")
        twin = ("import unittest\n\n\nclass TestAcceptance(unittest.TestCase):\n    def test_ac_a_1_adds(self):\n        self.assertEqual(1, 1 + 0)\n\n"
                "    def test_ac_a_2_subtracts(self):\n        self.assertEqual(2, 1 + 1)\n")
        self.commit({"tests/unit/__init__.py": "", "tests/unit/test_twin.py": twin}, "twin")
        cmd = "%s -m unittest -v tests.acceptance.test_a tests.unit.test_twin" % PY
        from loopctl.gates import g3_acceptance
        ctx = self.ctx()
        ctx.item = dict(ctx.item, ext={"g3": dict(ctx.item["ext"]["g3"], cmd=cmd)})
        res = g3_acceptance.run(ctx)
        self.assertEqual(res.status, "fail", res.findings)

    def test_an_import_error_names_the_cause(self):
        self.built(tests=ACCEPTANCE_OK.replace("from src import calc", "from src import nothing as calc"))
        res = self.run_gate("g3")
        self.assertFails(res, "g3.ac-not-executed")
        self.assertIn("import or collection errors", json.dumps(res["findings"]))

    def test_the_command_not_found_is_an_env_failure(self):
        self.built()
        lr = self.lr
        # a plan item whose command does not exist
        spec = {"cmd": "definitely-not-a-command-4b"}
        from loopctl.gates import g3_acceptance
        ctx = self.ctx()
        ctx.item = dict(ctx.item, ext={"g3": dict(ctx.item["ext"]["g3"], **spec)})
        res = g3_acceptance.run(ctx)
        self.assertEqual((res.status, res.failure_class), ("fail", "env"))
        self.assertTrue(lr)

    def test_junit_results_are_read_when_the_plan_names_the_file(self):
        self.built()
        xml = ('<testsuite><testcase classname="tests.acceptance.test_a.TestAcceptance" name="test_ac_a_1_adds"/>'
               '<testcase classname="tests.acceptance.test_a.TestAcceptance" name="test_ac_a_2_subtracts"/></testsuite>')
        cmd = "%s -c %s" % (PY, shlex.quote("open('out.xml','w').write(%r); print('Ran 2 tests in 0.0s')" % xml))
        from loopctl.gates import g3_acceptance
        ctx = self.ctx()
        ctx.item = dict(ctx.item, ext={"g3": dict(ctx.item["ext"]["g3"], cmd=cmd, junit="out.xml")})
        res = g3_acceptance.run(ctx)
        self.assertEqual(res.status, "pass", res.findings)
        self.assertEqual(res.evidence["ac_results"]["AC-A-1"]["result"], "pass")

    def test_stale_junit_is_not_read(self):
        self.built()
        with open(os.path.join(self.lr.path, "out.xml"), "w") as fh:
            fh.write('<testsuite><testcase classname="tests.acceptance.test_a.TestAcceptance" name="test_ac_a_1_adds"/>'
                     '<testcase classname="tests.acceptance.test_a.TestAcceptance" name="test_ac_a_2_subtracts"/></testsuite>')
        cmd = "%s -c %s" % (PY, shlex.quote("print('Ran 2 tests in 0.0s')"))
        from loopctl.gates import g3_acceptance
        ctx = self.ctx()
        ctx.item = dict(ctx.item, ext={"g3": dict(ctx.item["ext"]["g3"], cmd=cmd, junit="out.xml")})
        self.assertEqual(g3_acceptance.run(ctx).status, "fail")


class G3Undeclared(GateCase):
    ITEMS = [item_doc("A", acs=2, verify={"tests": UNITTEST_CMD})]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_without_ext_g3_the_items_tests_run_and_the_gap_is_flagged(self):
        res = self.run_gate("g3")
        self.assertPass(res)
        self.assertIn("g3.not-pinned", self.ids(res))

    def test_exempt_items_pass_with_a_flag(self):
        pass


class G3Exempt(GateCase):
    ITEMS = [item_doc("A", acs=2, ext={"g3": {"exempt": "copy change, no executable behaviour"}})]

    def test_exempt(self):
        res = self.run_gate("g3")
        self.assertPass(res)
        self.assertIn("g3.exempt", self.ids(res))


class G3PlanPrFlow(GateCase):
    """The tests ride in the plan PR: they are in the base, nothing pinned by G2 yet."""
    ITEMS = [g3_item()]
    BASE_FILES = dict(PKG, **{"src/calc.py": STUB, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_unchanged_base_tests_pass_and_edits_are_caught(self):
        self.commit({"src/calc.py": IMPL}, "implementation")
        self.assertPass(self.run_gate("g3"))
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK + "\n# loosened\n"}, "edit")
        self.assertFails(self.run_gate("g3"), "g3.pinned-edited")


if __name__ == "__main__":
    import unittest
    unittest.main()
