"""status, --pending-human, the AC x evidence matrix, the bypass log and render (report sections 10.2, 9 G8, CO-44, RC-9)."""
from __future__ import annotations

import json
import unittest

from loopctl import gates, render, status

from . import LoopTestCase, item_doc


class Status(LoopTestCase):
    def test_status_before_and_after_the_plan_is_approved(self):
        lr = self.lr()
        j = lr.ctl("status", "--json").json
        self.assertEqual(j["schema"], "loopctl.status.v1")
        self.assertFalse(j["state"]["present"])
        self.assertEqual([i["status"] for i in j["items"]], ["pending"] * 3)
        self.assertEqual(j["pending_human"][0]["kind"], "approve-plan")
        self.assertEqual(lr.repo.git("show-ref", "--verify", "-q", "refs/loopctl/state/demo", check=False), "")      # status never creates the ref
        lr.fast_start()
        j = lr.ctl("status", "--json").json
        self.assertTrue(j["state"]["present"])
        self.assertEqual(j["eligible"], ["A"])
        self.assertEqual(j["run"]["lock"]["held"], True)
        self.assertEqual(j["pending_human"], [])

    def test_status_text_names_the_important_things(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.fail("A", "env", "db_down", lr.session)
        out = lr.ctl("status").out
        for needle in ("A        blocked", "env:db_down", "level capped to L1", "evidence gate: not passed", "AC coverage: 0 of 6", "Waiting on a human"):
            self.assertIn(needle, out)

    def test_status_writes_nothing(self):
        lr = self.lr()
        lr.fast_start()
        before = (lr.repo.git("rev-parse", "refs/loopctl/state/demo"), lr.repo.git("status", "--porcelain"))
        for args in (["status"], ["status", "--json"], ["status", "--pending-human"], ["status", "--matrix"], ["status", "--bypass"], ["render"], ["validate"]):
            lr.ctl(*args)
        self.assertEqual((lr.repo.git("rev-parse", "refs/loopctl/state/demo"), lr.repo.git("status", "--porcelain")), before)

    def test_pending_human_lists_every_kind_with_a_command(self):
        lr = self.lr(items=[item_doc("A", waivers=[{"approver": "Pat", "expiry": "2020-01-01", "finding_ids": ["CWE-1"], "reason": "r"}]),
                            item_doc("B"), item_doc("C")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        lr.context().rs.set_halt("kill switch", "Pat")
        pend = status.pending_human(lr.context(), lr.state(), lr.context().plan)
        kinds = [p["kind"] for p in pend]
        for k in ("halt", "approve-item", "waiver"):
            self.assertIn(k, kinds)
        for p in pend:
            self.assertTrue(p["command"])
            self.assertTrue(p["why"])
        out = lr.ctl("status", "--pending-human").out
        self.assertIn("loopctl approve item A --by <you>", out)
        self.assertIn("loopctl resume", out)

    def test_pending_human_covers_blocked_env_breaker_cost_and_revert_plans(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B"), item_doc("C")], extra_cfg="max_retries_per_item: 0\nmax_consecutive_failures: 1\nrun_budget_usd: 1")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        loop.fail("A", "item", "tests_failed", lr.session)
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        loop.fail("A", "item", "tests_failed", lr.session)
        lr.context().ledger().record("run-1", "A", 1, None, {"result": "no cost field"})
        pend = {p["kind"]: p for p in lr.ctl("status", "--pending-human", "--json").json["pending"]}
        self.assertEqual(sorted(pend), ["breaker", "cost", "unblock"])
        self.assertIn("approve unblock A", pend["unblock"]["command"])
        self.assertIn("approve cost", pend["cost"]["command"])

    def test_the_matrix_shows_gate_cells_and_coverage(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        res = {"status": "pass", "mode": "on", "blocking": False, "head": None, "failure_class": "none", "findings": [],
               "evidence": {"ac_results": {"AC-A-1": {"result": "pass", "evidence": "t1"}, "AC-A-2": {"result": "fail", "evidence": "t2"}}}}
        loop.record_gate("A", "g3", res, lr.session)
        loop.record_gate("A", "g6", {"status": "pass", "mode": "on", "blocking": False, "failure_class": "none", "findings": [],
                                     "evidence": {"ac_results": {"AC-A-1": {"result": "pass", "evidence": "t1"}}}}, lr.session)
        m = lr.ctl("status", "--matrix", "--json").json
        self.assertEqual(m["columns"], ["g3", "g6"])
        rows = {r["ac"]: r for r in m["rows"]}
        self.assertEqual(rows["AC-A-1"]["verdict"], "pass")
        self.assertEqual(rows["AC-A-2"]["verdict"], "fail")
        self.assertEqual(rows["AC-B-1"]["verdict"], "none")
        self.assertEqual(m["coverage"], {"covered": 1, "total": 6, "percent": 16.7})
        text = lr.ctl("status", "--matrix", "A").out
        self.assertIn("AC-A-1", text)
        self.assertNotIn("AC-B-1", text)
        self.assertIn("coverage: 1/2", text)

    def test_a_failing_gate_result_beats_a_passing_one_in_the_matrix(self):
        st = type("S", (), {"items": {"A": {"status": "building", "ac": {"AC-A-1": {"g3": {"result": "pass"}, "g7": {"result": "fail"}}}}}})()
        lp = type("L", (), {"ids": lambda self: ["A"], "items": {"A": item_doc("A", acs=1)}})()
        self.assertEqual(status.ac_matrix(lp, st)["rows"][0]["verdict"], "fail")

    def test_deadlock_and_blocked_downstream(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.park("A", "ESCALATION_NEEDED", "x", lr.session)
        j = lr.ctl("status", "--json").json
        self.assertTrue(j["deadlock"])
        self.assertEqual(j["blocked_downstream"], ["B", "C"])
        self.assertIn("stuck behind a blocked item", lr.ctl("status").out)

    def test_the_bypass_log_is_shown_and_ingested_by_reconcile(self):
        lr = self.lr()
        lr.fast_start()
        self.assertIn("no ruleset bypass", lr.ctl("status", "--bypass").out)
        lr.repo.write("suites.json", json.dumps([{"evaluation_result": "bypass", "actor_name": "pat-admin", "ref": "refs/heads/main",
                                                  "pushed_at": "2026-10-20T10:00:00Z", "after_sha": "a" * 40},
                                                 {"evaluation_result": "pass", "actor_name": "bot", "ref": "refs/heads/feat/x"}]))
        lr.repo.git("add", "suites.json")
        lr.repo.git("commit", "-q", "-m", "recorded rule suites")
        r = lr.ctl("reconcile", "--apply", "--rule-suites", "suites.json")
        self.assertEqual(r.rc, 0, r)
        out = lr.ctl("status", "--bypass").out
        self.assertIn("pat-admin", out)
        self.assertIn("refs/heads/main", out)
        self.assertNotIn("bot", out)
        lr.ctl("reconcile", "--apply", "--rule-suites", "suites.json")                  # idempotent
        self.assertEqual(len(lr.ctl("status", "--bypass", "--json").json), 1)

    def test_hostile_titles_cannot_inject_into_the_terminal_or_the_markdown_table(self):
        evil = "Evil | row \x1b[31m red \x1b]0;pwn\x07 <script>x</script> `tick` ‮RTL\nNEWLINE"
        lr = self.lr(items=[item_doc("A", title=evil)])
        lr.fast_start()
        for args in (["status"], ["render"], ["render", "--format", "mermaid"], ["validate"]):
            out = lr.ctl(*args).out
            self.assertNotIn("\x1b", out)
            self.assertNotIn("\x07", out)
            self.assertNotIn("‮", out)
            self.assertNotIn("<script>", out)
        md = lr.ctl("render").out
        board = [ln for ln in md.splitlines() if ln.startswith("| A ")][0]
        self.assertEqual(board.count("|") - board.count("\\|"), 7)                       # one row, six cells: the pipe in the title is escaped


class Render(LoopTestCase):
    def test_markdown_has_the_board_progress_critical_path_and_acs(self):
        lr = self.lr(milestones=["M1"])
        lr.fast_start()
        md = lr.ctl("render").out
        for needle in ("# Build Plan: demo", "## Status Board", "| A | M1 | Item A | - | S | pending |", "| B | M1 | Item B | A | S | pending |",
                       "Progress: 0/3 merged", "Critical path: A -> B -> C", "**AC-A-1**", "## M1"):
            self.assertIn(needle, md)
        lr.complete("A")
        self.assertIn("| A | M1 | Item A | - | S | merged |", lr.ctl("render").out)
        self.assertIn("Progress: 1/3 merged", lr.ctl("render").out)
        self.assertEqual(lr.ctl("render").out, lr.ctl("render").out)                      # deterministic

    def test_mermaid_and_json(self):
        lr = self.lr()
        lr.fast_start()
        m = lr.ctl("render", "--format", "mermaid").out
        self.assertTrue(m.startswith("flowchart LR"))
        self.assertIn("A ==> B", m)
        self.assertIn("B ==> C", m)
        j = lr.ctl("render", "--format", "json").json
        self.assertEqual([i["id"] for i in j["items"]], ["A", "B", "C"])
        self.assertEqual(j["critical_path"], ["A", "B", "C"])

    def test_render_without_state_and_with_unreadable_state(self):
        lr = self.lr()
        self.assertEqual(lr.ctl("render").rc, 0)
        lr.repo.git("update-ref", "refs/loopctl/state/demo", "HEAD")
        r = lr.ctl("render")
        self.assertEqual(r.rc, 0)
        self.assertIn("STATE UNREADABLE", r.out)
        self.assertEqual(lr.ctl("render", "--no-state").out.count("UNREADABLE"), 0)

    def test_render_never_writes_a_file(self):
        lr = self.lr()
        lr.ctl("render")
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
