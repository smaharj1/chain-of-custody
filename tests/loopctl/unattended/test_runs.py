"""Unattended runs end to end: the closing message, the start that never waits, what plan text can and cannot reach (report 10.4, 15 item 5, 16.1 row 7;
DEC-31). The real driver, the fake `claude`, a fake `notify_command`.
"""
from __future__ import annotations

import json
import os
import re
import time
import unittest

from loopctl import driver, unattended
from loopctl.gates import GateResult

from . import ENG, UnattendedTestCase, item_doc

SAFE = re.compile(r"^[A-Za-z0-9 .,:_()/#@%+=-]*\Z")


class Runs(UnattendedTestCase):
    """The real driver, the fake claude and the fake notifier."""

    def test_a_finished_run_says_it_started_and_finished_and_nothing_else_reaches_the_channel(self):
        evil = "x'; touch PWNED #$(touch PWNED2)`touch PWNED3` \u202e\x1b[31m"
        lr = self.un(timing={"stall_run_s": 0.4}, cfg={"notify_env_allow": "[TOPIC, AWS_SECRET_ACCESS_KEY]"}, milestones=["M1", "M2"],
                     items=[item_doc("A", estimate=ENG, title=evil, intent=evil, slug="evil-a"), item_doc("B", ["A"], estimate=ENG, milestone="M2")])
        lr.env.update({"TOPIC": "ops", "AWS_SECRET_ACCESS_KEY": "hunter2", "GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x"})
        lr.set_scenario(lr.happy(["A", "B"]))

        hung = []

        def hung_gate(ctx):
            if not hung:                         # once (item A): the driver's own thread is blocked, with no tick, no event and no child to time out
                hung.append(1)
                time.sleep(1.2)
            return GateResult.passed(note="stub")
        lr.stubs.results["g0"] = hung_gate
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        calls = lr.notifier.calls()
        self.assertEqual([c["msg"]["kind"] for c in calls], ["start", "stall", "done"])          # the silent driver was noticed once, not once per check
        self.assertEqual((calls[1]["msg"]["level"], calls[1]["msg"]["outcome"]), ("alert", "driver_silent"))
        for c in calls:
            self.assertEqual(c["argv"], [])                                                      # the message is never on the command line
            self.assertEqual(c["env"]["KIT_NOTIFY_KIND"], c["msg"]["kind"])
            self.assertEqual(c["env"]["TOPIC"], "ops")                                           # an allowed name is passed
            for secret in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDECODE"):
                self.assertNotIn(secret, c["env_names"])                                         # credential families never are, even when asked for
            blob = c["stdin"] + json.dumps(c["env"])
            for needle in ("touch", "PWNED", "\\u202e", "\\u001b", "`", "$("):                  # plan text never travels: ids, codes and fixed sentences only
                self.assertNotIn(needle, blob)
            self.assertRegex(c["msg"]["body"], SAFE)
        for name in ("PWNED", "PWNED2", "PWNED3"):
            self.assertFalse(os.path.exists(os.path.join(lr.path, name)))
        done = calls[2]["msg"]
        self.assertEqual((done["level"], done["outcome"]), ("info", "complete"))
        self.assertIn("merged 2", done["body"])
        # the offline half of the Phase 7 exit criterion (three unattended fixture runs with zero control-flow rescues is pending-human): attended,
        # `per-milestone` would pause after A at the M1/M2 boundary; unattended does not, and nothing in the run asked for a human step.
        self.assertEqual((lr.item("A")["status"], lr.item("B")["status"]), ("merged", "merged"))
        self.assertNotIn("checkpoint_answered", [e["event"] for e in lr.events()])
        self.assertIsNone(lr.state().header["parked"])
        self.assertEqual([a for a in lr.state().d["approvals"] if a["scope"] != "plan"], [])
        prog = unattended.read_progress(lr.rs)
        self.assertEqual((prog["state"], prog["outcome"], prog["exit"]), ("ended", "complete", 0))
        self.assertEqual(unattended.assess(prog, time.time(), prog["host"], lambda p: True, 1800.0)["status"], "idle")
        self.assertEqual([e["kind"] for e in lr.events("notify")], ["start", "stall", "done"])

    def test_a_start_that_cannot_reach_you_never_starts_a_child(self):
        lr = self.un()
        lr.set_scenario(lr.happy())
        lr.approve()
        lr.notifier.behave("fail")
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertEqual(lr.calls(), [])
        self.assertEqual(lr.item("A")["status"], "pending")
        self.assertEqual([(e["kind"], e["ok"]) for e in lr.events("notify")][:1], [("start", False)])

    def test_no_notify_command_is_refused_before_the_lock_and_nothing_is_written(self):
        lr = self.un(cfg={"notify_command": '""'})
        lr.approve()
        seq = lr.state().seq
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertIn("unattended.notify", r.err)
        self.assertFalse(os.path.exists(lr.rs.lock_path))
        self.assertEqual((lr.calls(), lr.state().seq), ([], seq))

    def test_an_exception_that_escapes_the_driver_is_still_a_stop_that_notifies(self):
        lr = self.un()
        lr.set_scenario(lr.happy())
        lr.approve()
        real = driver.Driver.run_items
        driver.Driver.run_items = lambda self: (_ for _ in ()).throw(RuntimeError("a bug"))
        self.addCleanup(setattr, driver.Driver, "run_items", real)
        with self.assertRaises(RuntimeError):
            lr.run()
        self.assertEqual(lr.notifier.kinds(), ["start", "stop"])
        m = lr.notifier.calls()[1]["msg"]
        self.assertEqual((m["level"], m["outcome"]), ("alert", "error"))
        self.assertEqual(unattended.read_progress(lr.rs)["state"], "ended")


if __name__ == "__main__":
    unittest.main()
