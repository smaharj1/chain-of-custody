"""tools/probes/files/common/probe_hook.py: the marker is written first, then the mode's behaviour."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from tests.helpers import BaseTestCase, clean_env, read_jsonl
from tests.probes import FILES_DIR

HOOK = os.path.join(FILES_DIR, "common", "probe_hook.py")
EVENT = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tool_use_id": "t1",
         "agent_id": "a1", "agent_type": "probe-leaf", "tool_input": {"command": "echo hi"}}


class ProbeHook(BaseTestCase):
    def run_hook(self, *args, stdin=None, timeout=20):
        log = os.path.join(self.tmpdir(), "log.jsonl")
        proc = subprocess.run([sys.executable, HOOK] + [a.replace("LOG", log) for a in args],
                              input=(stdin if stdin is not None else json.dumps(EVENT)).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              env=clean_env(home=self.tmpdir()))
        return proc, log

    def test_mark_writes_a_marker_and_exits_zero_silently(self):
        proc, log = self.run_hook("mark", "--tag", "t-mark", "--log", "LOG")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        row = read_jsonl(log)[0]
        self.assertEqual(row["marker"]["name"], "t-mark")
        self.assertEqual(row["marker"]["agent_id"], "a1")
        self.assertEqual(row["marker"]["agent_type"], "probe-leaf")
        self.assertEqual(row["marker"]["hook_event_name"], "PreToolUse")

    def test_crash_writes_the_marker_first_then_fails(self):
        proc, log = self.run_hook("crash", "--tag", "t-crash", "--log", "LOG")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"RuntimeError", proc.stderr)
        self.assertEqual(read_jsonl(log)[0]["marker"]["name"], "t-crash")

    def test_deny_exits_two_with_a_reason(self):
        proc, log = self.run_hook("deny", "--tag", "t-deny", "--log", "LOG")
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"probe-deny", proc.stderr)
        self.assertEqual(len(read_jsonl(log)), 1)

    def test_sleep_marks_then_sleeps(self):
        start = time.time()
        proc, log = self.run_hook("sleep", "--seconds", "0.3", "--tag", "t-sleep", "--log", "LOG")
        self.assertEqual(proc.returncode, 0)
        self.assertGreaterEqual(time.time() - start, 0.3)
        self.assertEqual(read_jsonl(log)[0]["marker"]["name"], "t-sleep")

    def test_inject_prints_additional_context_with_the_nonce(self):
        proc, log = self.run_hook("inject", "--tag", "t-inj", "--nonce", "N-123", "--log", "LOG",
                                  stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "source": "compact"}))
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("N-123", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(read_jsonl(log)[0]["marker"]["source"], "compact")

    def test_statusline_keeps_the_stdin_and_prints_a_line(self):
        proc, log = self.run_hook("statusline", "--tag", "t-sl", "--log", "LOG",
                                  stdin=json.dumps({"model": {"id": "x"}, "cost": {"total_cost_usd": 0.1}}))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), "probe-status")
        m = read_jsonl(log)[0]["marker"]
        self.assertEqual(m["stdin_keys"], ["cost", "model"])

    def test_garbage_stdin_still_marks(self):
        proc, log = self.run_hook("mark", "--tag", "t-bad", "--log", "LOG", stdin="not json {")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(read_jsonl(log)[0]["marker"]["name"], "t-bad")
