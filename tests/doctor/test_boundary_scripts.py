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
