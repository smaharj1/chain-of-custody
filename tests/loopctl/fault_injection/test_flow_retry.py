"""Retries, rework and discard: a failing gate sends the engineer back, a reward-hack signature or no progress discards, a second discard
stops the run (report 10.4, RC-19, L-2, L-8; FI-16).
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl.gates import GateResult

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Retry(DriverTestCase):
    def test_a_failing_gate_sends_the_engineer_back_with_the_failure_and_then_passes(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        n = []

        def g6(ctx):
            n.append(1)
            if len(n) == 1:
                return GateResult.failed([{"id": "g6.tests", "severity": "error", "message": "test_home fails on AC-A-1"}],
                                         ac_results={"AC-A-1": {"result": "fail", "evidence": "tests/test_home.py"}})
            return GateResult.passed(ac_results={"AC-A-1": {"result": "pass", "evidence": "tests/test_home.py"}})
        lr.stubs.results["g6"] = g6
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A"), lr.eng_run("A", iteration=2)]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["counters"]["verify"], it["lastFailure"]["reason"], it["lastFailure"]["failedAcs"]), ("merged", 1, "tests_failed", ["AC-A-1"]))
        self.assertEqual(it["ac"]["AC-A-1"]["g6"]["result"], "pass")                       # the AC x evidence view has the latest word
        eng = lr.calls_for("backend-engineer")
        self.assertEqual(len(eng), 2)
        self.assertNotIn("REWORK", eng[0]["stdin"])
        for needle in ("REWORK round", "item/tests_failed", "AC-A-1", "test_home fails on AC-A-1", "higher iteration number"):
            self.assertIn(needle, eng[1]["stdin"])
        names = lr.stubs.names()
        self.assertEqual((names.count("g6"), names.count("g0"), names.count("g7")), (2, 1, 1))     # G0 starts an item once; the others run per round
        self.assertEqual(len(lr.calls_for("code-reviewer")), 1)                                  # the reviewer saw only the passing round

    def test_a_blocking_g5_stops_the_other_gates_and_is_a_reward_hack_discard_to_a_fresh_attempt(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        flip = []

        def g5(ctx):
            flip.append(1)
            return GateResult.failed([{"id": "g5.protected-test", "severity": "error", "message": "tests/test_home.py was edited"}]) if len(flip) == 1 else GateResult.passed()
        lr.stubs.results["g5"] = g5
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A"), lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.stubs.names()[:3], ["g0", "g5", "g0"])                          # g5 blocked: g1..g11 did not run; attempt 2 started from G0
        it = lr.item("A")
        self.assertEqual((it["status"], it["attempt"], it["counters"]["discards"], it["deadBranches"]), ("merged", 2, 1, ["dead/slice-a-a1"]))
        self.assertEqual(lr.branch_names(), ["dead/slice-a-a1", "main"])
        self.assertIn("feat/slice-a-a2", lr.repo.git("log", "--merges", "--format=%s", "main"))
        self.assertEqual(len(lr.calls_for("backend-engineer")), 2)
        dead = lr.repo.git("ls-tree", "-r", "--name-only", "dead/slice-a-a1").splitlines()
        self.assertIn("src/a.py", dead)                                                      # the discarded attempt is kept for inspection, not merged
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")

    def test_a_second_discard_is_a_hard_stop_that_trips_the_breaker_and_ends_the_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building", "verifying")
        lr.repo.commit_file("src/a.py", "x = 1\n", "work")                                    # something on the branch, so the discard has a branch to keep
        d = drv.fail("A", "item", "reward_hack", (), "verifying", "pinned test edited")
        self.assertEqual(d.action, "discard")
        self.assertEqual((lr.item("A")["status"], lr.item("A")["attempt"]), ("pending", 2))
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertEqual(lr.branch_names(), ["dead/slice-a-a1", "main"])
        drv.begin("A")
        lr.walk("A", "design_review", "building", "verifying")
        lr.repo.commit_file("src/a2.py", "x = 2\n", "work again")
        d = drv.fail("A", "item", "reward_hack", (), "verifying", "pinned test edited again")
        self.assertEqual(d.action, "hard_stop")
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["attempt"], it["counters"]["discards"]), ("blocked", "retries_exhausted", 2, 1))
        st = lr.state()
        self.assertEqual((st.header["breaker"], st.header["runStatus"]), (1, "parked"))
        self.assertEqual(lr.item("B")["status"], "pending")                                  # park and stop: B was not started
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a2", "dead/slice-a-a1", "main"])
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")


if __name__ == "__main__":
    unittest.main()
