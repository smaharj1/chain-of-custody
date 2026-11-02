"""Discard paths beyond the reward hack: no progress (the same failing criteria twice), an exhausted retry budget, and the worst class winning a
round (report 10.4, RC-19, L-8; FI-16).
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl.gates import GateResult

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Discard(DriverTestCase):
    def test_the_same_failing_criteria_twice_is_no_progress_and_discards(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        seen = []

        def g3(ctx):
            seen.append(ctx.attempt)
            if len(seen) <= 2:
                return GateResult.failed([{"id": "g3.ac", "severity": "error", "message": "AC-A-2 still fails"}], ac_results={"AC-A-2": {"result": "fail", "evidence": "t"}})
            return GateResult.passed()
        lr.stubs.results["g3"] = g3
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["attempt"]), ("merged", 2))
        self.assertEqual(seen, [1, 1, 2])                                                    # failed twice in attempt 1 (same AC), then a fresh attempt
        self.assertEqual(len(lr.calls_for("backend-engineer")), 3)

    def test_a_blocking_gate_of_a_worse_class_wins_the_round(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building", "verifying")
        results = [("g3", {"status": "fail", "mode": "on", "blocking": True, "failure_class": "item", "findings": [{"id": "g3.x", "message": "an AC failed"}], "evidence": {}}),
                   ("g9", {"status": "error", "mode": "on", "blocking": True, "failure_class": "env", "findings": [{"id": "gate.error", "message": "registry unreachable"}], "evidence": {}}),
                   ("g11", {"status": "pass", "mode": "on", "blocking": False, "failure_class": "none", "findings": [], "evidence": {}})]
        self.assertTrue(drv.gate_failure("A", results, "verifying"))
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["lastFailure"]["class"]), ("blocked", "env:doctor_failed", "env"))     # one failure, the worst class
        self.assertEqual(it["counters"]["verify"], 0)                                          # an env failure uses no retry budget
        self.assertFalse(drv.gate_failure("A", results[2:], "verifying"))                       # a round with nothing blocking records nothing


if __name__ == "__main__":
    unittest.main()
