"""verify.py runs the hello-loop fixture end to end (Phase 3 exit criterion for G6, DEC-27).

The fixture's starting tree plus its kit-overlay config is the base commit; the reference solution applied item by item is
the head. The per-item commands come from the fixture's own verify.json, read as an items file.
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest
from unittest import mock

import verify

from ..baseline import FIXTURE, load_tool
from ..helpers import BaseTestCase, TempRepo

PREPARE = load_tool("prepare")
ITEMS = ["F1", "F2", "F3"]
ITEMS_FILE = os.path.join(FIXTURE, "verify.json")


class HelloLoopVerify(BaseTestCase):
    def setUp(self):
        super().setUp()
        shim = self.tmpdir("hello-shim-")
        os.symlink(sys.executable, os.path.join(shim, "python3"))
        patcher = mock.patch.dict(os.environ, {"PATH": shim + os.pathsep + os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DATABASE_URL", None)

    def project(self, upto: str) -> "tuple":
        """A repo whose `main` is the run's starting tree plus the overlay config and whose `feat/hello` has the solution through `upto`."""
        r = TempRepo(initial_commit=False)
        self._repos.append(r)
        for rel in PREPARE.fixture_files(FIXTURE):
            dst = os.path.join(r.path, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(FIXTURE, rel), dst)
        cfg = os.path.join(r.path, ".claude", "loop.config.md")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        shutil.copyfile(os.path.join(FIXTURE, "kit-overlay", ".claude", "loop.config.md"), cfg)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "base")
        base = r.head()
        r.git("switch", "-q", "-c", "feat/hello")
        for item in ITEMS[: ITEMS.index(upto) + 1]:
            stage = os.path.join(FIXTURE, "reference-solution", item)
            for dirpath, _dirs, files in os.walk(stage):
                for fn in files:
                    src = os.path.join(dirpath, fn)
                    dst = os.path.join(r.path, os.path.relpath(src, stage))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "solution through " + upto)
        return r, base

    def test_every_item_verifies_with_its_own_commands(self):
        for item in ITEMS:
            with self.subTest(item=item):
                r, base = self.project(item)
                res = verify.verify(r.path, base=base, item=item, items_file=ITEMS_FILE, write_log=False)
                self.assertEqual(res["status"], "pass", res)
                self.assertTrue(res["ok"] and res["complete"] and res["trusted"])
                stages = {s["name"]: s for s in res["stages"]}
                self.assertEqual(stages["item-tests"]["status"], "pass")
                self.assertGreater(stages["item-tests"]["tests_collected"], 0)
                self.assertGreater(stages["tests"]["tests_collected"], 0)
                self.assertEqual(stages["smoke"]["status"], "pass")
                self.assertEqual(res["ac_ids"][0], "AC-%s-1" % item)
                self.assertEqual(res["config_source"]["kind"], "base-sha")

    def test_the_demo_route_comes_from_the_items_file(self):
        r, base = self.project("F2")
        res = verify.verify(r.path, base=base, item="F2", items_file=ITEMS_FILE, write_log=False)
        self.assertEqual(res["routes"], ["/api/bookmarks"])

    def test_a_wrong_route_fails_the_smoke_stage(self):
        r, base = self.project("F3")
        res = verify.verify(r.path, base=base, routes=["/nope"], write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "smoke"))

    def test_a_missing_item_of_the_plan_fails_its_tests(self):
        r, base = self.project("F1")
        res = verify.verify(r.path, base=base, item="F2", items_file=ITEMS_FILE, write_log=False)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "tests"))
        self.assertEqual([s["name"] for s in res["stages"] if s["status"] == "fail"], ["item-tests"])


if __name__ == "__main__":
    unittest.main()
