"""IMP-25: pr_prepare.py never pushes, never opens a PR, never writes to git. Proved three ways: the source, the allowlist, and the argv log."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import unittest
from unittest import mock

import pr_prepare

from . import FeatureCase, good_body
from .. import REPO_ROOT

SCRIPT = os.path.join(REPO_ROOT, ".claude", "scripts", "pr_prepare.py")


def git_subcommand(argv):
    """The git subcommand of an argv like ['git', '-c', 'k=v', '-C', path, 'log', ...]."""
    args = list(argv[1:])
    i = 0
    while i < len(args):
        if args[i] in ("-c", "-C"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return None


class Source(unittest.TestCase):
    def test_the_script_cannot_start_a_process_of_its_own(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("import subprocess", "from subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "Popen", "pty."):
            self.assertNotIn(banned, src, banned)

    def test_no_push_or_gh_call_is_constructed_as_an_argument_list(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        for m in re.finditer(r"_git\([^\n]*\)|_out\([^\n]*\)", src):
            call = m.group(0)
            self.assertNotRegex(call, r"\"(push|commit|add|reset|checkout|switch|merge|rebase|tag|branch|fetch|pull|stash|clean|rm|mv|update-ref|gc)\"", call)

    def test_the_allowlist_holds(self):
        r = "."
        for args in (["push"], ["commit", "-m", "x"], ["add", "-A"], ["reset", "--hard"], ["checkout", "x"], ["switch", "x"], ["merge", "x"],
                     ["rebase", "x"], ["fetch"], ["tag", "t"], ["branch", "-D", "x"], ["update-ref", "refs/heads/x", "y"], ["stash"],
                     ["config", "user.name", "x"], ["config", "--unset", "x"], ["remote", "add", "o", "u"], ["remote", "set-url", "o", "u"], []):
            with self.subTest(args):
                with self.assertRaises(pr_prepare.PrError):
                    pr_prepare._git(r, args)
        for args in (["log", "-1"], ["rev-parse", "HEAD"], ["config", "--get", "user.name"], ["remote", "get-url", "origin"], ["status", "--porcelain"]):
            with self.subTest(args):
                pr_prepare._git(REPO_ROOT, args)  # allowed; the result does not matter

    def test_the_read_only_set_has_no_writer(self):
        self.assertEqual(sorted(pr_prepare.READ_ONLY_GIT & {"push", "commit", "add", "reset", "checkout", "switch", "merge", "rebase", "fetch",
                                                            "tag", "branch", "update-ref", "stash", "clean", "rm", "mv", "pull"}), [])


class Runtime(FeatureCase):
    def test_every_process_started_during_a_full_flow_is_read_only_git(self):
        r = self.make_feature()
        self.write_verify(r)
        seen = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def run(argv, *a, **kw):
            seen.append(list(argv))
            return real_run(argv, *a, **kw)

        def popen(argv, *a, **kw):
            seen.append(list(argv))
            return real_popen(argv, *a, **kw)

        with mock.patch.object(subprocess, "run", run), mock.patch.object(subprocess, "Popen", popen):
            text, ctx = pr_prepare.init_body(r.path)
            pr_prepare.write_body(ctx.body_file, text)
            pr_prepare.check(r.path, mode="check")
            self.ready(r)
            res = pr_prepare.check(r.path, mode="prepare")
            pr_prepare.find_reverts(r.path, [r.head()])
        self.assertTrue(res["ok"], res["findings"])
        self.assertGreater(len(seen), 10)
        for argv in seen:
            self.assertEqual(argv[0], "git", argv)
            sub = git_subcommand(argv)
            self.assertIn(sub, pr_prepare.READ_ONLY_GIT, argv)
            self.assertNotIn("push", argv)

    def test_nothing_changed_in_the_repository(self):
        r = self.make_feature()
        self.ready(r)
        before = (r.git("rev-parse", "HEAD"), r.git("status", "--porcelain=v1"), r.git("for-each-ref"), r.git("config", "--local", "--list"))
        pr_prepare.check(r.path, mode="prepare")
        after = (r.git("rev-parse", "HEAD"), r.git("status", "--porcelain=v1"), r.git("for-each-ref"), r.git("config", "--local", "--list"))
        self.assertEqual(before, after)

    def test_a_hostile_git_config_does_not_run_anything(self):
        r = self.make_feature()
        self.ready(r)
        marker = os.path.join(self.tmpdir(), "ran")
        hook = os.path.join(self.tmpdir(), "fsmonitor.sh")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\nexit 1\n" % shlex.quote(marker))
        os.chmod(hook, 0o755)
        r.git("config", "core.fsmonitor", hook)
        r.git("config", "diff.external", hook)
        r.git("config", "core.hooksPath", os.path.dirname(hook))
        res = pr_prepare.check(r.path, mode="prepare")
        self.assertTrue(res["ok"], res["findings"])
        self.assertFalse(os.path.exists(marker), "git ran a program named in the repository's config")

    def test_a_hostile_branch_name_never_reaches_a_command(self):
        r = self.make_feature()
        self.ready(r)
        for bad in ("feat/x; rm -rf ~", "--upload-pack=evil", "feat/$(id)", "a b", "feat/x\nrm", "../../x", "feat/x.lock", "feat/@{u}"):
            with self.subTest(bad):
                res = pr_prepare.check(r.path, mode="prepare", branch=bad)
                self.assertFalse(res["ok"])
                self.assertEqual(res["commands"], [])

    def test_a_hostile_title_is_quoted_not_executed(self):
        r = self.make_feature()
        self.write_verify(r)
        self.write_body(r, good_body(r.head(), self.base, title="x'; touch /tmp/pwned; echo '"))
        res = pr_prepare.check(r.path, mode="prepare")
        create = [c for c in res["commands"] if c.startswith("gh pr create")][0]
        words = shlex.split(create)
        self.assertEqual(words[words.index("--title") + 1], "x'; touch /tmp/pwned; echo '")
        self.assertEqual(len([w for w in words if w == "touch"]), 0)


if __name__ == "__main__":
    unittest.main()
