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
    def test_overload_rate_limit_an_unreadable_cli_error_and_a_turn_limit_are_each_classified_and_retried(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [
            OVERLOADED, {"behavior": "rate_limited", "total_cost_usd": 0.0}, {"behavior": "error", "result": "Segmentation weirdness: ???", "total_cost_usd": 0.0},
            {"behavior": "max_turns", "total_cost_usd": 0.02}, lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        self.assertEqual(it["status"], "merged")
        c = it["counters"]
        self.assertEqual((c["platform"], c["partial"], c["verify"], c["transient"], lr.state().header["breaker"]), (3, 1, 0, 0, 0))     # independent counters (L-8); no retry budget used
        events = [(e["failure_class"], e["reason"]) for e in lr.rs.read_events("loopctl") if e.get("event") == "fail" and e.get("source") == "driver"]
        self.assertEqual(events, [("platform", "overloaded"), ("platform", "rate_limit"), ("platform", "cli_crash"), ("item", "partial")])
        self.assertEqual(self.slept, [failure.backoff_s(30, 1, "A:1"), failure.backoff_s(30, 2, "A:1"), failure.backoff_s(30, 3, "A:1")])   # jittered, deterministic, growing
        self.assertGreaterEqual(self.slept[0], 15)
        self.assertLessEqual(self.slept[0], 30)
        eng = lr.calls_for("backend-engineer")
        self.assertEqual(len(eng), 5)
        for n in (1, 2, 3):
            self.assertNotIn("REWORK", eng[n]["stdin"])                                       # a platform failure re-runs the same prompt: nothing for the engineer to change
        self.assertIn("REWORK", eng[4]["stdin"])                                              # the turn limit is a PARTIAL: continue with what is left
        self.assertEqual(lr.stubs.names().count("g0"), 1)                                     # the same item, the same branch
        self.assertEqual(it["attempt"], 1)
        self.assertAlmostEqual(lr.context().ledger().totals(run_id=lr.rs.read_run()["runId"])["usd"], 0.10)

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


if __name__ == "__main__":
    unittest.main()
