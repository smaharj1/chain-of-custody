"""Engineer statuses: every status a report can carry, and every way a report can be malformed (engineer-protocol 6 and 7, report 10.4, L-8;
FI-03 malformed report, FI-17 statuses).
"""
from __future__ import annotations

import json
import os
import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import driver, launch

from . import DriverTestCase, item_doc, report_text

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class EngineerStatuses(DriverTestCase):
    def test_partial_then_clarification_then_a_bad_trailer_then_a_deviation_all_lead_to_a_merge(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [
            lr.eng_run("A", status="PARTIAL"), lr.eng_run("A", status="NEEDS_CLARIFICATION", iteration=2), lr.eng_run("A", result="Done.", iteration=3),
            lr.eng_run("A", status="CONTRACT_DEVIATION", iteration=4)]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        c = it["counters"]
        self.assertEqual((it["status"], c["partial"], c["clarify"], c["verify"]), ("merged", 1, 1, 1))     # each in its own counter (L-8): the bad trailer used `verify`
        eng = lr.calls_for("backend-engineer")
        self.assertEqual(len(eng), 4)
        self.assertIn("The previous round failed: item/partial", eng[1]["stdin"])
        self.assertIn("Answer to your clarification request: Round 1", eng[2]["stdin"])
        self.assertNotIn("Answer to your clarification", eng[0]["stdin"])
        brief = lr.repo.git("show", "main:docs/features/slice-a/briefs/backend.md")
        self.assertIn("Round 1:", brief)                                                       # the answer is in the brief (custody), as the protocol says
        self.assertEqual(lr.item("A")["lastFailure"]["reason"], "malformed_report")

    def case(self, lr, drv, status, iteration=1):
        """One engineer report, judged by the driver as if a child had just written it (no process: the pipeline test has those)."""
        rel = "docs/features/slice-a/reports/backend.md"
        lr.repo.write(rel, report_text(status, iteration))
        res = launch.ChildResult()
        res.parsed = {"result": "x\n" + json.dumps({"status": status, "report_path": rel, "iteration": iteration})}
        return drv.engineer_report("A", "backend-engineer", driver.Outcome(res))

    def test_a_third_clarification_blocks_the_item_for_a_human(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building")
        drv.write_briefs("A")
        drv.wip_commit("WIP(loopctl): A briefs")
        for n in (1, 2):
            self.assertFalse(self.case(lr, drv, "NEEDS_CLARIFICATION", n))
            self.assertEqual(lr.item("A")["status"], "building")                              # answered from the plan: the stage runs the engineer again
            self.assertIn("Round %d" % n, drv.clarification[("A", 1)])
        self.assertFalse(self.case(lr, drv, "NEEDS_CLARIFICATION", 3))
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["counters"]["clarify"]), ("blocked", "NEEDS_CLARIFICATION", 3))
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])
        brief = lr.repo.git("show", "blocked/slice-a-a1:docs/features/slice-a/briefs/backend.md")
        self.assertIn("Round 1:", brief)                                                       # the answers are in the brief (custody)
        self.assertIn("Round 2:", brief)
        self.assertEqual(lr.item("B")["status"], "pending")

    def test_escalation_and_contract_inconsistency_park_for_a_human(self):
        for status, code in (("ESCALATION_NEEDED", "ESCALATION_NEEDED"), ("CONTRACT_INCONSISTENCY", "contract_blocked")):
            with self.subTest(status):
                lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
                drv = lr.driver_at("A", "design_review", "building")
                self.assertFalse(self.case(lr, drv, status))
                it = lr.item("A")
                self.assertEqual((it["status"], it["blockReason"]), ("needs_human", code))
                self.assertEqual(lr.item("B")["status"], "pending")
                self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])
                self.assertEqual(lr.state().header["runStatus"], "parked")

    def test_partial_and_the_deviations(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        drv = lr.driver_at("A", "design_review", "building")
        self.assertFalse(self.case(lr, drv, "PARTIAL"))
        it = lr.item("A")
        self.assertEqual((it["status"], it["counters"]["partial"], it["counters"]["verify"]), ("building", 1, 0))
        for status in ("APPROVED", "CONTRACT_DEVIATION", "DESIGN_DEVIATION"):
            self.assertTrue(self.case(lr, drv, status, 2), status)

    def test_two_engineers_run_in_dependency_order_and_each_commits_its_own_work(self):
        item = item_doc("A", estimate={"loc": 100, "engineers": ["backend-engineer", "database-engineer"]})
        lr = self.dr(items=[item])
        lr.set_scenario(lr.happy(by_role={"database-engineer": [lr.eng_run("A", domain="database", files={"db/a.sql": "create table a(id int);\n"})],
                                          "backend-engineer": [lr.eng_run("A", files={"src/a.py": "x = 1\n"})]}))
        lr.approve()
        self.assertEqual(lr.run().rc, 0)
        self.assertEqual([c["role"] for c in lr.calls()], ["database-engineer", "backend-engineer", "code-reviewer"])
        subjects = lr.repo.git("log", "--format=%s", "main").splitlines()
        self.assertIn("WIP(loopctl): A database", subjects)
        self.assertIn("WIP(loopctl): A backend", subjects)
        self.assertLess(subjects.index("WIP(loopctl): A backend"), subjects.index("WIP(loopctl): A database"))      # log lists newest first
        brief = lr.repo.git("show", "main:docs/features/slice-a/briefs/backend.md")
        self.assertIn("docs/features/slice-a/reports/database.md", brief)                         # the backend brief depends on the database report

    def test_the_engineer_report_checks_that_decide_a_status(self):
        """Every way a report can be wrong is `malformed_report`: an item failure, never an approval (engineer-protocol 7)."""
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"max_retries_per_item": "50"})
        drv = lr.driver_at("A", "design_review", "building")
        rel = "docs/features/slice-a/reports/backend.md"
        good = {"status": "APPROVED", "report_path": rel, "iteration": 1}

        def attempt(report, result):
            if report is not None:
                lr.repo.write(rel, report)
            elif os.path.exists(os.path.join(lr.path, rel)):
                os.unlink(os.path.join(lr.path, rel))
            res = launch.ChildResult()
            res.parsed = {"result": result}
            return drv.engineer_report("A", "backend-engineer", driver.Outcome(res))
        ok = report_text("APPROVED")
        cases = {
            "valid": (ok, "Done.\n" + json.dumps(good), True),
            "no trailer": (ok, "Done.", False),
            "trailer says another status": (ok, "x\n" + json.dumps(dict(good, status="PARTIAL")), False),
            "trailer path elsewhere": (ok, "x\n" + json.dumps(dict(good, report_path="docs/features/slice-a/reports/../../../x.md")), False),
            "trailer path of another item": (ok, "x\n" + json.dumps(dict(good, report_path="docs/features/other/reports/backend.md")), False),
            "trailer in a code fence": (ok, "x\n```json\n" + json.dumps(good) + "\n```", False),
            "report file missing": (None, "x\n" + json.dumps(good), False),
            "report without its Status section": (ok.replace("## Status\nAPPROVED\n", ""), "x\n" + json.dumps(good), False),
            "unknown status": (report_text("DONE_AND_DUSTED"), "x\n" + json.dumps(dict(good, status="DONE_AND_DUSTED")), False),
            "NONE without checked": (ok.replace("NONE\nchecked: layering", "NONE"), "x\n" + json.dumps(good), False),
        }
        for name, (report, result, expect) in cases.items():
            with self.subTest(name):
                rec0 = lr.item("A")["counters"]["verify"]
                got = attempt(report, result)
                self.assertEqual(got, expect, name)
                if not expect:
                    self.assertEqual(lr.item("A")["counters"]["verify"], rec0 + 1)
                    self.assertEqual(lr.item("A")["lastFailure"]["reason"], "malformed_report")


if __name__ == "__main__":
    unittest.main()
