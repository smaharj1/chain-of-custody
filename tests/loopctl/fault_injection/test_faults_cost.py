"""Spend: an unreadable cost parks, ceilings park, 80% of the run budget parks, and a human's `approve cost` lets the run go on
(report 10.5, CS-26, CO-7; IMP-25; FI-07 malformed JSON, FI-08 budget exceeded).
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Cost(DriverTestCase):
    def ledger(self, lr):
        return lr.context().ledger()

    def run_id(self, lr):
        return lr.rs.read_run()["runId"]

    def test_malformed_json_is_a_cli_failure_and_its_unknown_cost_parks_until_a_human_accepts_it(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "malformed"}, lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["lastFailure"]["reason"]), ("needs_human", "cost_unknown", "cli_crash"))
        self.assertEqual(it["counters"]["platform"], 1)
        t = self.ledger(lr).totals(run_id=self.run_id(lr))
        self.assertEqual((t["usd"], t["unknown"]), (0.0, 1))
        self.assertEqual(self.slept, [])                                                      # no point sleeping before a park
        self.assertEqual(len(lr.calls()), 1)                                                  # and nothing more was launched
        st = lr.ctl("status").out
        self.assertIn("1 unknown entry", st)
        # the human looks, accepts the unknown spend, and unblocks the item: the run goes on
        self.assertEqual(lr.run().rc, 1)                                                      # not before: the next start still sees the parked item
        self.assertEqual(lr.ctl("approve", "cost", "--by", "Pat", "--note", "checked the console").rc, 0)
        self.assertEqual(lr.ctl("approve", "unblock", "A", "--by", "Pat", "--choice", "reset").rc, 0)
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "merged")
        self.assertEqual(lr.item("A")["attempt"], 1)
        self.assertEqual(len(self.slept), 1)                                                  # the platform backoff recorded before the park is waited out, not skipped
        self.assertGreater(self.slept[0], 1)

    def test_a_cost_field_that_was_renamed_parks_even_though_the_child_succeeded(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [dict(lr.eng_run("A"), cost_field="totalCostUsd")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "cost_unknown"))
        self.assertIn("no total_cost_usd field", [e for e in self.ledger(lr).entries()[0]][0]["why"])
        self.assertEqual([c["role"] for c in lr.calls()], ["backend-engineer"])               # the reviewer was never started on work whose cost nobody knows

    def test_a_child_that_overshoots_the_run_budget_parks_the_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)], cfg={"run_budget_usd": "0.2"})
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A", cost=0.5)]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "budget_run"))
        self.assertAlmostEqual(self.ledger(lr).totals(run_id=self.run_id(lr))["usd"], 0.5)
        self.assertEqual(lr.calls()[0]["argv"][lr.calls()[0]["argv"].index("--max-budget-usd") + 1], "0.2000")
        self.assertEqual(len(lr.calls()), 1)
        self.assertEqual(lr.item("B")["status"], "pending")
        self.assertEqual(lr.run().rc, 1)                                                      # and a re-run does not launch anything: the budget is still spent
        self.assertEqual(len(lr.calls()), 1)

    def test_eighty_percent_of_the_run_budget_parks_until_a_human_accepts_the_rest(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "1.0"})
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A", cost=0.85)]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "budget_run_80"))
        self.assertIn("80%", r.out)
        lr.ctl("approve", "cost", "--by", "Pat")
        lr.ctl("approve", "unblock", "A", "--by", "Pat", "--choice", "reset")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A", cost=0.05)]}))
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "merged")
        budgets = [float(c["argv"][c["argv"].index("--max-budget-usd") + 1]) for c in lr.calls()]
        self.assertAlmostEqual(budgets[0], 1.0, places=3)
        self.assertAlmostEqual(budgets[1], 1.0, places=3)                                     # a restart is a new run: run_budget_usd is per `loopctl run` (day_budget_usd spans runs)

    def test_a_child_that_runs_out_of_its_own_budget_parks_the_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "0.3"})
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "budget_exceeded", "total_cost_usd": 0.3}]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(lr.item("A")["blockReason"], "budget_run")
        self.assertEqual(self.slept, [])

    def test_no_ceiling_at_all_refuses_to_start_a_driver_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"run_budget_usd": "0"})
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertIn("run_budget_usd is not set", r.err)
        self.assertEqual(lr.calls(), [])


if __name__ == "__main__":
    unittest.main()
