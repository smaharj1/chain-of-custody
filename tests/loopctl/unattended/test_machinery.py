"""The supervisor's rules one step at a time (no child is started), and the external watchdog (report 6.3, 10.4; IMP-25)."""
from __future__ import annotations

import json
import os
import re
import time
import unittest

from loopctl import driver, launch, unattended

from . import UnattendedTestCase

SAFE = re.compile(r"^[A-Za-z0-9 .,:_()/#@%+=-]*\Z")


class Machinery(UnattendedTestCase):
    """The supervisor's rules, one driver step at a time (no child is started)."""

    def stopped(self, drv):
        with self.assertRaises(driver._Stop) as cm:
            drv.check_stop()
        return cm.exception

    def test_a_security_block_demotes_notifies_with_the_item_and_blocks_the_next_unattended_start_until_a_human_lifts_it(self):
        lr = self.un()
        drv = lr.driver_at("A", "design_review", "building", "verifying", "reviewing")
        drv.park("A", "security_blocked", "the security-reviewer blocked the item")
        stop = self.stopped(drv)
        self.assertEqual((stop.outcome, stop.code), ("demoted", 1))
        drv.outcome = stop.outcome
        drv.supervisor.finish(stop.code)
        dem = lr.state().header["demotion"]
        self.assertEqual((dem["to"], dem["reason"], dem["item"]), ("L3", "security_block", "A"))
        m = lr.notifier.calls()[-1]["msg"]
        self.assertEqual((m["kind"], m["level"], m["item"], m["code"], m["outcome"]), ("demoted", "alert", "A", "security_blocked", "demoted"))
        self.assertIn("approve demotion", m["body"])
        self.assertEqual(lr.notifier.kinds().count("demoted"), 1)                              # one notification for the whole stop
        r = lr.ctl("unattended", "check")
        self.assertEqual(r.rc, 1)
        self.assertIn("demoted to L3 (security_block)", r.out)
        self.assertEqual(lr.ctl("approve", "demotion", "--by", "Pat").rc, 0)
        self.assertIsNone(lr.state().header.get("demotion"))
        self.assertEqual(lr.ctl("approve", "demotion", "--by", "Pat").rc, 3)                   # nothing left to approve

    def test_a_test_integrity_flag_and_a_stall_storm_demote_and_the_demotion_is_written_once(self):
        lr = self.un(cfg={"stall_storm_n": "2"})
        drv = lr.driver_at("A", "design_review", "building")
        drv.fail("A", "transient", "stall", stage="building", backoff=False)
        self.assertIsNone(drv.supervisor.pending)                                              # one stall is not a storm
        drv.fail("A", "transient", "stall", stage="building", backoff=False)
        self.assertEqual(drv.supervisor.pending[0], "stall_storm")
        drv.fail("A", "item", "reward_hack", stage="verifying", backoff=False)
        self.assertEqual(drv.supervisor.pending[0], "stall_storm")                              # the first cause is kept
        self.assertEqual(self.stopped(drv).outcome, "demoted")
        self.assertEqual(lr.state().header["demotion"]["reason"], "stall_storm")
        self.assertEqual(len([e for e in lr.events() if e.get("event") == "demoted"]), 1)
        lr2 = self.un()
        d2 = lr2.driver_at("A", "design_review", "building")
        d2.fail("A", "item", "reward_hack", stage="verifying", backoff=False)
        self.assertEqual(self.stopped(d2).outcome, "demoted")
        self.assertEqual(lr2.state().header["demotion"]["reason"], "integrity_flag")

    def test_an_item_waiting_for_a_human_is_reported_as_waiting_and_is_not_a_start_until_the_human_has_approved_it(self):
        lr = self.un()
        drv = lr.driver_at("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        drv.outcome = "awaiting_human"
        drv.supervisor.finish(0)
        m = lr.notifier.calls()[-1]["msg"]
        self.assertEqual((m["kind"], m["level"], m["item"]), ("waiting", "action", "A"))
        lr.loop().ctx.lock().release(lr.session)
        r = lr.ctl("unattended", "check")
        self.assertEqual(r.rc, 1)
        self.assertIn("unattended.work", r.out)
        self.assertIn("A waits for a human", r.out)
        self.assertEqual(lr.ctl("run").rc, 3)
        self.assertEqual(lr.ctl("approve", "item", "A", "--by", "Pat").rc, 0)
        self.assertEqual(lr.ctl("unattended", "check", "--quiet").rc, 0)                        # event resume: the human approved, a scheduler may start the run

    def test_a_killed_child_is_priced_only_when_the_run_has_measured_one_and_never_when_the_result_was_odd(self):
        lr = self.un()
        sup = lr.driver_at("A", begin=False).supervisor

        def res(parsed, duration, reason=None):
            r = launch.ChildResult()
            r.parsed, r.duration_s, r.reason = parsed, duration, reason
            return r
        self.assertIsNone(sup.child_cost(res(None, 5.0, "stall"), 3.0))                        # nothing measured: unknown, so the run parks
        self.assertIsNone(sup.child_cost(res({"result": "x", "total_cost_usd": 0.5}, 10.0), 3.0))   # a result with a cost: recorded as it is, and measured
        self.assertIsNone(sup.child_cost(res({"result": "x", "cost": 0.5}, 10.0), 3.0))        # a result without a cost field: left alone, it parks
        self.assertIsNone(sup.child_cost(res({"total_cost_usd": -1}, 10.0), 3.0))              # a hostile cost is not measured and not priced
        self.assertEqual(sup.measured, [(10.0, 0.5)])
        out = sup.child_cost(res(None, 20.0, "stall"), 100.0)
        self.assertEqual(out, ({"total_cost_usd": 1.25}, "estimated"))                          # 0.05 $/s x 20 s x 1.25
        self.assertEqual(sup.child_cost(res(None, 20.0, "timeout"), 0.5), ({"total_cost_usd": 0.5}, "estimated"))   # capped by the allowance
        for why in ("halt", "lock_lost", "interrupted"):
            self.assertIsNone(sup.child_cost(res(None, 20.0, why), 100.0), why)                # the run is ending anyway: stays unknown
        self.assertAlmostEqual(sup.estimated_usd, 1.75)

    def test_stop_reasons_come_in_order_and_only_once(self):
        lr = self.un()
        sup = lr.driver_at("A", begin=False).supervisor
        self.assertIsNone(sup.stop_reason())
        sup.start_failed = "channel down"
        self.assertEqual(sup.stop_reason()[0], "notify_failed")
        self.assertEqual(sup.stop_reason(), None)                                               # reported once


class Watch(UnattendedTestCase):
    """The external watchdog: what no thread inside the driver can see."""

    def record(self, lr, **kw):
        rec = {"schema": unattended.SCHEMA, "runId": "run-1", "pid": os.getpid(), "host": __import__("socket").gethostname(), "state": "running",
               "lastBeat": time.time(), "item": "A", "stallRunS": 1800}
        rec.update(kw)
        lr.rs.ensure()
        with open(unattended.progress_path(lr.rs), "w") as fh:
            json.dump(rec, fh)

    def dead_pid(self):
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_a_driver_that_died_while_its_run_is_recorded_as_running_is_reported_once(self):
        lr = self.un()
        self.record(lr, pid=self.dead_pid())
        r = lr.ctl("unattended", "watch", "--json")
        self.assertEqual(r.rc, 1, r)
        self.assertEqual((r.json["status"], r.json["notified"]), ("dead", True))
        m = lr.notifier.calls()[0]["msg"]
        self.assertEqual((m["kind"], m["level"], m["item"], m["outcome"]), ("watchdog", "alert", "A", "dead"))
        again = lr.ctl("unattended", "watch", "--json")
        self.assertEqual((again.json["status"], again.json["notified"]), ("dead", False))       # told once, not every five minutes
        self.assertEqual(len(lr.notifier.calls()), 1)

    def test_a_broken_channel_is_tried_again_at_the_next_check_and_a_silent_driver_is_a_stall(self):
        lr = self.un()
        self.record(lr, lastBeat=time.time() - 4000)
        lr.notifier.behave("fail")
        first = lr.ctl("unattended", "watch", "--json")
        self.assertEqual((first.json["status"], first.json["notified"]), ("stalled", False))
        lr.notifier.behave("ok")
        second = lr.ctl("unattended", "watch", "--json")
        self.assertEqual((second.json["status"], second.json["notified"]), ("stalled", True))
        self.assertEqual(len(lr.notifier.calls()), 2)

    def test_a_healthy_ended_or_halted_run_is_quiet(self):
        lr = self.un()
        for kw in ({}, {"state": "ended"}):
            self.record(lr, **kw)
            self.assertEqual(lr.ctl("unattended", "watch").rc, 0)
        self.record(lr)
        lr.rs.set_halt("a human stopped it", "Pat")
        self.assertEqual(lr.ctl("unattended", "watch", "--json").json["status"], "halted")
        self.assertEqual(lr.notifier.calls(), [])
        self.assertEqual(lr.ctl("unattended", "watch").rc, 0)

    def test_assess_is_a_pure_table(self):
        now, host = 10000.0, "h1"
        rec = {"state": "running", "host": host, "pid": 7, "lastBeat": now - 10}
        alive = lambda p: True  # noqa: E731
        dead = lambda p: False  # noqa: E731
        self.assertEqual(unattended.assess(rec, now, host, alive, 600)["status"], "ok")
        self.assertEqual(unattended.assess(rec, now, host, dead, 600)["status"], "dead")
        self.assertEqual(unattended.assess(dict(rec, lastBeat=now - 601), now, host, alive, 600)["status"], "stalled")
        self.assertEqual(unattended.assess(dict(rec, host="other", lastBeat=now - 601), now, host, dead, 600)["status"], "unknown_host")
        self.assertEqual(unattended.assess(dict(rec, host="other"), now, host, dead, 600)["status"], "ok")      # another machine's pid says nothing
        self.assertEqual(unattended.assess(None, now, host, alive, 600)["status"], "idle")
        self.assertEqual(unattended.assess(dict(rec, state="ended"), now, host, alive, 600)["status"], "idle")
        self.assertEqual(unattended.assess(rec, now, host, alive, 600, halted=True)["status"], "halted")
        for junk in ("x", None, True, [1], float("nan")):                                         # a record without a usable beat is silent, never a crash
            res = unattended.assess(dict(rec, lastBeat=junk), now, host, alive, 600)
            self.assertIn(res["status"], ("stalled", "ok"), junk)
            json.dumps(res)
        self.assertEqual(unattended.assess(dict(rec, lastBeat="x"), now, host, dead, 600)["status"], "dead")

    def test_the_estimate_for_a_killed_child_is_measured_capped_and_never_invented(self):
        est = unattended.estimate_killed_cost
        self.assertIsNone(est([], 100, 5))                                                      # nothing measured: unknown, so the run parks
        self.assertIsNone(est([(0.001, 5.0)], 100, 5))                                          # a measurement too short to give a rate
        self.assertEqual(est([(10, 1.0), (10, 0.2)], 20, 100), 2.5)                             # the highest rate (0.1/s) x 20 s x 1.25
        self.assertEqual(est([(10, 1.0)], 1000, 3), 3.0)                                        # never more than the child's own allowance
        self.assertIsNone(est([(10, 1.0)], 0, 3))
        self.assertIsNone(est([(10, 1.0)], 10, 0))


if __name__ == "__main__":
    unittest.main()
