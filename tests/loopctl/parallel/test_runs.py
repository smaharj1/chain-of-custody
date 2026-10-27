"""Parallel runs end to end: `loopctl run` with `parallel_mode: worktree`, the real driver and scheduler, the fake `claude`, stub gates (report 11.5 item 3,
10.3, 16.1 row 8; D-6, D-29, DEC-34). Each run asserts a great deal on purpose: a run is a few hundred git calls and the suite has a time budget."""
from __future__ import annotations

import json
import os
import socket
import unittest

from loopctl import branches, state as sm
from loopctl.parallel import scheduler
from kitlib import jsonio

from . import ParTestCase, pitem, put, read


class Happy(ParTestCase):
    def test_two_independent_items_are_built_side_by_side_and_merged_one_at_a_time(self):
        lr = self.pr(items=[pitem("A"), pitem("B")], cfg={"verify_ports": "[24500]", "run_budget_usd": "10"})
        lr.set_scenario(lr.play(delay=0.4))
        lr.approve()
        tip0 = lr.repo.git("rev-parse", "main")
        r = lr.run("--json")
        self.assertEqual(r.rc, 0, r)
        doc = json.loads(r.out)
        self.assertEqual((doc["outcome"], doc["exit"], sorted(doc["merged_this_run"]), doc["children"]), ("complete", 0, ["A", "B"], 4))
        # side by side: both were admitted before either merged, and their children ran at the same moment
        evs = [e["event"] + ":" + str(e.get("item")) for e in lr.events() if e.get("source") == "driver" and e["event"] in ("admit", "merged")]
        self.assertEqual(sorted(evs[:2]), ["admit:A", "admit:B"])
        self.assertTrue(lr.overlapped("A", "B"))
        # their own ports, their own worktrees; the allowance is the budget divided by the workers (and the two add up to no more than the budget)
        eng = [c for c in lr.child_log() if c["role"] == "backend-engineer"]
        self.assertEqual(sorted(c["env"]["KIT_PORT_OFFSET"] for c in eng), ["0", "100"])
        self.assertEqual({c["env"]["KIT_PORTS"] for c in eng}, {"24500", "24600"})
        self.assertEqual({os.path.basename(c["cwd"]) for c in eng}, {"slice-a-a1", "slice-b-a1"})
        budgets = [float(c["argv"][c["argv"].index("--max-budget-usd") + 1]) for c in eng]
        self.assertEqual(budgets, [5.0, 5.0])
        # merged serially, with the trailer; the one that finished second was rebased onto the first and verified again
        self.assertEqual(sorted(lr.order_merged()), ["A", "B"])
        self.assertEqual(len(lr.repo.git("log", "--merges", "--format=%H", "main").splitlines()), 2)
        self.assertEqual(len([e for e in lr.events() if e.get("event") == "rebased"]), 1)
        g6 = [c for c in lr.stubs.calls if c["gate"] == "g6"]
        self.assertEqual(len(g6), 3)                                                         # once per item, and again for the rebased one
        self.assertEqual(len({(c["item"], c["head"]) for c in g6}), 3)
        files = lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines()
        for f in ("src/a/m.py", "src/b/m.py", "docs/features/slice-a/reports/backend.md", "docs/features/slice-b/reviews/code-reviewer-r1.md"):
            self.assertIn(f, files)
        st = lr.state()
        self.assertEqual({i: x["status"] for i, x in st.items.items()}, {"A": "merged", "B": "merged"})
        self.assertEqual(st.header["runStatus"], "idle")
        self.assertAlmostEqual(doc["spent_usd"], 0.16)
        # nothing is left behind
        self.assertEqual((lr.worktrees_left(), lr.leases_left(), lr.children_left()), ([], [], []))
        self.assertEqual(lr.branch_names(), ["main"])
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        self.assertNotEqual(lr.repo.git("rev-parse", "main"), tip0)
        self.assertFalse(lr.rs.lock_held())


class Faults(ParTestCase):
    def test_an_item_that_lies_about_its_footprint_is_parked_the_honest_one_is_merged_and_the_run_says_it_stopped(self):
        lr = self.pr(items=[pitem("A"), pitem("B")])
        play = lr.play(files={"A": {"src/a/m.py": "x = 1\n", "README.md": "# not mine\n"}})
        lr.set_scenario(play)
        lr.approve()
        r = lr.run("--json")
        doc = json.loads(r.out)
        self.assertEqual((r.rc, doc["outcome"]), (1, "parked"), r)
        self.assertEqual(doc["merged_this_run"], ["B"])
        st = lr.state()
        self.assertEqual((st.items["A"]["status"], st.items["A"]["blockReason"]), ("needs_human", "footprint_violation"))
        self.assertEqual(st.items["B"]["status"], "merged")
        self.assertEqual(lr.repo.git("show", "main:README.md"), "# temp repo")               # the lie never reached the integration branch
        self.assertIn("blocked/slice-a-a1", lr.branch_names())                                # but the work is kept
        self.assertEqual(lr.repo.git("show", "blocked/slice-a-a1:README.md"), "# not mine")
        self.assertEqual((lr.worktrees_left(), lr.leases_left()), ([], []))

    def test_a_crash_in_a_worker_removes_every_worktree_and_the_next_run_recovers_what_a_killed_driver_left(self):
        lr = self.pr(items=[pitem("S1", schema=True), pitem("S2", schema=True)])
        lr.set_scenario(lr.play(delay=0.1))
        lr.approve()
        real = scheduler.Worker.stage_building

        def boom(self, iid):
            raise RuntimeError("a bug in a stage")
        scheduler.Worker.stage_building = boom
        try:
            with self.assertRaises(RuntimeError):
                lr.run("--json")
        finally:
            scheduler.Worker.stage_building = real
        self.assertEqual((lr.worktrees_left(), lr.leases_left(), lr.children_left()), ([], [], []))
        self.assertFalse(lr.rs.lock_held())
        self.assertEqual(lr.state().items["S1"]["status"], "building")                         # in flight; the branch holds whatever there was
        self.assertEqual(lr.state().items["S2"]["status"], "pending")                          # the schema lane never let it start
        # a driver that was KILLED leaves more: a lease with a dead owner, a worktree with unsaved work on an item branch, a port slot
        from loopctl.parallel import mutex, worktrees
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        mutex.LeaseStore(lr.rs, pid=p.pid).acquire("ports-3", "worker:S9")
        mutex.LeaseStore(lr.rs, pid=p.pid).acquire("item-s9", "worker:S9")
        lr.repo.git("branch", "feat/ghost-a1", "main")
        ghost = worktrees.create(lr.path, "ghost-a1", "feat/ghost-a1")
        put(os.path.join(ghost, "unsaved.txt"), "a dead worker's file\n")
        r = lr.run("--json")
        doc = json.loads(r.out)
        self.assertEqual((r.rc, doc["outcome"], doc["merged_this_run"]), (0, "complete", ["S1", "S2"]), r)      # the schema lane: one after the other
        self.assertFalse(lr.overlapped("S1", "S2"))
        self.assertEqual((lr.worktrees_left(), lr.leases_left(), lr.children_left()), ([], [], []))
        self.assertIn("unsaved.txt", lr.repo.git("show", "--stat", "--format=", "dead/ghost-a1"))             # its work was saved, then the branch set aside
        self.assertIn("dead/slice-s1-a1", lr.branch_names())                                                    # S1's first, interrupted attempt
        self.assertEqual(lr.state().items["S1"]["attempt"], 1)
        names = [e.get("event") for e in lr.events()]
        self.assertIn("lease_reaped", names)
        self.assertIn("worktree_removed", names)


class Refusals(ParTestCase):
    def test_it_will_not_start_without_the_evidence_gate_with_the_wrong_runner_or_with_the_workflow_host(self):
        lr = self.pr(items=[pitem("A")])
        lr.set_scenario(lr.play())
        lr.approve()
        os.unlink(os.path.join(lr.path, "docs", "baseline", "evidence.json"))
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertIn("evidence", r.err + r.out)
        self.assertEqual(lr.worktrees_left(), [])
        self.assertEqual(lr.state().items["A"]["status"], "pending")
        # dry run shows the rows
        d = lr.run("--dry-run", "--json")
        rows = {p["id"]: p for p in json.loads(d.out)["preflight"]}
        self.assertFalse(rows["parallel.evidence"]["ok"])
        self.assertTrue(rows["parallel.runner"]["ok"])

    def test_pipeline_host_workflow_and_parallel_with_a_serial_runner_are_refused_by_the_start(self):
        lr = self.pr(items=[pitem("A")], cfg={"pipeline_host": "workflow", "runner": "sequential"})
        lr.approve()
        d = lr.run("--dry-run", "--json")
        rows = {p["id"]: p for p in json.loads(d.out)["preflight"]}
        self.assertFalse(rows["parallel.pipeline-host"]["ok"])
        self.assertFalse(rows["parallel.runner"]["ok"])
        self.assertIn("design-only", rows["parallel.pipeline-host"]["message"])


class Unattended(ParTestCase):
    def test_the_progress_file_names_every_item_in_progress_and_keeps_the_old_key(self):
        from loopctl import unattended
        from .test_scheduler_units import fake_slot
        lr = self.pr(items=[pitem("A"), pitem("B")])
        lr.approve()
        loop = lr.fast_start()
        import io
        from loopctl.parallel import scheduler as sch
        drv = sch.ParallelDriver(loop.ctx, loop, lr.rs.read_run(), False, out=io.StringIO())
        sup = unattended.Supervisor(drv)
        drv.slots.update({"B": fake_slot("B"), "A": fake_slot("A")})
        sup.item = "B"
        sup.write_progress("running")
        prog = unattended.read_progress(lr.rs)
        self.assertEqual((prog["items"], prog["item"], prog["pid"]), (["A", "B"], "B", os.getpid()))


if __name__ == "__main__":
    unittest.main()
