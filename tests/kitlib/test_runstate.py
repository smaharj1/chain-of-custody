"""runstate: the <git-common-dir>/loopctl/ contract between guards and loopctl."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest

from kitlib import runstate
from tests.helpers import BaseTestCase


class Layout(BaseTestCase):
    def test_for_path_is_shared_by_every_worktree(self):
        repo = self.repo()
        wt = repo.add_worktree("wt", branch="feat/a")
        a = runstate.RunState.for_path(repo.path)
        b = runstate.RunState.for_path(wt)
        self.assertEqual(a.root, b.root)
        self.assertEqual(a.root, os.path.join(repo.common_dir(), "loopctl"))
        self.assertIsNone(runstate.RunState.for_path(self.tmpdir()))

    def test_ensure_creates_the_documented_tree_with_private_mode(self):
        rs = runstate.RunState.for_path(self.repo().path)
        self.assertFalse(rs.exists())
        rs.ensure()
        for sub in ("sessions", "nonces", "events"):
            self.assertTrue(os.path.isdir(os.path.join(rs.root, sub)), sub)
        self.assertEqual(os.stat(rs.root).st_mode & 0o777, 0o700)
        self.assertEqual(rs.heldout_dir, os.path.join(rs.root, "heldout"))
        self.assertEqual(os.path.basename(rs.lock_path), "lock")
        self.assertEqual(os.path.basename(rs.halt_path), "HALT")

    def test_path_components_are_validated(self):
        for ok in ("abc", "a.b-c_d", "0123", "A" * 128):
            self.assertTrue(runstate.valid_id(ok), ok)
        for bad in ("", ".", "..", "a/b", "../x", "a b", "a\n", "A" * 129, None, 5, "x\0y"):
            self.assertFalse(runstate.valid_id(bad), repr(bad))


class Lock(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.rs = runstate.RunState.for_path(self.repo().path)

    def rec(self, sid="s1", **kw):
        r = {"runId": "r-" + sid, "sessionId": sid, "worktree": "/w", "driverPid": None, "planRev": "abc"}
        r.update(kw)
        return r

    def test_acquire_is_exclusive_and_fills_defaults(self):
        self.assertTrue(self.rs.acquire_lock(self.rec("s1")))
        self.assertFalse(self.rs.acquire_lock(self.rec("s2")))
        cur = self.rs.read_lock()
        self.assertEqual(cur["sessionId"], "s1")
        self.assertEqual(cur["host"], socket.gethostname())
        self.assertEqual(cur["heartbeat"], cur["startedAt"])
        for f in runstate.LOCK_FIELDS:
            self.assertIn(f, cur)

    def test_concurrent_acquire_one_winner(self):
        n = 16
        barrier = threading.Barrier(n)
        wins = []

        def go(i):
            barrier.wait()
            if self.rs.acquire_lock(self.rec("s%d" % i)):
                wins.append(i)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1)
        self.assertEqual(self.rs.read_lock()["sessionId"], "s%d" % wins[0])

    def test_heartbeat_only_by_owner(self):
        self.rs.acquire_lock(self.rec("s1"))
        old = self.rs.read_lock()["heartbeat"]
        self.assertFalse(self.rs.heartbeat("s2"))
        self.assertTrue(self.rs.heartbeat("s1", ts=time.time() + 120))
        self.assertNotEqual(self.rs.read_lock()["heartbeat"], old)
        self.assertEqual(self.rs.read_lock()["sessionId"], "s1")

    def test_release_only_by_owner(self):
        self.rs.acquire_lock(self.rec("s1"))
        self.assertFalse(self.rs.release_lock("s2"))
        self.assertTrue(self.rs.lock_held())
        self.assertTrue(self.rs.release_lock("s1"))
        self.assertFalse(self.rs.lock_held())
        self.assertFalse(self.rs.release_lock("s1"))
        self.assertTrue(self.rs.acquire_lock(self.rec("s3")))

    def test_corrupt_lock_counts_as_held(self):
        self.rs.ensure()
        with open(self.rs.lock_path, "w") as fh:
            fh.write("{not json")
        self.assertTrue(self.rs.lock_held())
        self.assertFalse(self.rs.acquire_lock(self.rec("s1")))
        with self.assertRaises(ValueError):
            self.rs.read_lock()
        self.assertFalse(self.rs.heartbeat("s1"))
        self.assertFalse(self.rs.release_lock("s1"))

    def test_steal_requires_the_expected_record(self):
        self.rs.acquire_lock(self.rec("s1"))
        cur = self.rs.read_lock()
        self.assertFalse(self.rs.steal_lock({"runId": "other", "heartbeat": cur["heartbeat"]}, self.rec("s2")))
        self.assertEqual(self.rs.read_lock()["sessionId"], "s1")
        self.assertTrue(self.rs.steal_lock(cur, self.rec("s2")))
        self.assertEqual(self.rs.read_lock()["sessionId"], "s2")
        self.assertEqual([n for n in os.listdir(self.rs.root) if "stolen" in n], [])

    def test_steal_loses_if_the_holder_heartbeats_first(self):
        self.rs.acquire_lock(self.rec("s1"))
        seen = self.rs.read_lock()
        self.rs.heartbeat("s1", ts=time.time() + 300)
        self.assertFalse(self.rs.steal_lock(seen, self.rec("s2")))
        self.assertEqual(self.rs.read_lock()["sessionId"], "s1")

    def test_concurrent_steal_one_winner(self):
        self.rs.acquire_lock(self.rec("s0"))
        seen = self.rs.read_lock()
        n = 10
        barrier = threading.Barrier(n)
        wins = []

        def go(i):
            barrier.wait()
            if self.rs.steal_lock(seen, self.rec("t%d" % i)):
                wins.append(i)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1, wins)
        self.assertEqual(self.rs.read_lock()["sessionId"], "t%d" % wins[0])

    def test_stale_rules(self):
        now = time.time()
        old = runstate.now_iso(now - 3600)
        fresh = runstate.now_iso(now - 10)
        me = socket.gethostname()
        base = {"host": me, "driverPid": None, "sessionId": "s1"}
        # old heartbeat, owner gone -> stale
        self.assertTrue(runstate.lock_is_stale(dict(base, heartbeat=old), now, 30, lambda s: False))
        # fresh heartbeat -> not stale
        self.assertFalse(runstate.lock_is_stale(dict(base, heartbeat=fresh), now, 30, lambda s: False))
        # old heartbeat but the session is still registered -> not stale
        self.assertFalse(runstate.lock_is_stale(dict(base, heartbeat=old), now, 30, lambda s: True))
        # old heartbeat but the driver pid is alive -> not stale
        self.assertFalse(runstate.lock_is_stale(dict(base, heartbeat=old, driverPid=os.getpid()), now, 30, lambda s: False))
        # dead driver pid -> stale
        dead = self.procs.spawn(["true"])
        dead.wait()
        self.assertTrue(runstate.lock_is_stale(dict(base, heartbeat=old, driverPid=dead.pid), now, 30, lambda s: False))
        # another host: cannot judge liveness from here -> never stale
        self.assertFalse(runstate.lock_is_stale(dict(base, heartbeat=old, host="some-other-host"), now, 30, lambda s: False))
        # unparsable heartbeat -> not stale (fail safe)
        self.assertFalse(runstate.lock_is_stale(dict(base, heartbeat="soon"), now, 30, lambda s: False))
        self.assertFalse(runstate.lock_is_stale({}, now, 30, None))

    def test_pid_alive(self):
        self.assertTrue(runstate.pid_alive(os.getpid()))
        for bad in (None, 0, -1, "12", True, 99999999):
            self.assertFalse(runstate.pid_alive(bad), repr(bad))

    def test_iso_round_trip(self):
        t = 1767225600.0
        self.assertEqual(runstate.parse_iso(runstate.now_iso(t)), t)
        self.assertIsNone(runstate.parse_iso("yesterday"))
        self.assertIsNone(runstate.parse_iso(None))


class Halt(BaseTestCase):
    def test_halt_lifecycle(self):
        rs = runstate.RunState.for_path(self.repo().path)
        self.assertFalse(rs.is_halted())
        rs.set_halt("budget_halted", "loopctl")
        self.assertTrue(rs.is_halted())
        info = rs.read_halt()
        self.assertEqual((info["reason"], info["by"]), ("budget_halted", "loopctl"))
        self.assertTrue(rs.clear_halt())
        self.assertFalse(rs.is_halted())
        self.assertFalse(rs.clear_halt())

    def test_any_entry_named_halt_halts_even_if_unreadable(self):
        rs = runstate.RunState.for_path(self.repo().path).ensure()
        with open(rs.halt_path, "w") as fh:
            fh.write("garbage")
        self.assertTrue(rs.is_halted())
        self.assertIn("unreadable", rs.read_halt()["reason"])
        os.unlink(rs.halt_path)
        os.mkdir(rs.halt_path)  # a directory named HALT also halts
        self.assertTrue(rs.is_halted())

    def test_dangling_symlink_halts(self):
        rs = runstate.RunState.for_path(self.repo().path).ensure()
        os.symlink("/nonexistent/target", rs.halt_path)
        self.assertTrue(rs.is_halted())


class Sessions(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.rs = runstate.RunState.for_path(self.repo().path)

    def test_register_get_unregister(self):
        rec = self.rs.register_session("sess-1", "conductor", runId="r1")
        self.assertEqual(rec["role"], "conductor")
        got = self.rs.get_session("sess-1")
        self.assertEqual((got["session_id"], got["role"], got["runId"]), ("sess-1", "conductor", "r1"))
        self.assertIn("registered_at", got)
        self.assertIsNone(self.rs.get_session("other"))
        self.assertTrue(self.rs.unregister_session("sess-1"))
        self.assertIsNone(self.rs.get_session("sess-1"))
        self.assertFalse(self.rs.unregister_session("sess-1"))

    def test_hostile_ids_never_reach_the_filesystem(self):
        for bad in ("../escape", "a/b", "", "..", "x\0y"):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValueError):
                    self.rs.register_session(bad, "conductor")
                self.assertIsNone(self.rs.get_session(bad))
                self.assertFalse(self.rs.unregister_session(bad))
                with self.assertRaises(ValueError):
                    self.rs.write_nonce(bad)
                self.assertIsNone(self.rs.read_nonce(bad))
        self.assertEqual(os.listdir(self.rs.sessions_dir) if os.path.isdir(self.rs.sessions_dir) else [], [])

    def test_load_registry_skips_corrupt_files(self):
        self.rs.register_session("a", "conductor")
        self.rs.register_session("b", "backend-engineer")
        with open(os.path.join(self.rs.sessions_dir, "c.json"), "w") as fh:
            fh.write("{bad")
        with open(os.path.join(self.rs.sessions_dir, "notes.txt"), "w") as fh:
            fh.write("x")
        reg = self.rs.load_registry()
        self.assertEqual(sorted(reg), ["a", "b"])

    def test_registry_feeds_the_classifier(self):
        from kitlib import caller
        self.rs.register_session("cond", "conductor")
        self.rs.register_session("child", "app-engineer")
        reg = self.rs.load_registry()
        self.assertEqual(caller.classify({"session_id": "cond"}, reg).kind, "conductor")
        self.assertEqual(caller.classify({"session_id": "child"}, reg).kind, "engineer")
        self.assertEqual(caller.classify({"session_id": "stranger"}, reg).kind, "unknown")

    def test_run_record(self):
        self.assertIsNone(self.rs.read_run())
        self.rs.write_run({"runId": "r1", "conductor_session_id": "cond"})
        self.assertEqual(self.rs.read_run()["conductor_session_id"], "cond")

    def test_nonce(self):
        self.assertIsNone(self.rs.read_nonce("s1"))
        n = self.rs.write_nonce("s1")
        self.assertRegex(n, r"^[0-9a-f]{32}$")
        self.assertEqual(self.rs.read_nonce("s1"), n)
        n2 = self.rs.write_nonce("s1")
        self.assertNotEqual(n, n2)
        self.assertEqual(self.rs.read_nonce("s1"), n2)
        self.assertIsNone(self.rs.read_nonce("s2"))


class Events(BaseTestCase):
    def test_append_is_redacted_and_ordered(self):
        rs = runstate.RunState.for_path(self.repo().path)
        secret = "gh" + "p_" + "A" * 36
        rs.append_event("agent-1", {"type": "tool", "cmd": "curl -H 'Authorization: Bearer %s'" % ("z" * 30)})
        rs.append_event("agent-1", {"type": "tool", "env": {"GH_TOKEN": secret}, "note": "pw password=hunter22hunter"})
        with open(rs.event_path("agent-1")) as fh:
            raw = fh.read()
        self.assertNotIn("z" * 30, raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("hunter22hunter", raw)
        events = rs.read_events("agent-1")
        self.assertEqual([e["type"] for e in events], ["tool", "tool"])
        self.assertIn("ts", events[0])
        self.assertEqual(os.stat(rs.event_path("agent-1")).st_mode & 0o777, 0o600)

    def test_streams_are_per_name_and_names_are_validated(self):
        rs = runstate.RunState.for_path(self.repo().path)
        rs.append_event("a", {"n": 1})
        rs.append_event("b", {"n": 2})
        self.assertEqual(rs.read_events("a")[0]["n"], 1)
        self.assertEqual(rs.read_events("missing"), [])
        with self.assertRaises(ValueError):
            rs.append_event("../x", {})

    def test_concurrent_appends_keep_lines_intact(self):
        rs = runstate.RunState.for_path(self.repo().path).ensure()
        n_threads, per = 8, 50

        def go(i):
            for j in range(per):
                rs.append_event("shared", {"t": i, "j": j, "pad": "p" * 200})

        ts = [threading.Thread(target=go, args=(i,)) for i in range(n_threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        events = rs.read_events("shared")
        self.assertEqual(len(events), n_threads * per)
        self.assertFalse([e for e in events if "_unparsable" in e])

    def test_unparsable_line_is_reported_not_fatal(self):
        rs = runstate.RunState.for_path(self.repo().path).ensure()
        with open(rs.event_path("x"), "w") as fh:
            fh.write('{"a": 1}\nnot json\n')
        ev = rs.read_events("x")
        self.assertEqual(ev[0], {"a": 1})
        self.assertIn("_unparsable", ev[1])


if __name__ == "__main__":
    unittest.main()
