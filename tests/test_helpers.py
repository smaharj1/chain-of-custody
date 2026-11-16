"""The shared test helpers themselves: fake claude, fake gh, FakeHome, TempRepo (IMP-14)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest

from tests.helpers import (BaseTestCase, FakeHome, ProcessTracker, clean_env, install_fake_claude, install_fake_gh,
                           make_bare_remote, read_jsonl)
from tests.helpers.fakehome import FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN
from tests.helpers.procs import wait_for


def slurp(path):
    with open(path) as fh:
        return fh.read()


class FakeClaude(BaseTestCase):
    def install(self, scenario):
        d = self.tmpdir()
        return d, install_fake_claude(d, scenario)

    def run_claude(self, path, *args, **kw):
        return subprocess.run([path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **kw)

    def test_version(self):
        _, c = self.install({"version": "9.9.9 (Fake)"})
        p = self.run_claude(c, "--version")
        self.assertEqual(p.stdout.decode().strip(), "9.9.9 (Fake)")
        self.assertEqual(self.run_claude(c, "-v").returncode, 0)
        self.assertEqual(read_jsonl(c + ".log.jsonl"), [])  # --version is not a run

    def test_succeed_json_with_cost_and_session(self):
        _, c = self.install({"runs": [{"behavior": "succeed", "result": "done", "total_cost_usd": 0.5, "session_id": "sid-1"}]})
        p = self.run_claude(c, "-p", "hello", "--output-format", "json")
        out = json.loads(p.stdout.decode())
        self.assertEqual(p.returncode, 0)
        self.assertEqual((out["result"], out["total_cost_usd"], out["session_id"], out["is_error"]), ("done", 0.5, "sid-1", False))

    def test_succeed_stream_json_and_text(self):
        _, c = self.install({"runs": [{"behavior": "succeed", "result": "hi"}], "default": {"behavior": "succeed", "result": "plain"}})
        lines = self.run_claude(c, "--output-format", "stream-json").stdout.decode().strip().split("\n")
        self.assertEqual([json.loads(l)["type"] for l in lines], ["system", "assistant", "result"])
        self.assertEqual(self.run_claude(c, "-p", "x").stdout.decode().strip(), "plain")

    def test_cost_field_can_be_omitted_or_renamed(self):
        _, c = self.install({"runs": [{"behavior": "succeed", "total_cost_usd": None},
                                      {"behavior": "succeed", "cost_field": "cost"}]})
        a = json.loads(self.run_claude(c, "--output-format", "json").stdout.decode())
        b = json.loads(self.run_claude(c, "--output-format", "json").stdout.decode())
        self.assertNotIn("total_cost_usd", a)
        self.assertNotIn("total_cost_usd", b)
        self.assertIn("cost", b)

    def test_sequence_of_runs_then_default(self):
        _, c = self.install({"runs": [{"behavior": "overloaded"}, {"behavior": "succeed", "result": "second"}]})
        a = self.run_claude(c, "--output-format", "json")
        b = self.run_claude(c, "--output-format", "json")
        third = self.run_claude(c, "--output-format", "json")
        self.assertEqual(a.returncode, 1)
        self.assertIn("overloaded_error", a.stdout.decode())
        self.assertIn("529", a.stderr.decode())
        self.assertEqual(json.loads(b.stdout.decode())["result"], "second")
        self.assertEqual(json.loads(third.stdout.decode())["result"], "ok")  # default is a success
        self.assertEqual([e["behavior"] for e in read_jsonl(c + ".log.jsonl")], ["overloaded", "succeed", "succeed"])

    def test_crash_exit_code_signal_and_partial_output(self):
        _, c = self.install({"runs": [{"behavior": "crash", "exit_code": 3, "stderr": "boom", "partial_stdout": "par"},
                                      {"behavior": "crash", "signal": "SIGKILL"}]})
        a = self.run_claude(c)
        self.assertEqual((a.returncode, a.stdout, a.stderr.strip()), (3, b"par", b"boom"))
        self.assertEqual(self.run_claude(c).returncode, -signal.SIGKILL)

    def test_malformed_json(self):
        _, c = self.install({"runs": [{"behavior": "malformed"}]})
        p = self.run_claude(c, "--output-format", "json")
        self.assertEqual(p.returncode, 0)
        with self.assertRaises(ValueError):
            json.loads(p.stdout.decode())

    def test_stall_can_be_killed_by_group(self):
        d, c = self.install({})
        pidfile = os.path.join(d, "pid")
        from tests.helpers.bins import set_scenario
        set_scenario(c, {"runs": [{"behavior": "stall", "seconds": 60, "pidfile": pidfile}]})
        proc = self.procs.spawn([c])
        self.assertTrue(wait_for(lambda: os.path.exists(pidfile)))
        self.assertIsNone(proc.poll())
        self.procs.kill_group(proc)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def test_stall_that_ignores_sigterm(self):
        d, c = self.install({})
        pidfile = os.path.join(d, "pid")
        from tests.helpers.bins import set_scenario
        set_scenario(c, {"runs": [{"behavior": "stall", "seconds": 60, "ignore_sigterm": True, "pidfile": pidfile}]})
        proc = self.procs.spawn([c])
        self.assertTrue(wait_for(lambda: os.path.exists(pidfile)))
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        self.assertIsNone(proc.poll(), "the fake should survive SIGTERM when asked to")

    def test_writes_report_and_files_relative_to_cwd(self):
        d, c = self.install({})
        work = self.tmpdir()
        report = os.path.join(work, "reports", "backend.md")
        from tests.helpers.bins import set_scenario
        set_scenario(c, {"runs": [{"behavior": "succeed", "report": {"path": report, "text": "STATUS: DONE\n"},
                                   "write_files": [{"path": "rel/out.txt", "content": "x", "mode": 0o600}]}]})
        self.run_claude(c, "-p", "go", cwd=work)
        with open(report) as fh:
            self.assertEqual(fh.read(), "STATUS: DONE\n")
        self.assertEqual(slurp(os.path.join(work, "rel", "out.txt")), "x")
        self.assertEqual(os.stat(os.path.join(work, "rel", "out.txt")).st_mode & 0o777, 0o600)

    def test_log_records_argv_cwd_env_and_stdin(self):
        d, c = self.install({"log_env": ["KIT_RUN_ID", "NOPE"], "read_stdin": True, "runs": [{"behavior": "succeed"}]})
        env = dict(os.environ, KIT_RUN_ID="run-7")
        env.pop("NOPE", None)
        subprocess.run([c, "-p", "--agent", "backend-engineer"], input=b"prompt text", env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=d)
        entry = read_jsonl(c + ".log.jsonl")[0]
        self.assertEqual(entry["argv"], ["-p", "--agent", "backend-engineer"])
        self.assertEqual(entry["env"], {"KIT_RUN_ID": "run-7", "NOPE": None})
        self.assertEqual(entry["stdin"], "prompt text")
        self.assertEqual(os.path.realpath(entry["cwd"]), os.path.realpath(d))

    def test_scenario_via_env_var_json_and_path(self):
        d, c = self.install({})
        p = subprocess.run([c, "--version"], env=dict(os.environ, FAKE_CLAUDE_SCENARIO='{"version": "1.2.3"}'),
                           stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.decode().strip(), "1.2.3")
        path = os.path.join(d, "scn.json")
        with open(path, "w") as fh:
            json.dump({"version": "4.5.6"}, fh)
        p = subprocess.run([c, "--version"], env=dict(os.environ, FAKE_CLAUDE_SCENARIO=path), stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.decode().strip(), "4.5.6")

    def test_installed_copy_runs_under_env_i(self):
        _, c = self.install({"version": "7.7.7"})
        p = subprocess.run(["/usr/bin/env", "-i", c, "--version"], stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.decode().strip(), "7.7.7")

    def test_unknown_behavior_fails_loudly(self):
        _, c = self.install({"runs": [{"behavior": "dance"}]})
        p = self.run_claude(c)
        self.assertEqual(p.returncode, 64)


class FakeGh(BaseTestCase):
    def test_replays_json_and_records_argv(self):
        d = self.tmpdir()
        gh = install_fake_gh(d, [
            {"argv_prefix": ["api", "repos/acme/widgets/rulesets"], "stdout_json": [{"id": 1, "name": "main"}]},
            {"argv_contains": ["auth", "status"], "stdout": "Logged in\n", "exit": 0},
            {"regex": r"^pr view \d+", "stdout_json": {"state": "OPEN"}, "times": 1},
        ])
        run = lambda *a: subprocess.run([gh] + list(a), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(json.loads(run("api", "repos/acme/widgets/rulesets").stdout.decode()), [{"id": 1, "name": "main"}])
        self.assertEqual(run("auth", "status").stdout.decode(), "Logged in\n")
        self.assertEqual(json.loads(run("pr", "view", "7").stdout.decode())["state"], "OPEN")
        again = run("pr", "view", "7")  # "times": 1 is used up
        self.assertEqual(again.returncode, 1)
        self.assertIn("no recorded response", again.stderr.decode())
        log = read_jsonl(gh + ".log.jsonl")
        self.assertEqual([e["argv"][0] for e in log], ["api", "auth", "pr", "pr"])
        self.assertEqual([e["matched"] for e in log], [0, 1, 2, None])

    def test_unmatched_call_fails_and_is_logged(self):
        gh = install_fake_gh(self.tmpdir(), [])
        p = subprocess.run([gh, "pr", "merge", "5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(read_jsonl(gh + ".log.jsonl")[0]["argv"], ["pr", "merge", "5"])

    def test_can_shadow_the_real_gh_on_path(self):
        d = self.tmpdir()
        install_fake_gh(d, [{"argv_prefix": ["--version"], "stdout": "gh fake\n"}])
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
        p = subprocess.run(["gh", "--version"], env=env, stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.decode(), "gh fake\n")


class FakeHomeTests(BaseTestCase):
    def test_plant_all_creates_each_credential_kind(self):
        h = self.home()
        paths = h.plant_all()
        self.assertEqual(set(h.planted), {
            "aws_config", "aws_credentials", "aws_sso_cache", "gh_hosts", "git_credentials", "gitconfig",
            "ssh_agent_socket", "ssh_key", "kube_config", "gcloud_adc", "azure_token_cache", "docker_config"})
        for p in paths:
            self.assertTrue(os.path.exists(p), p)
            self.assertTrue(p.startswith(h.path))
        self.assertIn(FAKE_AWS_KEY_ID, slurp(h.planted["aws_credentials"]))
        self.assertIn(FAKE_GH_TOKEN, slurp(h.planted["gh_hosts"]))
        self.assertEqual(os.stat(h.planted["aws_credentials"]).st_mode & 0o777, 0o600)

    def test_fake_values_look_like_secrets_but_are_obviously_fake(self):
        from kitlib import redact
        for v in (FAKE_AWS_KEY_ID, FAKE_GH_TOKEN):
            self.assertNotEqual(redact.redact(v), v)
        self.assertEqual(len(FAKE_AWS_SECRET), 40)
        self.assertIn("FAKE", FAKE_AWS_KEY_ID)

    def test_env_is_scrubbed_and_points_home_at_the_fake(self):
        os.environ["AWS_ACCESS_KEY_ID"] = "x"
        os.environ["GITHUB_TOKEN"] = "x"
        os.environ["SSH_AUTH_SOCK"] = "/real/agent"
        os.environ["KUBECONFIG"] = "/real/kube"
        os.environ["GIT_DIR"] = "/real/git"
        try:
            h = self.home()
            env = h.env()
            self.assertEqual(env["HOME"], h.path)
            for k in ("AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "KUBECONFIG", "GIT_DIR", "SSH_AUTH_SOCK"):
                self.assertNotIn(k, env, k)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
            sock = h.plant_ssh_agent_socket()
            self.assertEqual(h.env()["SSH_AUTH_SOCK"], sock)
            self.assertNotIn("SSH_AUTH_SOCK", h.env(include_planted=False))
        finally:
            for k in ("AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "KUBECONFIG", "GIT_DIR"):
                os.environ.pop(k, None)

    def test_clean_env_strips_claudecode_and_extra_wins(self):
        os.environ["CLAUDECODE"] = "1"
        try:
            env = clean_env("/tmp/h", {"EXTRA": "1", "HOME": "/override"})
            self.assertNotIn("CLAUDECODE", env)
            self.assertEqual(env["EXTRA"], "1")
            self.assertEqual(env["HOME"], "/override")
        finally:
            os.environ.pop("CLAUDECODE", None)

    def test_subprocess_in_fake_home_does_not_see_the_real_home(self):
        h = self.home()
        h.plant_aws_credentials()
        out = subprocess.run([sys.executable, "-c", "import os;print(os.path.expanduser('~'))"], env=h.env(),
                             stdout=subprocess.PIPE).stdout.decode().strip()
        self.assertEqual(out, h.path)


class TempRepoTests(BaseTestCase):
    def test_repo_has_a_main_branch_and_deterministic_commits(self):
        a, b = self.repo(), self.repo()
        self.assertEqual(a.git("symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(a.head(), b.head())  # fixed identity and dates
        self.assertEqual(a.git("log", "-1", "--format=%an <%ae>"), "Kit Test <kit-test@example.invalid>")

    def test_does_not_read_user_git_config(self):
        r = self.repo()
        self.assertEqual(r.git("config", "--global", "--list", check=False), "")

    def test_worktrees_and_remotes(self):
        r, bare = self.repo_with_remote()
        self.assertEqual(bare.ref("refs/heads/main"), r.head())
        wt = r.add_worktree("w2", branch="feat/z")
        self.assertTrue(os.path.isdir(wt))

    def test_commit_file_returns_the_new_head(self):
        r = self.repo()
        sha = r.commit_file("a/b.txt", "hello", "msg")
        self.assertEqual(sha, r.head())
        self.assertEqual(r.git("show", "HEAD:a/b.txt"), "hello")

    def test_independent_bare_remote(self):
        b = make_bare_remote()
        self.addCleanup(b.cleanup)
        self.assertIsNone(b.ref("refs/heads/main"))


class ProcessTrackerTests(unittest.TestCase):
    def test_cleanup_kills_the_whole_group(self):
        pt = ProcessTracker()
        p = pt.spawn([sys.executable, "-c", "import subprocess,time,sys;"
                      "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                      "print(c.pid, flush=True);time.sleep(60)"])
        child = int(p.stdout.readline().decode().strip())
        pt.cleanup()
        self.assertTrue(wait_for(lambda: not _alive(child), timeout=5))


def _alive(pid):
    try:
        os.kill(pid, 0)
        # a zombie still answers kill(0); check the state
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE).stdout.decode().strip()
        return bool(out) and not out.startswith("Z")
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
