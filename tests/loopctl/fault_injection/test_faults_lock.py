"""The run lock under contention and after a crash (report 10.3, 19.7; CS-19, L-11, F-7; FI-02 stale local lock, FI-09 lock contention).

4a tests the lock itself (`tests/loopctl/test_lock.py`). Here the driver is the holder: nobody else may write while it runs, it stops the
moment it is no longer the holder, and the lock of a driver that is gone is taken over only when that is certain.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers import ProcessTracker
from ...helpers.procs import wait_for

from kitlib import runstate
from loopctl import driver, lock as lock_mod
from loopctl.gates import GateResult

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class Contention(DriverTestCase):
    def intruder(self, lr, sid="intruder"):
        try:
            os.unlink(lr.rs.lock_path)
        except OSError:
            pass
        lr.rs.acquire_lock({"runId": "run-other", "sessionId": sid, "driverPid": os.getpid(), "worktree": "/elsewhere", "planRev": "r"})

    def test_nobody_else_can_start_write_or_take_the_lock_while_a_driver_holds_it(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        seen = {}

        def g0(ctx):
            if ctx.item_id == "A":
                seen["second run"] = lr.ctl("run")
                seen["conductor begin"] = lr.ctl("begin", "A", "--session", "sess-9")
                seen["acquire"] = lr.ctl("lock", "acquire", "--session", "sess-9", "--nonce", lr.canary("sess-9"))
                seen["steal"] = lr.ctl("lock", "steal", "--session", "sess-9", "--nonce", lr.canary("sess-9"), "--after", "1")
                seen["status"] = lr.ctl("lock", "status", "--json").json
            return GateResult.passed()
        lr.stubs.results["g0"] = g0
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        for name in ("second run", "conductor begin", "acquire", "steal"):
            self.assertEqual(seen[name].rc, 3, (name, seen[name]))
        self.assertIn("lock", seen["second run"].err)
        self.assertEqual(seen["status"]["record"]["driverPid"], os.getpid())
        self.assertFalse(seen["status"]["stale"])
        self.assertEqual(lr.item("A")["status"], "merged")                                              # and the run did not notice any of it
        self.assertFalse(lr.rs.lock_held())

    def test_a_lock_stolen_between_two_steps_stops_the_driver_without_touching_the_thieves_lock(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building")
        self.intruder(lr)
        with self.assertRaises(driver._Stop) as cm:
            drv.check_stop()
        self.assertEqual((cm.exception.outcome, cm.exception.code), ("lock_lost", 3))
        with self.assertRaises(Exception) as cm2:
            drv.advance("A", "verifying", "engineers approved", head=lr.repo.head())
        self.assertIn("belongs to session intruder", str(cm2.exception))                        # the state refuses a writer that is not the holder
        drv.settle_worktree()                                                                    # and the exit path does not touch a worktree that is not ours
        self.assertEqual(lr.rs.read_lock()["sessionId"], "intruder")                          # not released, not stamped, not removed
        self.assertEqual(lr.item("A")["status"], "building")                                   # nothing was advanced without the lock
        self.assertEqual(lr.repo.git("branch", "--show-current"), "feat/slice-a-a1")
        self.assertFalse(lr.rs.read_lock().get("heartbeat") is None)
        self.assertFalse(drv.hb.beat(force=True))

    def test_a_lock_stolen_while_a_child_runs_kills_the_child_and_ends_the_run(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], timing={"stall_s": 60})
        pidfile = os.path.join(lr.home, "child.pid")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "stall", "seconds": 60, "pidfile": pidfile}]}))
        lr.approve()

        def steal():
            wait_for(lambda: os.path.exists(pidfile) and open(pidfile).read().strip(), 10)
            self.intruder(lr)
        t = threading.Thread(target=steal)
        t.start()
        t0 = time.time()
        r = lr.run()
        t.join()
        self.assertEqual(r.rc, 3, r)
        self.assertLess(time.time() - t0, 8)
        with open(pidfile) as fh:
            pid = int(fh.read())
        self.assertTrue(wait_for(lambda: not runstate.pid_alive(pid), 3), "the child was left running")
        self.assertEqual(lr.rs.read_lock()["sessionId"], "intruder")
        self.assertEqual(lr.children_left(), [])
        self.assertIn("no longer holds the run lock", r.out)


class StaleLock(DriverTestCase):
    def lr(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        return lr

    def put(self, lr, **rec):
        base = {"runId": "run-old", "sessionId": "driver-1", "driverPid": dead_pid(), "worktree": lr.path, "planRev": "r",
                "host": socket.gethostname(), "startedAt": "2020-01-01T00:00:00Z", "heartbeat": "2020-01-01T00:00:00Z"}
        base.update(rec)
        with open(lr.rs.lock_path, "w") as fh:
            json.dump(base, fh)

    def test_only_the_lock_of_a_dead_driver_on_this_host_is_taken_over(self):
        lr = self.lr()
        refused = {
            "a live pid with an ancient heartbeat": dict(driverPid=os.getpid()),
            "a dead pid from another host": dict(host="some-other-machine.invalid"),
            "a conductor's lock (no driver pid), however old": dict(driverPid=None, sessionId="sess-1"),
            "a pid that is not a number": dict(driverPid="123"),
            "a boolean pid": dict(driverPid=True),
            "pid zero": dict(driverPid=0),
            "a negative pid": dict(driverPid=-5),
        }
        for name, rec in refused.items():
            with self.subTest(name):
                self.put(lr, **rec)
                r = lr.run()
                self.assertEqual(r.rc, 3, (name, r))
                self.assertIn("another session", r.err)
                self.assertEqual(lr.calls(), [])
                self.assertEqual(lr.rs.read_lock()["runId"], "run-old")                        # untouched
        for name, text in (("an unparsable lock", "{broken"), ("a lock that is JSON null", "null"), ("a lock that is a list", "[1]")):
            with self.subTest(name):
                with open(lr.rs.lock_path, "w") as fh:
                    fh.write(text)
                r = lr.run()
                self.assertEqual(r.rc, 3, (name, r))
                with open(lr.rs.lock_path) as fh:
                    self.assertEqual(fh.read(), text)

    def test_a_dead_drivers_lock_is_taken_over_at_once_whatever_its_heartbeat_says(self):
        lr = self.lr()
        for beat in ("2020-01-01T00:00:00Z", runstate.now_iso()):                           # ancient, and a second ago
            self.put(lr, heartbeat=beat)
            r = lr.run("--dry-run")
            self.assertIn("takes it over", r.out, beat)

    def test_two_takers_cannot_both_win(self):
        lr = self.lr()
        self.put(lr)
        lk = lr.context().lock()
        lk.takeover("run-new-1", "driver-2", lr.path, os.getpid(), "r")
        with self.assertRaises(Exception) as cm:
            lk.takeover("run-new-2", "driver-3", lr.path, os.getpid(), "r")                  # the second sees a live driver (this process) and refuses
        self.assertIn("does not belong to a dead driver", str(cm.exception))
        self.assertEqual(lr.rs.read_lock()["sessionId"], "driver-2")

    def test_owner_dead_is_strict(self):
        me = socket.gethostname()
        self.assertTrue(lock_mod.owner_dead({"driverPid": dead_pid(), "host": me}))
        for rec in ({"driverPid": os.getpid(), "host": me}, {"driverPid": dead_pid(), "host": "elsewhere"}, {"driverPid": dead_pid()}, {"driverPid": None, "host": me},
                    {"driverPid": True, "host": me}, {"driverPid": 0, "host": me}, {"driverPid": "9", "host": me}, {}):
            self.assertFalse(lock_mod.owner_dead(rec), rec)

    def test_a_dead_drivers_inflight_item_is_discarded_back_to_pending_and_its_branch_kept_as_dead(self):
        lr = self.lr()
        loop = lr.fast_start()                                                               # a conductor-style start: item state we can then abandon
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building")
        lr.repo.git("switch", "-q", "-c", "feat/slice-a-a1")
        lr.repo.commit_file("src/partial.py", "half = True\n", "half done")
        lr.repo.git("switch", "-q", "main")
        os.unlink(lr.rs.lock_path)
        self.put(lr)                                                                          # the previous driver, dead
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "merged")
        self.assertIn("lock_taken_over", [e["event"] for e in lr.rs.read_events("loopctl")])
        self.assertFalse(lr.rs.lock_held())
        self.assertEqual(lr.branch_names(), ["dead/slice-a-a1", "main"])
        self.assertIn("src/partial.py", lr.repo.git("ls-tree", "-r", "--name-only", "dead/slice-a-a1"))
        self.assertNotIn("partial.py", lr.repo.git("ls-tree", "-r", "--name-only", "main"))   # the half-done work was not merged


if __name__ == "__main__":
    unittest.main()
