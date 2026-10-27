"""What must hold before and around every launch: no nesting, a version in range, a safe profile, a live canary, no stall, one child at a time
(report 5.8, 11.1, 14.2, RC-17, CS-2; FI-04 stall, FI-12 canary, FI-13 nesting and version, FI-15 double launch, FI-21 hostile profile).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers import ProcessTracker
from ...helpers.procs import wait_for

from kitlib import runstate
from loopctl import driver, launch

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Refusals(DriverTestCase):
    def fresh(self, **kw):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], **kw)
        lr.set_scenario(lr.happy())
        lr.approve()
        return lr

    def refused(self, lr, env=None, needle=""):
        r = lr.run(env=env)
        self.assertEqual(r.rc, 3, r)
        self.assertIn(needle, r.out + r.err)
        self.assertEqual(lr.calls(), [])                                                       # nothing was launched
        self.assertEqual(lr.item("A")["status"], "pending")                                    # and no state was touched
        self.assertFalse(lr.rs.lock_held())
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        return r

    def test_a_driver_inside_a_claude_code_session_refuses_whatever_else_is_set(self):
        lr = self.fresh()
        self.refused(lr, {"CLAUDECODE": "1"}, "CS-2")
        self.refused(lr, {"CLAUDECODE": "1", launch.ENV_HARNESS: "0"}, "CS-2")
        self.refused(lr, {"CLAUDECODE": "yes", launch.ENV_HARNESS: "true"}, "CS-2")

    def path_with_git_only(self, lr, *extra):
        """A PATH that has git (loopctl needs it) and nothing named claude, so that a lookup cannot find a real one."""
        d = os.path.join(lr.home, "pathdir-%d" % len(os.listdir(lr.home)))
        os.makedirs(d)
        os.symlink(shutil.which("git"), os.path.join(d, "git"))
        return os.pathsep.join((d,) + extra)

    def test_the_harness_exception_needs_the_fake_binary_and_a_real_looking_binary_never_qualifies(self):
        lr = self.fresh()
        real = os.path.join(lr.home, "bin2")
        os.makedirs(real)
        p = os.path.join(real, "claude")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\necho should never run >> %s/ran\nexit 0\n" % lr.home)
        os.chmod(p, 0o755)
        self.refused(lr, {"CLAUDECODE": "1", launch.ENV_HARNESS: "1", "KIT_CLAUDE_BIN": p}, "CS-2")
        self.assertFalse(os.path.exists(os.path.join(lr.home, "ran")))                          # not even started for a version check

    def test_a_version_below_the_floor_or_unreadable_or_a_missing_binary_refuses(self):
        lr = self.fresh()
        self.refused(lr, {"KIT_CLAUDE_VERSION": "2.1.100"}, "below the kit floor")
        self.refused(lr, {"KIT_CLAUDE_VERSION": "banana"}, "cannot be read")
        self.refused(lr, {"KIT_CLAUDE_BIN": os.path.join(lr.home, "nope")}, "not an executable file")
        lr.env.pop("KIT_CLAUDE_BIN")
        self.refused(lr, {"PATH": self.path_with_git_only(lr)}, "claude is not on PATH")

    def test_a_version_above_the_tested_maximum_refuses_at_l3(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"autonomy_level": "L3"})
        lr.repo.write(".claude/KIT_VERSION", "2.0.0 (2026-10-01)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.310\n")
        lr.commit_all("kit version")
        lr.set_scenario(lr.happy())
        lr.approve()
        self.refused(lr, {"KIT_CLAUDE_VERSION": "2.1.399"}, "above the tested maximum")

    def test_a_tampered_headless_profile_is_refused_before_any_state_changes(self):
        lr = self.fresh()
        tpl = os.path.join(lr.home, "templates")
        shutil.copytree(os.path.join(os.path.dirname(os.path.abspath(launch.__file__)), "..", "..", "templates", "settings"), tpl)
        path = os.path.join(tpl, "headless-child.settings.json")
        with open(path) as fh:
            s = json.load(fh)
        s["permissions"]["allow"].append("Workflow")                                            # the boundary fixture of RC-17: a Workflow allow injected
        with open(path, "w") as fh:
            json.dump(s, fh)
        orig = launch._templates_dir
        launch._templates_dir = lambda: tpl
        self.addCleanup(setattr, launch, "_templates_dir", orig)
        self.refused(lr, None, "Workflow is allowed")


class Canary(DriverTestCase):
    def test_a_child_whose_hooks_did_not_load_is_an_environment_failure_and_its_work_is_not_trusted(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [dict(lr.eng_run("A"), canary=False)]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["lastFailure"]["reason"]), ("blocked", "env:canary_missing", "canary_missing"))
        self.assertEqual(lr.state().header["levelCap"]["level"], "L1")                          # an env failure caps the effective level
        self.assertEqual([c["role"] for c in lr.calls()], ["backend-engineer"])                 # no reviewer, no B
        self.assertNotIn("src/a.py", lr.repo.git("ls-tree", "-r", "--name-only", "main"))      # the unproven work is kept on the branch, not merged
        self.assertAlmostEqual(lr.context().ledger().totals(run_id=lr.rs.read_run()["runId"])["usd"], 0.05)    # but its cost was recorded
        r = lr.run()
        self.assertEqual(r.rc, 3, r)                                                            # and the cap holds until `approve env`


class Stall(DriverTestCase):
    def test_a_stalled_child_is_killed_classed_transient_and_the_run_waits_for_a_human_because_its_cost_is_unknown(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], timing={"stall_s": 0.8})
        pidfile = os.path.join(lr.home, "child.pid")
        grand = os.path.join(lr.home, "grand.pid")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "stall", "seconds": 60, "pidfile": pidfile, "spawn_sleeper": grand}, lr.eng_run("A")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        for f in (pidfile, grand):
            with open(f) as fh:
                pid = int(fh.read())
            self.assertTrue(wait_for(lambda: not runstate.pid_alive(pid), 3), "%s survived" % f)    # the child AND what it leaked into its process group
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "cost_unknown"))
        self.assertEqual((it["lastFailure"]["class"], it["lastFailure"]["reason"]), ("transient", "stall"))
        self.assertEqual(it["counters"]["transient"], 1)
        self.assertEqual(self.slept, [])
        # the human accepts the unknown cost and unblocks: the second attempt of the same item finishes
        lr.ctl("approve", "cost", "--by", "Pat")
        lr.ctl("approve", "unblock", "A", "--by", "Pat", "--choice", "reset")
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "merged")


class Guards(DriverTestCase):
    def test_a_second_child_is_never_started_while_one_of_this_driver_is_alive(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building")
        procs = ProcessTracker()
        self.addCleanup(procs.cleanup)
        sleeper = procs.spawn(["sleep", "30"])
        launch.write_child_record(lr.rs, {"sessionId": "child-1", "role": "backend-engineer", "item": "A", "pid": sleeper.pid, "pgid": sleeper.pid, "driverPid": os.getpid()})
        with self.assertRaises(driver._Stop) as cm:
            drv.launch_child("A", "backend-engineer", "building", "do it")
        self.assertEqual(cm.exception.outcome, "double_launch")
        self.assertEqual(lr.calls(), [])
        sleeper.kill()
        sleeper.wait()
        self.assertFalse(drv.live_child())                                                     # a record whose process is gone does not block

    def test_the_credentials_of_the_shell_and_the_session_marker_never_reach_a_child_and_the_harness_run_works(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(dict(lr.happy(), log_env=["CLAUDECODE", launch.ENV_HARNESS, "AWS_ACCESS_KEY_ID", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY",
                                                    "KIT_CLAUDE_VERSION", "HOME"]))
        lr.approve()
        env = {"CLAUDECODE": "1", launch.ENV_HARNESS: "1", "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKEFAKE", "GH_TOKEN": "ghp_fake", "GITHUB_TOKEN": "ghp_fake2",
               "SSH_AUTH_SOCK": "/tmp/agent.sock", "ANTHROPIC_API_KEY": "sk-ant-fake"}
        r = lr.run(env=env)
        self.assertEqual(r.rc, 0, r)
        for c in lr.calls():
            self.assertEqual(c["env"], {"CLAUDECODE": None, launch.ENV_HARNESS: None, "AWS_ACCESS_KEY_ID": None, "GH_TOKEN": None, "GITHUB_TOKEN": None, "SSH_AUTH_SOCK": None,
                                        "ANTHROPIC_API_KEY": "sk-ant-fake", "KIT_CLAUDE_VERSION": "2.1.302", "HOME": lr.home})
        text = open(lr.calls_log).read() + json.dumps(lr.rs.read_events("loopctl"))
        self.assertNotIn("AKIAFAKEFAKEFAKEFAKE", text)
        self.assertNotIn("sk-ant-fake", json.dumps(lr.rs.read_events("loopctl")))               # and the event log never records a secret

    def test_the_effective_level_is_computed_once_for_the_gates_but_not_against_the_test_double(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv = lr.driver_at("A", begin=False)
        calls = []
        drv.ctx.effective_level = lambda *a, **k: calls.append(1) or {"effective": "L2", "configured": "L2", "caps": []}
        drv.compute_level, drv.fake = True, False
        self.assertEqual(drv.compute_effective_level(), "L2")
        drv.fake = True
        self.assertIsNone(drv.compute_effective_level())
        drv.fake, drv.compute_level = False, False
        self.assertIsNone(drv.compute_effective_level())
        self.assertEqual(len(calls), 1)
        drv.compute_level = True
        drv.ctx.effective_level = lambda *a, **k: {"configured": None, "effective": "L0", "caps": [], "error": "the doctor could not be read"}
        self.assertEqual(drv.compute_effective_level(), "L0")                                  # fail closed


if __name__ == "__main__":
    unittest.main()
