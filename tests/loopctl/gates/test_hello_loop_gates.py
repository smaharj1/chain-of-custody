"""The Phase 4b exit criterion on the real fixture: G3's held-out half (runner: driver) judges hello-loop's reference solution, offline.

The fixture's own black-box acceptance runner (`examples/hello-loop/acceptance/run_checks.py`, which no baseline run receives) is installed
as the HELD-OUT tests under `<git-common-dir>/loopctl/heldout/F1/`. The reference solution passes every criterion; the same item without
its migration fails them, and the verdict names the criteria. This is the "hidden from the engineer, run by loopctl" half of report section 9 G3.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from tests.baseline import FIXTURE, load_tool
from .. import LoopRepo, LoopTestCase

from loopctl import gates

PREPARE = load_tool("prepare")
ITEM = "F1"


def fixture_item():
    with open(os.path.join(FIXTURE, "plan", "items", "F1.json")) as fh:
        it = json.load(fh)
    it["milestone"] = "M1"
    it["ext"] = {"g3": {"visible": [], "cmd": "%s acceptance_visible.py" % sys.executable, "heldout": True}}
    return it


class HelloLoop(LoopTestCase):
    @classmethod
    def setUpClass(cls):
        item = fixture_item()
        cfg = ("runner: driver\nheldout_cmd: \"%s $LOOPCTL_HELDOUT_DIR/run_checks.py --project . --item F1 --json\"\n"
               "verify_tests: \"%s -m unittest discover -s tests -t .\"\n" % (sys.executable, sys.executable))
        cls.lr = LoopRepo(items=[item], extra_cfg=cfg, real_gates=True)
        for rel in PREPARE.fixture_files(FIXTURE):
            dst = os.path.join(cls.lr.path, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(FIXTURE, rel), dst)
        # the visible command the plan names: the same harness, run from the fixture (the criteria have ids, the harness prints per-criterion results)
        with open(os.path.join(cls.lr.path, "acceptance_visible.py"), "w") as fh:
            fh.write("import subprocess, sys\nsys.exit(subprocess.call([sys.executable, %r, '--project', '.', '--item', 'F1', '--json']))\n"
                     % os.path.join(FIXTURE, "acceptance", "run_checks.py"))
        with open(os.path.join(cls.lr.path, ".gitignore"), "w") as fh:
            fh.write("__pycache__/\n*.pyc\n.data/\n")
        cls.lr.commit_all("the fixture, before the item")
        cls.base = cls.lr.repo.head()

    @classmethod
    def tearDownClass(cls):
        cls.lr.cleanup()

    def setUp(self):
        self.lr.repo.git("reset", "-q", "--hard")
        self.lr.repo.git("clean", "-fdq")
        self.lr.repo.git("switch", "-q", "main")
        self.lr.repo.git("switch", "-q", "-C", "feat/f1")
        self.heldout = os.path.join(self.lr.context().rs.heldout_dir, ITEM)
        os.makedirs(self.heldout, exist_ok=True)
        shutil.copyfile(os.path.join(FIXTURE, "acceptance", "run_checks.py"), os.path.join(self.heldout, "run_checks.py"))

    def tearDown(self):
        shutil.rmtree(self.heldout, ignore_errors=True)
        self.lr.repo.git("reset", "-q", "--hard")
        self.lr.repo.git("clean", "-fdq")
        self.lr.repo.git("switch", "-q", "main")

    def apply_solution(self, drop=()):
        stage = os.path.join(FIXTURE, "reference-solution", ITEM)
        for dirpath, _dirs, files in os.walk(stage):
            for fn in files:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, stage)
                if any(rel.startswith(d) for d in drop):
                    continue
                dst = os.path.join(self.lr.path, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
        self.lr.commit_all("F1")

    def gate(self, name):
        ctx = self.lr.context().gate_context(ITEM, None, None, {})
        return gates.run_gate(name, ctx)

    def test_the_reference_solution_passes_visible_and_held_out(self):
        self.apply_solution()
        res = self.gate("g3")
        self.assertEqual(res["status"], "pass", json.dumps(res["findings"], indent=1)[:2000])
        self.assertIn("visible 5/5; held-out 5/5; gap +0%", res["evidence"]["summary"])
        self.assertEqual(sorted(res["evidence"]["ac_results"]), ["AC-F1-%d" % n for n in range(1, 6)])
        self.assertTrue(all(c["result"] == "pass" for c in res["evidence"]["ac_results"].values()))

    def test_the_same_item_without_its_migration_fails_and_names_the_criteria(self):
        self.apply_solution(drop=("hello_loop/migrations",))
        res = self.gate("g3")
        self.assertEqual(res["status"], "fail")
        failed = {f["message"].split()[0] for f in res["findings"] if f["id"] in ("g3.ac-failed", "g3.heldout-failed")}
        self.assertTrue({"AC-F1-1", "AC-F1-3"} <= failed, failed)

    def test_the_held_out_runner_is_not_in_the_engineers_tree(self):
        self.apply_solution()
        self.assertFalse(os.path.exists(os.path.join(self.lr.path, "acceptance")))
        self.assertEqual(self.lr.repo.git("status", "--porcelain", "--untracked-files=all"), "")

    def test_no_held_out_tests_installed_is_a_failure_when_the_plan_declares_them(self):
        self.apply_solution()
        shutil.rmtree(self.heldout)
        res = self.gate("g3")
        self.assertEqual(res["status"], "fail")
        self.assertIn("g3.heldout-missing", [f["id"] for f in res["findings"]])


if __name__ == "__main__":
    import unittest
    unittest.main()
