"""hooks.manifest.json, wiring.py, settings.v2.json and the §7.2 templates (report §7.1 handler hygiene, §7.2, IMP-13, IMP-16)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

from guards import engine, wiring
from guards.engine import GUARDS

from tests import REPO_ROOT
from tests.helpers import BaseTestCase

SETTINGS_V2 = os.path.join(REPO_ROOT, ".claude", "settings.v2.json")
TEMPLATES = os.path.join(REPO_ROOT, ".claude", "templates", "settings")
EXTRA_GUARDS = {"injection", "telemetry", "selftest"}


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = wiring.load_manifest()

    def test_generated_files_match_the_manifest(self) -> None:
        proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "wiring.py"), "--check"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_every_guard_named_in_the_manifest_exists(self) -> None:
        for e in self.manifest["entries"]:
            for g in e.get("guards", []):
                self.assertTrue(g in GUARDS or g in EXTRA_GUARDS, "%s names unknown guard %s" % (e["id"], g))

    def test_ids_are_unique_and_scripts_exist(self) -> None:
        ids = [e["id"] for e in self.manifest["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in self.manifest["entries"]:
            if e.get("script"):
                self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, ".claude", "scripts", e["script"])), e["script"])

    def test_blocking_events_are_blocking_entries(self) -> None:
        for e in self.manifest["entries"]:
            self.assertEqual(bool(e.get("blocking")), e["event"] in engine.BLOCKING_EVENTS, e["id"])

    def test_halt_is_first_on_every_pre_tool_entry(self) -> None:
        for e in self.manifest["entries"]:
            if e["event"] == "PreToolUse":
                self.assertEqual(e["guards"][0], "halt", e["id"])

    def test_matchers_route_the_right_tools(self) -> None:
        def ids(tool, event="PreToolUse"):
            return [e["id"] for e in engine.entries_for(self.manifest, event, tool)]
        self.assertEqual(ids("Bash"), ["pre-bash"])
        for t in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertEqual(ids(t), ["pre-edit"], t)
        self.assertEqual(ids("mcp__aws__run_script"), ["pre-mcp"])
        for t in ("Agent", "Task", "Workflow"):
            self.assertEqual(ids(t), ["pre-dispatch"], t)
        for t in ("Read", "Grep", "Glob", "WebFetch"):
            self.assertEqual(ids(t), [], t)
        self.assertEqual(sorted(ids("WebFetch", "PostToolUse")), ["post-telemetry", "post-untrusted"])
        self.assertEqual(ids("Glob", "PostToolUse"), ["post-telemetry"])
        self.assertEqual(ids("", "ConfigChange"), ["config-change"])

    def test_every_handler_has_a_timeout_and_a_quoted_root(self) -> None:
        for event, groups in wiring.build_hooks(self.manifest).items():
            for g in groups:
                for h in g["hooks"]:
                    self.assertGreater(h["timeout"], 0)
                    self.assertLessEqual(h["timeout"], 30)
                    self.assertIn('"$CLAUDE_PROJECT_DIR/', h["command"])
                    self.assertNotRegex(h["command"], r"(?<!\")\$CLAUDE_PROJECT_DIR")
                    self.assertNotIn("http", h["command"].replace("hookSpecificOutput", ""))

    def test_on_failure_block_on_every_blocking_capable_handler(self) -> None:
        hooks = wiring.build_hooks(self.manifest)
        for event in engine.BLOCKING_EVENTS:
            for g in hooks.get(event, []):
                for h in g["hooks"]:
                    self.assertEqual(h.get("onFailure"), "block", event)
        for event in ("PostToolUse", "SubagentStart", "SubagentStop", "PermissionDenied", "SessionStart"):
            for g in hooks.get(event, []):
                for h in g["hooks"]:
                    self.assertNotIn("onFailure", h, "no effect on non-blocking events [docs]; not set")

    def test_session_start_runs_the_canary_and_the_selftest(self) -> None:
        cmds = [h["command"] for g in wiring.build_hooks(self.manifest)["SessionStart"] for h in g["hooks"]]
        self.assertTrue(any("session_start.py" in c for c in cmds))
        self.assertTrue(any("session-start-selftest" in c for c in cmds))

    def test_settings_v2_has_the_live_files_shape(self) -> None:
        v2 = load(SETTINGS_V2)
        live = load(os.path.join(REPO_ROOT, ".claude", "settings.json"))
        self.assertEqual(set(v2), {"permissions", "hooks"})
        self.assertEqual(v2["permissions"], live["permissions"])
        self.assertEqual(v2["hooks"], wiring.build_hooks(self.manifest))

    def test_plugin_form_uses_the_plugin_root(self) -> None:
        doc = wiring.build_plugin_hooks(self.manifest)
        cmds = [h["command"] for gs in doc["hooks"].values() for g in gs for h in g["hooks"]]
        self.assertTrue(all("${CLAUDE_PLUGIN_ROOT}/scripts/" in c for c in cmds))
        self.assertFalse(any("CLAUDE_PROJECT_DIR" in c for c in cmds))


class GeneratedCommandTests(BaseTestCase):
    """Run the wired command string itself, through sh, the way the platform does."""

    def sh(self, command, payload, env_extra=None, path=None):
        env = {"CLAUDE_PROJECT_DIR": REPO_ROOT, "PATH": path if path is not None else os.environ.get("PATH", ""), "HOME": self.tmpdir(), "LC_ALL": "C"}
        env.update(env_extra or {})
        proc = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              cwd=self.tmpdir())
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def cmd(self, entry_id):
        man = wiring.load_manifest()
        e = [x for x in man["entries"] if x["id"] == entry_id][0]
        return wiring.command_for(e)

    def test_wired_pre_bash_command_blocks_end_to_end(self) -> None:
        rc, out, err = self.sh(self.cmd("pre-bash"), {"session_id": "s1", "cwd": self.tmpdir(), "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                                       "tool_input": {"command": "git push origin main"}})
        self.assertEqual(rc, 2, err)
        self.assertIn("guard-git:push", err)

    def test_wired_pre_bash_command_allows(self) -> None:
        rc, out, err = self.sh(self.cmd("pre-bash"), {"session_id": "s1", "cwd": self.tmpdir(), "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                                       "tool_input": {"command": "ls"}})
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_missing_python_fails_closed_on_a_blocking_entry(self) -> None:
        rc, out, err = self.sh(self.cmd("pre-bash"), {"x": 1}, path="/nonexistent")
        self.assertEqual(rc, 2)
        self.assertIn("guards:no-python3", err)

    def test_missing_python_at_session_start_prints_a_banner(self) -> None:
        for entry in ("session-start-canary", "session-start-selftest"):
            rc, out, err = self.sh(self.cmd(entry), {"x": 1}, path="/nonexistent")
            self.assertEqual(rc, 0, entry)
            doc = json.loads(out)["hookSpecificOutput"]
            self.assertEqual(doc["hookEventName"], "SessionStart")
            self.assertIn("python3 was not found", doc["additionalContext"])

    def test_missing_python_on_a_diagnostic_entry_is_silent(self) -> None:
        rc, out, err = self.sh(self.cmd("post-telemetry"), {"x": 1}, path="/nonexistent")
        self.assertEqual((rc, out), (0, ""))

    def test_unset_project_dir_fails_closed(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": ""}
        rc, out, err = self.sh(self.cmd("pre-bash"), {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}, env_extra=env)
        self.assertNotEqual(rc, 0, "python cannot open /.claude/scripts/guards/run.py: exit 2, a block")
        self.assertEqual(rc, 2)


class TemplateTests(unittest.TestCase):
    def tmpl(self, name):
        return load(os.path.join(TEMPLATES, name))

    def test_workflow_is_denied_headlessly_and_never_allowed(self) -> None:
        for name in ("headless-child.settings.json", "critic.settings.json", "interactive.settings.json", "deny-baseline.json"):
            doc = self.tmpl(name)
            self.assertIn("Workflow", doc["permissions"]["deny"], name)
            self.assertFalse([a for a in doc["permissions"].get("allow", []) if a.startswith("Workflow") or a in ("*",)], name)

    def test_headless_profile(self) -> None:
        h = self.tmpl("headless-child.settings.json")
        self.assertEqual(h["permissions"]["defaultMode"], "dontAsk")
        self.assertIs(h["enableAllProjectMcpServers"], False)
        self.assertTrue(h["permissions"]["allow"], "an explicit allowedTools list")
        self.assertNotIn("Bash", h["permissions"]["allow"], "no blanket Bash")
        self.assertFalse([a for a in h["permissions"]["allow"] if re.match(r"^Bash\((npm|git|pnpm|yarn|python3?):\*\)$", a)], "named scripts, not Bash(npm:*)")
        self.assertEqual(h["hooks"], wiring.build_hooks(wiring.load_manifest()), "the child passes the kit hooks explicitly (V5)")
        self.assertIn("env", h)

    def test_sandbox_profile_is_fail_closed(self) -> None:
        for name in ("headless-child.settings.json", "critic.settings.json", "interactive.settings.json"):
            sb = self.tmpl(name)["sandbox"]
            self.assertIs(sb["enabled"], True)
            self.assertIs(sb["failIfUnavailable"], True)
            self.assertIs(sb["allowUnsandboxedCommands"], False)
            self.assertNotIn("docker", " ".join(sb.get("excludedCommands", [])))
            self.assertIn("169.254.0.0/16", sb["network"]["deniedCidrs"])

    def test_critic_cannot_edit(self) -> None:
        deny = self.tmpl("critic.settings.json")["permissions"]["deny"]
        for t in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertIn(t, deny)

    def test_deny_block_contents(self) -> None:
        deny = self.tmpl("deny-baseline.json")["permissions"]["deny"]
        for must in ("Bash(terraform apply:*)", "Bash(cdk deploy:*)", "Bash(pulumi up:*)", "Bash(kubectl apply:*)", "Bash(helm install:*)",
                     "Bash(sam deploy:*)", "Bash(sudo:*)", "Bash(docker run --privileged:*)", "Bash(claude:*)", "Bash(git push --force:*)",
                     "Read(**/.env)", "Read(**/.env.production)", "Edit(.claude/settings.json)", "Edit(.claude/hooks/**)", "Edit(.claude/scripts/**)",
                     "Edit(.github/workflows/**)", "mcp__*__run_script", "mcp__*deploy*", "Workflow"):
            self.assertIn(must, deny)
        self.assertNotIn("Read(**/.env.example)", deny, ".env.example stays readable")
        self.assertEqual(len(deny), len(set(deny)))

    def test_deny_block_is_consistent_with_the_guards(self) -> None:
        """Every Bash(...) deny entry is also denied by the guards for every caller (the deny block is the second layer)."""
        from tests.guards.harness import Fixtures, evaluate_row
        fx = Fixtures()
        try:
            for entry in self.tmpl("deny-baseline.json")["permissions"]["deny"]:
                m = re.match(r"^Bash\((.*?)(:\*)?\)$", entry)
                if not m:
                    continue
                cmd = m.group(1).replace("\\:", ":")
                if cmd in ("su", "doas"):
                    cmd += " id"
                for caller in ("engineer", "conductor"):
                    with self.subTest(entry=entry, caller=caller):
                        d = evaluate_row(fx, {"cmd": cmd, "caller": caller, "repo": "feat", "config": {"delivery": "push-branch"}})
                        self.assertIsNotNone(d, "%r is in the deny block but the guards allow it for %s" % (cmd, caller))
        finally:
            fx.cleanup()


class DoctorAcceptsTheTemplates(BaseTestCase):
    def test_workflow_allow_check_passes_on_every_template(self) -> None:
        from doctorlib import settings as dsettings  # noqa: F401  (registers the check)
        from doctorlib.model import Context
        repo = self.repo()
        ctx = Context(repo=repo.path, home=self.tmpdir(), env={}, extra_settings=[os.path.join(TEMPLATES, n) for n in os.listdir(TEMPLATES) if n.endswith(".json")])
        res = dsettings.check_workflow_allow(ctx)
        self.assertTrue(all(r.status == "pass" for r in res), res)


if __name__ == "__main__":
    unittest.main()
