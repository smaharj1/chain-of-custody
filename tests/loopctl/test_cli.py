"""The command line: entry point, exit codes, the small commands, and the source-level rules (fixed argv, no shell, no claude)."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR

from loopctl import cli, common

from . import LoopTestCase

LOOPCTL_DIR = os.path.join(SCRIPTS_DIR, "loopctl")


def sources():
    for dirpath, _d, names in os.walk(LOOPCTL_DIR):
        for n in names:
            if n.endswith(".py"):
                p = os.path.join(dirpath, n)
                with open(p, encoding="utf-8") as fh:
                    yield os.path.relpath(p, LOOPCTL_DIR), fh.read()


class SourceRules(unittest.TestCase):
    def test_no_shell_no_os_system_no_eval(self):
        for rel, src in sources():
            for banned in ("shell=True", "os.system", "os.popen", "eval(", "exec(", "pickle", "yaml.load", "import requests", "urllib.request", "socket.create_connection"):
                self.assertNotIn(banned, src, "%s uses %s" % (rel, banned))

    def test_subprocess_is_only_used_where_it_is_expected_and_always_with_a_list(self):
        # 4b: the doctor, `<tool> --version`; launch.py (Phase 4c) starts the child claude and one `ps`
        allowed = {"cli.py", "context.py", "abort.py", "launch.py", os.path.join("gates", "g0_env.py"), os.path.join("gates", "_toolchain.py")}
        for rel, src in sources():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "subprocess" and node.func.attr in ("run", "Popen", "call", "check_output", "check_call"):
                    self.assertIn(rel, allowed, "%s starts a process" % rel)
                    arg0 = node.args[0] if node.args else None
                    self.assertIsInstance(arg0, (ast.List, ast.Name, ast.BinOp, ast.Call), rel)
                    kw = {k.arg: k.value for k in node.keywords}
                    self.assertIn("shell", kw, "%s: subprocess call without shell=False" % rel)
                    self.assertIs(kw["shell"].value, False)

    def test_loopctl_never_names_the_claude_binary(self):
        for rel, src in sources():
            if rel in ("launch.py", "driver.py"):            # the driver (Phase 4c) is the one process that launches claude (CS-2): only these two modules know it
                continue
            for m in re.finditer(r"""["']claude["']|\bclaude -p\b|anthropic""", src):
                self.fail("%s mentions launching claude: %r" % (rel, m.group(0)))

    def test_python39_syntax_and_no_third_party_imports(self):
        stdlib = {"__future__", "argparse", "ast", "calendar", "collections", "copy", "decimal", "glob", "hashlib", "hmac", "importlib", "json", "math",
                  "os", "pkgutil", "re", "secrets", "shlex", "shutil", "signal", "socket", "stat", "subprocess", "sys", "tempfile", "threading", "time", "types",
                  "typing", "uuid", "xml"}
        for rel, src in sources():
            tree = ast.parse(src, feature_version=(3, 9))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for n in names:
                    self.assertTrue(n in stdlib or n in ("kitlib", "loopctl", "pr_prepare", "verify", "doctor", "doctorlib", "guards", "session_start"), "%s imports %s" % (rel, n))

    def test_every_git_call_goes_through_kitlib_gitutil(self):
        for rel, src in sources():
            self.assertNotIn('["git"', src, rel)
            self.assertNotIn("'git'", src, rel)


class Entry(LoopTestCase):
    def run_main(self, *args, cwd=None, env=None):
        e = dict(os.environ)
        e.update(env or {})
        e.pop("CLAUDECODE", None)
        return subprocess.run([sys.executable, os.path.join(LOOPCTL_DIR)] + list(args), cwd=cwd or REPO_ROOT, env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, shell=False, timeout=60)

    def test_the_directory_is_runnable_and_does_not_leak_its_own_modules_onto_the_path(self):
        p = self.run_main("version")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.decode().startswith("loopctl "))
        p = self.run_main("help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("TIER 2 WRITES", p.stdout.decode())
        probe = "import sys, runpy; sys.argv=['loopctl','version']; runpy.run_path(%r, run_name='__main__')" % os.path.join(LOOPCTL_DIR, "__main__.py")
        p = subprocess.run([sys.executable, "-c", "import os, sys; sys.path.insert(0, %r); import loopctl.state; print(loopctl.state.__name__)" % SCRIPTS_DIR],
                           stdout=subprocess.PIPE, shell=False)
        self.assertEqual(p.stdout.decode().strip(), "loopctl.state")

    def test_main_py_inside_the_package_directory_works_too(self):
        p = subprocess.run([sys.executable, os.path.join(LOOPCTL_DIR, "__main__.py"), "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_real_process_end_to_end(self):
        lr = self.lr()
        env = dict(lr.env)
        p = subprocess.run([sys.executable, LOOPCTL_DIR, "approve", "plan", "--by", "Pat", "--repo", lr.path], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, shell=False, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run([sys.executable, LOOPCTL_DIR, "status", "--json", "--repo", lr.path], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           shell=False, timeout=60)
        self.assertEqual(json.loads(p.stdout)["state"]["present"], True)
        p = subprocess.run([sys.executable, LOOPCTL_DIR, "nonsense"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        self.assertEqual(p.returncode, 2)

    def test_not_a_repository(self):
        d = self.tmp()
        p = self.run_main("status", "--repo", d)
        self.assertEqual(p.returncode, 2)
        self.assertIn("not inside a git repository", p.stderr.decode())

    def tmp(self):
        import tempfile, shutil
        d = tempfile.mkdtemp(prefix="loopctl-nogit-")
        self.addCleanup(shutil.rmtree, d, True)
        return d


class SmallCommands(LoopTestCase):
    def test_help_version_and_unknown(self):
        lr = self.lr()
        self.assertEqual(lr.ctl("version").out.strip().split()[0], "loopctl")
        self.assertIn("READ", lr.ctl("help").out)
        r = lr.ctl("frobnicate")
        self.assertEqual(r.rc, 2)
        self.assertIn("unknown command", r.err)

    def test_validate_json_and_text_and_exit_codes(self):
        lr = self.lr()
        j = lr.ctl("validate", "--json").json
        self.assertEqual((j["ok"], j["items"], j["critical_path"]["length"], j["width"]["max"]), (True, 3, 3, 1))
        out = lr.ctl("validate").out
        self.assertIn("critical path: 3 item(s): A -> B -> C", out)
        self.assertIn("width: 1 item(s) could run at once", out)
        lr.repo.write("plan/items/A.json", json.dumps({"id": "A"}))
        r = lr.ctl("validate")
        self.assertEqual(r.rc, 1)
        self.assertIn("INVALID", r.out)
        self.assertEqual(lr.ctl("validate", "--committed").rc, 0)        # the committed plan is still fine

    def test_validate_strict_makes_warnings_errors(self):
        lr = self.lr(items=[__import__("tests.loopctl", fromlist=["x"]).item_doc("A", acs=4)])
        self.assertEqual(lr.ctl("validate").rc, 0)
        self.assertEqual(lr.ctl("validate", "--strict").rc, 1)

    def test_validate_warns_about_an_unknown_gate_toggle(self):
        lr = self.lr(extra_cfg="gates:\n  g99: on")
        r = lr.ctl("validate")
        self.assertIn("config.unknown-gate", r.out)

    def test_evidence_check(self):
        lr = self.lr()
        r = lr.ctl("evidence", "--check")
        self.assertEqual(r.rc, 1)
        self.assertIn("NOT PASSED", r.out)
        self.assertIn("capped at L3", r.out)
        j = lr.ctl("evidence", "--check", "--json").json
        self.assertEqual((j["passed"], j["effective_level_cap"]), (False, "L3"))
        self.assertEqual(lr.ctl("evidence").rc, 2)
        lr.repo.write("docs/baseline/evidence.json", json.dumps({
            "schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
            "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"}))
        r = lr.ctl("evidence", "--check")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("PASSED", r.out)

    def test_cost_projection(self):
        lr = self.lr(extra_cfg="run_budget_usd: 12.5")
        r = lr.ctl("cost", "--projection")
        self.assertIn("unavailable (no baseline)", r.out)
        j = lr.ctl("cost", "--projection", "--json").json
        self.assertEqual(j["ceilings"]["run"], 12.5)
        self.assertEqual(lr.ctl("cost").rc, 2)

    def test_lock_commands(self):
        lr = self.lr()
        nonce = lr.canary()
        self.assertIn("lock: none", lr.ctl("lock", "status").out)
        self.assertEqual(lr.ctl("lock", "acquire", "--session", "sess-1").rc, 3)                 # canary nonce missing
        self.assertEqual(lr.ctl("lock", "acquire", "--session", "sess-1", "--nonce", "bad").rc, 3)
        self.assertEqual(lr.ctl("lock", "acquire", "--session", "sess-1", "--nonce", nonce).rc, 0)
        self.assertIn("held by session sess-1", lr.ctl("lock", "status").out)
        nonce2 = lr.canary("sess-2")
        self.assertEqual(lr.ctl("lock", "acquire", "--session", "sess-2", "--nonce", nonce2).rc, 3)
        self.assertEqual(lr.ctl("lock", "steal", "--session", "sess-2", "--nonce", nonce2).rc, 3)   # not stale
        self.assertEqual(lr.ctl("lock", "release", "--session", "sess-2").rc, 3)
        self.assertEqual(lr.ctl("lock", "release", "--session", "sess-1").rc, 0)
        lr.ctl("halt", "--reason", "x")
        self.assertEqual(lr.ctl("lock", "acquire", "--session", "sess-1", "--nonce", nonce).rc, 3)   # no lock while halted

    def test_halt_and_resume(self):
        lr = self.lr()
        self.assertEqual(lr.ctl("halt", "--reason", "pause for lunch", "--by", "Pat").rc, 0)
        h = lr.context().rs.read_halt()
        self.assertEqual((h["reason"], h["by"]), ("pause for lunch", "Pat"))
        r = lr.ctl("resume", env={"CLAUDECODE": "1"})
        self.assertEqual(r.rc, 3, r)
        self.assertIn("human command", r.err)
        self.assertTrue(lr.context().rs.is_halted())
        self.assertEqual(lr.ctl("resume").rc, 0)
        self.assertFalse(lr.context().rs.is_halted())
        self.assertIn("no HALT", lr.ctl("resume").out)

    def test_a_deeply_nested_halt_or_run_file_does_not_crash_status(self):
        lr = self.lr()
        lr.fast_start()
        rs = lr.context().rs
        with open(rs.halt_path, "w") as fh:
            fh.write("[" * 200000)
        with open(rs.run_path, "w") as fh:
            fh.write("[" * 200000)
        r = lr.ctl("status")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("HALT", r.out)
        self.assertEqual(lr.ctl("status", "--pending-human").rc, 0)

    def test_doctor_is_wrapped_not_reimplemented(self):
        lr = self.lr()
        r = lr.ctl("doctor", "--level")
        self.assertIn("== Autonomy level ==", r.out)
        self.assertIn("evidence", r.out)
        j = json.loads(lr.ctl("doctor", "--json").out)
        self.assertEqual(j["schema"], "doctor.v1")
        self.assertEqual(j["repo"], lr.path)
        s = lr.ctl("status", "--level").out
        self.assertIn("effective level L", s)

    def test_gate_command_runs_a_registered_gate_and_records_it(self):
        import types
        from loopctl import gates
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        r = lr.c("gate", "g6", "A")
        self.assertEqual(r.rc, 2, r)                                                # not installed
        gates.register(types.SimpleNamespace(NAME="g6", OWNER_METRIC="m", run=lambda ctx: gates.GateResult.failed([{"id": "x", "severity": "error", "message": "no"}]),
                                             __name__="fake"))
        self.addCleanup(gates.unregister, "g6")
        r = lr.c("gate", "g6", "A")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("BLOCKING", r.out)
        self.assertEqual(lr.item("A")["gates"]["g6"]["status"], "fail")
        self.assertEqual(lr.c("gate", "g6", "ZZZ").rc, 3)
        self.assertEqual(lr.ctl("gate", "g6", "A").rc, 3)                           # a caller that is not the lock holder

    def test_spend_record_parks_on_an_unknown_cost(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.write("ok.json", json.dumps({"total_cost_usd": 0.5, "session_id": "child-1"}))
        lr.repo.write("bad.json", json.dumps({"result": "ok"}))
        r = lr.c("spend", "record", "--item", "A", "--result", "ok.json")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("$0.5000", r.out)
        r = lr.ctl("spend", "record", "--item", "A", "--result", "bad.json", "--session", lr.session)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("UNKNOWN cost", r.out)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("needs_human", "cost_unknown"))
        self.assertIn("approve cost", lr.ctl("status", "--pending-human").out)
        lr.ctl("approve", "cost", "--by", "Pat")
        self.assertNotIn("approve cost", lr.ctl("status", "--pending-human").out)

    def test_a_spend_over_the_ceiling_parks_the_item(self):
        lr = self.lr(extra_cfg="run_budget_usd: 1")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.write("c.json", json.dumps({"total_cost_usd": 1.5}))
        r = lr.c("spend", "record", "--item", "A", "--result", "c.json")
        self.assertEqual(r.rc, 3, r)
        self.assertEqual(lr.item("A")["blockReason"], "budget_run")


if __name__ == "__main__":
    unittest.main()
