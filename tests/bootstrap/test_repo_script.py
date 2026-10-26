"""repo_script.py: the GitHub-side setup of a bootstrapped project, printed as a script for a human (DEC-13, IMP-15, H4).

The generator writes text only. The generated script is executed here with a fake `gh` and a local bare remote, never the real ones.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.helpers import BaseTestCase, install_fake_gh, read_jsonl

sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
import repo_script as rs  # noqa: E402

GEN = os.path.join(SCRIPTS_DIR, "github", "repo_script.py")
ANY = [{"regex": ".", "stdout": "", "exit": 0}]


def params(**kw):
    base = dict(repo="acme/widgets")
    base.update(kw)
    return rs.Params(**base)


class Generate(unittest.TestCase):
    def test_is_deterministic_and_contains_only_what_was_asked(self):
        a = rs.generate(params(create_repo=True, boundary=True, labels_=True, approvers=["alice", "bob"], workflows=["intake", "smoke"],
                               config_plan=".claude/bootstrap/config-plan.json", description="A widget app"))
        b = rs.generate(params(create_repo=True, boundary=True, labels_=True, approvers=["alice", "bob"], workflows=["intake", "smoke"],
                               config_plan=".claude/bootstrap/config-plan.json", description="A widget app"))
        self.assertEqual(a, b)
        for want in ("gh repo create acme/widgets --private --source . --remote origin --description 'A widget app'", "git push -u origin main",
                     "kit_apply sh .claude/scripts/github/apply-boundary.sh --repo acme/widgets --variant solo",
                     "kit_apply sh .claude/scripts/github/apply-labels.sh --repo acme/widgets",
                     "gh variable set FACTORY_APPROVERS --repo acme/widgets --body alice,bob",
                     "kit_write python3 .claude/scripts/bootstrap_config.py apply .claude/bootstrap/config-plan.json",
                     "copy_new .claude/templates/github/workflows/intake.yml .github/workflows/intake.yml"):
            self.assertIn(want, a)
        minimal = rs.generate(params())
        for absent in ("gh repo create", "git push", "apply-boundary", "apply-labels", "FACTORY_APPROVERS", "bootstrap_config.py apply", "copy_new .claude"):
            self.assertNotIn(absent, minimal)

    def test_the_script_never_contains_a_merge_a_secret_or_a_remote_fetch(self):
        text = rs.generate(params(create_repo=True, boundary=True, labels_=True, approvers=["alice"], workflows=list(rs.WORKFLOWS)))
        for banned in ("gh pr merge", "gh secret set ANTHROPIC_API_KEY --body", "--body-file", "curl ", "wget ", "| sh", "| bash", "eval ", "sudo", "rm -rf", "--force"):
            self.assertNotIn(banned, text.replace("gh secret set ANTHROPIC_API_KEY --repo acme/widgets", ""))
        # the key is named once, as something the human types
        self.assertEqual(text.count("ANTHROPIC_API_KEY"), 1)

    def test_every_variant_is_valid_shell(self):
        for kw in ({}, {"create_repo": True}, {"boundary": True, "variant": "team", "release_team_id": 7, "environment": "production", "reviewer_ids": [1, 2]},
                   {"labels_": True, "approvers": ["a-b", "c"]}, {"workflows": list(rs.WORKFLOWS), "config_plan": ".claude/bootstrap/p.json"},
                   {"description": "it's \"odd\" $HOME `x` ; & | > <", "create_repo": True}, {"launch_skill": ".claude/bootstrap/launch-app.SKILL.md"}):
            with self.subTest(kw=kw):
                p = subprocess.run(["sh", "-n"], input=rs.generate(params(**kw)).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_team_options(self):
        text = rs.generate(params(boundary=True, variant="team", release_team_id=4242, environment="production", reviewer_ids=[11, 22]))
        self.assertIn("--variant team --release-team-id 4242 --environment production --reviewer-id 11 --reviewer-id 22", text)


class Validation(unittest.TestCase):
    def bad(self, **kw):
        with self.assertRaises(rs.ScriptError, msg=str(kw)):
            params(**kw)

    def test_repo(self):
        for repo in ("widgets", "a/b/c", "acme/wid gets", "", "acme/;rm -rf ~", "acme/$(id)", "acme/`id`", "acme/x\n", "-rf/x", "../x", "acme/..", "acme/x.git", "acme/x|y"):
            with self.subTest(repo):
                self.bad(repo=repo)

    def test_description(self):
        for d in ("x" * 201, "line\nbreak", "bell\x07", "esc\x1b[0m", "‮flip", "tab\there", "zero​width"):
            with self.subTest(d):
                self.bad(description=d)

    def test_branch(self):
        for b in ("", "-x", "main; id", "a..b", "x.lock", "a b", "a/", "a//b", "a/.b", "main\n", "$(id)", "x" * 101, ".hidden", "ma`in"):
            with self.subTest(b):
                self.bad(default_branch=b)
        params(default_branch="release/1.x")

    def test_approvers(self):
        for a in (["a;b"], ["-x"], ["a b"], ["x" * 40], ["a-"], [""], ["alice", "ALICE"], ["u%d" % i for i in range(21)], ["a\nb"], ["$(id)"], ["a,b"]):
            with self.subTest(a):
                self.bad(approvers=a)

    def test_workflows_plan_and_ids(self):
        self.bad(workflows=["evil"])
        self.bad(workflows=["intake", "intake"])
        self.bad(workflows=["../intake"])
        for plan in ("../x.json", ".claude/bootstrap/../../x.json", "/etc/passwd", ".claude/bootstrap/x", ".claude/bootstrap/a b.json", ".claude/bootstrap/x.json;id", "x.json"):
            with self.subTest(plan):
                self.bad(config_plan=plan)
        self.bad(boundary=True, release_team_id=-1)
        self.bad(boundary=True, release_team_id=True)
        self.bad(boundary=True, reviewer_ids=[0])
        self.bad(boundary=True, reviewer_ids=["1; id"])
        self.bad(boundary=True, environment="prod uction")
        self.bad(boundary=True, environment="-x")
        self.bad(environment="production")             # belongs to --boundary
        for src in ("../x.md", ".claude/bootstrap/x.sh", ".claude/skills/x.md", "/etc/passwd", ".claude/bootstrap/a b.md", ".claude/bootstrap/../../x.md", "x.md"):
            with self.subTest(src):
                self.bad(launch_skill=src)
        self.bad(visibility="world")
        self.bad(variant="both")

    def test_shell_metacharacters_that_pass_validation_are_quoted(self):
        text = rs.generate(params(create_repo=True, description="a $(touch /tmp/x) `id` ; rm -rf ~ & 'q' \"d\" > f"))
        line = [ln for ln in text.split("\n") if ln.startswith("step gh repo create")][0]
        argv = shlex.split(line)
        self.assertEqual(argv[-1], "a $(touch /tmp/x) `id` ; rm -rf ~ & 'q' \"d\" > f")
        self.assertEqual(argv[:4], ["step", "gh", "repo", "create"])


class NoExecution(unittest.TestCase):
    def test_the_generator_has_no_way_to_start_a_program(self):
        with open(GEN) as fh:
            src = fh.read()
        for banned in ("subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "shell=True", "Popen"):
            self.assertNotIn(banned, src)

    def test_cli_prints_text_and_a_digest_and_writes_only_under_bootstrap(self):
        p = subprocess.run([sys.executable, GEN, "--repo", "acme/widgets", "--labels"], env=plain_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(hashlib.sha256(p.stdout.encode()).hexdigest(), p.stderr.split("sha256 ")[1].strip())


class Cli(BaseTestCase):
    def root(self):
        root = os.path.realpath(self.tmpdir())
        os.makedirs(os.path.join(root, ".claude"))
        return root

    def gen(self, root, *args):
        return subprocess.run([sys.executable, GEN, "--repo-dir", root] + list(args), env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_out_path_rules(self):
        root = self.root()
        ok = self.gen(root, "--repo", "acme/widgets", "--out", ".claude/bootstrap/setup.sh", "--json")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        doc = json.loads(ok.stdout)
        with open(os.path.join(root, ".claude", "bootstrap", "setup.sh"), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), doc["sha256"])
        for out in ("../x.sh", "/tmp/x.sh", ".claude/x.sh", ".claude/bootstrap/../../x.sh", ".claude/bootstrap/a/b.sh", "x.sh", ".claude/bootstrap/.hidden", ""):
            with self.subTest(out):
                if out == "":
                    continue
                r = self.gen(root, "--repo", "acme/widgets", "--out", out)
                self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(root, ".claude", "bootstrap"))), ["setup.sh"])

    def test_out_refuses_symlinks(self):
        root = self.root()
        outside = os.path.realpath(self.tmpdir())
        os.symlink(outside, os.path.join(root, ".claude", "bootstrap"))
        self.assertEqual(self.gen(root, "--repo", "acme/widgets", "--out", ".claude/bootstrap/setup.sh").returncode, 2)
        self.assertEqual(os.listdir(outside), [])
        os.unlink(os.path.join(root, ".claude", "bootstrap"))
        os.makedirs(os.path.join(root, ".claude", "bootstrap"))
        victim = os.path.join(outside, "victim")
        with open(victim, "w") as fh:
            fh.write("keep")
        os.symlink(victim, os.path.join(root, ".claude", "bootstrap", "setup.sh"))
        self.assertEqual(self.gen(root, "--repo", "acme/widgets", "--out", ".claude/bootstrap/setup.sh").returncode, 2)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep")
        # a symlinked .claude
        root2 = os.path.realpath(self.tmpdir())
        os.symlink(outside, os.path.join(root2, ".claude"))
        self.assertEqual(self.gen(root2, "--repo", "acme/widgets", "--out", ".claude/bootstrap/setup.sh").returncode, 2)


class RunTheScript(BaseTestCase):
    """Execute generated scripts against a fake gh and a local bare remote."""

    def setUp(self):
        BaseTestCase.setUp(self)
        self.repo_, self.bare_ = self.repo_with_remote()
        self.root = self.repo_.path
        os.makedirs(os.path.join(self.root, ".claude"), exist_ok=True)
        for sub in ("scripts", "templates"):
            os.symlink(os.path.join(REPO_ROOT, ".claude", sub), os.path.join(self.root, ".claude", sub))
        self.bindir = self.tmpdir()
        install_fake_gh(self.bindir, ANY)
        self.marker = os.path.join(self.tmpdir(), "marker")

    def env(self, extra=None):
        e = plain_env(self.root, {"FAKE_GH_LOG": os.path.join(self.bindir, "gh.log.jsonl")})
        e["PATH"] = self.bindir + os.pathsep + e["PATH"]
        if extra:
            e.update(extra)
        return e

    def make(self, **kw):
        args = ["--repo", "acme/widgets", "--out", ".claude/bootstrap/setup.sh"]
        for k, v in kw.items():
            flag = "--" + k.replace("_", "-")
            if v is True:
                args.append(flag)
            else:
                args += [flag, v]
        p = subprocess.run([sys.executable, GEN, "--repo-dir", self.root] + args, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return os.path.join(self.root, ".claude", "bootstrap", "setup.sh")

    def run_script(self, script, *args, env=None):
        return subprocess.run(["sh", script] + list(args), env=env or self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120, cwd=self.tmpdir())

    def gh_calls(self):
        return [c["argv"] for c in read_jsonl(os.path.join(self.bindir, "gh.log.jsonl"))]

    def test_dry_run_prints_and_changes_nothing_remote_or_local(self):
        script = self.make(create_repo=True, boundary=True, labels=True, approvers="alice,bob", workflows="intake,smoke",
                           description="x $(touch %s) `touch %s` y" % (self.marker, self.marker))
        before = self.bare_.ref("refs/heads/main")
        for phase in ("local", "remote"):
            p = self.run_script(script, phase)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("DRY RUN", p.stdout)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".github")))
        self.assertEqual(self.bare_.ref("refs/heads/main"), before)
        self.assertIn("gh label create factory:go", p.stdout)        # the kit scripts' own dry runs are shown too
        self.assertIn("gh api -X POST repos/acme/widgets/rulesets", p.stdout)

    def test_apply_remote_runs_the_listed_commands_with_values_verbatim(self):
        desc = "x $(touch %s) `touch %s` ; y" % (self.marker, self.marker)
        script = self.make(create_repo=True, boundary=True, labels=True, approvers="alice,bob", description=desc)
        self.repo_.git("checkout", "-q", "-b", "scratch")
        self.repo_.git("checkout", "-q", "main")
        self.repo_.commit_file("new.txt", "x", "new")
        p = self.run_script(script, "remote", "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.marker), "the description was executed")
        calls = self.gh_calls()
        create = [c for c in calls if c[:3] == ["repo", "create", "acme/widgets"]][0]
        self.assertEqual(create[create.index("--description") + 1], desc)
        self.assertIn(["variable", "set", "FACTORY_APPROVERS", "--repo", "acme/widgets", "--body", "alice,bob"], calls)
        self.assertEqual(len([c for c in calls if c[:2] == ["label", "create"]]), 10)
        self.assertTrue(any(c[:3] == ["api", "-X", "POST"] and c[3] == "repos/acme/widgets/rulesets" for c in calls))
        self.assertEqual(self.bare_.ref("refs/heads/main"), self.repo_.head())  # the first push reached the (local) remote
        self.assertIn("Still yours to do", p.stdout)
        self.assertNotIn("merge", " ".join(" ".join(c) for c in calls))

    def test_refuses_inside_a_session_and_with_bad_arguments(self):
        script = self.make(labels=True)
        for extra in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(extra):
                p = self.run_script(script, "remote", "--apply", env=self.env(extra))
                self.assertEqual(p.returncode, 2)
                self.assertIn("refusing", p.stderr)
        self.assertEqual(self.gh_calls(), [])
        for args in ([], ["both"], ["remote", "--yes"], ["remote", "--apply", "--force"], ["local", "$(id)"]):
            with self.subTest(args):
                self.assertEqual(self.run_script(script, *args).returncode, 2)
        self.assertEqual(self.gh_calls(), [])

    def test_a_failing_command_stops_the_script(self):
        install_fake_gh(self.bindir, [{"argv_prefix": ["label", "create"], "stderr": "boom", "exit": 1}, {"regex": ".", "exit": 0}])
        script = self.make(labels=True, approvers="alice")
        p = self.run_script(script, "remote", "--apply")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn(["variable", "set"], [c[:2] for c in self.gh_calls()])

    def test_the_first_push_needs_a_commit_and_the_right_branch(self):
        script = self.make(create_repo=True)
        self.repo_.git("checkout", "-q", "-b", "other")
        p = self.run_script(script, "remote", "--apply")
        self.assertEqual(p.returncode, 1)
        self.assertIn("check out main", p.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_local_phase_copies_workflows_without_clobbering_and_applies_the_plan(self):
        wf = os.path.join(self.root, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "smoke.yml"), "w") as fh:
            fh.write("mine\n")
        # a config for the plan to edit
        with open(os.path.join(REPO_ROOT, ".claude", "loop.config.md")) as fh:
            cfg = fh.read()
        with open(os.path.join(self.root, ".claude", "loop.config.md"), "w") as fh:
            fh.write(cfg)
        planp = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "bootstrap_config.py"), "--repo", self.root, "plan", "--set", "solo_maintainer=true"],
                               env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(planp.returncode, 0, planp.stderr)
        script = self.make(workflows="intake,smoke", config_plan=".claude/bootstrap/config-plan.json")
        p = self.run_script(script, "local", "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(wf, "intake.yml")))
        with open(os.path.join(wf, "smoke.yml")) as fh:
            self.assertEqual(fh.read(), "mine\n")
        with open(os.path.join(self.root, ".claude", "loop.config.md")) as fh:
            self.assertIn("solo_maintainer: true", fh.read())
        self.assertEqual(self.gh_calls(), [])

    def test_the_launch_skill_is_installed_by_the_human_script_and_not_over_an_existing_one(self):
        src = os.path.join(self.root, ".claude", "bootstrap")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "launch-app.SKILL.md"), "w") as fh:
            fh.write("---\nname: launch-app\ndescription: d\n---\nbody\n")
        script = self.make(launch_skill=".claude/bootstrap/launch-app.SKILL.md")
        target = os.path.join(self.root, ".claude", "skills", "launch-app", "SKILL.md")
        p = self.run_script(script, "local")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(target))   # a dry run installs nothing
        p = self.run_script(script, "local", "--apply")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(target) as fh:
            self.assertIn("name: launch-app", fh.read())
        with open(target, "w") as fh:
            fh.write("edited by hand\n")
        self.assertEqual(self.run_script(script, "local", "--apply").returncode, 0)
        with open(target) as fh:
            self.assertEqual(fh.read(), "edited by hand\n")


if __name__ == "__main__":
    unittest.main()
