"""Serial integration (report 11.5 item 3 "Serial integrate", D-6): footprint, rebase, re-verify, lease checks, merge, in plan order.

No child session runs: a finished worker is staged (`DriverCase.stage`), and the scheduler's own `integrate_one` merges it. Every item id is used by one test.
"""
from __future__ import annotations

import os

from loopctl import state as sm
from loopctl.gates import GateResult
from loopctl.parallel import integrate, mutex, worktrees

from . import DriverCase, pitem, put

ITEMS = [pitem(i) for i in "ABCDEFGHIJKLM"] + [
    pitem("CA", paths=["src/shared/**"], slug="shared-ca"), pitem("CB", paths=["src/shared/**"], slug="shared-cb"),
    pitem("MA", paths=["db/migrations/**"], slug="mig-a"), pitem("MB", paths=["db/migrations/**"], slug="mig-b"),
    pitem("MC", paths=["db/migrations/**"], slug="mig-c", migrations=[{"name": "m1", "up": "db/migrations/0003_y.sql", "down": "db/migrations/0003_y.down.sql", "dataLossClass": "none"}]),
    pitem("MD", paths=["db/migrations/**"], slug="mig-d", migrations=[{"name": "m2", "up": "db/migrations/0009_z.sql", "down": "db/migrations/0009_z.down.sql", "dataLossClass": "none"}])]


class Integrate(DriverCase):
    ITEMS = ITEMS

    def merges(self):
        return self.lr.order_merged(self.start_tip)           # what this test merged: the repo is shared by the class

    def test_ready_items_merge_in_plan_order_whatever_order_they_finished_and_the_second_is_rebased_and_reverified(self):
        b = self.stage("B")                                     # B finished first ...
        a = self.stage("A")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.merges(), ["A"])                  # ... but the plan puts A first
        self.assertTrue(os.path.isdir(b.path) and b.iid in self.drv.slots)
        tip_a = self.lr.repo.git("rev-parse", "main")
        self.lr.stubs.calls = []
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.merges(), ["A", "B"])
        # B's branch had been cut before A merged: rebased onto A's merge, and verify ran again on the rebased code, in B's own worktree
        self.assertEqual(self.lr.repo.git("merge-base", "main^2", tip_a), tip_a)
        g6 = [c for c in self.lr.stubs.calls if c["gate"] == "g6"]
        self.assertEqual(len(g6), 1)
        self.assertEqual((g6[0]["item"], g6[0]["worktree"]), ("B", b.path))
        self.assertEqual(g6[0]["base"], tip_a)                  # the item's own diff, from the new branch point
        # the first merge needed no rebase and no new verify
        log = self.lr.repo.git("log", "--merges", "--format=%B", "main")
        self.assertIn("Loop-Item: A\nLoop-Attempt: 1", log)
        self.assertIn("src/a/m.py", self.main_files())
        self.assertIn("src/b/m.py", self.main_files())
        st = self.lr.state()
        self.assertEqual((st.items["A"]["status"], st.items["B"]["status"]), ("merged", "merged"))
        # nothing of either is left: worktrees, branches, leases, ports
        self.assertEqual(self.lr.worktrees_left(), [])
        self.assertEqual(self.lr.branch_names(), ["main"])
        self.assertEqual([r["name"] for r in self.drv.leases.list()], [])
        self.assertEqual(self.drv.slots, {})
        self.assertIs(self.drv.integrate_one(), False)

    def test_a_rebase_that_conflicts_is_aborted_and_parks_the_item_for_a_human(self):
        ca = self.stage("CA", {"src/shared/x.txt": "from CA\n"})
        cb = self.stage("CB", {"src/shared/x.txt": "from CB\n"})        # the plan never runs these two together; the integrator must still be safe
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.merges(), ["CA"])
        tip = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)             # nothing merged
        r = self.lr.item("CB")
        self.assertEqual((r["status"], r["blockReason"]), ("needs_human", "merge_conflict"))
        self.assertEqual(self.lr.repo.git("show", "main:src/shared/x.txt"), "from CA")
        self.assertIn("blocked/shared-cb-a1", self.lr.branch_names())             # CB's work is kept, on a blocked/* branch
        self.assertEqual(self.lr.repo.git("show", "blocked/shared-cb-a1:src/shared/x.txt"), "from CB")
        self.assertFalse(os.path.lexists(cb.path))
        self.assertTrue(any(e.get("event") == "rebase_conflict" for e in self.lr.events()))
        self.assertTrue(self.drv.draining)                                          # a park makes the scheduler drain
        self.assertEqual(self.lr.repo.git("status", "--porcelain"), "")
        self.assertIsNotNone(ca)

    def test_an_item_that_changed_files_outside_its_declared_footprint_is_never_merged(self):
        e = self.stage("E", {"src/e/m.py": "ok = 1\n", "README.md": "# rewritten\n", "docs/features/slice-e/reports/backend.md": "report\n"})
        tip = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)
        r = self.lr.item("E")
        self.assertEqual((r["status"], r["blockReason"]), ("needs_human", "footprint_violation"))
        ev = [x for x in self.lr.events() if x.get("event") == "footprint_violation"][-1]
        self.assertEqual(ev["paths"], ["README.md"])                                 # its own custody trail is not a lie
        self.assertNotIn("README.md", [l for l in self.main_files() if l != "README.md"])
        self.assertEqual(self.lr.repo.git("show", "main:README.md"), "# temp repo")
        self.assertFalse(os.path.lexists(e.path))

    def test_a_file_moved_out_of_the_footprint_and_a_deleted_one_are_seen_as_well(self):
        self.lr.repo.commit_file("elsewhere/old.txt", "x\n", "an existing file outside every footprint")
        slot = self.stage("F", {})
        self.lr.repo.git("-C", slot.path, "rebase", "--quiet", "main")
        os.makedirs(os.path.join(slot.path, "src", "f"))
        self.lr.repo.git("-C", slot.path, "mv", "elsewhere/old.txt", "src/f/new.txt")
        self.lr.repo.git("-C", slot.path, "commit", "-q", "-m", "move it into the footprint")
        self.drv.item_base["F"] = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.item("F")["blockReason"], "footprint_violation")        # the delete half of the move is outside it
        ev = [x for x in self.lr.events() if x.get("event") == "footprint_violation"][-1]
        self.assertEqual(ev["paths"], ["elsewhere/old.txt"])

    def test_a_lease_that_expires_mid_merge_stops_the_merge_and_nothing_is_changed(self):
        g = self.stage("G")
        tip = self.lr.repo.git("rev-parse", "main")
        rival = mutex.LeaseStore(self.drv.rs, ttl_s=60, clock=lambda: 10 ** 10, alive=lambda rec: False)     # another integrator that thinks ours is long dead
        taken = []
        integrate.HOOKS["before_merge"] = lambda drv, slot: taken.append(rival.acquire(mutex.INTEGRATE, "rival"))
        self.addCleanup(integrate.HOOKS.pop, "before_merge", None)
        self.assertIs(self.drv.integrate_one(), False)                                # "lost": no progress, nothing merged
        self.assertEqual(len(taken), 1)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)
        self.assertEqual(self.lr.item("G")["status"], "reviewing")
        self.assertTrue(any(e.get("event") == "lease_lost" for e in self.lr.events()))
        self.assertTrue(os.path.isdir(g.path))
        self.assertEqual(self.drv.slots["G"].lost, 1)
        # the rival is done; the next attempt takes a fresh lease and merges
        integrate.HOOKS.pop("before_merge")
        rival.release(taken[0])
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.merges()[-1:], ["G"])

    def test_losing_the_items_own_lease_counts_as_lost_and_the_fourth_loss_parks_it(self):
        h = self.stage("H")
        self.drv.leases.remove(mutex.item_lease("H"))                                  # a human ran `parallel cleanup`, or a rival reclaimed it
        tip = self.lr.repo.git("rev-parse", "main")
        for n in (1, 2, 3):
            self.assertIs(self.drv.integrate_one(), False)
            self.assertEqual(self.drv.slots["H"].lost, n)
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual((self.lr.item("H")["status"], self.lr.item("H")["blockReason"]), ("needs_human", "lease_lost"))
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)
        self.assertIsNotNone(h)

    def test_someone_else_holding_the_integration_slot_means_wait(self):
        self.stage("I")
        other = mutex.LeaseStore(self.drv.rs, ttl_s=60)
        held = other.acquire(mutex.INTEGRATE, "another integrator")
        self.assertIs(self.drv.integrate_one(), False)
        self.assertEqual(self.lr.item("I")["status"], "reviewing")
        other.release(held)
        self.assertIs(self.drv.integrate_one(), True)

    def test_a_failure_of_verify_on_the_rebased_code_sends_the_item_back_to_its_engineer(self):
        self.stage("K")
        self.stage("J")
        self.assertIs(self.drv.integrate_one(), True)                                   # J merges
        sent_back = []
        self.drv.respawn = lambda slot: sent_back.append(slot.iid)
        self.lr.stubs.results["g6"] = GateResult.failed([{"id": "g6.verify-failed", "severity": "error", "message": "the merged code does not build"}])
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(sent_back, ["K"])
        r = self.lr.item("K")
        self.assertEqual((r["status"], r["lastFailure"]["reason"], r["lastFailure"]["class"]), ("building", "tests_failed", "item"))
        self.assertNotIn("K", self.merges())
        self.assertTrue(os.path.isdir(self.drv.slots["K"].path))                         # same worktree, same branch
        self.assertFalse(self.drv.slots["K"].ready)

    def test_a_migration_that_sorts_before_one_already_merged_goes_back_to_the_engineer_to_be_regenerated(self):
        self.lr.repo.commit_file("db/migrations/0005_x.sql", "-- merged by hand\n", "a migration committed outside the loop")
        self.stage("MC", {"db/migrations/0003_y.sql": "-- mine\n", "db/migrations/0003_y.down.sql": "-- down\n"})
        self.stage("MD", {"db/migrations/0009_z.sql": "-- mine\n", "db/migrations/0009_z.down.sql": "-- down\n"})
        sent_back = []
        self.drv.respawn = lambda slot: sent_back.append(slot.iid)
        self.assertIs(self.drv.integrate_one(), True)            # plan order: MC first
        r = self.lr.item("MC")
        self.assertEqual((r["status"], r["lastFailure"]["reason"]), ("building", "migration_order"))
        self.assertIn("0003_y.sql sorts before", r["lastFailure"]["detail"])
        self.assertEqual(sent_back, ["MC"])
        self.drv.slots["MC"].ready = False
        self.assertIs(self.drv.integrate_one(), True)            # MD's migration sorts after: it merges
        self.assertEqual(self.merges()[-1], "MD")

    def test_an_item_that_is_not_in_review_is_not_integrated(self):
        self.stage("L", ready=True, upto="building")
        tip = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)
        self.assertTrue(any(e.get("event") == "integrate_skipped" for e in self.lr.events()))
        self.assertEqual(self.lr.item("L")["status"], "building")


if __name__ == "__main__":
    import unittest
    unittest.main()
