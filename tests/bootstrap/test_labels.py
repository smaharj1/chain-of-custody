"""labels.py / apply-labels.sh: dry run by default, every gh call printed, refuse inside Claude Code, hostile label files (IMP-15, RC-20)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env, session_env
from tests.helpers import BaseTestCase

sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
import labels  # noqa: E402

GH_DIR = os.path.join(SCRIPTS_DIR, "github")
APPLY_SH = os.path.join(GH_DIR, "apply-labels.sh")
TEMPLATE = os.path.join(REPO_ROOT, ".claude", "templates", "github", "labels.json")


class Recorder(object):
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, argv, stdin=None, stdout=None, stderr=None, timeout=None, shell=False, env=None):
        self.calls.append({"argv": list(argv), "shell": shell})

        class P(object):
            pass
        p = P()
        failing = self.fail_on == len(self.calls)
        p.returncode, p.stdout, p.stderr = (1, b"", b"HTTP 403") if failing else (0, b"", b"")
        return p


def run_main(argv, env=None, runner=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = labels.main(argv, env=env if env is not None else {"PATH": "/usr/bin"}, runner=runner or Recorder())
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def labels_file(tmp, items, raw=None):
    p = os.path.join(tmp, "labels.json")
    with open(p, "w") as fh:
        fh.write(raw if raw is not None else json.dumps({"labels": items}))
    return p


class Template(unittest.TestCase):
    def test_the_template_is_valid_and_has_the_report_labels(self):
        names = [l.name for l in labels.load_labels(TEMPLATE)]
        for want in ("factory:feature", "factory:bug", "factory:deps", "factory:chore", "factory:go",
                     "factory:requested", "factory:running", "factory:halt", "plan-ok", "preview"):
            self.assertIn(want, names)

    def test_approver_only_matches_what_label_guard_removes(self):
        with open(os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows", "label-guard.yml")) as fh:
            guard = fh.read()
        self.assertIn("factory:requested) exit 0", guard)
        for lab in labels.load_labels(TEMPLATE):
            guarded = lab.name != "factory:requested" and (lab.name.startswith("factory:") or lab.name in ("plan-ok", "preview"))
            self.assertEqual(lab.approver_only, guarded, lab.name)

    def test_no_label_name_could_be_taken_for_a_flag(self):
        for lab in labels.load_labels(TEMPLATE):
            self.assertFalse(lab.name.startswith("-"))


class DryRun(unittest.TestCase):
    def test_dry_run_prints_every_call_and_sends_none(self):
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "acme/widgets"], runner=rec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec.calls, [])
        self.assertIn("DRY RUN", out)
        n = len(labels.load_labels(TEMPLATE))
        self.assertEqual(out.count("gh label create"), n)
        self.assertIn("gh label create factory:go --repo acme/widgets --color 5319e7", out)
        self.assertIn("FACTORY_APPROVERS", out)

    def test_apply_makes_one_call_per_label_without_a_shell(self):
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--apply"], runner=rec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(rec.calls), len(labels.load_labels(TEMPLATE)))
        for c in rec.calls:
            self.assertFalse(c["shell"])
            self.assertEqual(c["argv"][:3], ["gh", "label", "create"])
            self.assertIn("--force", c["argv"])

    def test_a_failed_call_stops_the_run(self):
        rec = Recorder(fail_on=2)
        rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--apply"], runner=rec)
        self.assertEqual(rc, 1)
        self.assertEqual(len(rec.calls), 2)
        self.assertIn("Stopped", err)

    def test_list_is_offline(self):
        rc, out, _ = run_main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("factory:halt", out)


class Refusal(BaseTestCase):
    def test_refuses_inside_a_session_in_python(self):
        for env in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(env=env):
                rec = Recorder()
                rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--apply"], env=env, runner=rec)
                self.assertEqual(rc, 2)
                self.assertEqual(rec.calls, [])
                self.assertIn("refusing", err)

    def test_the_shell_wrapper_refuses_and_prints_nothing_else(self):
        for extra in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(extra=extra):
                p = subprocess.run(["sh", APPLY_SH, "--repo", "acme/widgets", "--apply"], env=plain_env(extra=extra),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(p.stdout, "")
                self.assertIn("refusing", p.stderr)

    def test_the_shell_wrapper_dry_runs_in_a_plain_terminal(self):
        p = subprocess.run(["sh", APPLY_SH, "--repo", "acme/widgets"], env=plain_env(self.tmpdir()),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DRY RUN", p.stdout)

    def test_session_env_helper_really_sets_a_marker(self):
        self.assertIn("CLAUDECODE", session_env())


class Hostile(BaseTestCase):
    """Label names and repo names are data; none may reach a command line as anything but one argument."""

    def test_bad_repo_names(self):
        for repo in ("widgets", "a/b/c", "acme/wid gets", "", "acme/;rm -rf ~", "acme/$(id)", "acme/`id`", "acme/x\nrm", "-rf/x",
                     "../x", "acme/..", "./x", "acme/x.git", "a" * 40 + "/x", "acme/" + "b" * 101, "acme/x|y", "acme/x&y", "acme/x\n", "acme/x\r", "-rf/x", "acme/-x", ".o/x", "acme/‮x"):
            with self.subTest(repo=repo):
                rec = Recorder()
                rc, out, err = run_main(["apply", "--repo", repo, "--apply"], runner=rec)
                self.assertEqual(rc, 2)
                self.assertEqual(rec.calls, [])

    def test_check_repo_directly_rejects_names_that_could_be_options(self):
        # argparse already rejects `--repo -rf/x`, so this calls the check itself: other callers (repo_script.py) pass the value straight in
        for repo in ("-rf/x", "acme/-x", "-/x", ".o/x", "acme/x\n"):
            with self.subTest(repo):
                with self.assertRaises(labels.LabelError):
                    labels.check_repo(repo)
        self.assertEqual(labels.check_repo("acme/.github"), "acme/.github")
        self.assertEqual(labels.check_repo("a-b/c_d.e"), "a-b/c_d.e")

    def test_bad_label_names(self):
        tmp = self.tmpdir()
        bad = ["ok\n", "-f", "--repo", "x;rm -rf ~", "$(id)", "`id`", "a\nb", "", " lead", "trail ", "a" * 51, "q\"uote", "a'b", "a|b", "a&b", "a>b", "‮ev", "a//b", "../x", "emoji\U0001f600"]
        for name in bad:
            with self.subTest(name=name):
                p = labels_file(tmp, [{"name": name, "color": "ffffff", "description": "d"}])
                rec = Recorder()
                rc, out, err = run_main(["apply", "--repo", "acme/widgets", "--labels-file", p, "--apply"], runner=rec)
                self.assertEqual(rc, 2)
                self.assertEqual(rec.calls, [])

    def test_bad_colors_and_descriptions(self):
        tmp = self.tmpdir()
        for color in ("ffffff\n", "#ffffff", "fff", "gggggg", "ffffff;id", "", 123456, None):
            with self.subTest(color=color):
                p = labels_file(tmp, [{"name": "ok", "color": color, "description": "d"}])
                self.assertEqual(run_main(["apply", "--repo", "a/b", "--labels-file", p])[0], 2)
        for desc in ("x" * 101, "line\nbreak", "bell\x07", "zero​width", "‮flip", 5):
            with self.subTest(desc=desc):
                p = labels_file(tmp, [{"name": "ok", "color": "ffffff", "description": desc}])
                self.assertEqual(run_main(["apply", "--repo", "a/b", "--labels-file", p])[0], 2)

    def test_description_with_shell_metacharacters_is_one_quoted_argument(self):
        tmp = self.tmpdir()
        p = labels_file(tmp, [{"name": "ok", "color": "ffffff", "description": "a $(id) `id` ; rm -rf ~ 'q' \"d\""}])
        rec = Recorder()
        rc, out, err = run_main(["apply", "--repo", "a/b", "--labels-file", p, "--apply"], runner=rec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(rec.calls[0]["argv"][-2], "a $(id) `id` ; rm -rf ~ 'q' \"d\"")
        # the printed form is shell-quoted, so pasting it cannot run the payload
        shown = [ln for ln in out.split("\n") if "gh label create" in ln][0].split("] ", 1)[1]
        import shlex
        self.assertEqual(shlex.split(shown)[-2], rec.calls[0]["argv"][-2])

    def test_hostile_files(self):
        tmp = self.tmpdir()
        cases = {
            "not json": "{nope",
            "list": "[]",
            "no labels": "{}",
            "empty": '{"labels": []}',
            "unknown field": '{"labels": [{"name":"a","color":"ffffff","run":"id"}]}',
            "duplicate": '{"labels": [{"name":"A","color":"ffffff"},{"name":"a","color":"ffffff"}]}',
            "non-object": '{"labels": ["a"]}',
            "too many": json.dumps({"labels": [{"name": "l%d" % i, "color": "ffffff"} for i in range(101)]}),
            "bool wrong": '{"labels": [{"name":"a","color":"ffffff","approver_only":"yes"}]}',
        }
        for title, raw in cases.items():
            with self.subTest(title):
                p = labels_file(tmp, None, raw=raw)
                rc, out, err = run_main(["apply", "--repo", "a/b", "--labels-file", p])
                self.assertEqual(rc, 2, err)

    def test_a_fifo_or_missing_or_huge_file_is_an_error_not_a_hang(self):
        tmp = self.tmpdir()
        fifo = os.path.join(tmp, "fifo.json")
        os.mkfifo(fifo)
        self.assertEqual(run_main(["apply", "--repo", "a/b", "--labels-file", fifo])[0], 2)
        self.assertEqual(run_main(["apply", "--repo", "a/b", "--labels-file", os.path.join(tmp, "none.json")])[0], 2)
        big = os.path.join(tmp, "big.json")
        with open(big, "w") as fh:
            fh.write('{"labels": [' + " " * (300 * 1024) + "]}")
        self.assertEqual(run_main(["apply", "--repo", "a/b", "--labels-file", big])[0], 2)


if __name__ == "__main__":
    unittest.main()
