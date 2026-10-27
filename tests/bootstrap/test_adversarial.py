"""Adversarial self-review of Phase 11a: each row is an attack tried against the receipt writer, the apply scripts or their inputs.

Grouped by what was attacked. A row that found a bug names the fix; the others record that the attempt failed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

import bootstrap_config as bc
import bootstrap_detect as bd
import bootstrap_receipt as br
import dependency_report as dr
import intake_validate as iv

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.bootstrap.test_receipt import T0, mkrepo
from tests.helpers import BaseTestCase

sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
import boundary  # noqa: E402
import labels  # noqa: E402
import repo_script  # noqa: E402

BOMB = "[" * 200000
DEEP = '{"a":' * 100000


class JsonBombs(BaseTestCase):
    """Found: RecursionError is not a ValueError, so a deeply nested file crashed every loader with a traceback. Fixed in all six."""

    def test_receipt(self):
        root = mkrepo(self)
        for raw in (BOMB, DEEP):
            with open(os.path.join(root, ".claude", "bootstrap-receipt.json"), "w") as fh:
                fh.write(raw)
            self.assertEqual(br.status(root)["state"], "invalid")
            with self.assertRaises(br.ReceiptError):
                br.add_step(root, "a", "t", "c", 0, b"", "recorded")

    def test_config_plan(self):
        root = mkrepo(self)
        os.makedirs(os.path.join(root, ".claude", "bootstrap"))
        p = os.path.join(root, ".claude", "bootstrap", "p.json")
        with open(p, "w") as fh:
            fh.write(BOMB)
        from kitlib import registry
        with self.assertRaises(bc.ConfigPlanError):
            bc.load_plan(root, p, registry.load_registry())

    def test_labels_file(self):
        tmp = self.tmpdir()
        p = os.path.join(tmp, "l.json")
        with open(p, "w") as fh:
            fh.write(BOMB)
        with self.assertRaises(labels.LabelError):
            labels.load_labels(p)

    def test_intake_answer(self):
        for raw in (BOMB, DEEP, json.dumps({"result": BOMB})):
            res, comment = iv.process(raw)
            self.assertEqual(res["status"], "invalid")

    def test_dependency_inputs_and_detect_manifests(self):
        tmp = self.tmpdir()
        p = os.path.join(tmp, "npm.json")
        with open(p, "w") as fh:
            fh.write(BOMB)
        self.assertIsNone(dr._read_json(p))
        with open(os.path.join(tmp, "package.json"), "w") as fh:
            fh.write(DEEP)
        self.assertIsNone(bd.node_facts(tmp))


class Complexity(BaseTestCase):
    """Found: drift detection compiled one regex per dependency and scanned every primer once per dependency. Now one pass."""

    def test_drift_is_linear_in_dependencies_and_text(self):
        root = os.path.realpath(self.tmpdir())
        os.makedirs(os.path.join(root, ".claude", "context"))
        pk = {"": {}}
        for i in range(4000):
            pk["node_modules/pkg-%d" % i] = {"version": "1.0.0"}
        with open(os.path.join(root, "package-lock.json"), "w") as fh:
            json.dump({"packages": pk}, fh)
        with open(os.path.join(root, ".claude", "context", "app.md"), "w") as fh:
            fh.write(("pkg-1@2 and a@1 " * 30000)[:dr.MAX_PRIMER])
        start = time.monotonic()
        report, issues, _ = dr.run(root)
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertEqual(report["drift"][0]["name"], "pkg-1")

    def test_range_parser_on_hostile_input(self):
        start = time.monotonic()
        for rng in ("^" * 99, "|| " * 30, " ".join(["<1"] * 40), "1 - " * 20, "x" * 100, "^1.2.3 " * 14):
            dr.satisfies("1.2.3", rng)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_big_issue_answers_are_rejected_before_parsing(self):
        start = time.monotonic()
        res, _ = iv.process("x" * 5_000_000, max_bytes=1000)
        self.assertEqual(res["status"], "invalid")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_file_listing_is_capped(self):
        root = os.path.realpath(self.tmpdir())
        for i in range(30):
            with open(os.path.join(root, "f%d.py" % i), "w") as fh:
                fh.write("x")
        old = bd.MAX_FILES
        bd.MAX_FILES = 10
        try:
            self.assertEqual(len(bd.list_files(root, False)), 10)
        finally:
            bd.MAX_FILES = old


class RunAllowlist(BaseTestCase):
    def setUp(self):
        BaseTestCase.setUp(self)
        self.root = mkrepo(self)
        br.init(self.root, "existing", {}, now=T0)

    def refused(self, argv):
        with self.assertRaises(br.ReceiptError, msg=str(argv)):
            br.run_and_record(self.root, "r", "t", argv, plain_env(self.root))

    def test_a_symlink_inside_the_repository_that_points_outside_is_not_a_path_inside(self):
        outside = os.path.join(self.tmpdir(), "fixture.json")
        with open(outside, "w") as fh:
            fh.write("{}")
        os.symlink(outside, os.path.join(self.root, "fx.json"))
        os.symlink(os.path.dirname(outside), os.path.join(self.root, "dir"))
        self.refused(["python3", ".claude/scripts/doctor.py", "--gh-fixture", "fx.json"])
        self.refused(["python3", ".claude/scripts/ci_lint.py", "dir"])
        self.refused(["python3", ".claude/scripts/doctor.py", "--repo", "dir"])

    def test_interpreter_options_and_other_entry_points(self):
        for argv in (["python3", "-c", "import os"], ["python3", "-m", "http.server"], ["python3", "-", ".claude/scripts/doctor.py"], ["python3", "-I", ".claude/scripts/doctor.py"],
                     ["python3", ".claude/scripts/doctor.py", "-c", "x"], ["python3", ".claude/scripts/doctor.py\0", "--json"], ["python3", "--version"],
                     ["python3", ".claude/scripts/doctor.py", "--level", "L9"], ["python3", ".claude/scripts/doctor.py", "--level", "L1;id"],
                     ["python3", ".claude/scripts/probe_run.py", "--go=abc"], ["python3", ".claude/scripts/probe_run.py", "--cmd"], ["python3", ".claude/scripts/loopctl"],
                     ["python3", ".claude/scripts/loopctl", "run"], ["python3", ".claude/scripts/loopctl", "approve"], ["python3", ".claude/scripts/loopctl", "doctor", "--live"],
                     ["python3", ".claude/scripts/guards/wiring.py", "--write"], ["python3", ".claude/scripts/guards/wiring.py", "--print"]):
            with self.subTest(argv):
                self.refused(argv)

    def test_the_child_inherits_no_credentials_even_when_passed_in(self):
        with open(os.path.join(self.root, ".claude", "scripts", "ci_lint.py"), "w") as fh:
            fh.write("import os\nprint(','.join(sorted(k for k in os.environ if k not in ('PATH','HOME','LANG','LC_ALL','PYTHONDONTWRITEBYTECODE','GIT_TERMINAL_PROMPT','TMPDIR','TZ') and not k.startswith('GIT_CONFIG') and not k.startswith('__CF') and not k.startswith('LC_'))))\n")
        env = plain_env(self.root, {"GITHUB_TOKEN": "x", "AWS_SESSION_TOKEN": "y", "NPM_TOKEN": "z", "SSH_AUTH_SOCK": "/s", "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py",
                                    "LD_PRELOAD": "/evil.so", "DYLD_INSERT_LIBRARIES": "/evil.dylib", "BASH_ENV": "/evil", "ENV": "/evil"})
        e = br.run_and_record(self.root, "r", "t", ["python3", ".claude/scripts/ci_lint.py"], env)
        self.assertEqual(e["_output"].decode().strip(), "")


class ApplyScripts(BaseTestCase):
    def test_the_slug_check_in_the_phase_1_script_now_anchors_the_end(self):
        """Found: boundary.py's slug regex ended in `$`, which matches before a trailing newline. Fixed (\\Z)."""
        for repo in ("acme/x\n", "acme/x\n\n"):
            with self.assertRaises(ValueError):
                boundary.build_apply_plan(repo, "solo")
            with self.assertRaises(ValueError):
                boundary.build_verify_plan(repo)

    def test_option_like_repo_names_are_refused_where_they_become_arguments(self):
        """Found: `-rf/x` passed the slug pattern and reached `gh ... --repo` and `gh repo create` as an argument."""
        for repo in ("-rf/x", "acme/-x", "--repo/x"):
            with self.assertRaises(labels.LabelError):
                labels.check_repo(repo)
            with self.assertRaises(repo_script.ScriptError):
                repo_script.Params(repo)

    def test_error_messages_do_not_echo_file_contents(self):
        tmp = self.tmpdir()
        p = os.path.join(tmp, "labels.json")
        with open(p, "w") as fh:
            fh.write("TOPSECRET-VALUE not json")
        with self.assertRaises(labels.LabelError) as cm:
            labels.load_labels(p)
        self.assertNotIn("TOPSECRET", str(cm.exception))
        root = mkrepo(self)
        plan = os.path.join(root, "p.json")
        with open(plan, "w") as fh:
            fh.write("TOPSECRET-VALUE")
        from kitlib import registry
        with self.assertRaises(bc.ConfigPlanError) as cm:
            bc.load_plan(root, plan, registry.load_registry())
        self.assertNotIn("TOPSECRET", str(cm.exception))

    def test_a_directory_or_missing_labels_file(self):
        tmp = self.tmpdir()
        for path in (tmp, os.path.join(tmp, "none.json"), os.devnull):
            with self.assertRaises(labels.LabelError):
                labels.load_labels(path)

    def test_the_setup_script_run_through_a_symlink_elsewhere_does_nothing(self):
        text = repo_script.generate(repo_script.Params("acme/widgets", labels_=True))
        d = os.path.realpath(self.tmpdir())
        real = os.path.join(d, "setup.sh")
        with open(real, "w") as fh:
            fh.write(text)
        link_dir = os.path.realpath(self.tmpdir())
        os.makedirs(os.path.join(link_dir, "a", "b"))
        os.symlink(real, os.path.join(link_dir, "a", "b", "setup.sh"))
        bindir = self.tmpdir()
        from tests.helpers import install_fake_gh, read_jsonl
        install_fake_gh(bindir, [{"regex": ".", "exit": 0}])
        env = plain_env(d, {"FAKE_GH_LOG": os.path.join(bindir, "log.jsonl")})
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        p = subprocess.run(["sh", os.path.join(link_dir, "a", "b", "setup.sh"), "remote", "--apply"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=d)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(read_jsonl(os.path.join(bindir, "log.jsonl")), [])

    def test_an_out_path_that_is_a_directory_is_refused(self):
        root = os.path.realpath(self.tmpdir())
        os.makedirs(os.path.join(root, ".claude", "bootstrap", "setup.sh"))
        with self.assertRaises(repo_script.ScriptError):
            repo_script.safe_out_path(root, ".claude/bootstrap/setup.sh")

    def test_known_gap_the_session_marker_is_a_tripwire(self):
        """`env -u CLAUDECODE` removes the first marker; the second still refuses. Removing both passes the check, as it does for
        apply-boundary.sh. What stops an agent then is that it holds no gh credential and the guards deny gh mutations."""
        from tests.bootstrap import session_env
        env = session_env()
        del env["CLAUDECODE"]
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        p = subprocess.run(["sh", os.path.join(SCRIPTS_DIR, "github", "apply-labels.sh"), "--repo", "acme/widgets"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 2)
        del env["CLAUDE_CODE_ENTRYPOINT"]
        p = subprocess.run(["sh", os.path.join(SCRIPTS_DIR, "github", "apply-labels.sh"), "--repo", "acme/widgets"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)   # a dry run: it prints and sends nothing, whoever runs it


class DetectEdgeCases(BaseTestCase):
    def test_odd_json_shapes_do_not_crash_detect(self):
        r = self.repo(initial_commit=False)
        r.write("package.json", json.dumps({"scripts": [], "dependencies": "x", "devDependencies": None, "release": 5, "name": {"a": 1}}))
        r.write("pyproject.toml", "\x00" * 10)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "x")
        d = bd.detect(r.path)
        self.assertEqual(d["stack"]["node"]["scripts"], [])

    def test_a_repo_with_a_trailing_newline_remote_name_in_config_is_ignored(self):
        self.assertEqual(bd.parse_remote("https://github.com/acme/widgets\n")["slug"], "acme/widgets")
        self.assertIsNone(bd.parse_remote("https://github.com/acme/wid\nets")["slug"])


if __name__ == "__main__":
    unittest.main()
