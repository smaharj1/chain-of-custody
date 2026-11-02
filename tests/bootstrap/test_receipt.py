"""bootstrap_receipt: the receipt format, the chain, the restricted `run`, and a hostile existing receipt (Phase 11a; Q6, F-15)."""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import subprocess
import sys
import unittest

import bootstrap_receipt as br

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.helpers import BaseTestCase

T0 = datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
CLI = os.path.join(SCRIPTS_DIR, "bootstrap_receipt.py")


def mkrepo(test, with_kit=True):
    """A temp directory with a .claude/ holding copies of the kit scripts the run allowlist names."""
    root = os.path.realpath(test.tmpdir())
    os.makedirs(os.path.join(root, ".claude", "scripts"))
    if with_kit:
        for name in ("doctor.py", "ci_lint.py", "bootstrap_detect.py", "bootstrap_receipt.py", "probe_run.py"):
            with open(os.path.join(root, ".claude", "scripts", name), "w") as fh:
                fh.write("import sys\nprint('stub', sys.argv[1:])\nsys.exit(int(__import__('os').environ.get('KIT_STUB_EXIT', '0')))\n")
    return root


def receipt_file(root):
    return os.path.join(root, ".claude", "bootstrap-receipt.json")


def read_doc(root):
    with open(receipt_file(root)) as fh:
        return json.load(fh)


def write_doc(root, doc):
    with open(receipt_file(root), "w") as fh:
        json.dump(doc, fh)


class Format(BaseTestCase):
    def test_init_record_emit_finish_roundtrip(self):
        root = mkrepo(self)
        br.init(root, "greenfield", {"frontend": "Next.js", "database": "PostgreSQL"}, now=T0)
        br.add_step(root, "detect", "detect the stack", "python3 .claude/scripts/bootstrap_detect.py --json", 0, b"out", "ran", now=T0)
        os.makedirs(os.path.join(root, ".claude", "bootstrap"))
        with open(os.path.join(root, ".claude", "bootstrap", "setup.sh"), "w") as fh:
            fh.write("echo hi\n")
        br.add_emit(root, "setup-script", "human-script", ".claude/bootstrap/setup.sh", now=T0)
        br.add_step(root, "doctor", "doctor", "python3 .claude/scripts/doctor.py --json", 1, b"x", "ran", "ok", now=T0)
        br.finish(root, now=T0)
        doc = read_doc(root)
        self.assertEqual(br.validate(doc), [])
        self.assertTrue(doc["complete"])
        self.assertEqual(doc["steps"][0]["output_sha256"], hashlib.sha256(b"out").hexdigest())
        self.assertEqual(doc["steps"][1]["sha256"], hashlib.sha256(b"echo hi\n").hexdigest())
        self.assertEqual(br.status(root), {"state": "complete", "steps": 3, "problem": None})
        text = br.render(doc)
        self.assertIn("COMPLETE", text)
        self.assertIn("sh .claude/bootstrap/setup.sh", text)

    def test_the_chain_catches_edits_removals_and_reordering(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        for i in range(4):
            br.add_step(root, "s%d" % i, "step", "cmd", 0, b"o%d" % i, "recorded", now=T0)
        good = read_doc(root)
        self.assertEqual(br.validate(good), [])
        edited = copy.deepcopy(good)
        edited["steps"][1]["exit_code"] = 3
        removed = copy.deepcopy(good)
        del removed["steps"][1]
        swapped = copy.deepcopy(good)
        swapped["steps"][1], swapped["steps"][2] = swapped["steps"][2], swapped["steps"][1]
        retitled = copy.deepcopy(good)
        retitled["mode"] = "greenfield"
        for name, doc in (("edited", edited), ("removed", removed), ("swapped", swapped), ("seed", retitled)):
            with self.subTest(name):
                self.assertTrue(any("chain" in p for p in br.validate(doc)), br.validate(doc))

    def test_appending_to_a_tampered_receipt_is_refused(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        br.add_step(root, "a", "step", "cmd", 0, b"", "recorded", now=T0)
        doc = read_doc(root)
        doc["steps"][0]["exit_code"] = 2
        write_doc(root, doc)
        with self.assertRaises(br.ReceiptError) as cm:
            br.add_step(root, "b", "step", "cmd", 0, b"", "recorded", now=T0)
        self.assertIn("not usable", str(cm.exception))
        self.assertEqual(len(read_doc(root)["steps"]), 1)

    def test_a_new_entry_reopens_a_finished_receipt(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        br.add_step(root, "detect", "d", "c", 0, b"", "recorded", now=T0)
        br.add_step(root, "doctor", "d", "c", 0, b"", "recorded", now=T0)
        br.finish(root, now=T0)
        br.add_step(root, "extra", "d", "c", 0, b"", "recorded", now=T0)
        self.assertFalse(read_doc(root)["complete"])

    def test_finish_needs_detect_doctor_and_no_unresolved_failure(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        with self.assertRaises(br.ReceiptError):
            br.finish(root)
        br.add_step(root, "detect", "d", "c", 0, b"", "recorded", now=T0)
        br.add_step(root, "doctor", "d", "c", 2, b"", "recorded", now=T0)
        with self.assertRaises(br.ReceiptError) as cm:
            br.finish(root)
        self.assertIn("doctor", str(cm.exception))
        br.add_step(root, "doctor", "d again", "c", 0, b"", "recorded", now=T0)
        self.assertTrue(br.finish(root)["complete"])
        br.add_step(root, "labels", "l", "c", 1, b"", "recorded", now=T0)
        with self.assertRaises(br.ReceiptError):
            br.finish(root)

    def test_init_twice_needs_force_and_force_keeps_the_old_file(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        with self.assertRaises(br.ReceiptError):
            br.init(root, "existing", {})
        br.init(root, "greenfield", {}, force=True, now=T0)
        br.init(root, "existing", {}, force=True, now=T0)
        olds = [n for n in os.listdir(os.path.join(root, ".claude")) if n.endswith(".old.json") or ".old." in n]
        self.assertEqual(len(olds), 2)

    def test_free_text_is_limited_cleaned_and_redacted(self):
        root = mkrepo(self)
        br.init(root, "existing", {"frontend": "Next.js"}, now=T0)
        token = "ghp_" + "A" * 36
        e = br.add_step(root, "a", "uses " + token, "curl -H 'Authorization: Bearer %s' x" % token, 0, b"", "recorded", note="n " + token, now=T0)
        self.assertNotIn(token, json.dumps(read_doc(root)))
        self.assertIn("title", e)
        for bad in ({"title": "x" * 121}, {"title": "a\x1b[31mred"}, {"title": "a‮b"}, {"title": ""}, {"command": "x" * 301}, {"command": "a\nb"}, {"note": "n" * 201}):
            args = dict(step_id="b", title="t", command="c", exit_code=0, output=b"", source="recorded")
            args.update(bad)
            with self.subTest(bad):
                with self.assertRaises(br.ReceiptError):
                    br.add_step(root, now=T0, **args)
        for bad_id in ("", "Upper", "1abc", "a b", "a/b", "../x", "a" * 41, "x\n"):
            with self.subTest(bad_id):
                with self.assertRaises(br.ReceiptError):
                    br.add_step(root, bad_id, "t", "c", 0, b"", "recorded", now=T0)
        for bad_stack in ({"Bad": "x"}, {"a": "x" * 121}, {"a": "x\x00"}, {"a b": "x"}):
            with self.subTest(bad_stack):
                with self.assertRaises(br.ReceiptError):
                    br.init(root, "existing", bad_stack, force=True, now=T0)


class Hostile(BaseTestCase):
    """An existing receipt, or the places it lives, controlled by an attacker."""

    def assertUnusable(self, root):
        st = br.status(root)
        self.assertEqual(st["state"], "invalid", st)
        with self.assertRaises(br.ReceiptError):
            br.add_step(root, "a", "t", "c", 0, b"", "recorded")
        p = subprocess.run([sys.executable, CLI, "--repo", root, "check"], env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 1)

    def test_not_json_wrong_shape_and_wrong_types(self):
        root = mkrepo(self)
        good = None
        br.init(root, "existing", {}, now=T0)
        br.add_step(root, "a", "t", "c", 0, b"", "recorded", now=T0)
        good = read_doc(root)
        cases = {
            "not json": "{nope",
            "array": "[]",
            "null": "null",
            "empty": "",
            "binary": "\x00\x01",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                with open(receipt_file(root), "w") as fh:
                    fh.write(raw)
                self.assertUnusable(root)
        mutations = {
            "steps not a list": lambda d: d.update(steps={"a": 1}),
            "step not an object": lambda d: d.update(steps=["x"]),
            "unknown top field": lambda d: d.update(extra=1),
            "missing field": lambda d: d.pop("mode"),
            "bad mode": lambda d: d.update(mode="both"),
            "bad schema": lambda d: d.update(schema="x"),
            "complete string": lambda d: d.update(complete="yes"),
            "bad time": lambda d: d.update(created_at="yesterday"),
            "step unknown field": lambda d: d["steps"][0].update(run="rm -rf ~"),
            "step bad exit": lambda d: d["steps"][0].update(exit_code=True),
            "step huge exit": lambda d: d["steps"][0].update(exit_code=10 ** 12),
            "step bad hash": lambda d: d["steps"][0].update(output_sha256="zz"),
            "step id traversal": lambda d: d["steps"][0].update(id="../x"),
            "step control chars": lambda d: d["steps"][0].update(title="a\x1b]0;pwned\x07"),
            "step type": lambda d: d["steps"][0].update(type="exec"),
            "step negative bytes": lambda d: d["steps"][0].update(output_bytes=-1),
            "stack list": lambda d: d.update(stack=["a"]),
            "too many steps": lambda d: d.update(steps=[d["steps"][0]] * 501),
            "kit_version object": lambda d: d.update(kit_version={"a": 1}),
        }
        for name, mut in mutations.items():
            with self.subTest(name):
                doc = copy.deepcopy(good)
                mut(doc)
                write_doc(root, doc)
                self.assertUnusable(root)

    def test_emit_entry_cannot_name_a_path_outside_bootstrap(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        os.makedirs(os.path.join(root, ".claude", "bootstrap"))
        with open(os.path.join(root, ".claude", "bootstrap", "s.sh"), "w") as fh:
            fh.write("x")
        br.add_emit(root, "s", "human-script", ".claude/bootstrap/s.sh", now=T0)
        doc = read_doc(root)
        for path in ("../../etc/passwd", "/etc/passwd", ".claude/bootstrap/../../x", ".github/workflows/x.yml", ".claude/bootstrap/a/b"):
            d = copy.deepcopy(doc)
            d["steps"][0]["path"] = path
            with self.subTest(path):
                self.assertTrue(br.validate(d))

    def test_symlink_receipt_is_refused_and_never_followed(self):
        root = mkrepo(self)
        target = os.path.join(self.tmpdir(), "victim")
        with open(target, "w") as fh:
            fh.write("precious")
        os.symlink(target, receipt_file(root))
        self.assertUnusable(root)
        with self.assertRaises(br.ReceiptError):
            br.init(root, "existing", {})
        # --force renames the link itself and leaves what it pointed at alone
        br.init(root, "existing", {}, force=True, now=T0)
        with open(target) as fh:
            self.assertEqual(fh.read(), "precious")
        self.assertFalse(os.path.islink(receipt_file(root)))

    def test_fifo_directory_and_huge_receipts_do_not_hang_or_crash(self):
        root = mkrepo(self)
        os.mkfifo(receipt_file(root))
        self.assertEqual(br.status(root)["state"], "invalid")
        os.unlink(receipt_file(root))
        os.mkdir(receipt_file(root))
        self.assertEqual(br.status(root)["state"], "invalid")
        os.rmdir(receipt_file(root))
        with open(receipt_file(root), "w") as fh:
            fh.write(" " * (br.MAX_RECEIPT + 10))
        self.assertEqual(br.status(root)["state"], "invalid")

    def test_dot_claude_as_a_symlink_out_of_the_repository(self):
        root = os.path.realpath(self.tmpdir())
        outside = os.path.realpath(self.tmpdir())
        os.symlink(outside, os.path.join(root, ".claude"))
        with self.assertRaises(br.ReceiptError):
            br.init(root, "existing", {})
        self.assertEqual(os.listdir(outside), [])

    def test_missing_kit_is_a_clear_error(self):
        root = os.path.realpath(self.tmpdir())
        with self.assertRaises(br.ReceiptError):
            br.init(root, "existing", {})

    def test_a_stale_lock_is_reported_not_stolen(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        with open(receipt_file(root) + ".lock", "w") as fh:
            fh.write("")
        orig = br.Lock.__init__

        def quick(self, root, wait=0.2):
            orig(self, root, wait)
        br.Lock.__init__ = quick
        try:
            with self.assertRaises(br.ReceiptError) as cm:
                br.add_step(root, "a", "t", "c", 0, b"", "recorded")
        finally:
            br.Lock.__init__ = orig
        self.assertIn("lock", str(cm.exception))

    def test_show_neutralises_terminal_escapes_from_a_valid_but_odd_receipt(self):
        self.assertEqual(br.safe_show("a\x1b[31mred\x07‮"), "a?[31mred??")


class Paths(BaseTestCase):
    def test_output_file_must_be_a_regular_file_inside_the_repository(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        outside = os.path.join(self.tmpdir(), "o.txt")
        with open(outside, "w") as fh:
            fh.write("secret-ish")
        inside_file = os.path.join(root, "out.txt")
        with open(inside_file, "w") as fh:
            fh.write("hello")
        link = os.path.join(root, "link.txt")
        os.symlink(outside, link)
        dirlink = os.path.join(root, "dirlink")
        os.symlink(os.path.dirname(outside), dirlink)
        fifo = os.path.join(root, "fifo")
        os.mkfifo(fifo)

        def rec(path):
            return subprocess.run([sys.executable, CLI, "--repo", root, "record", "--id", "a", "--title", "t", "--command", "c", "--exit", "0", "--output-file", path],
                                  env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(rec("out.txt").returncode, 0)
        self.assertEqual(read_doc(root)["steps"][0]["output_sha256"], hashlib.sha256(b"hello").hexdigest())
        for bad in (outside, "../" + os.path.basename(outside), "link.txt", "dirlink/o.txt", "fifo", "nope.txt", "/etc/passwd", ".", "out.txt\0x"):
            with self.subTest(bad):
                try:
                    p = rec(bad)
                except ValueError:
                    continue  # a NUL cannot even be passed on a command line
                self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(len(read_doc(root)["steps"]), 1)

    def test_emit_path_must_be_a_real_file_under_bootstrap(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        bdir = os.path.join(root, ".claude", "bootstrap")
        os.makedirs(bdir)
        outside = os.path.join(self.tmpdir(), "o.sh")
        with open(outside, "w") as fh:
            fh.write("x")
        os.symlink(outside, os.path.join(bdir, "link.sh"))
        with open(os.path.join(root, "top.sh"), "w") as fh:
            fh.write("x")
        for bad in ("link.sh", "../top.sh", "setup.sh/../../../top.sh", "missing.sh", "a b.sh", ".hidden", "x;y.sh"):
            with self.subTest(bad):
                with self.assertRaises(br.ReceiptError):
                    br.add_emit(root, "s", "human-script", ".claude/bootstrap/" + bad)
        for bad in ("top.sh", "/etc/passwd", ".claude/top.sh"):
            with self.subTest(bad):
                with self.assertRaises(br.ReceiptError):
                    br.add_emit(root, "s", "human-script", bad)
        os.symlink(bdir, os.path.join(root, ".claude", "bootlink"))
        with self.assertRaises(br.ReceiptError):
            br.add_emit(root, "s", "human-script", ".claude/bootlink/x.sh")


class Run(BaseTestCase):
    def run_argv(self, root, argv, env=None):
        return br.run_and_record(root, "r", "title", argv, env or plain_env(root))

    def test_runs_an_allowed_kit_script_and_records_exit_and_hash(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        e = self.run_argv(root, ["python3", ".claude/scripts/doctor.py", "--json", "--level", "L2"])
        self.assertEqual((e["status"], e["exit_code"], e["source"]), ("ok", 0, "ran"))
        self.assertIn(b"stub", e["_output"])
        self.assertEqual(e["output_sha256"], hashlib.sha256(e["_output"]).hexdigest())
        self.assertEqual(read_doc(root)["steps"][0]["command"], "python3 .claude/scripts/doctor.py --json --level L2")

    def test_exit_codes_outside_the_scripts_ok_set_are_failures(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        env = plain_env(root, {"KIT_STUB_EXIT": "1"})
        doctor = self.run_argv(root, ["python3", ".claude/scripts/doctor.py", "--json"], env)   # exit 1 = "errors present": an answer, not a failure
        lint = self.run_argv(root, ["python3", ".claude/scripts/ci_lint.py"], env)
        detect = self.run_argv(root, ["python3", ".claude/scripts/bootstrap_detect.py"], env)
        self.assertEqual((doctor["exit_code"], doctor["status"]), (1, "ok"))
        self.assertEqual((lint["exit_code"], lint["status"]), (1, "ok"))
        self.assertEqual((detect["exit_code"], detect["status"]), (1, "failed"))
        self.assertEqual(br.ALLOWED[".claude/scripts/guards/wiring.py"].ok_codes, (0,))

    def test_refused_commands(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        bad = [
            ["gh", "pr", "merge", "1"],
            ["git", "push"],
            ["sh", "-c", "id"],
            ["python3", "-c", "print(1)"],
            ["python3", "script.py"],
            ["python3", ".claude/scripts/verify.py"],
            ["python3", ".claude/scripts/doctor.py", "--live"],
            ["python3", ".claude/scripts/doctor.py", "--home", "/tmp"],
            ["python3", ".claude/scripts/doctor.py", "--repo", "/etc"],
            ["python3", ".claude/scripts/doctor.py", "--repo", ".."],
            ["python3", ".claude/scripts/doctor.py", "--gh-fixture", "/etc/passwd"],
            ["python3", ".claude/scripts/doctor.py", "rm"],
            ["python3", ".claude/scripts/doctor.py", "--json;id"],
            ["python3", ".claude/scripts/doctor.py", "--json\n--live"],
            ["python3", ".claude/scripts/ci_lint.py", "/etc"],
            ["python3", ".claude/scripts/ci_lint.py", "../../etc"],
            ["python3", ".claude/scripts/probe_run.py", "--go", "abc", "--image", "x"],
            ["python3", ".claude/scripts/probe_run.py", "--docker", "evil"],
            ["python3", ".claude/scripts/../scripts/doctor.py"],
            ["python3", "./.claude/scripts/doctor.py"],
            ["python3", "/abs/.claude/scripts/doctor.py"],
            ["python3"],
            [],
            ["python3", ".claude/scripts/bootstrap_receipt.py", "init", "--force"],
        ]
        for argv in bad:
            with self.subTest(argv=argv):
                with self.assertRaises(br.ReceiptError):
                    self.run_argv(root, argv)
        self.assertEqual(read_doc(root)["steps"], [])

    def test_a_script_that_resolves_outside_the_repository_is_refused(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        outside = os.path.join(self.tmpdir(), "evil.py")
        with open(outside, "w") as fh:
            fh.write("print('pwned')\n")
        os.unlink(os.path.join(root, ".claude", "scripts", "doctor.py"))
        os.symlink(outside, os.path.join(root, ".claude", "scripts", "doctor.py"))
        with self.assertRaises(br.ReceiptError):
            self.run_argv(root, ["python3", ".claude/scripts/doctor.py", "--json"])

    def test_environment_is_scrubbed(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        with open(os.path.join(root, ".claude", "scripts", "ci_lint.py"), "w") as fh:
            fh.write("import os\nprint(sorted(k for k in os.environ if k.startswith(('AWS','GH_','GITHUB','ANTHROPIC','SECRET','KIT_','CLAUDE'))))\n")
        env = plain_env(root, {"AWS_SECRET_ACCESS_KEY": "x", "GH_TOKEN": "y", "ANTHROPIC_API_KEY": "z", "SECRET_THING": "q", "KIT_CLAUDE_BIN": "/fake", "CLAUDECODE": "1"})
        e = self.run_argv(root, ["python3", ".claude/scripts/ci_lint.py"], env)
        self.assertEqual(e["_output"].decode().strip(), "['KIT_CLAUDE_BIN']")

    def test_a_timeout_is_a_failed_step(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        with open(os.path.join(root, ".claude", "scripts", "ci_lint.py"), "w") as fh:
            fh.write("import time\ntime.sleep(30)\n")
        e = br.run_and_record(root, "r", "slow", ["python3", ".claude/scripts/ci_lint.py"], plain_env(root), timeout=0.5)
        self.assertEqual((e["status"], e["exit_code"]), ("failed", 124))

    def test_cli_run_and_the_repo_root_is_the_working_directory(self):
        root = mkrepo(self)
        sub = os.path.join(root, "sub")
        os.makedirs(sub)
        p = subprocess.run([sys.executable, CLI, "--repo", root, "init", "--mode", "existing", "--stack", "backend=FastAPI"], env=plain_env(root),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run([sys.executable, CLI, "--repo", root, "run", "--id", "detect", "--title", "d", "--", "python3", ".claude/scripts/bootstrap_detect.py", "--json"],
                           env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=sub)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("step detect ok", p.stdout)
        p = subprocess.run([sys.executable, CLI, "--repo", root, "run", "--id", "bad", "--title", "d", "--", "gh", "pr", "merge"],
                           env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("run executes only", p.stderr)

    def test_the_source_has_no_shell_and_no_gh(self):
        with open(os.path.join(SCRIPTS_DIR, "bootstrap_receipt.py")) as fh:
            src = fh.read()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn('"gh"', src)
        self.assertEqual(src.count("subprocess.Popen"), 1)


class Concurrency(BaseTestCase):
    def test_parallel_appends_keep_a_valid_chain(self):
        root = mkrepo(self)
        br.init(root, "existing", {}, now=T0)
        procs = []
        for i in range(6):
            procs.append(subprocess.Popen([sys.executable, CLI, "--repo", root, "record", "--id", "s%d" % i, "--title", "t", "--command", "c", "--exit", "0", "--no-output"],
                                          env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        doc = read_doc(root)
        self.assertEqual(len(doc["steps"]), 6)
        self.assertEqual(br.validate(doc), [])


if __name__ == "__main__":
    unittest.main()
