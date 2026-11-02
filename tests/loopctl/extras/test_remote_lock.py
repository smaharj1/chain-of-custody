"""The remote ref lock (report 10.3, RC-10, RC-12): create-only, heartbeat, stale judgement, forged refs, races.

Two layers. `Logic` runs the lock's rules (who may take what, when) over an in-memory compare-and-swap remote, which costs nothing. `Git` runs the parts that
are git's doing (create-only leases, a ref that is not a lock, a remote that refuses, two real machines racing) against a LOCAL BARE remote with two clones.
"""
from __future__ import annotations

import json
import os
import threading
import unittest

from kitlib import runstate

from loopctl import common, remote_lock as rl
from loopctl.common import Refused, UsageError

from ...helpers import TempRepo
from . import Clock, Fleet, MemRemote, good_lock, mem_doc


def lock(machine, plan, clock, host, pid=111, alive=lambda p: False, ttl=3600.0, remote="origin", mem=None):
    lk = rl.RemoteLock(machine.path if machine is not None else "/mem", remote, plan, ttl_s=ttl, clock=clock, host=host, pid=pid, alive=alive)
    if mem is not None:
        lk.doc = mem_doc(mem, lk.doc)
    return lk


class Pure(unittest.TestCase):
    """Judging a record needs no repository."""
    NOW = 1_800_000_000.0

    def test_validate_lock_refuses_every_malformed_record(self):
        ok = good_lock("demo")
        self.assertIsNone(rl.validate_lock(ok, "demo", self.NOW))
        bad = {
            "not an object": [],
            "unknown field": dict(ok, extra=1),
            "wrong schema": dict(ok, schema="loopctl.lock.v0"),
            "another plan": dict(ok, plan="other", name="lock/other"),
            "name mismatch": dict(ok, name="lock/other"),
            "short token": dict(ok, token="abc"),
            "token not hex": dict(ok, token="z" * 32),
            "owner with shell": dict(ok, owner="x;rm -rf /"),
            "owner empty": dict(ok, owner=""),
            "host empty": dict(ok, host=""),
            "pid bool": dict(ok, pid=True),
            "pid negative": dict(ok, pid=-4),
            "acquired junk": dict(ok, acquired="yesterday"),
            "hb string": dict(ok, hb="now"),
            "hb nan": dict(ok, hb=float("nan")),
            "hb in the future": dict(ok, hb=self.NOW + 10 * 3600),
            "ttl tiny": dict(ok, ttl_s=1),
            "ttl huge": dict(ok, ttl_s=10 ** 9),
            "ttl bool": dict(ok, ttl_s=True),
            "meta not object": dict(ok, meta="x"),
            "meta too big": dict(ok, meta={"k": "v" * 5000}),
            "run id with a slash": dict(ok, runId="../x"),
        }
        for label, doc in bad.items():
            with self.subTest(label):
                self.assertIsNotNone(rl.validate_lock(doc, "demo", self.NOW), label)

    def test_judge_needs_an_expired_heartbeat_and_an_owner_that_is_gone(self):
        rec = good_lock("demo", host="here", hb=self.NOW - 7200, ttl_s=3600.0)
        dead, alive = (lambda p: False), (lambda p: True)
        self.assertEqual(rl.judge(good_lock("demo", hb=self.NOW - 60), self.NOW, here="here")[0], False)               # recent
        self.assertTrue(rl.judge(rec, self.NOW, here="here", pid_alive=dead)[0])                                          # expired and the pid is gone
        stale, why = rl.judge(rec, self.NOW, here="here", pid_alive=alive)
        self.assertFalse(stale)                                                                                           # expired but the owner is alive on this host
        self.assertIn("alive", why)
        stale, why = rl.judge(dict(rec, host="far-away"), self.NOW, here="here", pid_alive=dead)
        self.assertFalse(stale)                                                                                           # another host cannot be judged from here
        self.assertIn("--host-dead", why)
        self.assertTrue(rl.judge(dict(rec, host="far-away"), self.NOW, host_dead=True, here="here", pid_alive=alive)[0])
        self.assertFalse(rl.judge(rec, self.NOW, after_s=4 * 3600, here="here", pid_alive=dead)[0])                       # a caller can wait longer than the record says
        self.assertTrue(rl.judge(rec, self.NOW, after_s=60, here="here", pid_alive=dead)[0])                              # never shorter

    def test_names_that_are_not_remotes_are_refused_before_git_is_asked(self):
        for bad in ("https://example.invalid/x.git", "-oProxyCommand=evil", "../x", "a b", "", "origin;ls"):
            with self.subTest(bad):
                with self.assertRaises(Refused):
                    rl.RefDoc("/nonexistent", bad, "refs/loopctl/lock/p", "lock.json", lambda d: None).check_remote()
        for bad in ("a/b", "A", "x" * 80, "-x", ""):
            with self.assertRaises(UsageError):
                rl.lock_ref(bad)


class Logic(unittest.TestCase):
    """The rules, over an in-memory remote."""

    def setUp(self):
        self.mem = MemRemote()
        self.clock = Clock()
        self.plan = "demo"
        self.a = lock(None, self.plan, self.clock, "host-a", pid=os.getpid(), alive=runstate.pid_alive, mem=self.mem)
        self.b = lock(None, self.plan, self.clock, "host-b", pid=222, mem=self.mem)

    def test_hold_renew_release_and_the_status_never_shows_the_token(self):
        self.assertEqual(self.a.status()["state"], "none")
        h = self.a.acquire("alice", run_id="run-1-ab")
        st = self.b.status()
        self.assertEqual((st["state"], st["record"]["owner"], st["record"]["host"]), ("ok", "alice", "host-a"))
        self.assertNotIn("token", st["record"])
        self.assertNotIn(h.token, json.dumps(st))
        with self.assertRaises(Refused) as cm:                                                 # a second machine is refused and told who has it
            self.b.acquire("bob")
        self.assertIn("alice", str(cm.exception))
        self.assertIn("host-a", str(cm.exception))
        again = self.a.acquire("alice", token=h.token)                                         # a restarted run gets its own lock back by its token ...
        self.assertEqual(again.token, h.token)
        with self.assertRaises(Refused):
            self.a.acquire("alice", token="b" * 32)                                            # ... and a token that is not the record's gets nothing
        self.clock.advance(120)
        self.assertTrue(self.a.renew(again))
        self.assertEqual(self.a.status()["age_s"], 0)                                          # the heartbeat moved
        self.assertTrue(self.a.holds(again))
        self.assertTrue(self.a.release(again))
        self.assertEqual(self.b.status()["state"], "none")

    def test_a_stale_lock_whose_owner_is_still_alive_is_not_stolen_and_a_dead_one_is_and_the_old_owner_finds_out(self):
        h = self.a.acquire("alice")                                                            # this very process: alive
        self.clock.advance(3 * 3600)                                                           # the heartbeat lapsed long ago
        alive = [True]
        thief = lock(None, self.plan, self.clock, "host-a", alive=lambda p: alive[0], mem=self.mem)   # a thief on the owner's own host asks whether the pid answers
        with self.assertRaises(Refused) as cm:
            thief.steal("bob")
        self.assertIn("alive", str(cm.exception))
        with self.assertRaises(Refused):
            thief.steal("bob", host_dead=True)                                                 # --host-dead does not override a pid that answers
        self.assertTrue(self.a.holds(h))                                                       # nothing changed
        self.assertTrue(self.a.renew(h))                                                       # the owner can still bring it back
        self.clock.advance(3 * 3600)
        alive[0] = False                                                                       # now the process is gone
        thief.steal("bob")
        self.assertEqual(thief.status()["record"]["owner"], "bob")
        self.assertFalse(self.a.renew(h))                                                      # the old owner cannot re-stamp what is no longer its own
        self.assertFalse(self.a.holds(h))
        self.assertFalse(self.a.release(h))                                                    # nor delete it

    def test_a_lock_on_another_host_is_only_stolen_when_you_say_that_host_is_gone(self):
        self.a.acquire("alice")
        with self.assertRaises(Refused):
            self.b.steal("bob")                                                                # fresh heartbeat
        self.clock.advance(2 * 3600)
        with self.assertRaises(Refused) as cm:
            self.b.steal("bob")                                                                # expired, but host-a cannot be judged from host-b
        self.assertIn("--host-dead", str(cm.exception))
        with self.assertRaises(Refused):
            self.b.steal("bob", corrupt=True, host_dead=True)                                  # --corrupt is only for a ref that is not a lock record
        self.b.steal("bob", host_dead=True)
        self.assertEqual(self.b.status()["record"]["owner"], "bob")

    def test_a_steal_judged_stale_loses_if_the_owner_comes_back_before_the_push(self):
        h = self.a.acquire("alice")
        self.clock.advance(2 * 3600)
        self.b.doc.before_push = lambda: self.a.renew(h)               # between judging and pushing, the owner re-stamps the heartbeat
        with self.assertRaises(Refused) as cm:
            self.b.steal("bob", host_dead=True)
        self.assertIn("changed", str(cm.exception))
        self.assertTrue(self.a.holds(h))

    def test_two_machines_that_both_saw_a_free_lock_cannot_both_win(self):
        self.b.doc.before_push = lambda: self.a.acquire("alice")
        with self.assertRaises(Refused) as cm:
            self.b.acquire("bob")
        self.assertIn("first", str(cm.exception))
        self.assertEqual(self.a.status()["record"]["owner"], "alice")

    def test_a_ref_that_is_not_a_lock_counts_as_held_and_only_steal_corrupt_replaces_it(self):
        self.mem.refs[self.a.ref] = ("f" * 40, None, "its tree is not exactly lock.json")
        self.assertEqual(self.a.status()["state"], "corrupt")
        with self.assertRaises(Refused) as cm:
            self.a.acquire("alice")
        self.assertIn("not a loopctl lock", str(cm.exception))
        with self.assertRaises(Refused):
            self.a.steal("alice")                                                              # needs --corrupt
        h = self.a.steal("alice", corrupt=True)
        self.assertEqual(self.a.status()["state"], "ok")
        self.assertTrue(self.a.release(h))
        # a well-formed record is just as unremovable by a holder who never held it
        self.mem.refs[self.a.ref] = ("e" * 40, good_lock(self.plan, token="c" * 32, hb=self.clock()), None)
        mine = rl.Held("d" * 32, "", "alice")
        self.assertFalse(self.a.renew(mine))
        self.assertFalse(self.a.release(mine))
        self.assertEqual(self.a.status()["record"]["owner"], "mallory")


class Keep(unittest.TestCase):
    """The holder inside a run: token file, heartbeat, stop hook."""

    def setUp(self):
        self.mem = MemRemote()
        self.clock = Clock()
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.rs = runstate.RunState.for_path(self.repo.path).ensure()
        self.lk = lock(self.repo, "demo", self.clock, "host-a", ttl=3600.0, mem=self.mem)
        self.keeper = rl.Keeper(self.lk, self.rs, "alice", interval_s=60.0, run_id="run-1-ab")

    def test_start_remembers_the_token_and_release_forgets_it(self):
        h = self.keeper.start()
        with open(rl.state_path(self.rs)) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["token"], rec["plan"], rec["remote"]), (h.token, "demo", "origin"))
        self.assertEqual(oct(os.stat(rl.state_path(self.rs)).st_mode & 0o777), "0o600")
        self.assertTrue(self.keeper.release())
        self.assertFalse(os.path.exists(rl.state_path(self.rs)))
        self.assertEqual(self.lk.status()["state"], "none")

    def test_the_heartbeat_is_cheap_until_due_and_a_lost_lock_stops_the_run_at_once(self):
        self.keeper.start()
        self.assertEqual(self.keeper.beat(), "idle")
        self.assertIsNone(self.keeper.stop_reason())
        self.clock.advance(61)
        self.assertEqual(self.keeper.beat(), "ok")
        self.clock.advance(2 * 3600)                                                           # another machine takes it over (the owner looked dead to it)
        lock(None, "demo", self.clock, "host-b", pid=9, mem=self.mem).steal("bob", host_dead=True)
        why = self.keeper.stop_reason()
        self.assertEqual(why[0], "remote_lock_lost")
        self.assertEqual(why[2], common.EXIT_REFUSED)
        self.assertFalse(self.keeper.release())                                                # it does not delete the new owner's lock

    def test_an_unreachable_remote_stops_the_run_only_after_half_the_ttl(self):
        self.keeper.start()

        def down():
            raise rl.RemoteUnreachable("no route")
        self.lk.doc.observe = down
        self.clock.advance(61)
        self.assertEqual(self.keeper.beat(), "unreachable")
        self.assertIsNone(self.keeper.stop_reason())                                           # a hiccup is not a lost lock
        self.clock.advance(31 * 60)
        self.assertEqual(self.keeper.stop_reason()[0], "remote_lock_unreachable")


class Git(unittest.TestCase):
    """What git does, against a local bare remote."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    @classmethod
    def tearDownClass(cls):
        cls.fleet.cleanup()

    def setUp(self):
        self.plan = self.fleet.plan()
        self.clock = Clock()
        self.a = lock(self.fleet.a, self.plan, self.clock, "host-a", pid=os.getpid(), alive=runstate.pid_alive)
        self.b = lock(self.fleet.b, self.plan, self.clock, "host-b", pid=222)

    def remote_oid(self):
        return self.fleet.bare.ref(self.a.ref)

    def test_create_hold_renew_release_leave_the_history_on_the_remote(self):
        h = self.a.acquire("alice")
        self.assertEqual(self.remote_oid(), h.oid)
        self.assertEqual(self.b.status()["record"]["owner"], "alice")                          # another machine reads it through a fetch
        with self.assertRaises(Refused):
            self.b.acquire("bob")
        self.clock.advance(120)
        self.assertTrue(self.a.renew(h))
        self.assertEqual(self.remote_oid(), h.oid)
        self.assertEqual(self.fleet.bare.git("rev-list", "--count", self.a.ref), "2")          # acquire, heartbeat: the history is the audit trail
        self.assertEqual(self.fleet.bare.git("ls-tree", "--name-only", self.a.ref), "lock.json")
        self.assertTrue(self.a.release(h))
        self.assertIsNone(self.remote_oid())
        self.assertEqual(self.b.status()["state"], "none")

    def test_two_machines_that_both_saw_a_free_lock_cannot_both_win_the_push(self):
        # the loser read "none", then the winner's push landed, then the loser pushed: git refuses the second create (--force-with-lease=<ref>:)
        self.b.doc.before_push = lambda: self.a.acquire("alice")
        with self.assertRaises(Refused) as cm:
            self.b.acquire("bob")
        self.assertIn("first", str(cm.exception))
        self.assertEqual(self.a.status()["record"]["owner"], "alice")

    def test_real_threads_racing_for_a_free_lock_produce_exactly_one_holder(self):
        barrier, results = threading.Barrier(2), []

        def go(lk, who):
            barrier.wait()
            try:
                lk.acquire(who)
                results.append(who)
            except Refused:
                results.append("refused-" + who)
        ts = [threading.Thread(target=go, args=(self.a, "alice")), threading.Thread(target=go, args=(self.b, "bob"))]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(len([r for r in results if not r.startswith("refused")]), 1, results)
        self.assertEqual(len(results), 2)

    def test_a_ref_that_is_not_a_lock_counts_as_held_and_only_steal_corrupt_replaces_it(self):
        fl = self.fleet
        oid = fl.forge(fl.b, self.a.ref, blob="not a lock")
        st = self.a.status()
        self.assertEqual((st["state"], st["oid"]), ("corrupt", oid))
        with self.assertRaises(Refused) as cm:
            self.a.acquire("alice")
        self.assertIn("not a loopctl lock", str(cm.exception))
        with self.assertRaises(Refused):
            self.a.steal("alice")                                                              # needs --corrupt
        h = self.a.steal("alice", corrupt=True)
        self.assertEqual(self.a.status()["state"], "ok")
        self.assertTrue(self.a.release(h))

    def test_a_tree_with_a_second_file_and_a_well_formed_record_for_another_plan_read_as_corrupt(self):
        fl = self.fleet
        lk = lock(fl.a, fl.plan(), self.clock, "host-a")
        fl.forge(fl.b, lk.ref, files={"lock.json": json.dumps(good_lock("elsewhere")), "evil.sh": "x"})
        self.assertEqual(lk.status()["state"], "corrupt")
        lk2 = lock(fl.a, fl.plan(), self.clock, "host-a")
        fl.forge(fl.b, lk2.ref, files={"lock.json": json.dumps(good_lock("someone-else"))})
        self.assertEqual(lk2.status()["state"], "corrupt")                                     # a record that is well formed but for another plan

    def test_a_remote_that_refuses_the_push_says_so_rather_than_claiming_a_lost_race(self):
        hook = os.path.join(self.fleet.bare.path, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\necho 'refs/loopctl/* is not allowed here' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        try:
            with self.assertRaises(Refused) as cm:
                self.a.acquire("alice")
            self.assertIn("refused", str(cm.exception))
            self.assertNotIn("first", str(cm.exception))                                       # not "someone else got there first"
        finally:
            os.unlink(hook)

    def test_an_unreachable_remote_is_an_error_not_a_free_lock(self):
        missing = os.path.join(self.fleet.bare.parent, "does-not-exist.git")
        self.fleet.a.add_remote("ghost", missing)
        lk = lock(self.fleet.a, self.plan, self.clock, "host-a", remote="ghost")
        with self.assertRaises(rl.RemoteUnreachable):
            lk.status()
        with self.assertRaises(rl.RemoteUnreachable):
            lk.acquire("alice")


if __name__ == "__main__":
    unittest.main()
