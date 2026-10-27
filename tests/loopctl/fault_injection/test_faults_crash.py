"""A driver that dies (FI-01 crash mid-item, FI-15 signals, FI-20 a crash between the merge and its record).

The driver is a real process here (`python3 .claude/scripts/loopctl run`), killed the way it would be killed: SIGKILL leaves its child
running in its own session and its lock behind; SIGTERM must take the child down with it. The next `loopctl run` has to recover from
both without a human touching the state.
"""
from __future__ import annotations

import os
import signal
import time
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers import ProcessTracker
from ...helpers.procs import wait_for

from kitlib import runstate
from loopctl import state as state_mod

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Crash(DriverTestCase):
    def setUp(self):
        super(Crash, self).setUp()
        self.procs = ProcessTracker()
        self.addCleanup(self.procs.cleanup)

    def read_pid(self, path):
        def read():
            try:
                with open(path) as fh:
                    return fh.read().strip()
            except OSError:
                return ""
        self.assertTrue(wait_for(read, 15), "the child never started: %s" % path)
        return int(read())

    def mid_item(self, **cfg):
        """A driver process with an engineer child that never finishes. Returns (repo, driver process, child pid)."""
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg=dict({"gates": {n: "off" for n in
                     ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "g7", "g9", "g10", "g11", "evidence")}}, **cfg),
                     timing={"stall_s": 600, "canary_wait_s": 600, "term_grace_s": 0.5})
        pidfile = os.path.join(lr.home, "child.pid")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "stall", "seconds": 600, "pidfile": pidfile}, lr.eng_run("A")]}))
        lr.approve()
        p = lr.spawn_run(self.procs)
        return lr, p, self.read_pid(pidfile)

    def test_sigkill_leaves_an_orphan_and_a_lock_and_the_next_run_recovers_from_both(self):
        lr, p, child = self.mid_item()
        item = lr.item("A")
        self.assertEqual((item["status"], item["branch"]), ("building", "feat/slice-a-a1"))
        lock = lr.rs.read_lock()
        self.assertEqual(lock["driverPid"], p.pid)
        self.assertEqual(lr.repo.git("branch", "--show-current"), "feat/slice-a-a1")
        left = sorted(os.listdir(os.path.join(lr.rs.root, "children")))
        self.assertEqual(sorted(n.split(".", 1)[1] for n in left), ["err", "json", "out", "prompt.md", "settings.json"])      # one child, all of its files
        p.kill()
        p.wait()
        self.assertTrue(runstate.pid_alive(child), "the child lives in its own session and survives its driver")      # the problem to solve
        self.assertTrue(lr.rs.lock_held())
        self.assertFalse(runstate.pid_alive(lock["driverPid"]))
        self.assertEqual(lr.item("A")["status"], "building")                                 # the state still says the item is mid-flight
        n = len(lr.calls())
        # a plain re-run: the lock of a dead driver is taken over (no heartbeat wait), the orphan is killed, the item restarts, the plan completes
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A")]}))
        r = lr.run("--json")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(wait_for(lambda: not runstate.pid_alive(child), 5), "the orphaned child was not killed")
        evs = [e["event"] for e in lr.rs.read_events("loopctl")]
        self.assertIn("lock_taken_over", evs)
        self.assertIn("reaped", evs)
        a = lr.item("A")
        self.assertEqual((a["status"], a["attempt"]), ("merged", 1))
        self.assertEqual(lr.branch_names(), ["dead/slice-a-a1", "main"])                     # the half-done attempt is kept as dead/*, not merged
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertEqual(lr.children_left(), [])
        self.assertEqual(len(lr.calls()) - n, 2)                                              # an engineer and a reviewer, nothing duplicated

    def test_sigterm_takes_the_child_down_releases_the_lock_and_leaves_the_item_recoverable(self):
        lr, p, child = self.mid_item()
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 1, (out, err))
        self.assertIn(b"interrupted", out)
        self.assertTrue(wait_for(lambda: not runstate.pid_alive(child), 5), "the child outlived a SIGTERMed driver")
        self.assertFalse(lr.rs.lock_held())
        self.assertEqual(lr.children_left(), [])
        self.assertEqual(os.listdir(lr.rs.sessions_dir), [])
        self.assertEqual(lr.item("A")["status"], "building")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")                     # settled: the worktree is back on main and clean
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A")]}))
        self.assertEqual(lr.run().rc, 0)
        self.assertEqual(lr.item("A")["status"], "merged")

    def test_a_crash_between_the_merge_and_its_record_is_recovered_from_the_trailer(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        orig = state_mod.Loop.advance

        def crashing(self_, iid, to, *a, **kw):
            if to == "merged":
                raise RuntimeError("the driver died here")                                   # the git merge has happened; the record has not
            return orig(self_, iid, to, *a, **kw)
        state_mod.Loop.advance = crashing
        try:
            with self.assertRaises(RuntimeError):
                lr.run()
        finally:
            state_mod.Loop.advance = orig
        self.assertEqual(lr.item("A")["status"], "reviewing")
        self.assertFalse(lr.rs.lock_held())                                                   # the caller's `finally` released it
        merges = lr.repo.git("log", "--merges", "--format=%H|%s", "main").splitlines()
        self.assertEqual(len(merges), 1)
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        n = len(lr.calls_for("backend-engineer"))
        r = lr.run("--json")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["mergeCommit"], merges[0].split("|")[0])               # recovered from `Loop-Item: A` on a merge of feat/slice-a-a1
        self.assertEqual(lr.item("A")["status"], "merged")
        self.assertEqual(len(lr.calls_for("backend-engineer")) - n, 0)                        # A was not rebuilt on top of its own merge
        self.assertEqual(len(lr.repo.git("log", "--merges", "--format=%H", "main").splitlines()), 1)
        self.assertEqual(lr.branch_names(), ["main"])

    def test_a_merge_whose_record_loses_to_a_human_is_recovered_in_the_same_run_or_stops_loudly(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        orig = state_mod.Loop.advance

        def refusing(self_, iid, to, *a, **kw):
            if to == "merged" and not getattr(refusing, "done", False):
                refusing.done = True
                from loopctl.common import Refused
                raise Refused("simulated: the state moved under the record")
            return orig(self_, iid, to, *a, **kw)
        state_mod.Loop.advance = refusing
        try:
            r = lr.run()
        finally:
            state_mod.Loop.advance = orig
        self.assertEqual(r.rc, 0, r)                                                          # recover() found the merge by its trailer and recorded it
        self.assertEqual(lr.item("A")["status"], "merged")
        self.assertEqual(len(lr.repo.git("log", "--merges", "--format=%H", "main").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
