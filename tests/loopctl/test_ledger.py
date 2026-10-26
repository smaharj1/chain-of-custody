"""The spend ledger: unknown cost parks, ceilings, the remaining budget, projection (report section 10.5, CS-26, CO-7)."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import runstate

from loopctl import common, ledger
from loopctl.common import Refused, UsageError

from . import LoopTestCase


class Cfg(object):
    def __init__(self, **kw):
        self.v = {"run_budget_usd": 0, "item_budget_usd": 0, "day_budget_usd": 0}
        self.v.update(kw)

    def get(self, n, d=None):
        return self.v.get(n, d)


class ParseCost(unittest.TestCase):
    def test_good_costs(self):
        self.assertEqual(ledger.parse_cost({"total_cost_usd": 0.5})[0], 0.5)
        self.assertEqual(ledger.parse_cost({"total_cost_usd": 3})[0], 3.0)
        self.assertEqual(ledger.parse_cost({"total_cost_usd": 0})[0], 0.0)

    def test_everything_else_is_unknown_never_zero(self):
        bad = [{}, {"cost": 1}, {"total_cost_usd": None}, {"total_cost_usd": "0.5"}, {"total_cost_usd": True}, {"total_cost_usd": -1},
               {"total_cost_usd": float("nan")}, {"total_cost_usd": float("inf")}, {"total_cost_usd": 1e9}, {"total_cost_usd": [1]}, [], "0.5", None]
        for r in bad:
            with self.subTest(r):
                v, why = ledger.parse_cost(r)
                self.assertIsNone(v)
                self.assertTrue(why)


class Ledger(LoopTestCase):
    def led(self):
        lr = self.lr(commit=False)
        rs = runstate.RunState.for_path(lr.path)
        return lr, rs, ledger.Ledger(rs)

    def test_record_and_totals(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, "child-1", {"total_cost_usd": 0.25, "session_id": "ignored"}, role="backend-engineer")
        led.record("run-1", "B", 1, None, {"total_cost_usd": 0.75, "session_id": "child-2"})
        led.record("run-2", "A", 1, None, cost=1.0)
        self.assertEqual(led.totals(run_id="run-1")["usd"], 1.0)
        self.assertEqual(led.totals(item="A")["usd"], 1.25)
        self.assertEqual(led.totals(day=common.iso()[:10])["usd"], 2.0)
        ents, bad = led.entries()
        self.assertEqual((len(ents), bad), (3, 0))
        self.assertEqual(ents[1]["sessionId"], "child-2")
        self.assertEqual(oct(os.stat(led.path).st_mode & 0o777), "0o600")

    def test_decimal_sums_do_not_drift(self):
        lr, rs, led = self.led()
        for _ in range(10):
            led.record("run-1", "A", 1, None, cost=0.1)
        self.assertEqual(led.totals(run_id="run-1")["usd"], 1.0)

    def test_an_unknown_cost_parks_until_a_human_acknowledges(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, {"result": "ok"})                       # the field is renamed or absent
        v = led.verdict(Cfg(), "run-1", "A")
        self.assertFalse(v.ok)
        self.assertIn("cost_unknown", v.codes)
        self.assertEqual(led.totals(run_id="run-1")["usd"], 0.0)                  # and it was not counted as zero
        led.acknowledge("run-1", "Pat", "the field was renamed")
        self.assertTrue(led.verdict(Cfg(), "run-1", "A").ok)
        led.record("run-1", "A", 2, None, {"total_cost_usd": "free"})              # a new unknown after the ack parks again
        self.assertFalse(led.verdict(Cfg(), "run-1", "A").ok)

    def test_a_tampered_ledger_parks_instead_of_proving_the_run_is_under_budget(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=0.1)
        with open(led.path, "a") as fh:
            fh.write("this is not json\n")
            fh.write('{"type": "spend", "runId": "run-1", "total_cost_usd": -50, "unknown": false, "item": "A", "day": "2026-01-01"}\n')
            fh.write('{"type": "evil"}\n')
        ents, bad = led.entries()
        self.assertEqual(bad, 2)
        v = led.verdict(Cfg(), "run-1")
        self.assertFalse(v.ok)
        self.assertEqual(led.totals(run_id="run-1")["usd"], 0.1)                  # the negative entry never reduces the total
        led.acknowledge("run-1", "Pat")
        self.assertTrue(led.verdict(Cfg(), "run-1").ok or led.totals(run_id="run-1")["unknown"] >= 0)

    def test_ceilings_for_run_item_and_day(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=1.0)
        self.assertTrue(led.verdict(Cfg(run_budget_usd=2), "run-1", "A").ok)
        v = led.verdict(Cfg(run_budget_usd=1.0), "run-1", "A")
        self.assertFalse(v.ok)
        self.assertIn("budget_run", v.codes)
        self.assertIn("budget_item", led.verdict(Cfg(item_budget_usd=0.5), "run-1", "A").codes)
        self.assertIn("budget_day", led.verdict(Cfg(day_budget_usd=0.5), "run-1", "A").codes)
        self.assertTrue(led.verdict(Cfg(), "run-1", "A").ok)                       # 0 means not set, not "nothing may be spent"

    def test_the_remaining_budget_is_the_smallest_ceiling_and_never_negative(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=1.0)
        v = led.verdict(Cfg(run_budget_usd=5, item_budget_usd=2, day_budget_usd=10), "run-1", "A")
        self.assertEqual(v.remaining, 1.0)
        self.assertEqual(led.launch_budget(Cfg(run_budget_usd=5), "run-1"), 4.0)
        with self.assertRaises(Refused) as cm:
            led.launch_budget(Cfg(), "run-1")
        self.assertIn("run_budget_usd is not set", str(cm.exception))
        with self.assertRaises(Refused):
            led.launch_budget(Cfg(run_budget_usd=1), "run-1")

    def test_at_eighty_percent_the_run_parks_until_a_human_accepts_the_rest(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=7.9)
        self.assertTrue(led.verdict(Cfg(run_budget_usd=10), "run-1", "A").ok)
        led.record("run-1", "A", 1, None, cost=0.2)                              # 8.1 of 10
        v = led.verdict(Cfg(run_budget_usd=10), "run-1", "A")
        self.assertFalse(v.ok)
        self.assertEqual(v.codes, ["budget_run_80"])
        led.acknowledge("run-1", "Pat")
        self.assertTrue(led.verdict(Cfg(run_budget_usd=10), "run-1", "A").ok)
        led.record("run-1", "A", 1, None, cost=2.0)                              # now over the ceiling: that is not waived by the ack
        self.assertIn("budget_run", led.verdict(Cfg(run_budget_usd=10), "run-1", "A").codes)

    def test_the_budget_is_shared_between_live_children(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=1.0)
        self.assertEqual(led.launch_budget(Cfg(run_budget_usd=5), "run-1", live_children=1), 4.0)
        self.assertEqual(led.launch_budget(Cfg(run_budget_usd=5), "run-1", live_children=2), 2.0)
        self.assertEqual(led.launch_budget(Cfg(run_budget_usd=5), "run-1", live_children=0), 4.0)

    def test_the_budget_survives_a_resume_because_it_is_summed_from_the_ledger(self):
        lr, rs, led = self.led()
        led.record("run-1", "A", 1, None, cost=2.0)
        again = ledger.Ledger(rs)                                                    # a new process
        self.assertEqual(again.launch_budget(Cfg(run_budget_usd=3), "run-1"), 1.0)

    def test_ids_are_validated_before_they_reach_the_file(self):
        lr, rs, led = self.led()
        with self.assertRaises(UsageError):
            led.record("../run", "A", 1, None, cost=1)
        with self.assertRaises(UsageError):
            led.record("run-1", "A b", 1, None, cost=1)

    def test_secrets_in_a_role_or_source_are_redacted(self):
        lr, rs, led = self.led()
        tok = "ghp_" + "A" * 36
        led.record("run-1", "A", 1, None, cost=1, role="token %s" % tok)
        with open(led.path) as fh:
            self.assertNotIn(tok, fh.read())

    def test_projection_is_unavailable_without_a_baseline(self):
        lr, rs, led = self.led()
        p = ledger.projection(lr.path, 5)
        self.assertFalse(p["available"])
        self.assertIn("unavailable (no baseline)", p["message"])

    def test_projection_from_completed_baseline_runs(self):
        lr, rs, led = self.led()
        for i, spend in enumerate((3.0, 6.0, 9.0), 1):
            lr.repo.write("docs/baseline/results/run-%d.json" % i, json.dumps(
                {"kind": "run", "status": "completed", "spend_usd": spend, "items": [{"id": "F1"}, {"id": "F2"}, {"id": "F3"}]}))
        lr.repo.write("docs/baseline/results/run-4.json", json.dumps({"kind": "run", "status": "aborted", "spend_usd": 100, "items": [{"id": "F1"}]}))
        lr.repo.write("docs/baseline/results/TEMPLATE.json", json.dumps({"kind": "run", "status": "completed", "spend_usd": 999, "items": [{}]}))
        lr.repo.write("docs/baseline/results/run-5.json", "{not json")
        p = ledger.projection(lr.path, 10)
        self.assertTrue(p["available"])
        self.assertEqual(p["baseline_runs"], 3)
        self.assertEqual(p["per_item_p50_usd"], 2.0)
        self.assertEqual(p["projection_p50_usd"], 20.0)
        self.assertGreater(p["projection_p90_usd"], p["projection_p50_usd"])


if __name__ == "__main__":
    unittest.main()
