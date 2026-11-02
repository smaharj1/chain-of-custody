"""The run lock: O_EXCL acquire, heartbeat, staleness, steal, the unreadable lock (report section 19.7 Lock; CS-19, L-11)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest

from kitlib import runstate

from loopctl import common, lock as lockmod
from loopctl.common import Refused, UsageError

from . import LoopTestCase


class Staleness(unittest.TestCase):
    def rec(self, **kw):
        base = {"runId": "r1", "sessionId": "s1", "host": socket.gethostname(), "driverPid": None, "heartbeat": runstate.now_iso(1000.0)}
        base.update(kw)
        return base

    def test_fresh_heartbeat_is_never_stale(self):
        self.assertFalse(lockmod.is_stale(self.rec(), 1000.0 + 59 * 60, 60))

    def test_old_heartbeat_with_no_driver_is_stale(self):
        self.assertTrue(lockmod.is_stale(self.rec(), 1000.0 + 61 * 60, 60))

    def test_a_live_driver_pid_keeps_the_lock_alive(self):
        self.assertFalse(lockmod.is_stale(self.rec(driverPid=os.getpid()), 1000.0 + 5000 * 60, 60))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertTrue(lockmod.is_stale(self.rec(driverPid=proc.pid), 1000.0 + 5000 * 60, 60))

    def test_another_host_is_not_judged_unless_you_say_it_is_dead(self):
        r = self.rec(host="some-other-host.invalid", driverPid=os.getpid())
        self.assertFalse(lockmod.is_stale(r, 1000.0 + 5000 * 60, 60))
        self.assertTrue(lockmod.is_stale(r, 1000.0 + 5000 * 60, 60, host_dead=True))

    def test_an_unparsable_heartbeat_is_not_stale(self):
        for bad in (None, "", "yesterday", 5, {"x": 1}):
            self.assertFalse(lockmod.is_stale(self.rec(heartbeat=bad), 10 ** 10, 60), bad)


class Lock(LoopTestCase):
    def lk(self, lr=None):
        lr = lr or self.lr(commit=False)
        rs = runstate.RunState.for_path(lr.path)
        return lr, rs, lockmod.RunLock(rs, 60)

    def test_acquire_release_and_the_record_fields(self):
        lr, rs, lk = self.lk()
        rec = lk.acquire("run-1", "s1", lr.path, 123, "abc")
        for f in runstate.LOCK_FIELDS:
            self.assertIn(f, rec)
        self.assertEqual(oct(os.stat(rs.lock_path).st_mode & 0o777), "0o600")
        self.assertTrue(lk.held_by("s1"))
        self.assertFalse(lk.release("s2"))
        self.assertTrue(lk.release("s1"))
        self.assertFalse(rs.lock_held())

    def test_a_second_session_is_refused_and_the_same_session_resumes(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        with self.assertRaises(Refused) as cm:
            lk.acquire("run-2", "s2", lr.path)
        self.assertIn("held", str(cm.exception))
        again = lk.acquire("run-3", "s1", lr.path)
        self.assertEqual(again["runId"], "run-1")

    def test_invalid_ids_never_become_file_content(self):
        lr, rs, lk = self.lk()
        for bad in ("../x", "a b", "", "x" * 200, None):
            with self.assertRaises(UsageError):
                lk.acquire("run-1", bad, lr.path)

    def test_concurrent_acquire_has_exactly_one_winner(self):
        lr, rs, lk = self.lk()
        results = []

        def go(n):
            try:
                lockmod.RunLock(rs, 60).acquire("run-%d" % n, "s%d" % n, lr.path)
                results.append("won")
            except Refused:
                results.append("lost")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["lost"] * 7 + ["won"])

    def test_the_lock_is_visible_from_a_linked_worktree(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        wt = lr.repo.add_worktree("wt", "feat/wt")
        other = runstate.RunState.for_path(wt)
        self.assertEqual(other.root, rs.root)
        self.assertTrue(other.lock_held())

    def test_a_corrupt_lock_counts_as_held_and_needs_an_explicit_steal(self):
        lr, rs, lk = self.lk()
        rs.ensure()
        for junk in ("", "not json", "[]", "{" * 100, "null"):
            with open(rs.lock_path, "w") as fh:
                fh.write(junk)
            with self.assertRaises(Refused):
                lk.acquire("run-1", "s1", lr.path)
            st = lk.status()
            self.assertTrue(st["held"])
            with self.assertRaises(Refused):
                lk.steal("run-2", "s2", lr.path)                     # --corrupt is required
            rec = lk.steal("run-2", "s2", lr.path, corrupt=True)
            self.assertEqual(rec["runId"], "run-2")
            lk.release("s2")

    def test_steal_needs_a_stale_lock(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        with self.assertRaises(Refused) as cm:
            lk.steal("run-2", "s2", lr.path)
        self.assertIn("not stale", str(cm.exception))
        beat = runstate.parse_iso(lk.status()["record"]["heartbeat"])
        common.set_clock(lambda: beat + 3 * 3600)
        self.addCleanup(common.set_clock, None)
        st = lk.status()
        self.assertTrue(st["stale"])
        rec = lk.steal("run-2", "s2", lr.path)
        self.assertEqual((rec["runId"], rec["sessionId"]), ("run-2", "s2"))
        self.assertTrue(lk.held_by("s2"))

    def test_steal_refuses_when_the_driver_process_is_alive(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path, driver_pid=os.getpid())
        beat = runstate.parse_iso(lk.status()["record"]["heartbeat"])
        common.set_clock(lambda: beat + 3 * 3600)
        self.addCleanup(common.set_clock, None)
        with self.assertRaises(Refused) as cm:
            lk.steal("run-2", "s2", lr.path)
        self.assertIn("still alive", str(cm.exception))

    def test_steal_refuses_a_foreign_host_without_the_flag(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        with open(rs.lock_path) as fh:
            d = json.load(fh)
        d["host"] = "elsewhere.invalid"
        with open(rs.lock_path, "w") as fh:
            json.dump(d, fh)
        common.set_clock(lambda: runstate.parse_iso(d["heartbeat"]) + 3 * 3600)
        self.addCleanup(common.set_clock, None)
        with self.assertRaises(Refused) as cm:
            lk.steal("run-2", "s2", lr.path)
        self.assertIn("another host", str(cm.exception))
        self.assertEqual(lk.steal("run-2", "s2", lr.path, host_dead=True)["runId"], "run-2")

    def test_steal_with_a_tiny_ttl_is_floored_at_one_minute(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        with self.assertRaises(Refused):
            lk.steal("run-2", "s2", lr.path, after_min=0)

    def test_the_heartbeat_is_restamped_only_by_the_owner(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        common.set_clock(lambda: 4102444800.0)
        self.addCleanup(common.set_clock, None)
        self.assertFalse(lk.heartbeat("s2"))
        self.assertTrue(lk.heartbeat("s1"))

    def test_lock_events_are_logged(self):
        lr, rs, lk = self.lk()
        lk.acquire("run-1", "s1", lr.path)
        lk.release("s1")
        evs = [e["event"] for e in rs.read_events("loopctl")]
        self.assertEqual(evs, ["lock_acquired", "lock_released"])


if __name__ == "__main__":
    unittest.main()
