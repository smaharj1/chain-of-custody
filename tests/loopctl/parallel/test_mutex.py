"""Leases (report 10.3 / 19.7 Lock, D-29): one claim per item, the schema mutex, expiry, reclaiming after a crash. No git: a directory and a clock."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from kitlib import runstate

from loopctl.parallel import mutex
from loopctl import common


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], shell=False)
    p.wait()
    return p.pid


class Leases(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="leases-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1000.0]
        self.rs = runstate.RunState(self.dir)
        self.store = mutex.LeaseStore(self.rs, ttl_s=60, clock=lambda: self.now[0])

    def other(self, alive, pid=None):
        """A second holder: another store over the same directory that says whether any owner is alive."""
        return mutex.LeaseStore(self.rs, ttl_s=60, clock=lambda: self.now[0], alive=lambda rec: alive, pid=pid)

    def test_a_second_claim_on_one_item_is_refused_and_the_first_still_holds(self):
        a = self.store.acquire(mutex.item_lease("A"), "worker:A")
        with self.assertRaises(mutex.LeaseHeld) as cm:
            self.other(True).acquire(mutex.item_lease("A"), "worker:A-again")
        self.assertIn("held by worker:A", str(cm.exception))
        self.assertTrue(self.store.holds(a))

    def test_exactly_one_of_many_racing_workers_gets_the_item(self):
        wins, barrier = [], threading.Barrier(8)

        def go(n):
            store = mutex.LeaseStore(self.rs, ttl_s=60, clock=lambda: self.now[0])
            barrier.wait()
            try:
                wins.append(store.acquire(mutex.item_lease("A"), "w%d" % n))
            except mutex.LeaseHeld:
                pass
        ts = [threading.Thread(target=go, args=(n,)) for n in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1)

    def test_renew_release_and_the_token(self):
        a = self.store.acquire("integrate", "me")
        self.now[0] += 30
        self.assertTrue(self.store.renew(a))
        self.assertEqual(self.store.read("integrate")["hb"], 1030.0)
        forged = mutex.Lease("integrate", "0" * 32, "attacker")
        self.assertFalse(self.store.holds(forged))
        self.assertFalse(self.store.renew(forged))
        self.assertFalse(self.store.release(forged))
        self.assertTrue(self.store.holds(a))
        self.assertTrue(self.store.release(a))
        self.assertIsNone(self.store.read("integrate"))
        self.assertFalse(self.store.release(a))

    def test_a_lease_whose_owner_process_is_gone_is_taken_over_at_once(self):
        gone = self.other(True, pid=dead_pid())          # `alive` says yes, but the pid is certainly dead on this host
        old = gone.acquire(mutex.item_lease("A"), "dead-driver")
        new = self.store.acquire(mutex.item_lease("A"), "new-driver")
        self.assertEqual(self.store.read("item-a")["reclaimed"], "dead-driver")
        self.assertFalse(gone.holds(old))
        self.assertTrue(self.store.holds(new))

    def test_expiry_alone_is_not_death_an_owner_that_is_alive_keeps_a_slow_lease(self):
        slow = self.store.acquire("integrate", "slow-gate")
        self.now[0] += 100000
        self.assertTrue(self.store.expired(self.store.read("integrate")))
        with self.assertRaises(mutex.LeaseHeld):
            self.other(True).acquire("integrate", "impatient")
        self.assertTrue(self.store.holds(slow))

    def test_expired_and_owner_gone_is_reclaimed_and_the_old_holder_finds_out(self):
        old = self.store.acquire("integrate", "integrator")
        self.now[0] += 61
        rival = self.other(False)                         # its owner is gone as far as the rival can tell
        new = rival.acquire("integrate", "rival")
        self.assertFalse(self.store.holds(old))           # the check to make before anything that cannot be undone
        self.assertFalse(self.store.renew(old))
        self.assertFalse(self.store.release(old))
        self.assertTrue(rival.holds(new))                 # and its release did not remove the new holder's claim
        self.assertEqual(self.store.read("integrate")["owner"], "rival")

    def test_a_lease_from_another_host_is_never_judged(self):
        a = self.store.acquire("integrate", "far-away")
        rec = self.store.read("integrate")
        rec["host"] = "some-other-host.invalid"
        rec["pid"] = dead_pid()
        from kitlib import jsonio
        jsonio.atomic_write_json(os.path.join(self.store.dir, "integrate.json"), rec)
        self.now[0] += 10 ** 6
        with self.assertRaises(mutex.LeaseHeld):
            self.store.acquire("integrate", "me")
        self.assertEqual(self.store.reap(), [])
        self.assertIsNotNone(a)

    def test_an_unreadable_lease_counts_as_held_until_a_human_removes_it(self):
        path = os.path.join(self.store.dir, "item-a.json")
        os.makedirs(self.store.dir, exist_ok=True)
        for junk in ("{not json", "[]", '{"schema": "other"}', ""):
            with open(path, "w") as fh:
                fh.write(junk)
            with self.assertRaises(mutex.LeaseHeld):
                self.store.acquire("item-a", "me")
            self.assertEqual(self.store.reap(), [])
        self.assertTrue(self.store.list()[0]["corrupt"])
        self.assertTrue(self.store.remove("item-a"))
        self.store.acquire("item-a", "me")

    def test_names_cannot_become_paths(self):
        for bad in ("../x", "a/b", "", ".guard", "A", "x" * 80, "item-\n"):
            with self.subTest(bad):
                with self.assertRaises(common.UsageError):
                    self.store.acquire(bad, "me")
        self.assertEqual(mutex.item_lease("F1"), "item-f1")
        with self.assertRaises(common.UsageError):
            mutex.item_lease("../x")
        with self.assertRaises(common.UsageError):
            mutex.mutex_lease("disk")

    def test_metadata_is_json_only_and_small(self):
        a = self.store.acquire("item-a", "me", ok={"n": 1}, bad=object(), item="A")
        meta = self.store.read("item-a")["meta"]
        self.assertEqual(meta, {"ok": {"n": 1}, "item": "A"})
        with self.assertRaises(common.UsageError):
            self.store.acquire("item-b", "me", blob="x" * 5000)
        self.assertTrue(self.store.holds(a))

    def test_the_schema_mutex_is_all_or_nothing_and_in_one_order(self):
        held = self.store.acquire(mutex.mutex_lease("schema"), "worker:S1")
        with self.assertRaises(mutex.LeaseHeld):
            mutex.acquire_mutexes(self.other(True), "worker:S2", ["schema", "deps"])
        self.assertEqual([r["name"] for r in self.store.list()], ["mutex-schema"])          # `deps` was taken first and given back
        got = mutex.acquire_mutexes(self.other(True), "worker:D1", ["deps"])
        self.assertEqual([l.name for l in got], ["mutex-deps"])
        mutex.release_all(self.store, [held])
        with self.assertRaises(mutex.LeaseHeld):
            mutex.acquire_mutexes(self.other(True), "worker:X", ["schema", "deps"])
        self.assertEqual([r["name"] for r in self.store.list()], ["mutex-deps"])

    def test_reap_removes_only_what_a_dead_owner_left(self):
        dead = self.other(True, pid=dead_pid())
        dead.acquire("item-a", "dead")
        dead.acquire("ports-0", "dead")
        live = self.store.acquire("item-b", "live")
        self.assertEqual(sorted(self.store.reap()), ["item-a", "ports-0"])
        self.assertEqual([r["name"] for r in self.store.list()], ["item-b"])
        self.assertTrue(self.store.holds(live))
        self.assertEqual(self.store.reap(keep=lambda r: True), [])


if __name__ == "__main__":
    unittest.main()
