"""Stalls, a killed child's cost, a dead channel, a revert: the stops that must not be silent (report 6.3, 10.4, 15 item 5; IMP-25).
"""
from __future__ import annotations

import unittest

from loopctl import unattended
from loopctl.gates import GateResult

from . import ENG, UnattendedTestCase, item_doc


class Stalls(UnattendedTestCase):
    """The real driver, the fake claude and the fake notifier."""

    def test_a_stalled_child_after_a_measured_one_is_priced_from_the_run_and_a_storm_demotes(self):
        lr = self.un(timing={"stall_s": 0.5}, cfg={"stall_storm_n": "1"})
        lr.set_scenario(lr.happy(by_role={"code-reviewer": [{"behavior": "stall", "seconds": 30}]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        entries, _bad = lr.context().ledger().entries()
        est = [e for e in entries if e.get("source") == "estimated"]
        self.assertEqual(len(est), 1)
        self.assertFalse(est[0]["unknown"])
        self.assertTrue(0 < est[0]["total_cost_usd"] <= 10.0)                                    # priced from the engineer, never beyond the child's allowance
        self.assertEqual(lr.context().ledger().totals(run_id=lr.rs.read_run()["runId"])["unknown"], 0)    # so the run did not park on cost
        self.assertEqual(lr.state().header["demotion"]["reason"], "stall_storm")
        m = lr.notifier.calls()[-1]["msg"]
        self.assertEqual((m["kind"], m["outcome"], m["level"]), ("demoted", "demoted", "alert"))
        self.assertIn("stalls 1", m["body"])
        self.assertIn("estimated cost", m["body"])
        self.assertIn("cost_estimated", [e["event"] for e in lr.events()])
        self.assertEqual(lr.ctl("unattended", "check", "--quiet").rc, 1)                         # and the next unattended start is refused until a human lifts it

    def test_a_stalled_child_with_nothing_measured_yet_parks_and_a_dead_channel_leaves_the_closing_message_for_the_watchdog(self):
        lr = self.un(timing={"stall_s": 0.5})
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "stall", "seconds": 30}, lr.eng_run("A")]}))

        def go_down(ctx):
            lr.notifier.behave("fail")                                                          # the channel dies after the start message went out
            return GateResult.passed(note="stub")
        lr.stubs.results["g0"] = go_down
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)                                                            # the exit code is the stop's, not the channel's
        entries, _ = lr.context().ledger().entries()
        self.assertTrue(all(e.get("source") != "estimated" for e in entries))                   # nothing measured: never a guess from nothing
        ev = lr.events("notify")
        self.assertEqual([(e["kind"], e["ok"], e["code"], e["item"]) for e in ev], [("start", True, None, None), ("stop", False, "cost_unknown", "A")])
        self.assertIn("exit 3", ev[1]["why"])
        st = lr.ctl("unattended", "status", "--json").json
        self.assertFalse(st["last_notifications"][-1]["ok"])                                    # `unattended status` shows it
        self.assertIsNone(lr.state().header.get("demotion"))
        prog = unattended.read_progress(lr.rs)
        self.assertEqual((prog["state"], prog["unsent"]["kind"], prog["unsent"]["code"]), ("ended", "stop", "cost_unknown"))    # the closing message is kept
        lr.notifier.behave("fail")
        still = lr.ctl("unattended", "watch", "--json")                                           # the watchdog tries; the channel is still down: visible, exit 1
        self.assertEqual((still.rc, still.json["status"], still.json["notified"]), (1, "unsent", False))
        lr.notifier.behave("ok")
        sent = lr.ctl("unattended", "watch", "--json")
        self.assertEqual((sent.rc, sent.json["notified"], sent.json.get("resent")), (0, True, True))
        m = lr.notifier.calls()[-1]["msg"]
        self.assertEqual((m["kind"], m["code"], m["item"], m["run"]), ("stop", "cost_unknown", "A", prog["runId"]))
        self.assertIn("approve cost", m["body"])
        calls = len(lr.notifier.calls())
        self.assertEqual(lr.ctl("unattended", "watch", "--json").json["notified"], False)         # delivered once, not every five minutes
        self.assertEqual(len(lr.notifier.calls()), calls)

    def test_a_human_revert_of_a_merged_item_demotes_the_next_unattended_run_before_it_starts_anything(self):
        lr = self.un(items=[item_doc("A", estimate=ENG), item_doc("C", estimate=ENG, slug="slice-c")])
        lr.fast_start()
        mc = lr.complete("A")                                                                   # A merged by the state machine, a real --no-ff merge
        lr.loop().ctx.lock().release(lr.session)
        lr.repo.git("revert", "-m", "1", "--no-edit", mc)                                      # a human reverts A's merge on main
        lr.set_scenario(lr.happy(["C"]))
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        self.assertEqual(lr.state().header["demotion"]["reason"], "revert")
        self.assertEqual(lr.calls(), [])                                                       # recovery found it; nothing was launched
        self.assertEqual(lr.notifier.calls()[-1]["msg"]["kind"], "demoted")
        self.assertEqual(lr.item("C")["status"], "pending")


if __name__ == "__main__":
    unittest.main()
