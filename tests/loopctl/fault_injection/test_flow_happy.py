"""The driver's happy path, end to end, with the fake `claude` and stub gates (report 10.4, 11.1, 11.6, 15 item 6).

One run asserts a great deal on purpose: each driver run is a few hundred git calls, and the suite has a time budget (IMP-24).
"""
from __future__ import annotations

import json
import os
import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import gates
from loopctl.gates import GateResult

from . import DriverTestCase, item_doc, render_config

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


def two_items():
    return [item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)]


class Happy(DriverTestCase):
    def test_two_items_run_in_order_and_merge_with_the_trailer(self):
        lr = self.dr(items=two_items())
        lr.stubs.attrs["evidence"] = {"REQUIRED_FOR": (("reviewing", "merged"),)}         # a gate the transition needs but the driver does not run by phase
        lr.stubs.remove()
        lr.stubs.install()
        edit = {".claude/loop.config.md": render_config({"run_budget_usd": "99999"}), "src/a.py": "x = 1\n"}                  # an item that edits its own budget (CO-11)
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A", files=edit), lr.eng_run("B")]}))
        lr.approve()
        tip0 = lr.repo.git("rev-parse", "main")
        r = lr.run("--json")
        self.assertEqual(r.rc, 0, r)
        doc = json.loads(r.out)
        self.assertEqual((doc["schema"], doc["outcome"], doc["exit"], doc["merged_this_run"], doc["children"]), ("loopctl.driver.v1", "complete", 0, ["A", "B"], 4))
        self.assertAlmostEqual(doc["spent_usd"], 0.16)
        # state: both merged, run idle, breaker clear, the pre-commit counters untouched
        st = lr.state()
        self.assertEqual({i: x["status"] for i, x in st.items.items()}, {"A": "merged", "B": "merged"})
        self.assertEqual((st.header["runStatus"], st.header["breaker"], st.header["parked"]), ("idle", 0, None))
        self.assertEqual(sum(st.items["A"]["counters"].values()), 0)
        # git: two real --no-ff merges on main with the exact trailer, feature branches gone, tree clean, back on main
        log = lr.repo.git("log", "--merges", "--reverse", "--format=%H%x1f%P%x1f%B%x1e", "main")
        recs = [m.strip().split("\x1f") for m in log.split("\x1e") if m.strip()]
        self.assertEqual(len(recs), 2)
        self.assertIn("Loop-Item: A", recs[0][2])
        self.assertIn("Loop-Item: B", recs[1][2])
        self.assertEqual([st.items["A"]["mergeCommit"], st.items["B"]["mergeCommit"]], [recs[0][0], recs[1][0]])
        self.assertEqual(len(recs[0][1].split()), 2)
        self.assertEqual(lr.branch_names(), ["main"])
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertNotEqual(lr.repo.git("rev-parse", "main"), tip0)
        # custody: the work, the briefs, the reports and the reviews are in the history
        files = lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines()
        for f in ("src/a.py", "src/b.py", "docs/features/slice-a/briefs/backend.md", "docs/features/slice-a/reports/backend.md",
                  "docs/features/slice-b/reviews/code-reviewer-r1.md"):
            self.assertIn(f, files)
        brief = lr.repo.git("show", "main:docs/features/slice-a/briefs/backend.md")
        for needle in ("# Brief: backend-engineer", "**Report path**: `docs/features/slice-a/reports/backend.md`", "AC-A-1", "AC-A-2", "`python3 -m unittest`",
                       "## Clarifications", "Out of Scope", "`.claude/context/backend.md`"):
            self.assertIn(needle, brief)
        # nothing of the run is left behind
        self.assertEqual(lr.children_left(), [])
        self.assertEqual(os.listdir(lr.rs.sessions_dir), [])
        self.assertFalse(lr.rs.lock_held())
        # the launch line: fixed, one child at a time, the REMAINING budget each time, the prompt on stdin
        calls = lr.calls()
        self.assertEqual([c["role"] for c in calls], ["backend-engineer", "code-reviewer", "backend-engineer", "code-reviewer"])
        budgets = []
        for c in calls:
            a = c["argv"]
            self.assertEqual(a[0], "-p")
            self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(a[a.index("--output-format") + 1], "json")
            self.assertEqual(a[a.index("--agent") + 1], c["role"])
            self.assertEqual(set(json.loads(a[a.index("--agents") + 1])), {c["role"]})
            self.assertEqual(len(a[a.index("--session-id") + 1]), 36)
            self.assertNotIn("--dangerously-skip-permissions", a)
            budgets.append(float(a[a.index("--max-budget-usd") + 1]))
            self.assertEqual(c["cwd"], os.path.realpath(lr.path))
        for got, want in zip(budgets, [10.0, 9.95, 9.92, 9.87]):
            self.assertAlmostEqual(got, want, places=3)                                      # ... but it never raised the budget of the run that merged it
        self.assertIn("docs/features/slice-a/briefs/backend.md", calls[0]["stdin"])
        self.assertIn("run_budget_usd: 99999", lr.repo.git("show", "main:.claude/loop.config.md"))       # the edit is on main now (a human reviews it: G5, H6) ...
        self.assertIn("`git diff", calls[1]["stdin"])
        self.assertIn("CODE_REVIEW_APPROVED or CHANGES_REQUESTED", calls[1]["stdin"])
        self.assertNotIn("REWORK", calls[0]["stdin"])
        # gates: phase order per item, on the item's own diff, with the run's one config
        names = lr.stubs.names()
        per_item = ["g0", "g5", "g1", "g3", "g6", "g9", "g11", "g7", "evidence"]
        self.assertEqual(names, per_item + per_item)
        by_item = {}
        for c in lr.stubs.calls:
            by_item.setdefault(c["item"], []).append(c)
        self.assertEqual(by_item["A"][0]["base"], lr.base)                                  # the item's branch point...
        self.assertEqual(by_item["B"][0]["base"], st.items["A"]["mergeCommit"])              # ...moves forward as items merge
        self.assertTrue(all(c["cfg"] is lr.stubs.calls[0]["cfg"] for c in lr.stubs.calls))   # one config object: the run's anchor, never the branch's
        self.assertEqual(len({c["head"] for c in by_item["A"][1:7]}), 1)                      # the verify gates all judged one head
        self.assertEqual(st.items["A"]["review"]["code"], "approved")
        self.assertEqual(set(st.items["A"]["gates"]), {"g0", "g1", "g3", "g5", "g6", "g7", "g9", "g11", "evidence", "reviews"})
        # the event log
        evs = {e["event"] for e in lr.rs.read_events("loopctl") if e.get("source") == "driver"}
        for e in ("start", "begin", "gate", "advance", "launch", "child_done", "engineer_report", "merged"):
            self.assertIn(e, evs)
        # a finished plan is a no-op
        r = lr.run("--json")
        self.assertEqual((r.rc, json.loads(r.out)["outcome"], json.loads(r.out)["children"]), (0, "complete", 0), r)
        self.assertEqual(len(lr.calls()), 4)

    def test_a_gate_that_is_off_is_not_run_or_recorded_and_one_that_is_not_installed_is_skipped(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"gates": {"g6": "off", "g9": "report"}})
        lr.stubs.results["g9"] = GateResult.failed([{"id": "g9.new-dep", "severity": "warn", "message": "left-pad added"}])
        lr.set_scenario(lr.happy())
        lr.approve()
        real = gates.installed
        gates.installed = lambda: [n for n in real() if n != "g11"]                          # g11 "is not installed" (whatever Phase 4b ships)
        self.addCleanup(setattr, gates, "installed", real)
        self.assertEqual(lr.run().rc, 0)
        names = lr.stubs.names()
        self.assertNotIn("g6", names)
        self.assertNotIn("g11", names)
        self.assertIn("g9", names)
        self.assertNotIn("g6", lr.item("A")["gates"])
        g9 = lr.item("A")["gates"]["g9"]
        self.assertEqual((g9["mode"], g9["status"], g9["blocking"]), ("report", "fail", False))   # a report-mode gate that fails is recorded and does not block

    def test_an_env_failure_from_g0_parks_the_item_caps_the_level_and_launches_nothing(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.stubs.results["g0"] = lambda ctx: GateResult.errored("toolchain pin does not match", "env")
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("blocked", "env:doctor_failed"))
        self.assertEqual(lr.state().header["levelCap"]["level"], "L1")
        self.assertEqual(lr.calls(), [])                                                       # nothing was launched into a broken environment
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        r = lr.run()                                                                           # and the cap holds on the next start
        self.assertEqual(r.rc, 3, r)
        self.assertIn("level-cap", r.err)

    def test_a_missing_environment_variable_blocks_before_anything_starts(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG, requiresEnv=["STRIPE_TEST_KEY"])])
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("blocked", "needs_provisioning"))
        self.assertEqual(lr.calls(), [])
        r = lr.run(env={"STRIPE_TEST_KEY": "x"})                                               # the human blocked it; setting the variable alone does not unblock
        self.assertEqual(lr.item("A")["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
