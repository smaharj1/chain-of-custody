"""A child process and its liveness: stall, HALT, a lost lock, the canary, leaked process groups, orphans of a dead driver (FI-04, FI-12, FI-15).

These tests run `launch.run_child` and `heartbeat` directly against the fake `claude`, so every wait is a fraction of a second.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers import ProcessTracker, bins, clean_env
from ...helpers.procs import wait_for

from kitlib import runstate
from loopctl import heartbeat, launch

SID = "11111111-2222-3333-4444-555555555555"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="children-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.repo = type("R", (), {"path": os.path.join(self.tmp, "repo")})()         # the child only needs a working directory
        os.makedirs(self.repo.path)
        self.procs = ProcessTracker()
        self.addCleanup(self.procs.cleanup)
        self.rs = runstate.RunState(os.path.join(self.tmp, "gitdir")).ensure()
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.claude = bins.install_fake_claude(self.tmp, {})
        self.timing = launch.Timing(poll_s=0.03, stall_s=0.5, canary_wait_s=2.0, term_grace_s=0.3, beat_s=0.1)
        self.prompt = os.path.join(self.tmp, "prompt.md")
        with open(self.prompt, "w") as fh:
            fh.write("do the thing\n")

    def scenario(self, **run):
        base = {"nonce_dir": self.rs.nonces_dir, "events_dir": self.rs.events_dir, "log": self.log, "read_stdin": True, "runs": [run]}
        bins.set_scenario(self.claude, base)

    def spec(self, sid=SID, env=None, budget=1.0):
        return launch.ChildSpec("backend-engineer", sid, self.prompt, self.repo.path, "/tmp/none.settings.json", {"description": "d", "prompt": "p"},
                                ["Read"], budget, env if env is not None else clean_env(home=self.tmp), "A", "building", {"brief": "b.md"})

    def run_child(self, sid=SID, tick=None, with_canary=True, env=None, activity=True):
        return launch.run_child(self.claude, self.spec(sid, env), self.rs, self.timing, "run-1", os.getpid(), tick=tick,
                                activity=heartbeat.child_activity(self.rs, sid) if activity else None,
                                canary=(lambda: heartbeat.canary_present(self.rs, sid)) if with_canary else None)

    def alive(self, pid):
        return runstate.pid_alive(pid)

    def pidfile(self, name):
        return os.path.join(self.tmp, name)

    @staticmethod
    def _read(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def read_pid(self, path):
        self.assertTrue(wait_for(lambda: self._read(path), 3), path)
        return int(self._read(path))


class RunChild(Base):
    def test_a_normal_child_returns_its_json_and_leaves_a_record_for_the_driver_to_remove(self):
        self.scenario(behavior="succeed", total_cost_usd=0.07, result="hello")
        res = self.run_child()
        self.assertEqual((res.returncode, res.reason, res.signal_name), (0, None, None))
        self.assertEqual(res.parsed["total_cost_usd"], 0.07)
        self.assertEqual(res.parsed["session_id"], SID)               # the fake echoes --session-id
        self.assertTrue(res.canary)
        with open(os.path.join(self.rs.root, "children", SID + ".json")) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["role"], rec["item"], rec["stage"], rec["brief"], rec["driverPid"]), ("backend-engineer", "A", "building", "b.md", os.getpid()))
        self.assertEqual(rec["pgid"], rec["pid"])                     # its own process group
        self.assertFalse(os.path.exists(os.path.join(self.rs.root, "children", SID + ".out")))     # captured output is cleaned up
        launch.remove_child_record(self.rs, SID)
        self.assertEqual(os.listdir(os.path.join(self.rs.root, "children")), [])

    def test_the_prompt_arrives_on_stdin_and_the_environment_is_the_allowlist(self):
        bins.set_scenario(self.claude, {"log": self.log, "read_stdin": True, "log_env": ["AWS_ACCESS_KEY_ID", "GH_TOKEN", "HOME", "CLAUDECODE"], "runs": [{"behavior": "succeed"}]})
        env = launch.child_env(dict(clean_env(home=self.tmp), AWS_ACCESS_KEY_ID="AKIAFAKE", GH_TOKEN="ghp_fake", CLAUDECODE="1"))
        res = self.run_child(env=env, with_canary=False)
        self.assertEqual(res.returncode, 0)
        call = bins.read_jsonl(self.log)[0]
        self.assertEqual(call["stdin"], "do the thing\n")
        self.assertEqual(call["cwd"], os.path.realpath(self.repo.path))
        self.assertEqual(call["env"], {"AWS_ACCESS_KEY_ID": None, "GH_TOKEN": None, "HOME": self.tmp, "CLAUDECODE": None})
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], SID)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

    def test_a_child_that_dies_by_signal_is_reported_with_the_signal(self):
        self.scenario(behavior="crash", signal="SIGKILL", partial_stdout='{"type":"res')
        res = self.run_child(with_canary=False)
        self.assertEqual((res.returncode, res.signal_name), (-signal.SIGKILL, "SIGKILL"))
        self.assertIsNone(res.parsed)
        self.assertFalse(res.ended_by_us)

    def test_malformed_output_gives_no_parsed_object_and_exit_zero(self):
        self.scenario(behavior="malformed")
        res = self.run_child(with_canary=False)
        self.assertEqual(res.returncode, 0)
        self.assertIsNone(res.parsed)
        self.assertIn("trunc", res.stdout)

    def test_stderr_tail_is_kept_for_the_failure_detail(self):
        self.scenario(behavior="crash", exit_code=1, stderr="boom: the cli broke")
        res = self.run_child(with_canary=False)
        self.assertIn("boom", res.stderr_tail)


class Stall(Base):
    def test_no_sign_of_life_for_the_stall_limit_kills_the_whole_group(self):
        self.scenario(behavior="stall", seconds=60, pidfile=self.pidfile("stall.pid"), spawn_sleeper=self.pidfile("sleeper.pid"))
        t0 = time.time()
        res = self.run_child()
        self.assertEqual(res.reason, "stall")
        self.assertLess(time.time() - t0, 6)
        self.assertTrue(res.ended_by_us)
        pid, sleeper = self.read_pid(self.pidfile("stall.pid")), self.read_pid(self.pidfile("sleeper.pid"))
        self.assertTrue(wait_for(lambda: not self.alive(pid) and not self.alive(sleeper), 3), "the child or its grandchild survived")
        self.assertIn(res.signal_name, ("SIGTERM", "SIGKILL"))

    def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        self.scenario(behavior="stall", seconds=60, ignore_sigterm=True, pidfile=self.pidfile("stubborn.pid"))
        res = self.run_child()
        pid = self.read_pid(self.pidfile("stubborn.pid"))
        self.assertEqual((res.reason, res.signal_name), ("stall", "SIGKILL"))
        self.assertTrue(wait_for(lambda: not self.alive(pid), 3))

    def test_tool_events_keep_a_long_child_alive(self):
        self.timing.stall_s = 0.3
        self.scenario(behavior="succeed", tool_events=9, event_interval=0.1, total_cost_usd=0.01)       # ~0.8 s of work, an event every 0.1 s
        res = self.run_child()
        self.assertIsNone(res.reason)
        self.assertEqual(res.returncode, 0)
        self.assertGreater(res.duration_s, 0.6)                                                       # longer than the stall limit, and still not a stall

    def test_work_that_goes_quiet_after_events_is_a_stall(self):
        self.timing.stall_s = 0.5
        self.scenario(behavior="stall", seconds=60, tool_events=3, event_interval=0.1)
        res = self.run_child()
        self.assertEqual(res.reason, "stall")

    def test_without_an_activity_probe_the_start_time_is_the_only_sign(self):
        self.timing.stall_s = 0.4
        self.scenario(behavior="stall", seconds=60)
        self.assertEqual(self.run_child(activity=False).reason, "stall")


class Halt(Base):
    def test_a_tick_reason_ends_the_child_and_its_group(self):
        self.timing.stall_s = 60
        self.scenario(behavior="stall", seconds=60, pidfile=self.pidfile("halt.pid"), spawn_sleeper=self.pidfile("halt-sleeper.pid"))
        calls = []

        def tick():
            calls.append(1)
            return "halt" if len(calls) > 8 and os.path.exists(self.pidfile("halt-sleeper.pid")) else None
        res = self.run_child(tick=tick)
        self.assertEqual(res.reason, "halt")
        pid, sleeper = self.read_pid(self.pidfile("halt.pid")), self.read_pid(self.pidfile("halt-sleeper.pid"))
        self.assertTrue(wait_for(lambda: not self.alive(pid) and not self.alive(sleeper), 3))

    def test_the_driver_going_down_takes_the_child_with_it(self):
        self.timing.stall_s = 60
        self.scenario(behavior="stall", seconds=60, pidfile=self.pidfile("down.pid"))

        def tick():
            if os.path.exists(self.pidfile("down.pid")):
                raise KeyboardInterrupt()
            return None
        with self.assertRaises(KeyboardInterrupt):
            self.run_child(tick=tick)
        self.assertTrue(wait_for(lambda: not self.alive(self.read_pid(self.pidfile("down.pid"))), 3))

    def test_a_normal_exit_still_sweeps_a_leaked_grandchild(self):
        self.scenario(behavior="succeed", spawn_sleeper=self.pidfile("leak.pid"))
        res = self.run_child()
        self.assertEqual(res.returncode, 0)
        self.assertTrue(wait_for(lambda: not self.alive(self.read_pid(self.pidfile("leak.pid"))), 3), "the dev server outlived its item (L-27)")


class Canary(Base):
    def test_a_child_whose_hooks_did_not_load_has_no_nonce(self):
        self.scenario(behavior="succeed", canary=False)
        res = self.run_child()
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.canary)

    def test_a_long_running_child_without_a_nonce_is_killed_when_the_wait_expires(self):
        self.timing.canary_wait_s = 0.4
        self.timing.stall_s = 60
        self.scenario(behavior="stall", seconds=60, canary=False, pidfile=self.pidfile("nocanary.pid"))
        t0 = time.time()
        res = self.run_child()
        self.assertEqual(res.reason, "canary")
        self.assertLess(time.time() - t0, 4)
        self.assertTrue(wait_for(lambda: not self.alive(self.read_pid(self.pidfile("nocanary.pid"))), 3))

    def test_the_nonce_of_another_session_does_not_count(self):
        self.rs.write_nonce("someone-else")
        self.assertFalse(heartbeat.canary_present(self.rs, SID))
        self.assertTrue(heartbeat.canary_present(self.rs, SID, also=["someone-else"]))
        self.assertFalse(heartbeat.canary_present(self.rs, "../etc"))
        self.assertFalse(heartbeat.canary_present(self.rs, "", also=[None, 5]))


class Heart(Base):
    def lock(self, sid="drv-1"):
        from loopctl.lock import RunLock
        lk = RunLock(self.rs, 60)
        lk.acquire("run-1", sid, self.repo.path, os.getpid(), "rev")
        return lk

    def test_tick_restamps_the_lock_on_a_timer(self):
        self.lock()
        old = "2020-01-01T00:00:00Z"
        self.rs.heartbeat("drv-1", ts=1577836800.0)                      # an old stamp, so a re-stamp is visible at one-second resolution
        self.assertEqual(self.rs.read_lock()["heartbeat"], old)
        t = [1000.0]
        hb = heartbeat.Heartbeat(self.rs, "drv-1", every_s=30, clock=lambda: t[0])
        self.assertIsNone(hb.tick())                                    # first tick: due, stamps
        self.assertNotEqual(self.rs.read_lock()["heartbeat"], old)
        self.rs.heartbeat("drv-1", ts=1577836800.0)
        t[0] += 10
        self.assertTrue(hb.beat())
        self.assertEqual(self.rs.read_lock()["heartbeat"], old)         # not due yet: left alone
        t[0] += 40
        self.assertIsNone(hb.tick())
        self.assertNotEqual(self.rs.read_lock()["heartbeat"], old)      # due again

    def test_halt_a_lost_lock_and_an_interrupt_each_end_the_run_in_that_order(self):
        self.lock()
        hb = heartbeat.Heartbeat(self.rs, "drv-1", every_s=0.01)
        self.assertIsNone(hb.tick())
        self.rs.set_halt("stop", "Pat")
        self.assertEqual(hb.tick(), "halt")
        hb.interrupted = True
        self.assertEqual(hb.tick(), "interrupted")
        self.rs.clear_halt()
        hb.interrupted = False
        os.unlink(self.rs.lock_path)
        self.assertEqual(hb.tick(), "lock_lost")

    def test_a_stolen_corrupt_or_foreign_lock_is_a_lost_lock(self):
        lk = self.lock()
        hb = heartbeat.Heartbeat(self.rs, "drv-1", every_s=0.01)
        os.unlink(self.rs.lock_path)
        self.rs.acquire_lock({"runId": "run-2", "sessionId": "intruder", "driverPid": os.getpid(), "worktree": "/x", "planRev": "r"})
        self.assertEqual(hb.tick(), "lock_lost")
        self.assertFalse(hb.beat(force=True))
        self.assertEqual(self.rs.read_lock()["sessionId"], "intruder")                  # and the driver stamped nothing on it
        with open(self.rs.lock_path, "w") as fh:
            fh.write("null")
        self.assertEqual(hb.tick(), "lock_lost")
        with open(self.rs.lock_path, "w") as fh:
            fh.write("{broken")
        self.assertEqual(hb.tick(), "lock_lost")
        self.assertFalse(hb.holds_lock())

    def test_child_activity_reads_the_telemetry_stream_and_subagent_streams(self):
        act = heartbeat.child_activity(self.rs, SID)
        started = time.time()
        self.assertEqual(act(started), 0.0)
        os.makedirs(self.rs.events_dir, exist_ok=True)
        stream = os.path.join(self.rs.events_dir, "s-%s.jsonl" % SID)
        with open(stream, "a") as fh:
            fh.write("{}\n")
        a = act(started)
        self.assertGreater(a, started - 1)
        old = os.path.join(self.rs.events_dir, "a-agent1.jsonl")
        with open(old, "w") as fh:
            fh.write("{}\n")
        os.utime(old, (started - 100, started - 100))                                    # a stream from before this child
        self.assertEqual(act(started), a)
        other = os.path.join(self.rs.events_dir, "s-someone-else.jsonl")
        with open(other, "w") as fh:
            fh.write("{}\n")
        os.utime(other, (started + 100, started + 100))                                  # another session's stream does not count
        self.assertEqual(act(started), a)
        sub = os.path.join(self.rs.events_dir, "a-agent2.jsonl")
        with open(sub, "w") as fh:
            fh.write("{}\n")
        os.utime(sub, (started + 50, started + 50))
        self.assertEqual(act(started), started + 50)


class Orphans(Base):
    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def detached(self, sid, pidfile):
        """What a driver killed with SIGKILL leaves behind: a child in its own session, still running."""
        bins.set_scenario(self.claude, {"nonce_dir": self.rs.nonces_dir, "log": self.log, "runs": [{"behavior": "stall", "seconds": 60, "pidfile": pidfile}]})
        p = self.procs.spawn([self.claude, "-p", "--session-id", sid, "--agent", "backend-engineer"], stdin=subprocess.DEVNULL)
        pid = self.read_pid(pidfile)
        return p, pid

    def record(self, sid, pid, driver_pid, **kw):
        rec = {"sessionId": sid, "role": "backend-engineer", "item": "A", "pid": pid, "pgid": pid, "driverPid": driver_pid, "runId": "run-1"}
        rec.update(kw)
        launch.write_child_record(self.rs, rec)
        self.rs.register_session(sid, "backend-engineer", registered_by="driver")

    def test_a_child_of_a_dead_driver_is_killed_and_its_traces_removed(self):
        p, pid = self.detached(SID, self.pidfile("o1.pid"))
        self.record(SID, pid, self.dead_pid())
        out = launch.reap_orphans(self.rs, os.getpid(), grace_s=0.5)
        self.assertEqual([(o["sessionId"], o["killed"]) for o in out], [(SID, True)])
        self.assertTrue(wait_for(lambda: p.poll() is not None, 3))
        self.assertEqual(os.listdir(os.path.join(self.rs.root, "children")), [])
        self.assertIsNone(self.rs.get_session(SID))

    def test_a_child_whose_driver_is_alive_is_left_alone(self):
        p, pid = self.detached(SID, self.pidfile("o2.pid"))
        self.record(SID, pid, os.getpid())
        self.assertEqual(launch.reap_orphans(self.rs, 999999), [])                       # another process asking, but the driver pid is alive
        self.assertEqual(launch.reap_orphans(self.rs, os.getpid()), [])
        self.assertIsNone(p.poll())

    def test_a_recycled_pid_is_never_touched(self):
        sleeper = self.procs.spawn(["sleep", "30"])
        self.record(SID, sleeper.pid, self.dead_pid())
        out = launch.reap_orphans(self.rs, os.getpid(), grace_s=0.2)
        self.assertEqual([(o["sessionId"], o["killed"]) for o in out], [(SID, False)])
        self.assertIsNone(sleeper.poll())                                                # it is not our claude: untouched; the record is cleaned up
        self.assertEqual(os.listdir(os.path.join(self.rs.root, "children")), [])

    def test_hostile_or_unreadable_records_are_ignored_not_followed(self):
        d = os.path.join(self.rs.root, "children")
        os.makedirs(d, exist_ok=True)
        for name, body in (("a.json", "{broken"), ("b.json", "[1]"), ("c.json", json.dumps({"sessionId": "../x", "pid": 1, "pgid": 1, "driverPid": 999999})),
                           ("d.json", json.dumps({"sessionId": "ok", "pid": 1, "pgid": 1, "driverPid": 999999}))):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        out = launch.reap_orphans(self.rs, os.getpid())
        self.assertEqual([(o["sessionId"], o["killed"]) for o in out], [("ok", False)])      # pid 1 / pgid 1 is never signalled


if __name__ == "__main__":
    unittest.main()
