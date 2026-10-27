"""The hook protocol of run.py (IMP-22, IMP-16): exit 2 plus a one-line reason with the rule id on stderr, JSON for `ask` and
for context, fail closed on any exception, and the human state commands.

These go through the real script in a subprocess. The corpus (test_corpus.py) runs the same code in-process.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import unittest

from guards import cli, engine, telemetry
from kitlib import runstate

from tests import REPO_ROOT
from tests.helpers import BaseTestCase
from tests.helpers.fakehome import clean_env

RUN = os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "run.py")


def run_hook(event: str, payload, cwd: str, entry: str = None, raw: str = None, python: str = sys.executable, env=None):
    argv = [python, RUN, event] + (["--id", entry] if entry else [])
    proc = subprocess.run(argv, input=(raw if raw is not None else json.dumps(payload)).encode("utf-8"), cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or clean_env(cwd))
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


class HookProtocolTests(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.repo()
        self.repo.git("checkout", "-q", "-b", "feat/x")
        self.cwd = self.repo.path

    def hook(self, tool, tool_input, **extra):
        d = {"session_id": "s1", "cwd": self.cwd, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
        d.update(extra)
        return d

    def test_deny_exit_code_and_reason(self) -> None:
        rc, out, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git push origin main"}), self.cwd, "pre-bash")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("BLOCKED by guard-git:push: "), err)
        self.assertEqual(len(err.strip().splitlines()), 1, "one-line reason")

    def test_allow_is_silent(self) -> None:
        rc, out, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git status"}), self.cwd, "pre-bash")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_edit_guard(self) -> None:
        rc, _, err = run_hook("PreToolUse", self.hook("Write", {"file_path": os.path.join(self.cwd, ".claude", "settings.json"), "content": "{}"}), self.cwd, "pre-edit")
        self.assertEqual(rc, 2)
        self.assertIn("guard-file:settings", err)

    def test_ask_is_json_on_stdout(self) -> None:
        rc, out, err = run_hook("PreToolUse", self.hook("mcp__slack__slack_send_message", {}), self.cwd, "pre-mcp")
        self.assertEqual(rc, 0)
        doc = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(doc["hookEventName"], "PreToolUse")
        self.assertEqual(doc["permissionDecision"], "ask")
        self.assertIn("guard-infra:mcp", doc["permissionDecisionReason"])

    def test_malformed_input_fails_closed(self) -> None:
        for raw in ("", "not json", "[]", "null", "{"):
            with self.subTest(raw=raw):
                rc, _, err = run_hook("PreToolUse", None, self.cwd, "pre-bash", raw=raw)
                self.assertEqual(rc, 2)
                self.assertIn("guards:bad-input", err)

    def test_non_blocking_events_do_not_block_on_bad_input(self) -> None:
        rc, _, err = run_hook("PostToolUse", None, self.cwd, "post-telemetry", raw="not json")
        self.assertEqual(rc, 1)
        self.assertIn("guards:bad-input", err)

    def test_internal_error_fails_closed(self) -> None:
        orig = engine.evaluate
        engine.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            err, out = io.StringIO(), io.StringIO()
            rc = cli.main(["PreToolUse", "--id", "pre-bash"], stdin=io.StringIO(json.dumps(self.hook("Bash", {"command": "ls"}))), stdout=out, stderr=err)
        finally:
            engine.evaluate = orig
        self.assertEqual(rc, 2)
        self.assertIn("guards:internal-error", err.getvalue())

    def test_missing_manifest_fails_closed(self) -> None:
        orig = engine.MANIFEST_PATH
        engine.MANIFEST_PATH = os.path.join(self.tmpdir(), "missing.json")
        try:
            err = io.StringIO()
            rc = cli.main(["PreToolUse", "--id", "pre-bash"], stdin=io.StringIO(json.dumps(self.hook("Bash", {"command": "ls"}))), stdout=io.StringIO(), stderr=err)
        finally:
            engine.MANIFEST_PATH = orig
        self.assertEqual(rc, 2)

    def test_oversize_command_is_refused_not_skipped(self) -> None:
        rc, _, err = run_hook("PreToolUse", self.hook("Bash", {"command": "echo " + "a" * 600000}), self.cwd, "pre-bash")
        self.assertEqual(rc, 2)
        self.assertIn("guard-exec:parse-limit", err)

    def test_unknown_session_registry_is_least_privilege(self) -> None:
        rc, _, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git commit -m x"}), self.cwd, "pre-bash")
        self.assertEqual(rc, 2)
        self.assertIn("guard-git:readonly-caller", err)

    def test_registered_conductor_is_allowed(self) -> None:
        st = runstate.RunState.for_path(self.cwd).ensure()
        st.register_session("s1", "conductor")
        rc, _, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git commit -m x"}), self.cwd, "pre-bash")
        self.assertEqual((rc, err), (0, ""))
        rc, _, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git commit --no-verify -m x"}), self.cwd, "pre-bash")
        self.assertEqual(rc, 2)

    def test_halt_blocks_through_the_real_entry_point(self) -> None:
        runstate.RunState.for_path(self.cwd).ensure().set_halt("test", "human")
        rc, _, err = run_hook("PreToolUse", self.hook("Write", {"file_path": os.path.join(self.cwd, "src.py"), "content": "x"}), self.cwd, "pre-edit")
        self.assertEqual(rc, 2)
        self.assertIn("guard-halt:halted", err)

    def test_config_change_blocker(self) -> None:
        st = runstate.RunState.for_path(self.cwd).ensure()
        payload = {"session_id": "s1", "cwd": self.cwd, "hook_event_name": "ConfigChange", "source": "project_settings", "file_path": ".claude/settings.json"}
        rc, _, _ = run_hook("ConfigChange", payload, self.cwd, "config-change")
        self.assertEqual(rc, 0, "no run active")
        st.acquire_lock({"runId": "r", "sessionId": "s1"})
        rc, _, err = run_hook("ConfigChange", payload, self.cwd, "config-change")
        self.assertEqual(rc, 2)
        self.assertIn("guard-config-change", err)

    def test_dispatch_without_entry_id_uses_event_and_matcher(self) -> None:
        rc, _, err = run_hook("PreToolUse", self.hook("Bash", {"command": "git push"}), self.cwd)
        self.assertEqual(rc, 2)
        rc, _, err = run_hook("PreToolUse", self.hook("Read", {"file_path": "x"}), self.cwd)
        self.assertEqual(rc, 0, "a tool no entry matches is not guarded")

    def test_deny_is_recorded_as_an_event(self) -> None:
        run_hook("PreToolUse", self.hook("Bash", {"command": "git push"}), self.cwd, "pre-bash")
        st = runstate.RunState.for_path(self.cwd)
        events = st.read_events("s-s1")
        self.assertEqual(events[-1]["event"], "guard")
        self.assertEqual(events[-1]["rule"], "guard-git:push")
        self.assertNotIn("command", json.dumps(events[-1]), "no command text in telemetry")


class SelfTestCommand(BaseTestCase):
    def test_selftest_passes(self) -> None:
        proc = subprocess.run([sys.executable, RUN, "--selftest"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmpdir())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("behaved as expected", proc.stdout.decode())

    def test_session_start_selftest_records_and_stays_quiet(self) -> None:
        repo = self.repo()
        rc, out, err = run_hook("SessionStart", {"session_id": "s1", "cwd": repo.path, "hook_event_name": "SessionStart", "source": "startup"}, repo.path,
                                "session-start-selftest")
        self.assertEqual((rc, out), (0, ""))
        with open(os.path.join(runstate.RunState.for_path(repo.path).root, "selftest.json")) as fh:
            rec = json.load(fh)
        self.assertTrue(rec["ok"])
        self.assertGreaterEqual(rec["cases"], 10)

    def test_failed_selftest_is_reported_and_recorded(self) -> None:
        from guards import selftest
        repo = self.repo()
        orig = selftest.run_selftest
        selftest.run_selftest = lambda: (False, ["git push is denied: expected deny, got allow"], 15)
        try:
            out, err = io.StringIO(), io.StringIO()
            rc = cli.main(["SessionStart", "--id", "session-start-selftest"],
                          stdin=io.StringIO(json.dumps({"session_id": "s1", "cwd": repo.path, "hook_event_name": "SessionStart", "source": "startup"})),
                          stdout=out, stderr=err)
        finally:
            selftest.run_selftest = orig
        self.assertEqual(rc, 0)
        self.assertIn("SELFTEST FAILED", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        with open(os.path.join(runstate.RunState.for_path(repo.path).root, "selftest.json")) as fh:
            rec = json.load(fh)
        self.assertFalse(rec["ok"])

    def test_selftest_catches_a_guard_that_allows_everything(self) -> None:
        from guards import selftest
        orig = engine.evaluate
        engine.evaluate = lambda *a, **k: None
        try:
            ok, failures, n = selftest.run_selftest()
        finally:
            engine.evaluate = orig
        self.assertFalse(ok)
        self.assertGreaterEqual(len(failures), 10)

    def test_selftest_catches_a_guard_that_denies_everything(self) -> None:
        from guards import selftest
        from guards.lib import deny
        orig = engine.evaluate
        engine.evaluate = lambda *a, **k: deny("guard-x", "all", "no")
        try:
            ok, failures, n = selftest.run_selftest()
        finally:
            engine.evaluate = orig
        self.assertFalse(ok)
        self.assertTrue(any("expected allow" in f for f in failures))


class StateCommands(BaseTestCase):
    def run_cli(self, args, cwd, env=None):
        e = clean_env(cwd)
        e.update(env or {})
        proc = subprocess.run([sys.executable, RUN] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def test_register_and_unregister(self) -> None:
        repo = self.repo()
        rc, out, _ = self.run_cli(["register-session", "--session", "abc-123", "--role", "conductor"], repo.path)
        self.assertEqual(rc, 0)
        st = runstate.RunState.for_path(repo.path)
        self.assertEqual(st.get_session("abc-123")["role"], "conductor")
        self.assertEqual(self.run_cli(["unregister-session", "--session", "abc-123"], repo.path)[0], 0)
        self.assertIsNone(st.get_session("abc-123"))

    def test_state_commands_refuse_inside_claude_code(self) -> None:
        repo = self.repo()
        rc, _, err = self.run_cli(["set-halt", "--reason", "x"], repo.path, {"CLAUDECODE": "1"})
        self.assertEqual(rc, 2)
        self.assertIn("human command", err)

    def test_bad_session_id_is_refused(self) -> None:
        repo = self.repo()
        rc, _, err = self.run_cli(["register-session", "--session", "../x", "--role", "conductor"], repo.path)
        self.assertEqual(rc, 2)

    def test_halt_roundtrip(self) -> None:
        repo = self.repo()
        self.assertEqual(self.run_cli(["set-halt", "--reason", "stop"], repo.path)[0], 0)
        st = runstate.RunState.for_path(repo.path)
        self.assertTrue(st.is_halted())
        self.assertEqual(self.run_cli(["clear-halt"], repo.path)[0], 0)
        self.assertFalse(st.is_halted())

    def test_explain(self) -> None:
        proc = subprocess.run([sys.executable, RUN, "explain", "--caller", "engineer", "git", "commit", "--no-verify"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=self.tmpdir())
        out = proc.stdout.decode()
        self.assertIn("rule: guard-git:no-verify", out)
        self.assertIn("caller: engineer", out)


@unittest.skipUnless(os.path.exists("/usr/local/bin/python3.9") or shutil.which("python3.9"), "no Python 3.9 on this machine")
class Python39(BaseTestCase):
    def test_hook_runs_on_the_floor_interpreter(self) -> None:
        py = shutil.which("python3.9") or "/usr/local/bin/python3.9"
        repo = self.repo()
        rc, out, err = run_hook("PreToolUse", {"session_id": "s1", "cwd": repo.path, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                               "tool_input": {"command": "git push"}}, repo.path, "pre-bash", python=py)
        self.assertEqual(rc, 2, err)
        self.assertIn("guard-git:push", err)


if __name__ == "__main__":
    unittest.main()
