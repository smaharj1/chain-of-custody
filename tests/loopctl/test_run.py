"""`loopctl run`: preflight, the canary, the sequential hand-off, the driver seam, --dry-run, Tier 2 and the evidence gate (report 5.8, 11.2, 15)."""
from __future__ import annotations

import json
import sys
import types
import unittest

from loopctl import startrun

from . import LoopTestCase


class Run(LoopTestCase):
    def approved(self, **kw):
        lr = self.lr(**kw)
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 0)
        return lr

    def test_sequential_run_takes_the_lock_registers_the_conductor_and_prints_the_handoff(self):
        lr = self.approved()
        nonce = lr.canary()
        r = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertEqual(r.rc, 0, r)
        self.assertIn("runner is sequential", r.out)
        self.assertIn("next item  A", r.out)
        self.assertIn("begin <item> --session sess-1", r.out)
        ctx = lr.context()
        run = ctx.rs.read_run()
        self.assertEqual((run["conductor_session_id"], run["plan"], run["baseSha"], run["runner"]), ("sess-1", "demo", lr.base, "sequential"))
        self.assertTrue(ctx.lock().held_by("sess-1"))
        reg = ctx.rs.get_session("sess-1")
        self.assertEqual((reg["role"], reg["registered_by"]), ("conductor", "loopctl"))
        from kitlib import caller
        self.assertTrue(caller.classify({"session_id": "sess-1"}, ctx.rs.load_registry()).is_conductor)
        self.assertEqual(lr.state().header["runStatus"], "running")

    def test_running_again_from_the_same_session_resumes_and_another_session_is_refused(self):
        lr = self.approved()
        nonce = lr.canary()
        first = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        again = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertEqual(again.rc, 0, again)
        self.assertEqual(lr.context().rs.read_run()["runId"], first.out.split("run id     ")[1].split()[0])
        other_nonce = lr.canary("sess-2")
        r = lr.ctl("run", "--session", "sess-2", "--nonce", other_nonce)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("lock", r.err)

    def test_no_canary_no_run(self):
        lr = self.approved()
        r = lr.ctl("run", "--session", "sess-1", "--nonce", "abc")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("SessionStart hook did not run", r.err)
        nonce = lr.canary()
        for bad in (None, "", "wrong", nonce[:-1], nonce + "0", nonce.upper()):
            args = ["run", "--session", "sess-1"] + (["--nonce", bad] if bad is not None else [])
            r = lr.ctl(*args)
            self.assertEqual(r.rc, 3, (bad, r))
            self.assertFalse(lr.context().lock().status()["held"])
        r = lr.ctl("run")
        self.assertEqual(r.rc, 3)
        self.assertIn("--session", r.err)

    def test_a_session_id_is_validated(self):
        lr = self.approved()
        for bad in ("../x", "a b", "x" * 200):
            r = lr.ctl("run", "--session", bad, "--nonce", "n")
            self.assertEqual(r.rc, 2, (bad, r))

    def test_tier2_off_refuses_everything_that_writes_state(self):
        lr = self.lr(tier2=False)
        for cmd in (["approve", "plan", "--by", "Pat"], ["run", "--session", "s", "--nonce", "n"], ["begin", "A", "--session", "s"],
                    ["advance", "A", "designing", "--session", "s"], ["fail", "A", "--class", "item", "--reason", "tests_failed", "--session", "s"],
                    ["gate", "g6", "A"], ["abort", "--by", "Pat"], ["reconcile", "--apply"], ["clarify", "A", "--session", "s"],
                    ["park", "A", "--code", "x"], ["spend", "record", "--item", "A", "--result", "-"]):
            with self.subTest(cmd):
                r = lr.ctl(*cmd)
                self.assertEqual(r.rc, 3, r)
                self.assertIn("Tier 2", r.err)
        self.assertIsNone(lr.state())
        for cmd in (["validate"], ["status"], ["render"], ["lock", "status"], ["evidence", "--check"], ["cost", "--projection"], ["run", "--dry-run"]):
            self.assertIn(lr.ctl(*cmd).rc, (0, 1), cmd)

    def test_tier2_opt_in_must_be_in_the_base_sha_not_just_the_working_tree(self):
        lr = self.lr(tier2=False)
        lr.repo.write(".claude/loop.config.md", lr.cfg_text.replace("loopctl_tier2: false", "loopctl_tier2: true"))     # uncommitted
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)
        lr.repo.git("switch", "-q", "-c", "feat/x", "main")
        lr.commit_all("opt myself in")                                                                                   # committed on a branch
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)
        self.assertEqual(lr.context().base_sha, lr.base)
        self.assertEqual([n for n, _b, _h in lr.context().config_drift], ["loopctl_tier2"])
        self.assertIn("config drift", lr.ctl("status").out)

    def test_a_config_without_a_committed_loop_config_cannot_enable_tier2(self):
        lr = self.lr(commit=False)
        lr.write_plan(lr.items)
        lr.repo.write(".claude/loop.config.md", lr.cfg_text)
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "plan only", "--", "plan")
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)

    def test_preflight_refuses_without_an_approved_plan_with_halt_and_a_dirty_tree(self):
        lr = self.lr()
        nonce = lr.canary()
        r = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("state", r.err)
        lr.ctl("approve", "plan", "--by", "Pat")
        lr.repo.write("scratch.txt", "dirty")
        r = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertIn("clean-tree", r.err)
        import os
        os.unlink(os.path.join(lr.path, "scratch.txt"))
        lr.ctl("halt", "--reason", "stop", "--by", "Pat")
        r = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertIn("HALT is set", r.err)

    def test_a_plan_changed_since_approval_must_be_reapproved(self):
        lr = self.approved()
        lr.write_plan([item_ for item_ in lr.items] + [])
        lr.items[0]["title"] = "Changed after approval"
        lr.write_plan(lr.items)
        lr.commit_all("edit the plan")
        r = lr.ctl("run", "--session", "sess-1", "--nonce", lr.canary())
        self.assertEqual(r.rc, 3, r)
        self.assertIn("plan-approved", r.err)

    def test_an_evidence_gated_value_makes_run_and_every_tier2_command_refuse(self):
        lr = self.lr(extra_cfg="autonomy_level: L4")
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("evidence gate", r.err)
        self.assertIn("autonomy_level", r.err)
        r = lr.ctl("validate")
        self.assertEqual(r.rc, 1)
        self.assertIn("config.evidence-gated", r.out)
        # once a human commits a complete evidence file it is allowed
        lr.repo.write("docs/baseline/evidence.json", json.dumps({
            "schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
            "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"}))
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 0)

    def test_driver_runner_needs_a_budget_and_the_driver_module(self):
        lr = self.approved(extra_cfg="runner: driver")
        r = lr.ctl("run")
        self.assertEqual(r.rc, 2, r)                                      # the driver module is missing too
        self.assertIn("run_budget_usd is not set", r.err)
        lr2 = self.approved(extra_cfg="runner: driver\nrun_budget_usd: 5")
        r = lr2.ctl("run")
        self.assertIn("driver not installed", r.err)
        self.assertEqual(r.rc, 2)
        self.assertFalse(lr2.context().lock().status()["held"])            # refused before it touched the lock

    def test_driver_runner_dispatches_to_driver_run_and_always_releases_the_lock(self):
        lr = self.approved(extra_cfg="runner: driver\nrun_budget_usd: 5")
        calls = []
        fake = types.ModuleType("loopctl.driver")

        def run(ctx, loop, run_record, as_json):
            calls.append((run_record["runner"], ctx.lock().status()["record"]["driverPid"], type(loop).__name__))
            raise RuntimeError("driver crashed")
        fake.run = run
        sys.modules["loopctl.driver"] = fake
        self.addCleanup(sys.modules.pop, "loopctl.driver", None)
        with self.assertRaises(RuntimeError):
            lr.ctl("run")
        self.assertEqual(calls[0][0], "driver")
        self.assertEqual(calls[0][2], "Loop")
        self.assertIsNotNone(calls[0][1])
        self.assertFalse(lr.context().lock().status()["held"])
        fake.run = lambda ctx, loop, rec, js: 7
        self.assertEqual(lr.ctl("run").rc, 7)

    def test_dry_run_renders_everything_and_writes_nothing(self):
        lr = self.approved()
        before = lr.repo.git("rev-parse", "refs/loopctl/state/demo")
        r = lr.ctl("run", "--dry-run")
        self.assertEqual(r.rc, 0, r)
        for needle in ("plan demo: 3 item(s)", "order: A -> B -> C", "gates:", "projection: unavailable (no baseline)", "[ok] plan-approved"):
            self.assertIn(needle, r.out)
        j = lr.ctl("run", "--dry-run", "--json").json
        self.assertEqual(j["schema"], "loopctl.run-dry.v1")
        self.assertEqual({g["gate"] for g in j["gates"]}, set(__import__("loopctl.gates", fromlist=["x"]).KNOWN_GATES))
        self.assertTrue(j["ready"])
        self.assertEqual(lr.repo.git("rev-parse", "refs/loopctl/state/demo"), before)
        self.assertFalse(lr.context().lock().status()["held"])
        self.assertIsNone(lr.context().rs.read_run())

    def test_dry_run_reports_what_would_stop_a_run(self):
        lr = self.lr()
        r = lr.ctl("run", "--dry-run")
        self.assertEqual(r.rc, 1)
        self.assertIn("[NO] state", r.out)


if __name__ == "__main__":
    unittest.main()
