"""G1: acceptance-criterion ids trace from plan to tests to executed evidence."""
from __future__ import annotations

import json
import os
import sys

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, GateCase, g3_item

from loopctl import plan as plan_mod

MIG = {"name": "0001_widgets", "up": "db/0001.up.sql", "down": "db/0001.down.sql", "dataLossClass": "none", "stage": "expand"}
LOSSY = {"name": "0002_drop", "up": "db/0002.up.sql", "dataLossClass": "lossy", "noDownReason": "the column is gone", "stage": "contract"}
MIGRATE_SCRIPT = '''import os, sys
mode = open("mode.txt").read().strip() if os.path.exists("mode.txt") else "ok"
open("migrate.log", "a").write(os.environ["LOOPCTL_MIGRATION"] + "|" + os.environ.get("LOOPCTL_MIGRATION_DOWN", "") + "\\n")
if mode == "fail":
    print("down failed"); sys.exit(1)
if mode != "nodialect":
    print("dialect: " + ("postgres" if mode == "postgres" else "sqlite"))
'''
PY = '"%s"' % sys.executable


def state_for(head, g3="pass", g6=None, ac=None):
    st = {"status": "verifying", "attempt": 1, "gates": {}, "ac": {}}
    if g3:
        st["gates"]["g3"] = {"status": g3, "head": head}
    if g6:
        st["gates"]["g6"] = {"status": g6, "head": head}
    for a, cells in (ac or {}).items():
        st["ac"][a] = cells
    return st


class G1(GateCase):
    ITEMS = [g3_item("A"), item_doc("E", acs=1, ext={"g3": {"exempt": "docs only"}})]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})
    PASS = {"AC-A-1": {"g3": {"result": "pass"}}, "AC-A-2": {"g3": {"result": "pass"}}}

    def test_traced_and_executed_passes(self):
        res = self.run_gate("g1", state=state_for(self.head(), ac=self.PASS))
        self.assertPass(res)
        self.assertEqual(res["evidence"]["summary"], "AC coverage 2/2")
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-1"]["result"], "pass")
        self.assertIn("tests/acceptance/test_a.py", res["evidence"]["ac_results"]["AC-A-1"]["evidence"])

    def test_a_criterion_named_by_no_test_file(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts")}, "rename")
        res = self.run_gate("g1", state=state_for(self.head(), ac=self.PASS))
        self.assertFails(res, "g1.ac-untested")
        self.assertEqual(res["evidence"]["ac_results"]["AC-A-2"]["result"], "fail")

    def test_the_id_in_a_non_test_file_does_not_count(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts"), "docs/notes.md": "AC-A-2 is covered\n"}, "docs only")
        self.assertFails(self.run_gate("g1", state=state_for(self.head(), ac=self.PASS)), "g1.ac-untested")

    def test_ac_a_1_is_not_satisfied_by_ac_a_10(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_ac_a_10_subtracts")}, "ac-a-10")
        self.assertFails(self.run_gate("g1", state=state_for(self.head(), ac=self.PASS)), "g1.ac-untested")

    def test_evidence_for_an_older_head_is_stale(self):
        old = self.head()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "later")
        res = self.run_gate("g1", state=state_for(old, ac=self.PASS))
        self.assertFails(res, "g1.ac-no-evidence")

    def test_no_recorded_results_at_all(self):
        self.assertFails(self.run_gate("g1", state={"status": "verifying"}), "g1.ac-no-evidence")

    def test_a_failing_criterion_is_named(self):
        ac = {"AC-A-1": {"g3": {"result": "pass"}}, "AC-A-2": {"g3": {"result": "fail"}}}
        self.assertFails(self.run_gate("g1", state=state_for(self.head(), ac=ac)), "g1.ac-failing")

    def test_g6_alone_can_be_the_evidence(self):
        ac = {"AC-A-1": {"g6": {"result": "pass"}}, "AC-A-2": {"g6": {"result": "pass"}}}
        self.assertPass(self.run_gate("g1", state=state_for(self.head(), g3=None, g6="pass", ac=ac)))

    def test_changed_criteria_send_the_item_to_a_human(self):
        st = state_for(self.head(), ac=self.PASS)
        st["approvedAcDigest"] = "0" * 64
        res = self.run_gate("g1", state=st)
        self.assertFails(res, "g1.ac-changed")
        self.assertIn("needs_human", json.dumps(res["findings"]))

    def test_the_approved_digest_passes(self):
        from loopctl.context import Context
        ctx = Context.build(self.lr.path, self.lr.env)
        st = state_for(self.head(), ac=self.PASS)
        st["approvedAcDigest"] = plan_mod.ac_digest(ctx.plan.items["A"])
        self.assertPass(self.run_gate("g1", state=st))

    def test_an_exempt_item_is_flagged_not_failed(self):
        res = self.run_gate("g1", "E", state={"status": "verifying"})
        self.assertPass(res)
        self.assertIn("g1.exempt", self.ids(res))
        self.assertEqual(res["evidence"]["ac_results"], {})


if __name__ == "__main__":
    import unittest
    unittest.main()
