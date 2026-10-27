"""G3: the held-out tests, driver and CI-only (report section 9 G3, DEC-11, RC-6)."""
from __future__ import annotations

import json
import os
import sys

from . import ACCEPTANCE_OK, IMPL, PKG, STUB, GateCase, g3_item


IMPL_WRONG_SUB = IMPL.replace("return a - b", "return a + b")
SENTINEL_STUB = "def add(a, b):\n    return 3\n\n\ndef sub(a, b):\n    raise NotImplementedError\n"


def _heldout_runner() -> str:
    """A held-out harness kept outside the checkout: prints {"results": <r.json in LOOPCTL_HELDOUT_DIR>} (what a real wrapper would print)."""
    import atexit
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="g3-heldout-runner-")
    atexit.register(shutil.rmtree, d, True)
    path = os.path.join(d, "run.py")
    with open(path, "w") as fh:
        fh.write("import json, os\nprint(json.dumps({'results': json.load(open(os.path.join(os.environ['LOOPCTL_HELDOUT_DIR'], 'r.json')))}))\n")
    return path


class G3HeldOutDriver(GateCase):
    ITEMS = [g3_item(heldout=True)]
    CFG = "runner: driver\nheldout_cmd: \"%s %s\"\n" % (sys.executable, _heldout_runner())

    def built(self, held):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.pin_acceptance()
        self.commit({"src/calc.py": IMPL}, "implementation")
        if held is not None:
            d = os.path.join(self.ctx().paths["heldout"], "A")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "r.json"), "w") as fh:
                json.dump(held, fh)
        return os.path.join(self.ctx().paths["heldout"], "A")

    def tearDown(self):
        import shutil
        shutil.rmtree(os.path.join(self.ctx().paths["heldout"], "A"), ignore_errors=True)
        super().tearDown()

    def test_held_out_pass_and_the_gap_is_printed(self):
        self.built({"AC-A-1": "pass", "AC-A-2": "pass"})
        res = self.run_gate("g3")
        self.assertPass(res)
        self.assertIn("held-out 2/2", res["evidence"]["summary"])
        self.assertIn("gap +0%", res["evidence"]["summary"])
        self.assertIn("held-out", res["evidence"]["ac_results"]["AC-A-1"]["evidence"])

    def test_a_held_out_failure_fails_the_gate_even_when_the_visible_tests_pass(self):
        self.built({"AC-A-1": "pass", "AC-A-2": "fail"})
        res = self.run_gate("g3")
        self.assertFails(res, "g3.heldout-failed")
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-2"]["result"], "fail")
        self.assertIn("held-out 1/2", res["evidence"]["summary"])
        self.assertIn("gap +50%", res["evidence"]["summary"])

    def test_declared_but_missing_held_out_tests_fail_under_the_driver(self):
        self.built(None)
        res = self.run_gate("g3")
        self.assertFails(res, "g3.heldout-missing")
        self.assertEqual(res["failure_class"], "env")

    def test_held_out_that_covers_only_some_criteria_is_incomplete(self):
        self.built({"AC-A-1": "pass"})
        self.assertFails(self.run_gate("g3"), "g3.heldout-incomplete")

    def test_held_out_that_reports_nothing_is_not_a_pass(self):
        self.built({})
        self.assertFails(self.run_gate("g3"), "g3.heldout-not-executed")

    def test_the_held_out_directory_is_outside_the_checkout(self):
        d = self.built({"AC-A-1": "pass", "AC-A-2": "pass"})
        # in the git common dir: no checkout, branch or `git add -A` of the engineer's worktree ever contains it
        self.assertTrue(os.path.realpath(d).startswith(os.path.join(os.path.realpath(self.lr.path), ".git") + os.sep))
        self.assertEqual(self.lr.repo.git("status", "--porcelain", "--untracked-files=all"), "")


class G3HeldOutNoCommand(GateCase):
    ITEMS = [g3_item(heldout=True)]
    CFG = "runner: driver\n"
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_held_out_tests_without_heldout_cmd_are_an_env_failure(self):
        d = os.path.join(self.ctx().paths["heldout"], "A")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "x"), "w").close()
        try:
            self.assertFails(self.run_gate("g3"), "g3.heldout-cmd")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class G3HeldOutSequential(GateCase):
    ITEMS = [g3_item(heldout=True)]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_ci_only_is_said_out_loud(self):
        res = self.run_gate("g3")
        self.assertPass(res)
        self.assertIn("g3.heldout-ci-only", self.ids(res))
        self.assertIn("held-out ci-only", res["evidence"]["summary"])


class G3Isolation(GateCase):
    ITEMS = [g3_item(heldout=True)]
    CFG = "autonomy_level: L2\n"
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_l2_without_driver_or_the_ci_attestation_cannot_hold_held_out_tests(self):
        res = self.run_gate("g3")
        self.assertFails(res, "g3.heldout-isolation")
        self.assertEqual(res["failure_class"], "env")


class G3IsolationAttested(GateCase):
    ITEMS = [g3_item(heldout=True)]
    CFG = "autonomy_level: L2\nplatform_attested:\n  - g3_ci_only\n"
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def test_the_ci_only_attestation_satisfies_it(self):
        self.assertPass(self.run_gate("g3"))


if __name__ == "__main__":
    import unittest
    unittest.main()
