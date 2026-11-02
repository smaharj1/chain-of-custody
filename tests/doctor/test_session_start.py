"""session_start.py: the version check and the liveness canary (report §5.8, §14.2; RC-7). Advisory: it never blocks."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

import session_start

from kitlib import runstate

from tests import REPO_ROOT
from tests.helpers import BaseTestCase, install_fake_claude, read_jsonl

from .helpers import KIT_VERSION_V2, config_text

SCRIPT = os.path.join(REPO_ROOT, ".claude", "scripts", "session_start.py")


class Base(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.r = self.repo()
        self.r.write(".claude/KIT_VERSION", KIT_VERSION_V2)
        self.r.write(".claude/loop.config.md", config_text({}))
        self.env = {"PATH": "/nonexistent-dir", "KIT_CLAUDE_VERSION": "2.1.305"}

    def go(self, sid="sess-1", source="startup", env=None, cwd=None, **extra):
        hi = {"session_id": sid, "source": source, "cwd": cwd or self.r.path, "hook_event_name": "SessionStart"}
        hi.update(extra)
        return session_start.run(hi, env if env is not None else self.env)

    def state(self):
        return runstate.RunState.for_path(self.r.path)

    def set_level(self, level):
        self.r.write(".claude/loop.config.md", config_text({"autonomy_level": level}))


class Canary(Base):
    def test_startup_writes_a_nonce_and_prints_it(self):
        res = self.go()
        nonce = self.state().read_nonce("sess-1")
        self.assertIsNotNone(nonce)
        self.assertEqual(res["nonce"], nonce)
        self.assertIn("nonce=" + nonce, res["context"])
        self.assertIn("session=sess-1", res["context"])

    def test_nonce_file_is_in_the_git_common_dir_not_in_the_worktree_content(self):
        before = self.r.git("status", "--porcelain")
        self.go()
        p = os.path.join(self.r.common_dir(), "loopctl", "nonces", "sess-1")
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertEqual(self.r.git("status", "--porcelain"), before)

    def test_worktree_sessions_share_the_common_dir(self):
        wt = self.r.add_worktree("wt", branch="feat/x")
        self.go(sid="sess-wt", cwd=wt)
        self.assertIsNotNone(self.state().read_nonce("sess-wt"))

    def test_clear_and_startup_rotate_but_resume_and_compact_keep(self):
        a = self.go(source="startup")["nonce"]
        b = self.go(source="startup")["nonce"]
        self.assertNotEqual(a, b)
        c = self.go(source="resume")["nonce"]
        d = self.go(source="compact")["nonce"]
        self.assertEqual((c, d), (b, b))
        e = self.go(source="clear")["nonce"]
        self.assertNotEqual(e, b)

    def test_resume_without_a_prior_nonce_creates_one(self):
        self.assertIsNotNone(self.go(sid="fresh", source="resume")["nonce"])

    def test_hostile_session_ids_are_not_path_components(self):
        for sid in ("../../etc/x", "a/b", "", None, 5, "x" * 200, ".."):
            with self.subTest(sid=sid):
                res = self.go(sid=sid)
                self.assertIsNone(res["nonce"])
                self.assertIn("NOT recorded", res["context"])
        self.assertEqual(os.listdir(os.path.join(self.r.common_dir(), "loopctl", "nonces")) if os.path.isdir(os.path.join(self.r.common_dir(), "loopctl", "nonces")) else [], [])

    def test_outside_a_git_repo(self):
        d = self.tmpdir()
        res = self.go(cwd=d)
        self.assertIsNone(res["nonce"])
        self.assertIn("not inside a git repository", res["context"])

    def test_halt_is_announced(self):
        self.state().ensure().set_halt("operator stop", "human")
        self.assertIn("HALT is set (operator stop)", self.go()["context"])

    def test_context_stays_under_one_kib(self):
        self.r.write(".claude/loop.config.md", "```yaml\n" + "bogus_key: 1\n" * 5 + "```\n")
        res = self.go(env={"PATH": "/x"})
        self.assertLessEqual(len(res["context"]), session_start.MAX_CONTEXT)


class VersionRange(Base):
    def test_in_range_no_banner(self):
        res = self.go()
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("WARNING", res["context"])
        self.assertNotIn("REFUSED", res["context"])

    def test_below_minimum_is_a_loud_banner_at_l0_to_l2(self):
        for level in ("L0", "L1", "L2"):
            with self.subTest(level=level):
                self.set_level(level)
                res = self.go(env={"PATH": "/x", "KIT_CLAUDE_VERSION": "2.1.250"})
                self.assertIn("WARNING", res["context"])
                self.assertIn("BELOW the kit floor 2.1.302", res["context"])
                self.assertFalse(res["refused"])
                self.assertIsNotNone(res["nonce"])

    def test_below_minimum_is_a_refusal_at_l3_and_up_no_nonce(self):
        for level in ("L3", "L4", "L5"):
            with self.subTest(level=level):
                self.set_level(level)
                res = self.go(sid="s-" + level, env={"PATH": "/x", "KIT_CLAUDE_VERSION": "2.1.250"})
                self.assertTrue(res["refused"])
                self.assertIn("REFUSED", res["context"])
                self.assertIsNone(res["nonce"])
                self.assertIsNone(self.state().read_nonce("s-" + level))

    def test_above_tested_maximum(self):
        res = self.go(env={"PATH": "/x", "KIT_CLAUDE_VERSION": "2.2.0"})
        self.assertIn("ABOVE the tested maximum 2.1.310", res["context"])
        self.set_level("L3")
        self.assertTrue(self.go(env={"PATH": "/x", "KIT_CLAUDE_VERSION": "2.2.0"})["refused"])

    def test_untested_maximum_none(self):
        self.r.write(".claude/KIT_VERSION", "2.0.0-rc.1 (2026-10-26)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: none\n")
        res = self.go()
        self.assertIn("canary has never passed", res["context"])
        self.assertFalse(res["refused"])
        self.set_level("L3")
        self.assertTrue(self.go()["refused"])

    def test_unreadable_version(self):
        res = self.go(env={"PATH": "/nonexistent-dir"})
        self.assertEqual(res["status"], "unknown")
        self.assertIn("unreadable", res["context"])
        self.set_level("L3")
        self.assertTrue(self.go(env={"PATH": "/nonexistent-dir"})["refused"])

    def test_v1_kit_version_file_falls_back_to_the_floor(self):
        self.r.write(".claude/KIT_VERSION", "1.2.0 (2026-07-30)\n")
        res = self.go(env={"PATH": "/x", "KIT_CLAUDE_VERSION": "2.1.250"})
        self.assertIn("BELOW the kit floor 2.1.302", res["context"])

    def test_unreadable_config_assumes_l1_and_says_so(self):
        self.r.write(".claude/loop.config.md", "no yaml here")
        res = self.go()
        self.assertEqual(res["level"], 1)
        self.assertIn("loop.config unreadable", res["context"])

    def test_fake_claude_is_run_for_its_version_and_cached(self):
        d = self.tmpdir()
        fake = install_fake_claude(d, {"version": "2.1.305 (Claude Code)"})
        env = {"PATH": "/nonexistent-dir", "KIT_CLAUDE_BIN": fake}
        a = self.go(env=env)
        self.assertEqual(a["status"], "ok")
        runs_after_first = len(read_jsonl(fake + ".log.jsonl"))
        b = self.go(env=env)
        self.assertEqual(b["status"], "ok")
        self.assertTrue(os.path.isfile(os.path.join(self.r.common_dir(), "loopctl", "claude-version.json")))
        self.assertEqual(len(read_jsonl(fake + ".log.jsonl")), runs_after_first)  # --version is not logged as a run; the cache avoids a second exec

    def test_a_changed_binary_invalidates_the_cache(self):
        d = self.tmpdir()
        fake = install_fake_claude(d, {"version": "2.1.305"})
        env = {"PATH": "/nonexistent-dir", "KIT_CLAUDE_BIN": fake}
        self.assertEqual(self.go(env=env)["status"], "ok")
        from tests.helpers.bins import set_scenario
        os.utime(fake, (1, 1))
        set_scenario(fake, {"version": "2.1.200"})
        self.assertEqual(self.go(env=env)["status"], "below_min")

    def test_a_crashing_claude_is_unknown_not_a_crash(self):
        d = self.tmpdir()
        p = os.path.join(d, "claude")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit 3\n")
        os.chmod(p, 0o755)
        res = self.go(env={"PATH": "/x", "KIT_CLAUDE_BIN": p})
        self.assertEqual(res["status"], "unknown")


class NeverBlocks(Base):
    def run_script(self, stdin, env=None):
        e = {"PATH": "/usr/bin:/bin", "KIT_CLAUDE_VERSION": "2.1.305", "HOME": self.tmpdir()}
        e.update(env or {})
        p = subprocess.run([sys.executable, SCRIPT], input=stdin.encode(), env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, cwd=self.r.path)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def test_end_to_end_json_contract(self):
        rc, out, err = self.run_script(json.dumps({"session_id": "e2e", "source": "startup", "cwd": self.r.path}))
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("nonce=" + self.state().read_nonce("e2e"), doc["hookSpecificOutput"]["additionalContext"])

    def test_garbage_stdin_exits_zero_and_says_the_canary_is_missing(self):
        for stdin in ("not json", "[]", "null", "", "{" * 50):
            with self.subTest(stdin=stdin[:10]):
                rc, out, err = self.run_script(stdin)
                self.assertEqual(rc, 0)
                self.assertIn("additionalContext", out)

    def test_exceptions_inside_run_become_context_not_a_block(self):
        original = session_start.run
        try:
            def boom(*a, **k):
                raise RuntimeError("boom")
            session_start.run = boom
            out = io.StringIO()
            rc = session_start.main(stdin=io.StringIO("{}"), stdout=out, env={})
        finally:
            session_start.run = original
        self.assertEqual(rc, 0)
        self.assertIn("FAILED (RuntimeError: boom)", out.getvalue())
        self.assertIn("canary was not recorded", out.getvalue())

    def test_unwritable_runstate_dir(self):
        d = os.path.join(self.r.common_dir(), "loopctl")
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o500)
        try:
            res = self.go(sid="ro")
        finally:
            os.chmod(d, 0o700)
        if os.geteuid() != 0:
            self.assertIsNone(res["nonce"])
            self.assertIn("NOT recorded", res["context"])

    def test_the_hook_uses_no_network_and_starts_nothing_but_claude_version(self):
        with open(SCRIPT) as fh:
            src = fh.read()
        for banned in ("urllib", "socket", "requests", "http.client"):
            self.assertNotIn("import " + banned, src)


if __name__ == "__main__":
    unittest.main()
