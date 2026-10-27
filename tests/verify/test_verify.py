"""verify.py: definition at the base sha, stages, environment scrub, process groups, ports, JSON contract."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

import verify

from . import FAILING_TEST, PASSING_TEST, PY, UNITTEST, VerifyCase, config_text
from .. import REPO_ROOT

VERIFY_PY = os.path.join(REPO_ROOT, ".claude", "scripts", "verify.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ids(res):
    return [f["id"] for f in res["findings"]]


def stage(res, name):
    return [s for s in res["stages"] if s["name"] == name][0]


class ParseCounts(unittest.TestCase):
    def test_runner_summaries(self):
        cases = [
            ("unittest", "..\n----\nRan 12 tests in 0.1s\n\nOK (skipped=2)\n", (12, 2)),
            ("unittest zero", "Ran 0 tests in 0.0s\n\nNO TESTS RAN\n", (0, 0)),
            ("pytest", "=== 10 passed, 2 skipped, 1 failed in 0.52s ===\n", (13, 2)),
            ("pytest quiet", "12 passed in 0.10s\n", (12, 0)),
            ("pytest none", "no tests ran in 0.01s\n", (0, 0)),
            ("jest", "Tests:       1 skipped, 11 passed, 12 total\nTime: 1s\n", (12, 1)),
            ("vitest", " Tests  12 passed | 1 skipped (13)\n", (13, 1)),
            ("cargo", "test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured\ntest result: ok. 2 passed; 0 failed; 0 ignored;\n", (8, 1)),
            ("mocha", "  7 passing (20ms)\n  1 pending\n", (8, 1)),
            ("go -v", "--- PASS: TestA (0.00s)\n--- SKIP: TestB (0.00s)\nPASS\n", (2, 1)),
            ("unknown", "all good\n", (None, None)),
        ]
        for name, text, want in cases:
            with self.subTest(name):
                self.assertEqual(verify.parse_test_counts(text), want)


class Definition(VerifyCase):
    def test_pass_and_the_json_contract(self):
        r = self.make_repo()
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "pass", res)
        self.assertTrue(res["ok"] and res["complete"] and res["trusted"])
        self.assertEqual(res["schema"], "verify.v1")
        self.assertEqual(res["base"], self.base)
        self.assertEqual(res["head"], r.head())
        self.assertEqual(res["config_source"]["kind"], "base-sha")
        self.assertEqual(res["failure_class"], "none")
        self.assertEqual([s["name"] for s in res["stages"]], ["build", "lint", "typecheck", "item-tests", "tests", "smoke"])
        self.assertEqual(stage(res, "tests")["status"], "pass")
        self.assertEqual(stage(res, "tests")["tests_collected"], 1)
        self.assertEqual(stage(res, "build")["status"], "skipped")
        self.assertEqual(verify.exit_code(res), 0)
        for key in ("repo", "item", "dirty", "routes", "ac_ids", "findings", "log", "duration_s", "message", "gate", "blocking"):
            self.assertIn(key, res)
        json.dumps(res)  # serialisable

    def test_a_failing_test_is_class_tests_and_later_stages_are_skipped(self):
        r = self.make_repo({"verify_lint": "true", "verify_build": "true"})
        self.commit(r, {"tests/test_a.py": FAILING_TEST})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "tests"))
        self.assertEqual(stage(res, "build")["status"], "pass")
        self.assertEqual(stage(res, "tests")["status"], "fail")
        self.assertIn("AssertionError", stage(res, "tests")["output_tail"])
        self.assertEqual(stage(res, "smoke")["status"], "skipped")
        self.assertEqual(verify.exit_code(res), 1)

    def test_keep_going_runs_the_rest(self):
        r = self.make_repo({"verify_lint": "exit 3", "verify_typecheck": "true"})
        res = verify.verify(r.path, base=self.base, write_log=False, keep_going=True)
        self.assertEqual(res["failure_class"], "lint")
        self.assertEqual(stage(res, "typecheck")["status"], "pass")
        self.assertEqual(stage(res, "tests")["status"], "pass")
        self.assertEqual(res["status"], "fail")

    def test_zero_tests_collected_is_a_failure(self):
        r = self.make_repo({"verify_tests": "echo 'Ran 0 tests in 0.0s'; echo; echo 'NO TESTS RAN'"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "fail")
        self.assertIn("collected no tests", stage(res, "tests")["reason"])

    def test_an_unreadable_count_passes_with_a_warning(self):
        r = self.make_repo({"verify_tests": "echo fine"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "pass")
        self.assertIn("verify.test-count-unknown", ids(res))

    def test_command_not_found_is_an_environment_failure(self):
        r = self.make_repo({"verify_lint": "definitely-not-a-real-command-xyz"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "env"))

    def test_a_required_stage_without_a_command_is_a_definition_error(self):
        r = self.make_repo({"verify_require": ["tests", "lint"]})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("error", "config"))
        self.assertFalse(res["ok"])
        self.assertIn("lint", res["message"])
        self.assertEqual(verify.exit_code(res), 2)

    def test_the_placeholder_command_counts_as_unset(self):
        r = self.make_repo({"verify_tests": "<command>"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "error")

    def test_nothing_configured_and_nothing_required_is_an_error_not_a_pass(self):
        r = self.make_repo({"verify_tests": "<command>", "verify_require": []})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("nothing ran", res["message"])

    def test_only_marks_the_run_incomplete(self):
        r = self.make_repo({"verify_lint": "true"})
        res = verify.verify(r.path, base=self.base, write_log=False, only=["lint"])
        self.assertEqual(res["status"], "pass")
        self.assertFalse(res["ok"])
        self.assertFalse(res["complete"])

    def test_unknown_keys_and_bad_values_in_the_config_are_errors(self):
        r = self.make_repo({"verify_ports": ["x"], "verify_dev": "true"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "error")
        r2 = self.make_repo({"verify_require": ["tests", "nope"]})
        self.assertEqual(verify.verify(r2.path, base=self.base, write_log=False)["status"], "error")
        r3 = self.make_repo({"verify_dev": "true"})
        self.assertIn("verify_ports", verify.verify(r3.path, base=self.base, write_log=False)["message"])


class BaseShaRule(VerifyCase):
    """CO-11: an item cannot weaken its own gate."""

    def test_the_head_config_cannot_weaken_the_base_config(self):
        r = self.make_repo({"verify_tests": "exit 1"})
        self.commit(r, {".claude/loop.config.md": config_text({"verify_tests": "true"})})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "tests"))
        self.assertIn("verify.config-drift", ids(res))

    def test_a_dirty_working_tree_config_is_not_used_either(self):
        r = self.make_repo({"verify_tests": "exit 1"})
        r.write(".claude/loop.config.md", config_text({"verify_tests": "true"}))
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "fail")
        self.assertTrue(res["dirty"])

    def test_no_config_at_the_base_is_an_error_unless_allowed(self):
        r = self.repo()
        r.commit_file("tests/__init__.py", "")
        r.commit_file("tests/test_a.py", PASSING_TEST)
        base = r.head()
        r.git("switch", "-q", "-c", "feat/x")
        self.commit(r, {".claude/loop.config.md": config_text({"verify_tests": UNITTEST})})
        res = verify.verify(r.path, base=base, write_log=False)
        self.assertEqual(res["status"], "error")
        self.assertIn("base sha", res["message"])
        res = verify.verify(r.path, base=base, write_log=False, allow_head_config=True)
        self.assertEqual(res["status"], "pass")
        self.assertFalse(res["trusted"])
        self.assertEqual(res["config_source"]["kind"], "head")
        self.assertIn("verify.untrusted-config", ids(res))

    def test_base_default_is_the_merge_base_with_main(self):
        r = self.make_repo()
        self.commit(r, {"x.txt": "1"})
        res = verify.verify(r.path, write_log=False)
        self.assertEqual(res["base"], self.base)

    def test_a_bad_base_is_a_definition_error(self):
        r = self.make_repo()
        self.assertEqual(verify.verify(r.path, base="nope-not-a-rev", write_log=False)["status"], "error")
        self.assertEqual(verify.verify(r.path, base="--upload-pack=x", write_log=False)["status"], "error")

    def test_not_a_repo(self):
        res = verify.verify(self.tmpdir(), write_log=False)
        self.assertEqual(res["status"], "error")


class ItemsFile(VerifyCase):
    ITEMS = {"items": {"F1": {"tests": "echo 'Ran 3 tests in 0.1s'", "demo_route": "/x", "ac_ids": ["AC-F1-1"]}}}

    def test_the_items_file_is_read_at_the_base_sha(self):
        r = self.make_repo(files={"plan/items.json": json.dumps(self.ITEMS)})
        self.commit(r, {"plan/items.json": json.dumps({"items": {"F1": {"tests": "true"}}})})  # a weaker head copy
        res = verify.verify(r.path, base=self.base, item="F1", items_file="plan/items.json", write_log=False)
        self.assertEqual(stage(res, "item-tests")["status"], "pass")
        self.assertEqual(stage(res, "item-tests")["tests_collected"], 3)
        self.assertEqual(res["routes"], ["/x"])
        self.assertEqual(res["ac_ids"], ["AC-F1-1"])
        self.assertEqual(res["item"], "F1")

    def test_an_items_file_outside_the_repo_is_read_from_disk(self):
        r = self.make_repo()
        path = os.path.join(self.tmpdir(), "verify.json")
        with open(path, "w") as fh:
            json.dump({"items": {"F1": {"tests": "exit 4"}}}, fh)
        res = verify.verify(r.path, base=self.base, item="F1", items_file=path, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "tests"))
        self.assertEqual(stage(res, "item-tests")["exit_code"], 4)

    def test_missing_item_or_file_is_a_definition_error(self):
        r = self.make_repo()
        path = os.path.join(self.tmpdir(), "v.json")
        with open(path, "w") as fh:
            json.dump(self.ITEMS, fh)
        self.assertEqual(verify.verify(r.path, base=self.base, item="F9", items_file=path, write_log=False)["status"], "error")
        self.assertEqual(verify.verify(r.path, base=self.base, item="F1", items_file="plan/none.json", write_log=False)["status"], "error")
        self.assertEqual(verify.verify(r.path, base=self.base, item="../x", items_file=path, write_log=False)["status"], "error")

    def test_routes_must_be_plain_paths(self):
        r = self.make_repo()
        for bad in ("/a b", "x", "/a;rm -rf /", "/$(id)"):
            with self.subTest(bad):
                self.assertEqual(verify.verify(r.path, base=self.base, routes=[bad], write_log=False)["status"], "error")


class Environment(VerifyCase):
    def names_seen_by_tests(self, **kw):
        out = "env.txt"
        r = self.make_repo({"verify_tests": "%s -c \"import os;open('%s','w').write(chr(10).join(sorted(os.environ)))\"" % (PY, out)})
        planted = {"GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y", "ANTHROPIC_API_KEY": "z", "DATABASE_URL": "postgres://prod/db",
                   "SSH_AUTH_SOCK": "/tmp/agent"}
        with mock.patch.dict(os.environ, planted):
            res = verify.verify(r.path, base=self.base, write_log=False, **kw)
        self.assertEqual(res["status"], "pass", res)
        with open(os.path.join(r.path, out)) as fh:
            return fh.read().split("\n")

    def test_credentials_in_the_callers_environment_do_not_reach_the_stages(self):
        names = self.names_seen_by_tests()
        for bad in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK", "DATABASE_URL"):
            self.assertNotIn(bad, names)
        self.assertIn("PATH", names)
        self.assertIn("KIT_VERIFY", names)

    def test_the_caller_can_pass_an_ephemeral_database_url_explicitly(self):
        names = self.names_seen_by_tests(extra_env={"DATABASE_URL": "file:/tmp/ephemeral.db"})
        self.assertIn("DATABASE_URL", names)
        self.assertNotIn("GITHUB_TOKEN", names)

    def test_extra_env_cannot_be_a_credential(self):
        r = self.make_repo()
        for name in ("GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "MY_API_KEY", "NPM_TOKEN", "bad name"):
            with self.subTest(name):
                res = verify.verify(r.path, base=self.base, write_log=False, extra_env={name: "v"})
                self.assertEqual(res["status"], "error")

    def test_env_allow_passes_a_named_variable_but_not_a_credential_name(self):
        out = "env.txt"
        cmd = "%s -c \"import os;open('%s','w').write(os.environ.get('NODE_OPTIONS','missing'))\"" % (PY, out)
        r = self.make_repo({"verify_tests": cmd, "verify_env_allow": ["NODE_OPTIONS"]})
        with mock.patch.dict(os.environ, {"NODE_OPTIONS": "--max-old-space-size=512"}):
            self.assertEqual(verify.verify(r.path, base=self.base, write_log=False)["status"], "pass")
        with open(os.path.join(r.path, out)) as fh:
            self.assertEqual(fh.read(), "--max-old-space-size=512")
        r2 = self.make_repo({"verify_env_allow": ["GITHUB_TOKEN"]})
        self.assertEqual(verify.verify(r2.path, base=self.base, write_log=False)["status"], "error")


class ProcessGroups(VerifyCase):
    def test_a_timeout_kills_the_whole_group(self):
        r = self.make_repo({"verify_tests": "sleep 60 & echo $! > child.pid; wait", "verify_timeout_s": 1})
        self.kill_later(os.path.join(r.path, "child.pid"))
        t0 = time.time()
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "timeout"))
        self.assertEqual(stage(res, "tests")["status"], "timeout")
        with open(os.path.join(r.path, "child.pid")) as fh:
            pid = int(fh.read())
        for _ in range(40):
            if not self.alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(pid), "the grandchild survived the timeout")

    def test_a_process_left_behind_by_a_passing_stage_is_killed_and_reported(self):
        cmd = "%s -c \"print('Ran 1 test in 0.0s')\"; (sleep 60 >/dev/null 2>&1 & echo $! > leak.pid)" % PY
        r = self.make_repo({"verify_tests": cmd})
        self.kill_later(os.path.join(r.path, "leak.pid"))
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "pass")
        self.assertIn("verify.leaked-process", ids(res))
        with open(os.path.join(r.path, "leak.pid")) as fh:
            pid = int(fh.read())
        for _ in range(40):
            if not self.alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(pid))

    def test_sigterm_to_verify_kills_the_running_group(self):
        r = self.make_repo({"verify_tests": "sleep 60 & echo $! > child.pid; wait"})
        self.kill_later(os.path.join(r.path, "child.pid"))
        proc = subprocess.Popen([sys.executable, VERIFY_PY, "--repo", r.path, "--base", self.base, "--no-log"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        pid_file = os.path.join(r.path, "child.pid")
        for _ in range(100):
            if os.path.exists(pid_file) and open(pid_file).read().strip():
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        pid = int(open(pid_file).read())
        for _ in range(40):
            if not self.alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(pid))


class Smoke(VerifyCase):
    def test_a_busy_verify_port_is_an_env_failure_and_the_harness_never_starts(self):
        port = free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        self.addCleanup(srv.close)
        r = self.make_repo({"verify_run": "touch harness-ran", "verify_ports": [port], "verify_require": ["tests", "smoke"]})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "env"))
        self.assertIn(str(port), stage(res, "smoke")["reason"])
        self.assertFalse(os.path.exists(os.path.join(r.path, "harness-ran")))

    def test_a_harness_that_exits_nonzero_fails_the_smoke_stage(self):
        r = self.make_repo({"verify_run": "exit 1", "verify_ports": [free_port()]})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "smoke"))

    def test_the_route_is_appended_quoted_or_substituted(self):
        r = self.make_repo({"verify_run": "echo \"$@\" > args.txt", "verify_ports": [free_port()]})
        res = verify.verify(r.path, base=self.base, routes=["/api/things?x=1&y=2"], write_log=False)
        self.assertEqual(res["status"], "pass", res)
        with open(os.path.join(r.path, "args.txt")) as fh:
            self.assertEqual(fh.read().strip(), "/api/things?x=1&y=2")
        r2 = self.make_repo({"verify_run": "echo {route} > args.txt", "verify_ports": [free_port()]})
        verify.verify(r2.path, base=self.base, routes=["/p"], write_log=False)
        with open(os.path.join(r2.path, "args.txt")) as fh:
            self.assertEqual(fh.read().strip(), "/p")

    def test_a_harness_that_leaks_its_server_fails_with_class_leak(self):
        port = free_port()
        listener = ("import socket,time;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
                    "s.bind(('127.0.0.1',%d));s.listen(16);time.sleep(60)" % port)
        harness = ("import subprocess,sys,time,socket;p=subprocess.Popen([sys.executable,'-c',%r],start_new_session=True);"
                   "open('leak.pid','w').write(str(p.pid))\n"
                   "for _ in range(100):\n"
                   "    c=socket.socket()\n"
                   "    if c.connect_ex(('127.0.0.1',%d))==0: break\n"
                   "    time.sleep(0.05)\n") % (listener, port)
        r = self.make_repo({"verify_run": "%s harness.py" % PY, "verify_ports": [port]}, files={"harness.py": harness})
        self.kill_later(os.path.join(r.path, "leak.pid"))
        with mock.patch.object(verify, "PORT_SETTLE_S", 0.3):
            res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "leak"))
        self.assertIn("verify.port-leak", ids(res))

    def test_a_harness_that_leaves_a_process_in_its_group_is_killed_and_fails(self):
        r = self.make_repo({"verify_run": "(sleep 60 >/dev/null 2>&1 & echo $! > leak.pid)", "verify_ports": [free_port()]})
        self.kill_later(os.path.join(r.path, "leak.pid"))
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "leak"))
        self.assertFalse(self.alive(int(open(os.path.join(r.path, "leak.pid")).read())))

    def test_a_harness_without_declared_ports_warns(self):
        r = self.make_repo({"verify_run": "true"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "pass")
        self.assertIn("verify.ports-undeclared", ids(res))

    def test_native_dev_server_boot_poll_request_and_kill(self):
        port = free_port()
        r = self.make_repo({"verify_dev": "%s -m http.server $VERIFY_PORT --bind 127.0.0.1" % PY, "verify_ports": [port],
                            "verify_run_timeout_s": 20}, files={"index.html": "hello"})
        res = verify.verify(r.path, base=self.base, routes=["/index.html"], write_log=False)
        self.assertEqual(res["status"], "pass", res)
        self.assertIn("GET /index.html ok", stage(res, "smoke")["reason"])
        self.assertFalse(verify.port_busy(port), "the dev server's port was left bound")

    def test_native_dev_server_404_route_fails(self):
        port = free_port()
        r = self.make_repo({"verify_dev": "%s -m http.server $VERIFY_PORT --bind 127.0.0.1" % PY, "verify_ports": [port],
                            "verify_run_timeout_s": 20})
        res = verify.verify(r.path, base=self.base, routes=["/nope"], write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "smoke"))
        self.assertIn("404", stage(res, "smoke")["reason"])
        self.assertFalse(verify.port_busy(port))

    def test_native_dev_server_that_never_answers_times_out_and_is_killed(self):
        port = free_port()
        r = self.make_repo({"verify_dev": "sleep 60 & echo $! > child.pid; wait", "verify_ports": [port], "verify_run_timeout_s": 1})
        self.kill_later(os.path.join(r.path, "child.pid"))
        t0 = time.time()
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "timeout"))
        pid = int(open(os.path.join(r.path, "child.pid")).read())
        for _ in range(40):
            if not self.alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(pid))

    def test_native_dev_server_that_exits_early_fails_fast(self):
        r = self.make_repo({"verify_dev": "echo boom >&2; exit 3", "verify_ports": [free_port()], "verify_run_timeout_s": 30})
        t0 = time.time()
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(res["failure_class"], "smoke")
        self.assertIn("exited before it became ready", stage(res, "smoke")["reason"])
        self.assertIn("boom", stage(res, "smoke")["output_tail"])

    def test_http_status_only_talks_to_loopback(self):
        with self.assertRaises(ValueError):
            verify.http_status("http://example.invalid/")


class Hardening(VerifyCase):
    def test_a_hostile_git_config_does_not_run_anything(self):
        r = self.make_repo()
        marker = os.path.join(self.tmpdir(), "ran")
        hook = os.path.join(self.tmpdir(), "fsmonitor.sh")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\nexit 1\n" % marker)
        os.chmod(hook, 0o755)
        r.git("config", "core.fsmonitor", hook)
        r.git("config", "core.hooksPath", os.path.dirname(hook))
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertEqual(res["status"], "pass", res)
        self.assertFalse(os.path.exists(marker))

    def test_a_branch_that_edits_the_gate_is_flagged(self):
        r = self.make_repo()
        self.commit(r, {".claude/scripts/verify.py": "# tampered\n", ".github/workflows/ci.yml": "name: ci\n"})
        res = verify.verify(r.path, base=self.base, write_log=False)
        self.assertIn("verify.kit-modified", ids(res))
        msg = [f["message"] for f in res["findings"] if f["id"] == "verify.kit-modified"][0]
        self.assertIn(".claude/scripts/verify.py", msg)
        self.assertIn(".github/workflows/ci.yml", msg)
        self.assertEqual(res["status"], "pass")  # flagged for a human, not blocked: the base copy of the config still rules

    def test_an_untouched_branch_is_not_flagged(self):
        r = self.make_repo()
        self.commit(r, {"src/a.py": "x = 1\n"})
        self.assertNotIn("verify.kit-modified", ids(verify.verify(r.path, base=self.base, write_log=False)))


class GateAndLog(VerifyCase):
    def test_gate_off_skips_only_with_apply_gate(self):
        r = self.repo()  # `gates.g6` nests under `gates:`, so the config text is written by hand
        r.write(".claude/loop.config.md", "# c\n\n```yaml\nverify_tests: \"exit 1\"\ngates:\n  g6: off\n```\n")
        r.write("a.txt", "x")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "base")
        base = r.head()
        res = verify.verify(r.path, base=base, write_log=False)
        self.assertEqual(res["status"], "fail")
        res = verify.verify(r.path, base=base, write_log=False, apply_gate=True)
        self.assertEqual(res["status"], "skipped")
        self.assertTrue(res["ok"])
        self.assertFalse(res["blocking"])
        self.assertEqual(verify.exit_code(res, apply_gate=True), 0)

    def test_gate_report_never_fails_the_exit_code_with_apply_gate(self):
        r = self.repo()
        r.write(".claude/loop.config.md", "# c\n\n```yaml\nverify_tests: \"exit 1\"\ngates:\n  g6: report\n```\n")
        r.write("a.txt", "x")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "base")
        res = verify.verify(r.path, base=r.head(), write_log=False)
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["gate"], "report")
        self.assertEqual(verify.exit_code(res), 1)
        self.assertEqual(verify.exit_code(res, apply_gate=True), 0)

    def test_the_log_is_written_private_and_redacted(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        r = self.make_repo({"verify_tests": "echo %s; echo 'Ran 1 test in 0.0s'; exit 1" % secret})
        logs = self.tmpdir()
        res = verify.verify(r.path, base=self.base, item="F1", log_dir=os.path.join(logs, "v"), attempt=2)
        path = res["log"]
        self.assertTrue(path.endswith("F1-a2.log"), path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        self.assertNotIn(secret, json.dumps(res))

    def test_the_default_log_dir_is_in_the_git_common_dir(self):
        r = self.make_repo()
        res = verify.verify(r.path, base=self.base)
        self.assertEqual(os.path.dirname(res["log"]), os.path.join(r.common_dir(), "loopctl", "verify"))
        self.assertNotIn("loopctl", r.git("status", "--porcelain"))


class Cli(VerifyCase):
    def run_cli(self, repo, *args):
        proc = subprocess.run([sys.executable, VERIFY_PY, "--repo", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)
        return proc

    def test_exit_codes_and_json(self):
        r = self.make_repo()
        ok = self.run_cli(r.path, "--base", self.base, "--json", "--no-log")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["status"], "pass")
        r2 = self.make_repo({"verify_tests": "exit 1"})
        bad = self.run_cli(r2.path, "--base", self.base, "--json", "--no-log")
        self.assertEqual(bad.returncode, 1)
        r3 = self.make_repo({"verify_require": ["tests", "lint"]})
        err = self.run_cli(r3.path, "--base", self.base, "--no-log")
        self.assertEqual(err.returncode, 2)
        self.assertIn("ERROR", err.stdout)
        usage = self.run_cli(r.path, "--only", "bogus")
        self.assertEqual(usage.returncode, 2)

    def test_text_output_names_the_failing_stage_and_its_tail(self):
        r = self.make_repo({"verify_lint": "echo lint-complaint; exit 1"})
        out = self.run_cli(r.path, "--base", self.base, "--no-log")
        self.assertEqual(out.returncode, 1)
        self.assertIn("lint-complaint", out.stdout)
        self.assertIn("FAIL (lint)", out.stdout)


if __name__ == "__main__":
    unittest.main()
