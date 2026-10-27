"""Platform faults: a 529 or an overload, a rate limit, a CLI that fails in a way nobody can read, a turn limit (report 10.4, CO-8; FI-05, FI-14).

A platform failure is not the item's fault: it is retried with backoff (jittered, slept interruptibly), it uses no retry budget and it never
trips the circuit breaker. Past `max_platform_retries` the run stops with `platform_outage`.
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import failure

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}
OVERLOADED = {"behavior": "overloaded", "total_cost_usd": 0.0}


class Platform(DriverTestCase):
    def test_a_529_is_a_platform_failure_retried_with_backoff_and_costs_no_retry_budget(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [OVERLOADED, lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        c = it["counters"]
        self.assertEqual((it["status"], c["platform"], c["verify"], c["transient"], lr.state().header["breaker"]), ("merged", 1, 0, 0, 0))
        self.assertEqual(it["lastFailure"]["class"], "platform")
        self.assertEqual(it["lastFailure"]["reason"], "overloaded")
        self.assertEqual(self.slept, [failure.backoff_s(30, 1, "A:1")])                    # one backoff, deterministic jitter
        self.assertGreaterEqual(self.slept[0], 15)
        self.assertLessEqual(self.slept[0], 30)
        self.assertEqual(len(lr.calls_for("backend-engineer")), 2)
        self.assertEqual(lr.stubs.names().count("g0"), 1)                                  # not a new attempt: the same item, the same branch
        self.assertEqual(lr.state().items["A"]["attempt"], 1)
        self.assertAlmostEqual(lr.context().ledger().totals(run_id=lr.rs.read_run()["runId"])["usd"], 0.08)

    def test_repeated_overload_is_an_outage_and_ends_the_run_with_the_item_parked(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [OVERLOADED]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["counters"]["platform"]), ("needs_human", "platform_outage", 7))        # the 7th exceeds max_platform_retries (6)
        self.assertEqual(len(self.slept), 6)                                                 # six backoffs were slept, the seventh failure parked
        self.assertEqual(self.slept, sorted(self.slept))                                      # and they grow
        self.assertEqual(len(lr.calls_for("backend-engineer")), 7)
        self.assertEqual((lr.state().header["breaker"], lr.item("B")["status"]), (0, "pending"))   # an outage does not trip the item breaker
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])

    def test_rate_limit_an_unreadable_cli_error_and_a_turn_limit_are_each_classified_and_retried(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [
            {"behavior": "rate_limited", "total_cost_usd": 0.0},
            {"behavior": "error", "result": "Segmentation weirdness: ???", "total_cost_usd": 0.0},
            {"behavior": "max_turns", "total_cost_usd": 0.02},
            lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        self.assertEqual(it["status"], "merged")
        self.assertEqual((it["counters"]["platform"], it["counters"]["partial"], it["counters"]["verify"]), (2, 1, 0))     # independent counters (L-8)
        events = [(e["failure_class"], e["reason"]) for e in lr.rs.read_events("loopctl") if e.get("event") == "fail" and e.get("source") == "driver"]
        self.assertEqual(events, [("platform", "rate_limit"), ("platform", "cli_crash"), ("item", "partial")])
        self.assertEqual(len(self.slept), 2)                                                 # backoff for the two platform failures, none for the item one
        self.assertIn("REWORK", lr.calls_for("backend-engineer")[3]["stdin"])                # the turn limit is a PARTIAL: continue with what is left


if __name__ == "__main__":
    unittest.main()
