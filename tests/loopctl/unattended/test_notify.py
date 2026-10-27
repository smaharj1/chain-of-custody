"""notify.py: the hook that runs the human's notify_command and sends nothing itself (DEC-31, R2, IMP-25).

The command is split once, run with no shell, with the message on stdin and in KIT_NOTIFY_* only; shell syntax in it is refused; a program inside the
repository is refused; free text from a hook's input is never used. The fake notifier records what it was given.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest

from ... import REPO_ROOT, SCRIPTS_DIR  # noqa: F401
from .. import LoopRepo

import notify

from . import FakeNotify


def run_main(argv, stdin="", env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):                                                         # argparse writes its usage here
        rc = notify.main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env)
    return rc, out.getvalue(), err.getvalue()


class Command(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNotify()
        self.addCleanup(self.fake.cleanup)
        self.path = self.fake.path

    def refused(self, text, forbid=()):
        with self.assertRaises(notify.NotifyError, msg=text) as cm:
            notify.parse_command(text, None, forbid)
        return str(cm.exception)

    def test_only_a_program_and_plain_arguments_are_accepted(self):
        self.assertEqual(notify.parse_command(self.path + " --topic ops", None), [os.path.realpath(self.path), "--topic", "ops"])
        self.assertEqual(notify.parse_command('"%s" "two words" x' % self.path, None)[1:], ["two words", "x"])        # quotes group, they do not execute
        self.assertEqual(notify.parse_command("sh", None)[0], os.path.realpath(notify.shutil.which("sh")))            # a bare name on PATH

    def test_everything_a_shell_would_interpret_is_refused_not_guessed_at(self):
        for bad in ("%s; rm -rf ~", "%s | tee x", "%s && true", "%s > out", "%s < in", "%s `id`", "%s $(id)", "%s $HOME", "%s \\ x", "%s (x)", "%s {a,b}",
                    "%s *", "%s ?", "%s !x", "%s & "):
            self.assertIn("without a shell", self.refused(bad % self.path))

    def test_an_unset_relative_missing_or_oversized_command_is_refused(self):
        for text in (None, "", "   ", "<command>", 5):
            self.assertIn("not set", self.refused(text))
        self.assertIn("relative path", self.refused("./notify-fake"))
        self.assertIn("relative path", self.refused("bin/notify-fake"))
        self.assertIn("not an executable", self.refused("/no/such/program"))
        self.assertIn("not an executable", self.refused("no-such-program-xyz"))
        self.assertIn("one short line", self.refused(self.path + "\nrm x"))
        self.assertIn("unbalanced", self.refused(self.path + ' "x'))
        self.assertIn("too many", self.refused(self.path + " a" * 20))

    def test_the_forbidden_places_include_the_main_checkout_of_a_linked_worktree_but_never_a_whole_parent_directory(self):
        self.assertEqual(notify.forbidden_roots("/a/wt", "/a/main/.git"), ["/a/wt", "/a/main/.git", "/a/main"])
        self.assertEqual(notify.forbidden_roots("/r", "/srv/r.git"), ["/r", "/srv/r.git"])

    def test_a_program_inside_the_repository_or_its_git_directory_is_refused(self):
        repo = tempfile.mkdtemp(prefix="notify-repo-")
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        inside = os.path.join(repo, "hook.sh")
        with open(inside, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(inside, 0o755)
        self.assertIn("inside the repository", self.refused(inside, forbid=[repo]))
        link = os.path.join(self.fake.dir, "link")
        os.symlink(inside, link)                                                                  # a link outside that points inside is no way round it
        self.assertIn("inside the repository", self.refused(link, forbid=[repo]))
        self.assertEqual(notify.parse_command(self.path, None, [repo])[0], os.path.realpath(self.path))


class Text(unittest.TestCase):
    def test_clean_keeps_a_narrow_alphabet(self):
        self.assertEqual(notify.clean("ok. item A, reason x_y (z) a/b #1 @h 50% a+b=c-d"), "ok. item A, reason x_y (z) a/b #1 @h 50% a+b=c-d")
        hostile = "a'b\"c`d$e\\f;g|h&i<j>k{l}m*n?o!p\x1b[31m‮\n\t\x00 q"
        got = notify.clean(hostile)
        self.assertRegex(got, r"^[A-Za-z0-9 .,:_()/#@%+=-]*\Z")
        self.assertNotIn("\x1b", got)
        self.assertEqual(len(notify.clean("x" * 1000)), notify.MAX_BODY)

    def test_compose_drops_what_is_not_an_id_and_fixes_the_title(self):
        m = notify.compose("stop", "alert", run="run-1", item="A1", code="security_blocked", outcome="parked", detail="merged 1", next_step="run loopctl status")
        self.assertEqual((m["title"], m["item"], m["run"], m["code"]), ("loop run stopped", "A1", "run-1", "security_blocked"))
        self.assertIn("item A1", m["body"])
        self.assertIn("Next: run loopctl status", m["body"])
        bad = notify.compose("stop", "alert", run="a b", item="../x", code="x;rm", outcome="$(id)")
        self.assertEqual((bad["run"], bad["item"], bad["code"], bad["outcome"]), (None, None, None, None))
        with self.assertRaises(ValueError):
            notify.compose("ping", "alert")
        with self.assertRaises(ValueError):
            notify.compose("stop", "panic")

    def test_a_hook_message_takes_no_free_text_from_its_input(self):
        raw = json.dumps({"hook_event_name": "Notification", "message": "Your account is locked, visit http://evil.example now", "notification_type": "permission_prompt",
                          "tool_name": "Bash", "title": "URGENT"})
        m = notify.hook_message(raw)
        self.assertEqual((m["kind"], m["level"]), ("hook", "action"))
        self.assertNotIn("evil", json.dumps(m))
        self.assertNotIn("locked", json.dumps(m))
        self.assertIn("permission_prompt", m["body"])
        self.assertEqual(notify.hook_message(json.dumps({"hook_event_name": "StopFailure"}))["level"], "alert")
        self.assertIn("tool Bash", notify.hook_message(json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}))["body"])
        for junk in ("not json", "[1,2]", "", json.dumps({"hook_event_name": "x" * 500, "notification_type": "A; rm"})):
            self.assertEqual(notify.hook_message(junk)["kind"], "hook")


class Send(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNotify()
        self.addCleanup(self.fake.cleanup)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": self.fake.dir, "TOPIC": "ops", "AWS_ACCESS_KEY_ID": "AKIA", "GH_TOKEN": "t",
                    "SSH_AUTH_SOCK": "/s", "ANTHROPIC_API_KEY": "k", "CLAUDECODE": "1", "KIT_NOTIFY_KIND": "spoof", "SECRET": "s"}

    def msg(self):
        return notify.compose("stop", "alert", run="run-1", item="A", code="x_y")

    def test_the_message_goes_on_stdin_and_in_variables_and_never_on_the_command_line(self):
        res = notify.send(self.fake.path + " --fixed", self.msg(), self.env, allow=["TOPIC", "AWS_ACCESS_KEY_ID", "GH_TOKEN", "SSH_AUTH_SOCK", "NOT A NAME"])
        self.assertEqual(res, {"ok": True, "rc": 0, "why": ""})
        (c,) = self.fake.calls()
        self.assertEqual(c["argv"], ["--fixed"])
        self.assertEqual((c["msg"]["kind"], c["msg"]["item"]), ("stop", "A"))
        self.assertEqual((c["env"]["TOPIC"], c["env"]["KIT_NOTIFY_KIND"], c["env"]["KIT_NOTIFY_ITEM"]), ("ops", "stop", "A"))      # ours wins over a spoofed one
        for name in ("AWS_ACCESS_KEY_ID", "GH_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "CLAUDECODE", "SECRET"):
            self.assertNotIn(name, c["env_names"])                                                # a credential family is never passed, and nothing else unasked
        self.assertEqual(os.path.realpath(c["cwd"]), os.path.realpath(self.fake.dir))              # run from HOME, not from the repository

    def test_a_failing_a_missing_and_a_hanging_command_are_reported_not_raised(self):
        self.fake.behave("fail")
        r = notify.send(self.fake.path, self.msg(), self.env)
        self.assertEqual((r["ok"], r["rc"]), (False, 3))
        self.assertIn("the channel is down", r["why"])
        self.assertTrue(notify.send("/no/such", self.msg(), self.env)["refused"])
        self.fake.behave("hang")
        import time
        t = time.time()
        r = notify.send(self.fake.path, self.msg(), self.env, timeout_s=1)
        self.assertFalse(r["ok"])
        self.assertIn("killed", r["why"])
        self.assertLess(time.time() - t, 8)                                                       # the hung program (60 s) was killed with its group


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeNotify()
        cls.addClassCleanup(cls.fake.cleanup)
        cls.lr = LoopRepo(extra_cfg="notify_command: %s\nnotify_timeout_s: 5" % cls.fake.path)
        cls.addClassCleanup(cls.lr.cleanup)
        cls.bare = LoopRepo()                                                                      # no notify_command
        cls.addClassCleanup(cls.bare.cleanup)

    def setUp(self):
        self.fake.behave("ok")
        try:
            os.unlink(os.path.join(self.fake.dir, "calls.jsonl"))
        except OSError:
            pass
        stamp = os.path.join(self.lr.repo.common_dir(), "loopctl", notify.STAMP_NAME)
        if os.path.exists(stamp):
            os.unlink(stamp)

    def env(self, lr=None):
        return dict((lr or self.lr).env)

    def test_dry_run_shows_the_message_and_the_command_and_runs_nothing(self):
        rc, out, err = run_main(["--repo", self.lr.path, "--kind", "stop", "--level", "alert", "--item", "A", "--dry-run"], env=self.env())
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc["configured"])
        self.assertEqual(doc["would_run"], [os.path.realpath(self.fake.path)])
        self.assertEqual(doc["message"]["item"], "A")
        self.assertEqual(self.fake.calls(), [])

    def test_a_send_runs_the_command_once_and_a_failure_is_exit_1(self):
        rc, _out, err = run_main(["--repo", self.lr.path, "--kind", "test"], env=self.env())
        self.assertEqual((rc, len(self.fake.calls())), (0, 1), err)
        self.fake.behave("fail")
        rc, _out, err = run_main(["--repo", self.lr.path, "--kind", "test"], env=self.env())
        self.assertEqual(rc, 1)
        self.assertIn("failed", err)

    def test_nothing_configured_is_a_refusal_for_a_person_and_silence_for_a_hook(self):
        rc, _o, err = run_main(["--repo", self.bare.path, "--kind", "test"], env=self.env(self.bare))
        self.assertEqual(rc, 3)
        self.assertIn("not set", err)
        rc, _o, err = run_main(["--repo", self.bare.path, "--hook"], stdin='{"hook_event_name": "Notification"}', env=self.env(self.bare))
        self.assertEqual((rc, err), (0, ""))

    def test_a_hook_with_hostile_text_sends_only_its_fixed_message_and_is_rate_limited(self):
        hostile = json.dumps({"hook_event_name": "Notification", "message": "rm -rf ~ and visit http://evil.example", "notification_type": "idle_prompt"})
        rc, _o, err = run_main(["--repo", self.lr.path, "--hook"], stdin=hostile, env=self.env())
        self.assertEqual(rc, 0, err)
        (c,) = self.fake.calls()
        self.assertNotIn("evil", c["stdin"])
        self.assertNotIn("rm -rf", c["stdin"])
        run_main(["--repo", self.lr.path, "--hook"], stdin=hostile, env=self.env())
        self.assertEqual(len(self.fake.calls()), 1)                                                # a second inside 30 s is dropped: a hook can fire often
        run_main(["--repo", self.lr.path, "--hook", "--min-interval-s", "0"], stdin=hostile, env=self.env())
        self.assertEqual(len(self.fake.calls()), 2)

    def test_bad_arguments_and_bad_ids_fail_closed_with_exit_2(self):
        for argv in (["--repo", self.lr.path, "--kind", "stop"], ["--repo", self.lr.path, "--kind", "done", "--item", "A"], ["--repo", self.lr.path, "--kind", "nonsense"], ["--repo", self.lr.path, "--item", "../x"], ["--repo", self.lr.path, "--code", "a;b"],
                     ["--repo", self.lr.path, "--run", "a b"], ["--repo", "/no/such/dir"], ["--bogus"]):
            with self.subTest(argv=argv):
                rc, _o, err = run_main(argv, env=self.env())
                self.assertEqual(rc, 2)
        self.assertEqual(self.fake.calls(), [])

    def test_the_settings_snippet_runs_only_the_script_with_a_fixed_command(self):
        path = os.path.join(REPO_ROOT, ".claude", "templates", "notify", "notify-hooks.settings.json")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["hooks"]), ["Notification", "PermissionRequest", "StopFailure"])
        for entries in doc["hooks"].values():
            for e in entries:
                for h in e["hooks"]:
                    self.assertEqual(h["command"], 'python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/notify.py" --hook')
                    self.assertLessEqual(h["timeout"], 60)

    def test_the_script_is_executable_as_a_program(self):
        self.assertTrue(os.stat(os.path.join(SCRIPTS_DIR, "notify.py")).st_mode & stat.S_IRUSR)
        import subprocess
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "notify.py"), "--repo", self.lr.path, "--dry-run"], env=self.env(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, shell=False, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout.decode())["configured"])


if __name__ == "__main__":
    unittest.main()
