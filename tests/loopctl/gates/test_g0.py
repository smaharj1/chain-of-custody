"""G0: the environment is fit to start the item."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys

from .. import item_doc
from . import GateCase

from loopctl import gates

EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})
UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n\n    @unittest.skip('known')\n    def test_two(self):\n        pass\n"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()


class G0(GateCase):
    ITEMS = [item_doc("A", acs=1), item_doc("E", acs=1, slug="slice-e", requiresEnv=["DATABASE_URL"])]
    CFG = ("verify_tests: \"%s -m unittest -v tests.unit.test_u\"\nverify_ports:\n  - %d\nenv_check_cmd: \"%s check_env.py\"\n" % (sys.executable, PORT, sys.executable))
    BASE_FILES = {"tests/__init__.py": "", "tests/unit/__init__.py": "", "tests/unit/test_u.py": UNIT, "check_env.py": "import os, sys\nsys.exit(1 if os.path.exists('db-down') else 0)\n"}

    def test_a_fit_environment_passes_and_records_the_baseline(self):
        res = self.run_gate("g0")
        self.assertPass(res)
        self.assertIn("baseline 2 tests, 1 skipped", res["evidence"]["summary"])
        from loopctl.gates import _lib
        path = os.path.join(_lib.runstate_of(self.ctx()).root, "baseline", "A.json")
        with open(path) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["collected"], rec["skipped"], rec["base"]), (2, 1, self.base))

    def test_the_doctor_is_read_and_a_level_it_allows_does_not_fail_the_gate(self):
        res = self.run_gate("g0")
        self.assertNotIn("g0.level-capped", self.ids(res))          # configured L1, effective L1

    def test_a_missing_required_environment_variable_names_the_variable_not_a_value(self):
        res = self.run_gate("g0", "E")
        self.assertFails(res, "g0.missing-env-var")
        self.assertIn("DATABASE_URL", [f["message"] for f in res["findings"] if f["id"] == "g0.missing-env-var"][0])
        self.assertEqual(res["failure_class"], "env")
        self.lr.env["DATABASE_URL"] = "postgres://user:pw@host/db"
        try:
            res = self.run_gate("g0", "E")
            self.assertNotIn("g0.missing-env-var", self.ids(res))
            self.assertNotIn("user:pw", json.dumps(res))
        finally:
            del self.lr.env["DATABASE_URL"]

    def test_a_dirty_tree_is_an_env_failure(self):
        with open(os.path.join(self.lr.path, "stray.txt"), "w") as fh:
            fh.write("x")
        res = self.run_gate("g0")
        self.assertFails(res, "g0.tree-dirty")
        self.assertEqual(res["failure_class"], "env")

    def test_design_documents_do_not_make_the_tree_dirty(self):
        d = os.path.join(self.lr.path, "docs", "features", "slice-a")
        os.makedirs(d)
        with open(os.path.join(d, "technical-design.md"), "w") as fh:
            fh.write("# design\n")
        self.assertNotIn("g0.tree-dirty", self.ids(self.run_gate("g0")))

    def test_a_toolchain_pin_that_does_not_match_the_installed_tool(self):
        bindir = os.path.join(self.lr.path, "..", "fakebin")
        os.makedirs(bindir, exist_ok=True)
        node = os.path.join(bindir, "node")
        with open(node, "w") as fh:
            fh.write("#!/bin/sh\necho v25.1.0\n")
        os.chmod(node, os.stat(node).st_mode | stat.S_IEXEC)
        old = self.lr.env["PATH"]
        self.lr.env["PATH"] = os.path.realpath(bindir) + os.pathsep + old
        try:
            # the pin must be on main: it is read from the base
            self.lr.repo.git("switch", "-q", "main")
            self.lr.repo.write(".nvmrc", "v20.11.0\n")
            self.lr.commit_all("pin node")
            try:
                self.lr.repo.git("switch", "-q", "-c", "feat/pin-test")
                res = self.run_gate("g0")
                self.assertFails(res, "g0.toolchain")
                self.assertIn("pinned node 20.11.0 but the installed version is v25.1.0", json.dumps(res["findings"]))
            finally:
                self.lr.repo.git("switch", "-q", "main")
                self.lr.repo.git("reset", "-q", "--hard", self.base)
        finally:
            self.lr.env["PATH"] = old

    def test_a_busy_verify_port_is_transient(self):
        s = socket.socket()
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
        try:
            res = self.run_gate("g0")
            self.assertFails(res, "g0.port-in-use")
            self.assertEqual(res["failure_class"], "transient")
        finally:
            s.close()

    def test_the_project_env_check_failing_is_db_down(self):
        with open(os.path.join(self.lr.path, "db-down"), "w") as fh:
            fh.write("")
        res = self.run_gate("g0")
        self.assertIn("g0.env-check", self.ids(res))
        self.assertEqual(res["failure_class"], "env")

    def test_a_red_baseline_is_not_the_items_fault(self):
        self.lr.repo.git("switch", "-q", "main")
        self.lr.repo.write("tests/unit/test_u.py", UNIT.replace("1 + 0", "2"))
        self.lr.commit_all("red on main")
        try:
            self.lr.repo.git("switch", "-q", "-c", "feat/red-base")
            res = self.run_gate("g0")
            self.assertFails(res, "g0.baseline-red")
            self.assertEqual(res["failure_class"], "env")
        finally:
            self.lr.repo.git("switch", "-q", "main")
            self.lr.repo.git("reset", "-q", "--hard", self.base)

    def test_after_code_changed_the_baseline_is_not_rerun(self):
        self.commit({"src/x.py": "x = 1\n"}, "code")
        res = self.run_gate("g0")
        self.assertPass(res)
        self.assertIn("g0.baseline-skipped", self.ids(res))


class G0Level(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    CFG = "autonomy_level: L3\nverify_tests: \"%s -c 'print(1)'\"\n" % sys.executable

    def test_a_run_configured_above_the_doctors_level_does_not_start(self):
        res = self.run_gate("g0")
        self.assertFails(res, "g0.level-capped")
        self.assertEqual(res["failure_class"], "env")
        self.assertIn("configured level L3", [f["message"] for f in res["findings"] if f["id"] == "g0.level-capped"][0])


class G0NoVerify(GateCase):
    ITEMS = [item_doc("A", acs=1)]

    def test_an_unconfigured_baseline_is_an_env_failure(self):
        res = self.run_gate("g0")
        self.assertFails(res, "g0.baseline-config")


if __name__ == "__main__":
    import unittest
    unittest.main()
