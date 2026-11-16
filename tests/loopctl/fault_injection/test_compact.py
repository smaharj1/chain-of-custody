"""FI-06: a forced `/compact` mid-run (report 10.6, CS-24, CO-35). State must survive through SessionStart(`compact`).

The compact hook gives a session back the thread of the run from what loopctl can re-read; the canary hook (Phase 1) keeps the nonce.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers import bins, clean_env

import session_start
from loopctl import compact_hook, launch

from . import DriverRepo, DriverTestCase, item_doc

ITEMS = [item_doc("A", estimate={"loc": 100, "engineers": ["backend-engineer"]}), item_doc("B", ["A"], estimate={"loc": 100, "engineers": ["backend-engineer"]})]


def hook_input(lr, sid, source="compact"):
    return {"session_id": sid, "source": source, "cwd": lr.path, "hook_event_name": "SessionStart"}


class Shared(unittest.TestCase):
    """The read-only rows share one repository in mid-run (built once)."""

    @classmethod
    def setUpClass(cls):
        cls.lr = DriverRepo(items=ITEMS)
        cls.addClassCleanup(cls.lr.cleanup)
        loop = cls.lr.fast_start()
        loop.begin("A", cls.lr.session)
        cls.lr.walk("A", "design_review", "building")
        cls.nonce = cls.lr.canary()

    def test_the_conductor_gets_its_item_stage_next_action_and_canary_back(self):
        lr = self.lr
        out = compact_hook.build_context(hook_input(lr, lr.session))
        self.assertEqual((out["kind"], out["item"], out["status"]), ("conductor", "A", "building"))
        ctx = out["context"]
        for needle in ("item A: status building, attempt 1, branch feat/slice-a-a1", "run run-1, plan demo", "canary session=%s nonce=%s" % (lr.session, self.nonce),
                       "Next action: dispatch or resume the engineers", "advance A verifying --session sess-1", "plan/items/A.json", "loopctl status --json"):
            self.assertIn(needle, ctx)
        self.assertLessEqual(len(ctx), compact_hook.MAX_CONTEXT)

    def test_a_driver_child_gets_its_role_stage_brief_and_report_path(self):
        lr = self.lr
        sid = "child-1"
        launch.write_child_record(lr.rs, {"sessionId": sid, "role": "backend-engineer", "item": "A", "stage": "building", "pid": 1, "pgid": 1, "driverPid": os.getpid(),
                                          "brief": "docs/features/slice-a/briefs/backend.md", "report": "docs/features/slice-a/reports/backend.md", "worktree": lr.path})
        self.addCleanup(launch.remove_child_record, lr.rs, sid)
        out = compact_hook.build_context(hook_input(lr, sid))
        ctx = out["context"]
        self.assertEqual(out["kind"], "child")
        for needle in ("You are the backend-engineer of a driver-launched session (stage building", "No human is present", "Your brief: docs/features/slice-a/briefs/backend.md",
                       "Write your report to docs/features/slice-a/reports/backend.md", "Do not run loopctl, git writes, or launch claude", "item A: status building"):
            self.assertIn(needle, ctx)
        self.assertNotIn("advance A verifying", ctx)                       # a child never moves the state

    def test_a_session_that_is_not_part_of_the_run_gets_nothing(self):
        lr = self.lr
        out = compact_hook.build_context(hook_input(lr, "some-other-session"))
        self.assertEqual((out["kind"], out["context"]), ("none", ""))
        buf = io.StringIO()
        self.assertEqual(compact_hook.main(io.StringIO(json.dumps(hook_input(lr, "some-other-session"))), buf), 0)
        self.assertEqual(buf.getvalue(), "")                                  # no output at all: the hook is wired for every compaction
        for bad in (None, {}, {"session_id": "../etc/passwd", "cwd": lr.path}, {"session_id": 5}, [], "x"):
            self.assertEqual(compact_hook.build_context(bad)["context"], "")
        self.assertEqual(compact_hook.build_context({"session_id": "s", "cwd": "/nonexistent-dir"})["kind"], "none")

    def test_the_entry_point_prints_the_hook_protocol_and_never_fails(self):
        lr = self.lr
        out = io.StringIO()
        self.assertEqual(compact_hook.main(io.StringIO(json.dumps(hook_input(lr, lr.session))), out), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("item A", doc["hookSpecificOutput"]["additionalContext"])
        out = io.StringIO()
        self.assertEqual(compact_hook.main(io.StringIO("{not json"), out), 0)
        self.assertIn("FAILED", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])      # a failure is said, never hidden
        for stdin in ("", "[1,2]", "null"):
            out = io.StringIO()
            self.assertEqual(compact_hook.main(io.StringIO(stdin), out), 0)
            self.assertEqual(out.getvalue(), "")

    def test_the_script_runs_as_a_hook_command_and_exits_zero_on_garbage(self):
        lr = self.lr
        path = os.path.join(SCRIPTS_DIR, "loopctl", "compact_hook.py")
        proc = subprocess.run([sys.executable, path], input=json.dumps(hook_input(lr, lr.session)).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=lr.path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("item A: status building", json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])
        proc = subprocess.run([sys.executable, path], input=b"\xff\xfe garbage", stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=lr.path)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILED", proc.stdout.decode())

    def test_the_canary_nonce_survives_a_compaction_with_the_phase_1_hook(self):
        lr = self.lr
        env = dict(lr.env, KIT_CLAUDE_VERSION="2.1.302")
        for source in ("compact", "resume"):
            r = session_start.run({"session_id": lr.session, "source": source, "cwd": lr.path}, env)
            self.assertEqual(r["nonce"], self.nonce, source)
            self.assertIn("nonce=%s" % self.nonce, r["context"])
        self.assertIn("nonce=%s" % self.nonce, compact_hook.build_context(hook_input(lr, lr.session))["context"])


class Compact(DriverTestCase):
    def mid_run(self):
        lr = self.dr(items=ITEMS)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building")
        self.nonce = lr.canary()
        return lr

    def hook(self, lr, sid, source="compact"):
        return hook_input(lr, sid, source)

    def test_the_context_follows_the_item_through_failure_waiting_and_halt(self):
        lr = self.mid_run()
        lr.walk("A", "verifying")
        self.assertIn("run `loopctl gate <name> A`", compact_hook.build_context(self.hook(lr, lr.session))["context"])
        lr.loop().fail("A", "item", "tests_failed", lr.session, failed_acs=["AC-A-1"])
        ctx = compact_hook.build_context(self.hook(lr, lr.session))["context"]
        self.assertIn("last failure: item/tests_failed at verifying, action retry; failing criteria AC-A-1", ctx)
        self.assertIn("counters: verify=1", ctx)
        self.assertIn("status building", ctx)
        # text from the state cannot inject controls, and the cap holds
        lr.loop().park("A", "x" * 50 + "\x1b[31m\u202e", "d" * 500, lr.session)
        lr.ctl("halt", "--reason", "stop for now", "--by", "Pat")
        ctx = compact_hook.build_context(self.hook(lr, lr.session))["context"]
        self.assertIn("HALT is set (stop for now)", ctx)
        self.assertIn("Waiting on a human or on CI: A needs_human", ctx)
        self.assertIn("Next action: stop: a human decides", ctx)
        self.assertNotIn("\x1b", ctx)
        self.assertNotIn("\u202e", ctx)
        old = compact_hook.MAX_CONTEXT
        compact_hook.MAX_CONTEXT = 200
        try:
            small = compact_hook.build_context(self.hook(lr, lr.session))["context"]
        finally:
            compact_hook.MAX_CONTEXT = old
        self.assertEqual(len(small), 200)
        self.assertTrue(small.endswith("..."))

    def test_an_unreadable_state_is_said_not_hidden(self):
        lr = self.mid_run()
        lr.repo.git("update-ref", "refs/loopctl/state/demo", lr.repo.head())          # a forged ref: an ordinary commit
        ctx = compact_hook.build_context(self.hook(lr, lr.session))["context"]
        self.assertIn("could not be read from git", ctx)
        self.assertIn("run `loopctl status` and stop if it fails", ctx)

    def test_a_compaction_in_the_middle_of_a_driver_run_sees_the_running_state(self):
        """The whole thing: the driver launches the engineer; the engineer (the fake) is compacted at its start and the hook runs for
        real inside the child's own session. Its context names the item, the stage and the child's brief and report."""
        lr = self.dr(items=ITEMS[:1])
        out_file = os.path.join(lr.home, "compact.out")
        hook = {"argv": [sys.executable, os.path.join(SCRIPTS_DIR, "loopctl", "compact_hook.py")],
                "stdin_json": {"session_id": "$SID", "source": "compact", "cwd": lr.path}, "out": out_file}
        happy = lr.happy(by_role={"backend-engineer": [dict(lr.eng_run("A"), hook=hook)]})
        lr.set_scenario(happy)
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        with open(out_file) as fh:
            ctx = json.loads(fh.read())["hookSpecificOutput"]["additionalContext"]
        for needle in ("You are the backend-engineer of a driver-launched session (stage building", "item A: status building, attempt 1",
                       "Your brief: docs/features/slice-a/briefs/backend.md", "Write your report to docs/features/slice-a/reports/backend.md"):
            self.assertIn(needle, ctx)
        self.assertEqual(lr.item("A")["status"], "merged")                       # and the run carried on to the end


if __name__ == "__main__":
    unittest.main()
