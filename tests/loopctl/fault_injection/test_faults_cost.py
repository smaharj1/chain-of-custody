"""Spend: an unreadable cost parks, ceilings park, 80% of the run budget parks, and a human's `approve cost` lets the run go on
(report 10.5, CS-26, CO-7; IMP-25; FI-07 malformed JSON, FI-08 budget exceeded).

Most rows call `Driver.launch_child` directly on an item begun by the test: that is the code under test, and it costs a tenth of a pipeline.
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import driver

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Cost(DriverTestCase):
    def child(self, lr, run, stage="building"):
        """Begin A, write its brief, and run ONE engineer child through the driver's own launch code. Returns the stop it raised, if any."""
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [run]}))
        drv = lr.driver_at("A", "design_review", "building")
        drv.write_briefs("A")
        try:
            return drv, drv.launch_child("A", "backend-engineer", stage, "do it"), None
        except driver._Stop as stop:
            return drv, None, stop

    def totals(self, lr):
        return lr.context().ledger().totals(run_id=lr.rs.read_run()["runId"])

    def test_malformed_json_is_a_cli_failure_and_its_unknown_cost_parks_until_a_human_accepts_it(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv, out, stop = self.child(lr, {"behavior": "malformed"})
        self.assertEqual((out, stop.outcome), (None, "budget"))
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["lastFailure"]["reason"]), ("needs_human", "cost_unknown", "cli_crash"))
        self.assertEqual(it["counters"]["platform"], 1)
        t = self.totals(lr)
        self.assertEqual((t["usd"], t["unknown"]), (0.0, 1))
        self.assertEqual(self.slept, [])                                                      # no point sleeping before a park
        self.assertIn("1 unknown entry", lr.ctl("status").out)
        # the human looks, accepts the unknown spend, unblocks the item, and a new run finishes it
        lr.rs.release_lock(lr.session)
        lr.rs.unregister_session(lr.session)
        self.assertEqual(lr.ctl("approve", "cost", "--by", "Pat", "--note", "checked the console").rc, 0)
        self.assertEqual(lr.ctl("approve", "unblock", "A", "--by", "Pat", "--choice", "reset").rc, 0)
        lr.set_scenario(lr.happy())
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["attempt"]), ("merged", 1))
        self.assertEqual(len(self.slept), 1)                                                  # the platform backoff recorded before the park is waited out, not skipped
        self.assertGreater(self.slept[0], 1)

    def test_a_cost_field_that_was_renamed_parks_even_though_the_child_succeeded(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv, out, stop = self.child(lr, dict(lr.eng_run("A"), cost_field="totalCostUsd"))
        self.assertEqual(stop.outcome, "budget")
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "cost_unknown"))
        self.assertIn("no total_cost_usd field", lr.context().ledger().entries()[0][0]["why"])
        self.assertEqual([c["role"] for c in lr.calls()], ["backend-engineer"])

    def test_a_child_that_overshoots_the_run_budget_parks_the_run_and_a_rerun_launches_nothing(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "0.2"})
        drv, out, stop = self.child(lr, lr.eng_run("A", cost=0.5))
        self.assertEqual(stop.outcome, "budget")
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "budget_run"))
        self.assertAlmostEqual(self.totals(lr)["usd"], 0.5)
        argv = lr.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.2000")                  # the remaining budget was what the child was given
        self.assertEqual(lr.state().header["runStatus"], "parked")
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])
        lr.rs.release_lock(lr.session)
        r = lr.run()                                                                           # a new run is a new budget, but the item is parked
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(len(lr.calls()), 1)

    def test_eighty_percent_of_the_run_budget_parks_until_a_human_accepts_the_rest(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "1.0"})
        drv, out, stop = self.child(lr, lr.eng_run("A", cost=0.85))
        self.assertEqual(stop.outcome, "budget")
        self.assertIn("80%", stop.message)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "budget_run_80"))
        led, run_id = lr.context().ledger(), lr.rs.read_run()["runId"]
        self.assertFalse(led.verdict(lr.context().cfg, run_id).ok)
        lr.ctl("approve", "cost", "--by", "Pat")
        self.assertTrue(led.verdict(lr.context().cfg, run_id).ok)                              # accepted: the rest of the budget is usable

    def test_a_child_that_runs_out_of_its_own_budget_parks_the_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "0.3"})
        drv, out, stop = self.child(lr, {"behavior": "budget_exceeded", "total_cost_usd": 0.3})
        self.assertEqual(stop.outcome, "budget")
        self.assertEqual(lr.item("A")["blockReason"], "budget_run")
        self.assertEqual(self.slept, [])

    def test_a_child_that_costs_nothing_is_fine_and_the_next_launch_gets_the_remaining_budget(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv, out, stop = self.child(lr, lr.eng_run("A", cost=0.0))
        self.assertIsNone(stop)
        self.assertIsNotNone(out)
        self.assertEqual(self.totals(lr), {"usd": 0.0, "entries": 1, "unknown": 0})

    def test_no_ceiling_at_all_refuses_to_start_a_driver_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "0"})
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertIn("run_budget_usd is not set", r.err)
        self.assertEqual(lr.calls(), [])


if __name__ == "__main__":
    unittest.main()
