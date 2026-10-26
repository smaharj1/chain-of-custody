"""tools/probes/make-scratch.sh and make_scratch.py: refusals, --dry-run, and what a scratch repo holds."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from unittest import mock

from tests import REPO_ROOT
from tests.helpers import BaseTestCase, clean_env, install_fake_claude, read_jsonl
from tests.probes import PROBES_DIR, SETTINGS_DIR, load_tool

SCRIPT = os.path.join(PROBES_DIR, "make-scratch.sh")
PY = os.path.join(PROBES_DIR, "make_scratch.py")
ms = load_tool("make_scratch")


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def text_of(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def tree(path):
    out = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), path))
    return sorted(out)


class ScratchCase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmpdir("probes-")
        self.home = self.tmpdir("home-")
        self.bins = self.tmpdir("bins-")
        self.claude = install_fake_claude(self.bins, {})

    def env(self, **extra):
        return clean_env(home=self.home, extra=dict({"KIT_CLAUDE_BIN": self.claude, "PATH": self.bins + os.pathsep + os.environ["PATH"]}, **extra))

    def run_script(self, *args, env=None, script=SCRIPT):
        cmd = ["sh", script] if script.endswith(".sh") else [sys.executable, script]
        return subprocess.run(cmd + list(args), env=env or self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, cwd=self.tmpdir("cwd-"))

    def make(self, name, *args, **kw):
        return self.run_script(os.path.join(self.root, name), *args, **kw)

    def path(self, name):
        return os.path.join(os.path.realpath(self.root), name)

    def settings(self, name):
        return jload(os.path.join(self.path(name), ".claude", "settings.json"))

    def assert_claude_never_ran(self):
        self.assertEqual(read_jsonl(self.claude + ".log.jsonl"), [])


class Refusals(ScratchCase):
    def test_refuses_inside_a_claude_code_session_from_both_entry_points(self):
        for script in (SCRIPT, PY):
            for extra in ((), ("--dry-run",)):
                with self.subTest(script=os.path.basename(script), extra=extra):
                    proc = self.make("v1", "--probe", "V1", *extra, env=self.env(CLAUDECODE="1"), script=script)
                    self.assertEqual(proc.returncode, 3, proc.stderr)
                    self.assertIn("CLAUDECODE", proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(os.listdir(self.root), [])
        self.assert_claude_never_ran()

    def test_refuses_a_scratch_inside_the_kit_repo(self):
        target = os.path.join(REPO_ROOT, "zz-probe-scratch-never-created")
        proc = self.run_script(target, "--probe", "V1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("inside the kit repo", proc.stderr)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".observe.jsonl"))

    def test_refuses_a_scratch_nested_in_another_repo(self):
        other = self.repo()
        proc = self.run_script(os.path.join(other.path, "x"), "--probe", "V1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nested inside the git repo", proc.stderr)

    def test_refuses_a_non_empty_directory_and_reuse(self):
        os.makedirs(self.path("v1"))
        with open(os.path.join(self.path("v1"), "x"), "w") as fh:
            fh.write("x")
        proc = self.make("v1", "--probe", "V1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not empty", proc.stderr)
        self.assertEqual(self.make("v2", "--probe", "V1").returncode, 0)
        os.rename(self.path("v2"), self.path("moved"))
        proc = self.make("v2", "--probe", "V1")  # v2.observe.jsonl and v2.probe.json remain
        self.assertEqual(proc.returncode, 3)
        self.assertIn("already exists", proc.stderr)

    def test_refuses_unknown_closed_and_ill_formed_requests(self):
        cases = [(("--probe", "V99"), "unknown probe"),
                 (("--probe", "V2"), "closed"),
                 (("--probe", "V10"), "moot"),
                 (("--probe", "V11"), "closed"),
                 (("--probe", "V9"), "needs --variant"),
                 (("--probe", "V9", "--variant", "nope"), "no variant"),
                 (("--probe", "V18"), "needs --variant"),
                 (("--probe", "V1", "--control"), "no --control form"),
                 (("--probe", "V1", "--events", "Pre Tool"), "bad event name")]
        for args, text in cases:
            with self.subTest(args=args):
                proc = self.make("x", *args)
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertIn(text, proc.stderr)
        self.assertEqual(os.listdir(self.root), [])

    def test_usage_error_is_exit_two(self):
        self.assertEqual(self.run_script(os.path.join(self.root, "x")).returncode, 2)

    def test_probes_that_need_no_scratch_say_so_and_create_nothing(self):
        for probe in ("V15", "V27"):
            proc = self.make("x", "--probe", probe)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("needs no scratch repo", proc.stdout)
        self.assertEqual(os.listdir(self.root), [])


class DryRun(ScratchCase):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        proc = self.make("deep/v9", "--probe", "V9", "--variant", "crash", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[dry-run]", proc.stdout)
        self.assertIn("V9-crash.json", proc.stdout)
        self.assertIn(self.path("deep/v9"), proc.stdout)
        self.assertEqual(os.listdir(self.root), [])  # not even the parent directory
        self.assert_claude_never_ran()

    def test_dry_run_shows_the_control_form(self):
        proc = self.make("v9", "--probe", "V9", "--variant", "crash", "--control", "--dry-run")
        self.assertIn("onFailure stripped", proc.stdout)


class Creation(ScratchCase):
    def test_v1_scratch_has_a_clean_repo_the_logger_on_every_event_and_the_agents(self):
        proc = self.make("v1", "--probe", "V1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p = self.path("v1")
        self.assertEqual(self.run_git(p, "status", "--porcelain"), "")
        self.assertEqual(self.run_git(p, "branch", "--show-current"), "main")
        self.assertEqual(len(self.run_git(p, "log", "--oneline").splitlines()), 1)
        files = tree(p)
        for rel in (".claude/agents/probe-outer.md", ".claude/agents/probe-middle.md", ".claude/agents/probe-leaf.md",
                    ".claude/probe/probe_hook.py", "PROBE-PROMPT.txt", "PROBE-INFO.txt", "README.md", ".claude/settings.json"):
            self.assertIn(rel, files)
        s = self.settings("v1")
        self.assertEqual(sorted(s["hooks"]), sorted(ms.ALL_EVENTS))
        for event, entries in s["hooks"].items():
            cmd = entries[0]["hooks"][0]["command"]
            self.assertIn("observe.py", cmd)
            self.assertIn(self.path("v1.observe.jsonl"), cmd)
            if event in ms.TOOL_EVENTS:
                self.assertEqual(entries[0]["matcher"], "*")
        self.assertEqual(s["env"], {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3"})
        self.assertIn("Bash(echo probe-*)", s["permissions"]["allow"])
        manifest = jload(self.path("v1.probe.json"))
        self.assertEqual((manifest["probe"], manifest["schema"]), ("V1", "probe-manifest.v1"))
        self.assertFalse(os.path.exists(self.path("v1.observe.jsonl")))  # created by the first hook event
        for text in ("export SCRATCH=", "export LOG=", "claude --version", "analyze.py"):
            self.assertIn(text, proc.stdout)
        self.assertNotIn("{{", json.dumps(s))
        self.assert_claude_never_ran()

    def run_git(self, path, *args):
        return subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, env=self.env()).stdout.strip()

    def test_the_installed_logger_command_really_logs(self):
        self.make("v1", "--probe", "V1")
        s = self.settings("v1")
        cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        event = {"hook_event_name": "PreToolUse", "session_id": "s1", "agent_id": "a1", "tool_name": "Bash"}
        proc = subprocess.run(["sh", "-c", cmd], input=json.dumps(event).encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env())
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        rows = read_jsonl(self.path("v1.observe.jsonl"))
        self.assertEqual(rows[0]["event"]["agent_id"], "a1")

    def test_snippets_merge_by_concatenating_hooks_and_permissions(self):
        self.make("v9", "--probe", "V9", "--variant", "crash")
        s = self.settings("v9")
        for event in ("PreToolUse", "PermissionRequest", "ConfigChange"):
            entries = s["hooks"][event]
            self.assertIn("observe.py", entries[0]["hooks"][0]["command"])
            probe_cmds = [h for e in entries[1:] for h in e["hooks"]]
            self.assertEqual(len(probe_cmds), 1, event)
            self.assertEqual(probe_cmds[0]["onFailure"], "block")
            self.assertIn("probe_hook.py", probe_cmds[0]["command"])
            self.assertIn("crash", probe_cmds[0]["command"])
        self.assertEqual(s["hooks"]["PreToolUse"][1]["matcher"], "Bash")
        self.assertEqual(s["hooks"]["PermissionRequest"][1]["matcher"], "Write")
        self.assertTrue(os.path.isfile(os.path.join(self.path("v9"), "probe-v9-secret.txt")))
        self.assertEqual(text_of(os.path.join(self.path("v9"), ".claude", "settings.local.json")).strip(), "{}")
        # the local settings file is ignored, so the tree is clean even after the human edits it
        self.assertIn(".claude/settings.local.json", text_of(os.path.join(self.path("v9"), ".gitignore")))

    def test_control_strips_the_control_key(self):
        self.make("c", "--probe", "V9", "--variant", "timeout", "--control")
        text = json.dumps(self.settings("c"))
        self.assertNotIn("onFailure", text)
        self.assertIn("sleep", text)
        self.assertTrue(jload(self.path("c.probe.json"))["control"])
        self.make("d", "--probe", "V19", "--control")
        self.assertNotIn("failIfUnavailable", json.dumps(self.settings("d")))
        self.make("e", "--probe", "V19")
        self.assertTrue(self.settings("e")["sandbox"]["failIfUnavailable"])

    def test_placeholders_are_resolved_in_snippets_and_files(self):
        self.make("v5", "--probe", "V5")
        p = self.path("v5")
        s = self.settings("v5")
        self.assertEqual(s["sandbox"]["enabled"], True)
        headless = text_of(os.path.join(p, ".claude", "probe", "headless-settings.json"))
        self.assertNotIn("{{", headless)
        self.assertIn(self.path("v5.observe.jsonl"), headless)
        json.loads(headless)
        self.make("v24", "--probe", "V24")
        self.assertEqual(self.settings("v24")["sandbox"]["filesystem"]["denyRead"], [self.path("v24") + "/heldout"])

    def test_v5_records_the_branch_commits_in_the_manifest(self):
        self.make("v5", "--probe", "V5")
        p = self.path("v5")
        self.assertEqual(self.run_git(p, "branch", "--show-current"), "feature/probe-v5")
        facts = jload(self.path("v5.probe.json"))["facts"]
        self.assertEqual(facts["feature/probe-v5"], self.run_git(p, "rev-parse", "--short", "HEAD"))
        self.assertNotEqual(facts["main"], facts["feature/probe-v5"])

    def test_v6_gets_a_local_bare_remote_and_a_feature_branch_one_commit_ahead(self):
        proc = self.make("v6", "--probe", "V6")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        p, remote = self.path("v6"), self.path("v6.remote.git")
        self.assertEqual(self.run_git(p, "remote", "get-url", "origin"), remote)
        self.assertEqual(self.run_git(p, "branch", "--show-current"), "feature/probe")
        self.assertEqual(self.run_git(p, "rev-list", "--count", "origin/main..HEAD"), "1")
        self.assertEqual(self.run_git(p, "symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")
        self.assertEqual(self.run_git(p, "--git-dir", remote, "branch", "--list").split()[-1], "main")
        facts = jload(self.path("v6.probe.json"))["facts"]
        self.assertEqual(set(facts), {"main", "origin/main", "feature/probe"})
        # V25 reuses the V6 files and adds the setting
        self.make("v25", "--probe", "V25")
        self.assertEqual(self.settings("v25")["worktree"], {"baseRef": "head"})
        self.assertIn(".claude/agents/probe-wt.md", tree(self.path("v25")))

    def test_v17_has_a_remote_and_no_agents(self):
        self.make("v17", "--probe", "V17")
        self.assertTrue(os.path.isdir(self.path("v17.remote.git")))
        self.assertEqual(self.run_git(self.path("v17"), "branch", "--show-current"), "main")

    def test_v21_writes_the_managed_file_next_to_the_repo_not_into_the_project_settings(self):
        proc = self.make("v21", "--probe", "V21")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        managed = jload(os.path.join(self.path("v21"), ".claude", "probe", "managed-settings.json"))
        self.assertTrue(managed["allowManagedHooksOnly"])
        self.assertNotIn("allowManagedHooksOnly", self.settings("v21"))
        self.assertNotIn("probe-v21-denied", json.dumps(self.settings("v21")))
        self.assertIn("managed-settings.json", proc.stdout)

    def test_events_override(self):
        self.make("v1", "--probe", "V1", "--events", "SessionStart,PreToolUse")
        self.assertEqual(sorted(self.settings("v1")["hooks"]), ["PreToolUse", "SessionStart"])

    def test_every_probe_and_variant_can_be_prepared(self):
        for probe, spec in sorted(ms.PROBES.items()):
            if spec.get("scratch") is False:
                continue
            variants = ms.variants_of(probe) if probe != "V21" else [""]
            for variant in (variants if spec.get("variants_required") else [""]):
                name = "%s-%s" % (probe.lower(), variant or "plain")
                args = ["--probe", probe] + (["--variant", variant] if variant else [])
                with self.subTest(probe=probe, variant=variant):
                    proc = self.make(name, *args)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    p = self.path(name)
                    self.assertEqual(self.run_git(p, "status", "--porcelain"), "")
                    text = json.dumps(self.settings(name))
                    self.assertNotIn("{{", text)
                    for rel in tree(p):
                        if rel.endswith((".json", ".md", ".txt", ".sh", ".py")):
                            with open(os.path.join(p, rel), encoding="utf-8") as fh:
                                self.assertNotIn("{{", fh.read(), rel)
        self.assert_claude_never_ran()


class Cleanup(ScratchCase):
    def test_a_failure_part_way_removes_everything(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDECODE", None)
            scratch = self.path("half")
            with mock.patch.object(ms, "_git", side_effect=ms.ProbeError("boom")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = ms.main([scratch, "--probe", "V6"])
        self.assertEqual(code, 3)
        self.assertEqual(os.listdir(self.root), [])

    def test_the_wrapper_is_posix_sh(self):
        self.assertEqual(subprocess.run(["sh", "-n", SCRIPT]).returncode, 0)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


class MergeUnit(BaseTestCase):
    def test_merge_settings_unit(self):
        base = {"hooks": {"A": [{"x": 1}]}, "permissions": {"allow": ["a"]}, "sandbox": {"enabled": True}}
        extra = {"hooks": {"A": [{"x": 2}], "B": [{"y": 1}]}, "permissions": {"allow": ["a", "b"], "defaultMode": "dontAsk"},
                 "sandbox": {"network": {"allowLocalBinding": True}}, "env": {"K": "v"}}
        out = ms.merge_settings(base, extra)
        self.assertEqual(out["hooks"], {"A": [{"x": 1}, {"x": 2}], "B": [{"y": 1}]})
        self.assertEqual(out["permissions"], {"allow": ["a", "b"], "defaultMode": "dontAsk"})
        self.assertEqual(out["sandbox"], {"enabled": True, "network": {"allowLocalBinding": True}})
        self.assertEqual(out["env"], {"K": "v"})
        self.assertEqual(base["hooks"]["A"], [{"x": 1}])  # inputs untouched

    def test_strip_key_is_recursive(self):
        self.assertEqual(ms.strip_key({"a": [{"onFailure": 1, "b": 2}], "onFailure": 3}, "onFailure"), {"a": [{"b": 2}]})
