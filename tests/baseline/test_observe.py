"""tools/baseline/observe.py: always exit 0, redacted append-only JSONL, changes nothing."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.baseline import BASELINE_DIR, load_tool
from tests.helpers import BaseTestCase, clean_env, read_jsonl

OBSERVE = os.path.join(BASELINE_DIR, "observe.py")
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled so no secret-shaped literal sits in the repo
GH_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5


class Observe(BaseTestCase):
    def run_hook(self, stdin, *args, env_extra=None, home=None):
        env = clean_env(home=home or self.tmpdir("obs-home-"), extra=env_extra)
        proc = subprocess.run([sys.executable, OBSERVE] + list(args), input=stdin.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.tmpdir())
        return proc

    def test_appends_one_redacted_line_per_event_and_prints_nothing(self):
        log = os.path.join(self.tmpdir(), "sub", "obs.jsonl")
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "echo AWS_SECRET_ACCESS_KEY=" + "x" * 40 + " " + AWS_KEY},
                 "tool_response": {"stdout": "token " + GH_TOKEN}, "session_id": "s1"}
        for _ in range(2):
            proc = self.run_hook(json.dumps(event), "--log", log)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        rows = read_jsonl(log)
        self.assertEqual(len(rows), 2)
        with open(log) as fh:
            text = fh.read()
        for secret in (AWS_KEY, GH_TOKEN, "x" * 40):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED", text)
        self.assertEqual(rows[0]["event"]["hook_event_name"], "PostToolUse")
        self.assertRegex(rows[0]["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(oct(os.stat(log).st_mode & 0o777), oct(0o600))

    def test_log_path_from_environment_and_noop_without_one(self):
        log = os.path.join(self.tmpdir(), "env.jsonl")
        self.run_hook("{}", env_extra={"BASELINE_OBSERVE_LOG": log})
        self.assertEqual(len(read_jsonl(log)), 1)
        cwd_before = self.tmpdir()
        proc = self.run_hook("{}")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(os.listdir(cwd_before), [])

    def test_always_exits_zero_on_garbage_and_unwritable_log(self):
        log = os.path.join(self.tmpdir(), "g.jsonl")
        for stdin in ("", "not json at all", "[1, 2]", "\x00\xff", "{" * 5000):
            with self.subTest(stdin=stdin[:10]):
                self.assertEqual(self.run_hook(stdin, "--log", log).returncode, 0)
        rows = read_jsonl(log)
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual(self.run_hook("{}", "--log", "/proc/nonexistent/x.jsonl").returncode, 0)
        self.assertEqual(self.run_hook("{}", "--log").returncode, 0)
        blocker = os.path.join(self.tmpdir(), "file")
        open(blocker, "w").close()
        self.assertEqual(self.run_hook("{}", "--log", os.path.join(blocker, "x.jsonl")).returncode, 0)

    def test_home_directory_is_shown_as_tilde_and_long_strings_are_cut(self):
        home = self.tmpdir("obs-home2-")
        log = os.path.join(self.tmpdir(), "h.jsonl")
        big = "y" * 60000
        self.run_hook(json.dumps({"cwd": home + "/proj", "tool_response": {"stdout": big}}), "--log", log, home=home)
        ev = read_jsonl(log)[0]["event"]
        self.assertEqual(ev["cwd"], "~/proj")
        self.assertTrue(ev["tool_response"]["stdout"].endswith("...[truncated 10000 chars]"))

    def test_secret_straddling_the_truncation_point_is_still_redacted(self):
        tool = load_tool("observe")
        pad = "z " * 24999
        rec = tool.build_record(json.dumps({"s": pad + " " + AWS_KEY + " tail"}))
        self.assertNotIn(AWS_KEY, json.dumps(rec))

    def test_concurrent_appends_stay_whole_lines(self):
        log = os.path.join(self.tmpdir(), "c.jsonl")
        env = clean_env(home=self.tmpdir("obs-home3-"))
        procs = [subprocess.Popen([sys.executable, OBSERVE, "--log", log], stdin=subprocess.PIPE, env=env)
                 for _ in range(8)]
        for p in procs:
            p.communicate(json.dumps({"n": "x" * 2000}).encode())
        rows = read_jsonl(log)
        self.assertEqual(len(rows), 8)
