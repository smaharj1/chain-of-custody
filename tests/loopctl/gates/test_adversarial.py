"""Adversarial self-review of the gates (IMP-24 item 8): each test is an attempt to make a gate PASS when it should FAIL, or to make the
engineer's work look like the acceptance-author's. Every attempt here was tried against the first version of the gate it targets; the ones
that worked are why the corresponding checks exist."""
from __future__ import annotations

import os

from . import ACCEPTANCE_OK, IMPL, PKG, STUB, GateCase, g3_item



class Stubs(GateCase):
    """G2: a 'stub' that is really an implementation, or a test that is red for the wrong reason."""
    ITEMS = [g3_item("A")]

    def staged(self, stubs=STUB, tests=ACCEPTANCE_OK):
        self.commit(dict(PKG, **{"src/calc.py": stubs}), "stubs")
        self.commit({"tests/acceptance/test_a.py": tests}, "tests")
        return self.run_gate("g2")

    def test_code_that_runs_at_import_is_not_a_stub(self):
        for label, text in (("module call", STUB + "\nimport os\nos.system('true')\n"), ("loop", STUB + "\nfor i in range(3):\n    pass\n"),
                            ("class body call", "class K:\n    x = compute()\n\n" + STUB), ("default argument", "def add(a, b=compute()):\n    raise NotImplementedError\n\n\ndef sub(a, b):\n    raise NotImplementedError\n"),
                            ("exec", STUB + "\nexec('x = 1')\n")):
            with self.subTest(label):
                self.setUp()
                self.assertFails(self.staged(stubs=text), "g2.stub-has-logic")

    def test_a_test_that_asserts_nothing_is_not_made_valid_by_a_raising_stub(self):
        weak = ACCEPTANCE_OK.replace("self.assertEqual(calc.add(1, 2), 3)", "calc.add(1, 2)")
        self.assertFails(self.staged(tests=weak), "g2.no-assertion")

    def test_a_constant_assertion_is_refused_at_stubbing_not_only_at_verifying(self):
        weak = ACCEPTANCE_OK.replace("self.assertEqual(calc.add(1, 2), 3)", "calc.add(1, 2)\n        self.assertTrue(True)")
        self.assertFails(self.staged(tests=weak), "g2.fake-assertion")

    def test_a_stub_with_the_wrong_signature_is_invalid_red(self):
        res = self.staged(stubs="def add(a):\n    raise NotImplementedError\n\n\ndef sub(a, b):\n    raise NotImplementedError\n")
        self.assertFails(res, "g2.invalid-red")

    def test_uncommitted_changes_are_not_what_the_red_run_judges(self):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        with open(os.path.join(self.lr.path, "src/calc.py"), "a") as fh:
            fh.write("\n# edited, not committed\n")
        self.assertFails(self.run_gate("g2"), "g2.uncommitted-changes")


class Acceptance(GateCase):
    ITEMS = [g3_item("A")]

    def built(self, impl=IMPL):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.pin_acceptance()
        self.commit({"src/calc.py": impl}, "implementation")

    def test_judging_a_commit_the_worktree_is_not_at_is_an_error_not_a_pass(self):
        self.built()
        good = self.head()
        self.commit({"src/calc.py": IMPL.replace("a + b", "a - b")}, "now broken")
        res = self.run_gate("g3", head=good)           # ask for the good commit's verdict while the broken code is checked out
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "env")

    def test_a_tracked_edit_the_gate_would_run_but_head_does_not_hold(self):
        self.built(impl=IMPL.replace("a + b", "a - b"))
        with open(os.path.join(self.lr.path, "src/calc.py"), "w") as fh:
            fh.write(IMPL)                              # fixed in the working tree only
        res = self.run_gate("g3")
        self.assertFails(res, "g3.uncommitted-changes")

    def test_a_branch_cannot_turn_a_real_gate_off_by_editing_the_config(self):
        self.built()
        self.commit({".claude/loop.config.md": self.lr.cfg_text.replace("delivery: local", "delivery: local\ngates:\n  g5: off\n  g3: off")}, "off")
        res = self.run_gate("g5")
        self.assertEqual((res["mode"], res["status"]), ("on", "fail"))     # the config change is itself a protected path
        self.assertEqual(self.run_gate("g3")["mode"], "on")


class Trace(GateCase):
    """G1: an id repeated in a test the engineer wrote."""
    ITEMS = [g3_item("A")]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL,
                              "tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts")})

    def test_only_the_pinned_tests_can_trace_a_criterion(self):
        self.commit({"tests/unit/__init__.py": "", "tests/unit/test_mine.py": "# AC-A-2 is covered here, honestly\ndef test_mine():\n    assert 1\n"}, "mine")
        st = {"status": "verifying", "gates": {"g3": {"status": "pass", "head": self.head()}},
              "ac": {"AC-A-1": {"g3": {"result": "pass"}}, "AC-A-2": {"g3": {"result": "pass"}}}}
        res = self.run_gate("g1", state=st)
        self.assertFails(res, "g1.ac-untested")
        self.assertIn("AC-A-2", [f["message"] for f in res["findings"] if f["id"] == "g1.ac-untested"][0])


if __name__ == "__main__":
    import unittest
    unittest.main()
