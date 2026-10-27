"""apply-boundary.sh / verify-boundary.sh / boundary.py: dry run by default, every gh api call printed, refuse inside Claude Code (IMP-15)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests import SCRIPTS_DIR
from tests.helpers import BaseTestCase, clean_env, install_fake_gh, read_jsonl

sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
import boundary  # noqa: E402

GH_DIR = os.path.join(SCRIPTS_DIR, "github")
APPLY_SH = os.path.join(GH_DIR, "apply-boundary.sh")
VERIFY_SH = os.path.join(GH_DIR, "verify-boundary.sh")


class Recorder(object):
    """A runner standing in for subprocess.run that records the gh calls and their stdin."""

    def __init__(self, fail_on=None, responses=None):
        self.calls = []
        self.fail_on = fail_on
        self.responses = responses or {}

    def __call__(self, argv, input=None, stdout=None, stderr=None, timeout=None, shell=False, env=None):
        self.calls.append({"argv": list(argv), "input": json.loads(input.decode()) if input else None, "env": env})
        n = len(self.calls)

        class P(object):
            pass
        p = P()
        if self.fail_on == n:
            p.returncode, p.stdout, p.stderr = 1, b"", b"HTTP 422: Validation Failed"
        else:
            path = argv[-1] if argv[-1] != "-" else argv[-3]
            p.returncode, p.stdout, p.stderr = 0, json.dumps(self.responses.get(path, {})).encode(), b""
        return p


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_main(argv, env=None, runner=None, doctor=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = boundary.main(argv, env=env if env is not None else {"PATH": "/usr/bin"}, runner=runner or Recorder(), doctor=doctor or Recorder())
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class ApplyPlan(unittest.TestCase):
    def plan(self, variant, **kw):
        return boundary.build_apply_plan("acme/widgets", variant, **kw)

    def test_solo_plan_calls_in_order(self):
        calls, manual = self.plan("solo")
        self.assertEqual([(c.method, c.path) for c in calls], [("POST", "repos/acme/widgets/rulesets"), ("POST", "repos/acme/widgets/rulesets"), ("PATCH", "repos/acme/widgets"),
                                                                ("PUT", "repos/acme/widgets/actions/permissions/workflow")])
        main = calls[0].body
        self.assertEqual(main["bypass_actors"], [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}])
        self.assertEqual(calls[2].body, {"squash_merge_commit_title": "PR_TITLE", "squash_merge_commit_message": "PR_BODY"})
        self.assertEqual(calls[3].body, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})

    def test_team_plan_has_no_bypass_and_skips_tags_without_a_team(self):
        calls, manual = self.plan("team")
        self.assertEqual(calls[0].body["bypass_actors"], [])
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("--release-team-id" in m for m in manual))

    def test_team_plan_with_a_release_team(self):
        calls, manual = self.plan("team", release_team_id=4242)
        tag = calls[1].body
        self.assertEqual(tag["target"], "tag")
        self.assertEqual(tag["bypass_actors"], [{"actor_id": 4242, "actor_type": "Team", "bypass_mode": "always"}])
        self.assertEqual(boundary.load_template("tags-protection.team.json")["bypass_actors"][0]["actor_id"], 0)  # the template itself is not mutated

    def test_the_bot_is_never_a_bypass_actor_in_any_variant(self):
        for variant in ("solo", "team"):
            calls, _ = self.plan(variant, release_team_id=1)
            for c in calls:
                for a in (c.body or {}).get("bypass_actors", []) if isinstance(c.body, dict) else []:
                    self.assertNotEqual(a["actor_type"], "Integration")
                    self.assertNotEqual(a["actor_type"], "User")

    def test_environment_with_reviewers_and_self_review_prevented(self):
        calls, manual = self.plan("solo", environment="production", reviewer_ids=[11, 22])
        env = calls[-1]
        self.assertEqual((env.method, env.path), ("PUT", "repos/acme/widgets/environments/production"))
        self.assertTrue(env.body["prevent_self_review"])
        self.assertEqual(env.body["reviewers"], [{"type": "User", "id": 11}, {"type": "User", "id": 22}])

    def test_environment_without_reviewers_says_so(self):
        calls, manual = self.plan("solo", environment="production")
        self.assertNotIn("reviewers", calls[-1].body)
        self.assertTrue(any("without reviewers" in m for m in manual))

    def test_bad_input(self):
        for repo in ("widgets", "a/b/c", "acme/wid gets", "", "acme/;rm -rf"):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    boundary.build_apply_plan(repo, "solo")
        with self.assertRaises(ValueError):
            boundary.build_apply_plan("acme/widgets", "both")

    def test_manual_steps_name_codeowners_and_the_bot(self):
        _, manual = self.plan("solo")
        text = " ".join(manual)
        self.assertIn("CODEOWNERS.template", text)
        self.assertIn("bot-identity.md", text)

    def test_argv_construction(self):
        calls, _ = self.plan("solo")
        self.assertEqual(calls[0].argv(), ["gh", "api", "-X", "POST", "repos/acme/widgets/rulesets", "--input", "-"])
        self.assertEqual(boundary.Call("GET", "repos/a/b").argv(), ["gh", "api", "repos/a/b"])


class DryRun(BaseTestCase):
    def test_dry_run_prints_every_call_and_sends_nothing(self):
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--variant", "solo", "--environment", "production", "--reviewer-id", "7"], runner=rec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec.calls, [])
        self.assertIn("DRY RUN", out)
        self.assertEqual(out.count("gh api"), 5)
        for needle in ("gh api -X POST repos/acme/widgets/rulesets --input -", "gh api -X PATCH repos/acme/widgets --input -",
                       "gh api -X PUT repos/acme/widgets/actions/permissions/workflow", "environments/production", "squash_merge_commit_title", "\"bypass_mode\": \"pull_request\""):
            self.assertIn(needle, out)
        self.assertIn("Still yours to do", out)

    def test_shell_wrapper_dry_run_with_a_fake_gh_on_the_path(self):
        d = self.tmpdir()
        gh = install_fake_gh(d, [])
        env = clean_env(extra={"PATH": d + os.pathsep + os.environ.get("PATH", "")})
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)          # a developer runs the suite inside Claude Code; the wrapper refuses on either marker
        p = subprocess.run(["sh", APPLY_SH, "--repo", "acme/widgets", "--variant", "team"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertIn("DRY RUN", p.stdout.decode())
        self.assertEqual(read_jsonl(gh + ".log.jsonl"), [])

    def test_shell_wrappers_refuse_inside_claude_code_even_for_a_dry_run(self):
        for script, args in ((APPLY_SH, ["--repo", "acme/widgets", "--variant", "solo"]), (VERIFY_SH, ["--repo", "acme/widgets"])):
            with self.subTest(script=os.path.basename(script)):
                env = clean_env(extra={"CLAUDECODE": "1"})
                p = subprocess.run(["sh", script] + args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                self.assertEqual(p.returncode, 2)
                self.assertIn("CLAUDECODE", p.stderr.decode())
                self.assertEqual(p.stdout.decode(), "")

    def test_python_entry_refuses_too_so_calling_it_directly_does_not_bypass_the_wrapper(self):
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--variant", "solo", "--apply"], env={"CLAUDECODE": "1"}, runner=rec)
        self.assertEqual(rc, 2)
        self.assertEqual(rec.calls, [])
        p = subprocess.run([sys.executable, os.path.join(GH_DIR, "boundary.py"), "apply", "--repo", "a/b", "--variant", "solo", "--apply"],
                           env=clean_env(extra={"CLAUDECODE": "1"}), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 2)

    def test_scripts_are_executable_thin_wrappers(self):
        for s in (APPLY_SH, VERIFY_SH):
            self.assertTrue(os.access(s, os.X_OK))
            with open(s) as fh:
                text = fh.read()
            self.assertLess(len(text.split("\n")), 25)
            self.assertIn("CLAUDECODE", text)
            self.assertIn("boundary.py", text)

    def test_bad_arguments_exit_2(self):
        rc, out, err = run_main(["apply", "--repo", "nope", "--variant", "solo"])
        self.assertEqual(rc, 2)
        self.assertIn("OWNER/NAME", err)


class Apply(BaseTestCase):
    def test_apply_sends_each_call_with_its_json_body_on_stdin(self):
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--variant", "solo", "--apply"], runner=rec)
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["argv"][2:] for c in rec.calls], [["-X", "POST", "repos/acme/widgets/rulesets", "--input", "-"], ["-X", "POST", "repos/acme/widgets/rulesets", "--input", "-"],
                                                               ["-X", "PATCH", "repos/acme/widgets", "--input", "-"],
                                                               ["-X", "PUT", "repos/acme/widgets/actions/permissions/workflow", "--input", "-"]])
        self.assertEqual(rec.calls[0]["input"]["target"], "branch")
        self.assertEqual(rec.calls[1]["input"]["target"], "tag")

    def test_a_failure_stops_everything_after_it(self):
        rec = Recorder(fail_on=2)
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--variant", "solo", "--apply"], runner=rec)
        self.assertEqual(rc, 1)
        self.assertEqual(len(rec.calls), 2)
        self.assertIn("Stopped", err)

    def test_missing_gh(self):
        def no_gh(argv, **kw):
            raise FileNotFoundError("gh")
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--variant", "solo", "--apply"], runner=no_gh)
        self.assertEqual(rc, 1)
        self.assertIn("gh is not installed", err)


class Verify(BaseTestCase):
    def test_dry_run_lists_only_get_calls(self):
        rec = Recorder()
        rc, out, err = run_main(["verify", "--repo", "acme/widgets"], runner=rec)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, [])
        calls = [l for l in out.split("\n") if l.startswith("gh api")]
        self.assertEqual(len(calls), 6)
        for l in calls:
            self.assertNotIn("-X", l)

    def test_run_records_a_fixture_and_hands_it_to_the_doctor(self):
        d = self.tmpdir()
        out_path = os.path.join(d, "fx.json")
        slug = "acme/widgets"
        rec = Recorder(responses={"repos/%s" % slug: {"default_branch": "main"}, "repos/%s/rulesets" % slug: [{"id": 9, "name": "x"}], "repos/%s/rulesets/9" % slug: {"id": 9, "rules": []}})
        doc = Recorder()
        rc, out, err = run_main(["verify", "--repo", slug, "--run", "--out", out_path], runner=rec, doctor=doc)
        self.assertEqual(rc, 0, err)
        fx = load_json(out_path)
        self.assertEqual(fx["repos/%s" % slug], {"default_branch": "main"})
        self.assertIn("repos/%s/rulesets/9" % slug, fx)
        self.assertTrue(all(c["argv"][:2] == ["gh", "api"] and "-X" not in c["argv"] for c in rec.calls))
        self.assertIn("--gh-fixture", doc.calls[0]["argv"])
        self.assertIn(out_path, doc.calls[0]["argv"])
        self.assertIn("Live halves", out)

    def test_failed_reads_are_recorded_with_their_status(self):
        d = self.tmpdir()
        out_path = os.path.join(d, "fx.json")
        rec = Recorder(fail_on=3)
        rc, out, err = run_main(["verify", "--repo", "acme/widgets", "--run", "--out", out_path], runner=rec)
        fx = load_json(out_path)
        self.assertEqual(fx["repos/acme/widgets/branches/main/protection"]["__status"], 422)


if __name__ == "__main__":
    unittest.main()


# -- the sandbox must deny the H6 write set and the session registry (review fixes D1, GD-2, GD-3) --------------------------------------------------------

from tests import REPO_ROOT  # noqa: E402
from doctorlib import guardchecks  # noqa: E402,F401  (registers guards.v1-residue)
from tests.doctor.helpers import BoundaryCase  # noqa: E402


def _template() -> dict:
    return load_json(os.path.join(REPO_ROOT, ".claude", "templates", "security", "sandbox.settings.json"))


def _settings_with_write(entries):
    t = _template()
    t["sandbox"]["filesystem"]["denyWrite"] = list(entries)
    return t


class SandboxRegistryPath(BoundaryCase):
    """`sandbox.deny-paths` wants the whole H6 write set; `sandbox.registry-path` wants the session registry, by absolute path in a linked worktree."""
    GROUPS = {"sandbox"}

    def row(self, repo, settings, rid, **kw):
        repo.write(".claude/settings.json", json.dumps(settings))
        repo.git("add", "-A")
        return self.by_id(self.results(self.ctx(repo, **kw)), rid)[0]

    def test_the_shipped_template_passes_both_rows(self):
        repo = self.kit_repo()
        for rid in ("sandbox.deny-paths", "sandbox.registry-path"):
            r = self.row(repo, _template(), rid)
            self.assertEqual(r.status, "pass", r.message)

    def test_a_missing_h6_entry_is_an_error_and_named(self):
        from guards import paths
        repo = self.kit_repo()
        for missing in (".claude/scripts", ".claude/settings.local.json", ".github", ".claude/loop.config.md", "CODEOWNERS"):
            with self.subTest(missing=missing):
                entries = [e for e in paths.SANDBOX_DENY_WRITE if e != missing]
                r = self.row(repo, _settings_with_write(entries), "sandbox.deny-paths")
                self.assertEqual((r.status, r.severity), ("fail", "error"))
                self.assertIn(missing, r.message)

    def test_other_spellings_of_an_entry_count(self):
        from guards import paths
        repo = self.kit_repo()
        spelled = ["./" + e if k % 3 == 0 else (os.path.join(repo.path, e) if k % 3 == 1 else e + "/**") for k, e in enumerate(paths.SANDBOX_DENY_WRITE)]
        r = self.row(repo, _settings_with_write(spelled), "sandbox.deny-paths")
        self.assertEqual(r.status, "pass", r.message)

    def test_a_denied_parent_directory_covers_its_children(self):
        from guards import paths
        repo = self.kit_repo()
        entries = [".claude", ".git"] + [e for e in paths.SANDBOX_DENY_WRITE if not e.startswith((".claude/", ".git/"))]
        for rid in ("sandbox.deny-paths", "sandbox.registry-path"):
            r = self.row(repo, _settings_with_write(entries), rid)
            self.assertEqual(r.status, "pass", r.message)

    def test_registry_in_a_plain_checkout_is_a_warning_at_l1_and_an_error_when_the_run_depends_on_it(self):
        from guards import paths
        base = _settings_with_write([e for e in paths.SANDBOX_DENY_WRITE if e != ".git/loopctl/sessions"])
        for cfg, severity in (({}, "warn"), ({"autonomy_level": "L1"}, "warn"), ({"runner": "driver"}, "error"), ({"autonomy_level": "L2"}, "error"),
                              ({"autonomy_level": "L3"}, "error"), ({"autonomy": "unattended", "autonomy_level": "L4"}, "error")):
            with self.subTest(cfg=cfg):
                repo = self.kit_repo(config=cfg)
                r = self.row(repo, base, "sandbox.registry-path")
                self.assertEqual((r.status, r.severity), ("fail", severity), r.message)
                self.assertIn(".git/loopctl/sessions", r.remediation)
                self.assertIn("--sandbox-snippet", r.remediation)

    def test_linked_worktree_needs_the_absolute_path(self):
        from guards import paths
        main = self.kit_repo(config={"runner": "driver"}, commit=True)
        wt = main.add_worktree("wt", "feat/wt")
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")), "a linked worktree has a .git file")
        common = main.common_dir()
        rel_only = _template()
        ctx_for = lambda: self.ctx(type("R", (), {"path": wt})())      # the doctor only needs the path of the repo it is pointed at
        # relative entry only: not enough, because the git directory is outside the project directory
        with open(os.path.join(wt, ".claude", "settings.json"), "w") as fh:
            json.dump(rel_only, fh)
        r = self.by_id(self.results(ctx_for()), "sandbox.registry-path")[0]
        self.assertEqual((r.status, r.severity), ("fail", "error"), r.message)
        self.assertIn(os.path.join(common, "loopctl", "sessions"), r.remediation)
        # the absolute path (either spelling of the directory) satisfies it
        for spelling in (os.path.join(common, "loopctl", "sessions"), os.path.join(common, "loopctl", "sessions") + "/"):
            abs_ok = _template()
            abs_ok["sandbox"]["filesystem"]["denyWrite"].append(spelling)
            with open(os.path.join(wt, ".claude", "settings.json"), "w") as fh:
                json.dump(abs_ok, fh)
            r = self.by_id(self.results(ctx_for()), "sandbox.registry-path")[0]
            self.assertEqual(r.status, "pass", r.message)

    def test_not_a_git_repository_is_skipped(self):
        repo = self.kit_repo()
        import shutil
        shutil.rmtree(os.path.join(repo.path, ".git"))
        r = self.by_id(self.results(self.ctx(repo)), "sandbox.registry-path")[0]
        self.assertEqual(r.status, "skipped")


class V1Residue(BoundaryCase):
    """`guards.v1-residue` (D10, GD-7): the v1 shell hooks must not survive the activation of the v2 wiring."""
    GROUPS = {"settings"}
    V1_LIVE = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh"}]},
                                        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-main-edit.sh"}]}]}}

    def repo(self, live=None, v1_files=False, version="1.2.0 (2026-07-30)\n"):
        from guards import wiring
        with open(os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "hooks.manifest.json")) as fh:
            manifest_text = fh.read()
        files = {".claude/scripts/guards/hooks.manifest.json": manifest_text}
        if live is not None:
            files[".claude/settings.json"] = json.dumps(live)
        if v1_files:
            files[".claude/hooks/guard-git.sh"] = "#!/bin/sh\n"
            files[".claude/hooks/guard-main-edit.sh"] = "#!/bin/sh\n"
        return self.kit_repo(files=files, kit_version=version), wiring.build_settings(json.loads(manifest_text))

    def row(self, repo):
        return self.by_id(self.results(self.ctx(repo)), "guards.v1-residue")[0]

    def test_clean_install_passes(self):
        repo, v2 = self.repo(live=None)
        self.assertEqual(self.row(repo).status, "pass")
        repo, v2 = self.repo(live=v2)
        self.assertEqual(self.row(repo).status, "pass")

    def test_before_activation_it_is_a_warning_that_names_the_release_stage(self):
        repo, _ = self.repo(live=self.V1_LIVE, v1_files=True)
        r = self.row(repo)
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn("release stage", r.message)
        self.assertIn("guard-git.sh", r.message)

    def test_v2_active_with_a_v1_hook_still_wired_is_an_error(self):
        _, v2 = self.repo()
        live = json.loads(json.dumps(v2))
        live["hooks"]["PreToolUse"].append(self.V1_LIVE["hooks"]["PreToolUse"][0])
        repo, _ = self.repo(live=live)
        r = self.row(repo)
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn("guard-git.sh", r.message)

    def test_v2_active_with_the_v1_files_left_behind_is_an_error(self):
        _, v2 = self.repo()
        repo, _ = self.repo(live=v2, v1_files=True)
        r = self.row(repo)
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn("still exist", r.message)

    def test_a_2x_install_must_not_ship_the_v1_hooks_even_when_the_wiring_is_not_merged(self):
        repo, _ = self.repo(live={"permissions": {"allow": []}}, v1_files=True, version="2.0.0 (2026-11-01)\n")
        r = self.row(repo)
        self.assertEqual((r.status, r.severity), ("fail", "error"))
