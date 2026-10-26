"""tools/baseline/run-baseline.sh and prepare.py against a temporary source repo with a v1.2.0 tag.

Nothing here runs the real `claude`: a fake one is only put on the path, and the tests assert
the harness never executed it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.baseline import BASELINE_DIR, FIXTURE
from tests.helpers import BaseTestCase, clean_env, install_fake_claude, read_jsonl

SCRIPT = os.path.join(BASELINE_DIR, "run-baseline.sh")
PREPARE = os.path.join(BASELINE_DIR, "prepare.py")

V1_SETTINGS = {
    "permissions": {"allow": []},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh"}]}]},
}


class RunBaseline(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.repo()
        self.src.write(".claude/settings.json", json.dumps(V1_SETTINGS, indent=2))
        self.src.write(".claude/CLAUDE.md", "# Your Project Name\n<!-- template -->\n")
        self.src.write(".claude/context/app.md", "<!-- APP PRIMER -->\n")
        self.src.write(".claude/context/admin-app.md", "<!-- ADMIN PRIMER -->\n")
        self.src.write(".claude/context/infra.md", "<!-- INFRA PRIMER -->\n")
        self.src.write(".claude/agents/admin-app-engineer.md", "---\nname: admin-app-engineer\n---\n")
        self.src.write(".claude/agents/infra-engineer.md", "---\nname: infra-engineer\n---\n")
        self.src.write(".claude/loop.config.md", "# Loop Config\n")
        self.src.write("README.md", "kit readme\n")
        self.src.write(".gitignore", ".claude/loop.jsonl\nnode_modules/\n")
        self.src.git("add", "-A")
        self.src.git("commit", "-q", "-m", "v1.2.0 tree")
        self.tag_commit = self.src.head()
        self.src.git("tag", "-a", "v1.2.0", "-m", "v1.2.0")
        self.src.commit_file("LATER.md", "after the tag\n", "a later commit")  # the clone must not contain this
        self.bins = self.tmpdir("bins-")
        self.claude = install_fake_claude(self.bins, {})
        self.root = self.tmpdir("runs-")
        self.home = self.tmpdir("home-")

    def env(self, **extra):
        e = clean_env(home=self.home, extra=dict({"KIT_CLAUDE_BIN": self.claude}, **extra))
        return e

    def run_script(self, *args, env=None, script=SCRIPT):
        cmd = ["sh", script] if script.endswith(".sh") else [sys.executable, script]
        return subprocess.run(cmd + list(args), env=env or self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, cwd=self.tmpdir("cwd-"))

    def prepare(self, run_id="run-1", *extra, **kw):
        return self.run_script(run_id, "--dest-root", self.root, "--source", self.src.path, *extra, **kw)

    def dest(self, run_id="run-1"):
        return os.path.join(os.path.realpath(self.root), run_id)

    def assert_nothing_created(self):
        self.assertEqual(os.listdir(self.root), [])

    def assert_claude_never_ran(self):
        self.assertEqual(read_jsonl(self.claude + ".log.jsonl"), [])


class Refusals(RunBaseline):
    def test_refuses_inside_a_claude_code_session(self):
        for script in (SCRIPT, PREPARE):
            with self.subTest(script=os.path.basename(script)):
                proc = self.run_script("run-1", "--dest-root", self.root, "--source", self.src.path,
                                       env=self.env(CLAUDECODE="1"), script=script)
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertIn("CLAUDECODE", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assert_nothing_created()
        self.assert_claude_never_ran()

    def test_refuses_dry_run_inside_a_session_too(self):
        proc = self.prepare("run-1", "--dry-run", env=self.env(CLAUDECODE="1"))
        self.assertEqual(proc.returncode, 3)

    def test_refuses_when_claude_is_absent(self):
        proc = self.prepare(env=self.env(KIT_CLAUDE_BIN=os.path.join(self.bins, "no-such-claude")))
        self.assertEqual(proc.returncode, 4)
        self.assertIn("not found", proc.stderr)
        self.assert_nothing_created()
        env = self.env()
        env.pop("KIT_CLAUDE_BIN")
        env["PATH"] = "/usr/bin:/bin"  # no claude here
        proc = self.prepare(env=env)
        self.assertEqual(proc.returncode, 4)

    def test_refuses_a_missing_tag(self):
        self.src.git("tag", "-d", "v1.2.0")
        proc = self.prepare()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("tag v1.2.0 does not exist", proc.stderr)
        self.assert_nothing_created()

    def test_refuses_a_run_directory_inside_the_source_repo(self):
        proc = self.run_script("run-1", "--dest-root", os.path.join(self.src.path, "runs"),
                               "--source", self.src.path)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("inside the kit repo", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.src.path, "runs")))

    def test_refuses_a_run_directory_nested_in_another_repo(self):
        other = self.repo()
        proc = self.run_script("run-1", "--dest-root", os.path.join(other.path, "x"), "--source", self.src.path)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nested inside the git repo", proc.stderr)

    def test_refuses_a_non_empty_run_directory_and_reuse_of_a_run_id(self):
        os.makedirs(self.dest())
        with open(os.path.join(self.dest(), "x"), "w") as fh:
            fh.write("x")
        proc = self.prepare()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not empty", proc.stderr)
        self.assertEqual(self.prepare("run-2").returncode, 0)
        os.rename(self.dest("run-2"), os.path.join(self.root, "moved"))
        proc = self.prepare("run-2")  # run-2.observe.jsonl and manifest remain
        self.assertEqual(proc.returncode, 3)
        self.assertIn("already exists", proc.stderr)

    def test_refuses_bad_run_ids(self):
        for bad in ("../x", "a b", ".hidden", "a/b"):
            with self.subTest(bad):
                self.assertEqual(self.prepare(bad).returncode, 2)
        self.assert_nothing_created()

    def test_refuses_a_source_that_is_not_a_repo(self):
        proc = self.run_script("run-1", "--dest-root", self.root, "--source", self.tmpdir("plain-"))
        self.assertEqual(proc.returncode, 3)


class DryRun(RunBaseline):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        proc = self.prepare("run-1", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertIn("tag                : v1.2.0 (%s)" % self.tag_commit[:12], proc.stdout)
        self.assertIn("claude \"/orchestrate\"", proc.stdout)
        self.assert_nothing_created()
        self.assert_claude_never_ran()


class Prepare(RunBaseline):
    def setUp(self):
        super().setUp()
        self.proc = self.prepare()
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.d = self.dest()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.d] + list(args), env=clean_env(home=self.home),
                              stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()

    def read(self, rel):
        with open(os.path.join(self.d, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_clone_is_at_the_tag_on_main_with_no_remote_and_a_clean_tree(self):
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.git("rev-parse", "HEAD~1"), self.tag_commit)
        self.assertEqual(self.git("remote"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.d, "LATER.md")))
        self.assertEqual(self.read("README.md"), "kit readme\n")

    def test_fixture_is_overlaid_without_the_answer_key(self):
        for present in ("build-plan.json", "hello_loop/migrate.py", "tests/test_health.py", "pyproject.toml",
                        ".python-version", "scripts/verify_run.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.d, present)), present)
        for absent in ("reference-solution", "acceptance", "kit-overlay", "verify.json", "hello_loop/seed.py",
                       "hello_loop/repos", "tests/test_schema.py"):
            self.assertFalse(os.path.exists(os.path.join(self.d, absent)), absent)
        with open(os.path.join(FIXTURE, "build-plan.json"), encoding="utf-8") as fh:
            self.assertEqual(self.read("build-plan.json"), fh.read())
        self.assertNotIn("hello-loop", self.read("README.md"))

    def test_overlay_replaces_template_files_and_removals_are_applied(self):
        self.assertIn("# hello-loop", self.read(".claude/CLAUDE.md"))
        self.assertNotIn("<!--", self.read(".claude/CLAUDE.md"))
        self.assertIn("verify_tests: python3 -m unittest", self.read(".claude/loop.config.md"))
        self.assertIn("scripts/verify_run.py", self.read(".claude/context/local-dev.md"))
        for gone in (".claude/context/admin-app.md", ".claude/context/infra.md",
                     ".claude/agents/admin-app-engineer.md", ".claude/agents/infra-engineer.md"):
            self.assertFalse(os.path.exists(os.path.join(self.d, gone)), gone)
        self.assertTrue(os.path.isfile(os.path.join(self.d, ".claude/context/app.md")))
        self.assertNotIn("<!--", self.read(".claude/context/app.md"))

    def test_gitignore_keeps_the_kit_lines_and_adds_the_fixture_lines(self):
        lines = self.read(".gitignore").split("\n")
        for line in (".claude/loop.jsonl", "node_modules/", ".data/", "__pycache__/"):
            self.assertIn(line, lines)

    def test_settings_keep_existing_hooks_add_observation_and_enable_the_sandbox(self):
        settings = json.loads(self.read(".claude/settings.json"))
        self.assertEqual(settings["permissions"], {"allow": []})
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], V1_SETTINGS["hooks"]["PreToolUse"][0])
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[1]["matcher"], "*")
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "SubagentStop", "Stop", "PreCompact",
                      "PermissionRequest", "Notification", "SessionEnd"):
            self.assertIn(event, settings["hooks"], event)
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["network"]["allowLocalBinding"])

    def test_installed_hook_command_runs_and_logs_outside_the_clone(self):
        settings = json.loads(self.read(".claude/settings.json"))
        command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        shim = self.tmpdir("shim-")
        os.symlink(sys.executable, os.path.join(shim, "python3"))
        env = clean_env(home=self.home)
        env["PATH"] = shim + os.pathsep + env["PATH"]
        proc = subprocess.run(command, shell=True, env=env, input=b'{"hook_event_name":"PostToolUse"}',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        log = self.d + ".observe.jsonl"
        self.assertEqual(read_jsonl(log)[0]["event"], {"hook_event_name": "PostToolUse"})
        self.assertFalse(log.startswith(self.d + os.sep))

    def test_manifest_records_the_facts(self):
        with open(self.d + ".baseline.json", encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertEqual(m["kit_commit"], self.tag_commit)
        self.assertEqual(m["kit_tag"], "v1.2.0")
        self.assertEqual(m["run_commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(m["observe_log"], self.d + ".observe.jsonl")
        self.assertRegex(m["fixture_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(m["removed_paths_missing"], [])
        self.assertIn("PreToolUse", m["hook_events"])

    def test_prints_the_command_with_credentials_unset_and_never_runs_claude(self):
        out = self.proc.stdout
        self.assertIn("cd %s && env " % self.d, out)
        for var in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS",
                    "SSH_AUTH_SOCK"):
            self.assertIn("-u %s" % var, out)
        self.assertIn('claude "/orchestrate"', out)
        self.assertIn("run_checks.py", out)
        self.assert_claude_never_ran()

    def test_fixture_digest_is_stable_and_hex(self):
        sys.path.insert(0, BASELINE_DIR)
        try:
            import prepare as mod
        finally:
            sys.path.pop(0)
        before = mod.fixture_digest(FIXTURE)
        self.assertEqual(before, mod.fixture_digest(FIXTURE))
        self.assertRegex(before, r"^[0-9a-f]{64}$")

    def test_two_runs_are_independent(self):
        self.assertEqual(self.prepare("run-2").returncode, 0)
        self.assertTrue(os.path.isdir(self.dest("run-2")))
        self.assertNotEqual(self.git("rev-parse", "--show-toplevel"),
                            subprocess.run(["git", "-C", self.dest("run-2"), "rev-parse", "--show-toplevel"],
                                           stdout=subprocess.PIPE, universal_newlines=True).stdout.strip())

    def test_removal_list_with_an_unsafe_path_is_rejected(self):
        fixture = self.tmpdir("fixture-")
        os.makedirs(os.path.join(fixture, "kit-overlay"))
        with open(os.path.join(fixture, "build-plan.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(fixture, "kit-overlay", "remove.txt"), "w") as fh:
            fh.write("../../etc/passwd\n")
        proc = self.prepare("run-9", "--fixture", fixture)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("unsafe path", proc.stderr)
        self.assertFalse(os.path.exists(self.dest("run-9")))  # nothing half-built is left behind


class Cleanup(RunBaseline):
    def test_a_failure_after_the_clone_leaves_nothing_behind(self):
        bad = self.repo()
        bad.write(".claude/settings.json", "{not json")
        bad.git("add", "-A")
        bad.git("commit", "-q", "-m", "tree")
        bad.git("tag", "v1.2.0")
        proc = self.run_script("run-1", "--dest-root", self.root, "--source", bad.path)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assert_nothing_created()
