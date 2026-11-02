"""Parked work is never an orphan: `park` from any path shelves feat/* to blocked/*, and the sweep treats the branch of an in-flight or resting item
as live (review fixes D7, LC-2)."""
from __future__ import annotations

import unittest

from loopctl import branches, reconcile, state as sm

from . import LoopTestCase, item_doc


def names(lr):
    return {b["name"] for b in branches.list_branches(lr.path)}


class Parking(LoopTestCase):
    def branch_with_work(self, lr, iid="A"):
        br = lr.item(iid)["branch"]
        lr.repo.git("switch", "-q", "-c", br, "main")
        lr.repo.commit_file("src/%s.txt" % iid, "work\n", "work on " + iid)
        lr.repo.git("switch", "-q", "main")
        return br

    def test_park_through_the_cli_leaves_a_blocked_branch_and_no_feat_branch(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        br = self.branch_with_work(lr)
        # run.json names this repo as the loop worktree (fast_start wrote it), so loopctl may rename the branch
        r = lr.ctl("park", "A", "--code", "ESCALATION_NEEDED", "--session", lr.session)
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "needs_human")
        self.assertNotIn(br, names(lr))
        self.assertIn(br.replace("feat/", "blocked/"), names(lr))
        self.assertEqual(lr.item("A")["parkedBranch"], br.replace("feat/", "blocked/"))
        self.assertEqual(lr.repo.git("show", "%s:src/A.txt" % br.replace("feat/", "blocked/")), "work")

    def test_a_second_park_of_the_same_branch_is_harmless(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        self.branch_with_work(lr)
        loop.park("A", "ESCALATION_NEEDED", "x", lr.session)
        self.assertIsNone(loop._shelve("A"))                         # already gone: nothing to do

    def test_a_branch_checked_out_in_another_checkout_is_not_renamed_from_outside(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        br = lr.item("A")["branch"]
        lr.repo.git("switch", "-q", "-c", br, "main")                 # the human's own checkout is ON the branch
        loop.ctx.rs.write_run(dict(loop.ctx.rs.read_run(), worktree="/somewhere/else"))        # and this is not the registered loop worktree
        loop.park("A", "ESCALATION_NEEDED", "x", lr.session)
        self.assertIn(br, names(lr))                                 # a branch someone has checked out is left alone
        lr.repo.git("switch", "-q", "main")


class Sweep(LoopTestCase):
    def test_reconcile_apply_does_not_rename_the_feat_branch_of_a_resting_item(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        br = lr.item("A")["branch"]
        lr.repo.git("switch", "-q", "-c", br, "main")
        lr.repo.commit_file("src/A.txt", "work\n", "work")
        lr.repo.git("switch", "-q", "main")
        # a record that rests (needs_human) while its feat/ branch still exists, as a park from a path that did not shelve would leave it
        def rest(st):
            sm._force_status(st, "A", "needs_human", "test", "parked", "ESCALATION_NEEDED")
        loop.store.update(rest, "test")
        rep = lr.ctl("reconcile", "--apply")
        self.assertEqual(rep.rc, 0, rep)
        self.assertIn(br, names(lr))
        self.assertNotIn("dead/" + br.split("/", 1)[1], names(lr))

    def test_a_branch_no_record_names_still_goes_to_dead_and_a_pending_item_branch_is_an_orphan(self):
        lr = self.lr()
        loop = lr.fast_start()
        lr.repo.git("switch", "-q", "-c", "feat/ghost-a1", "main")
        lr.repo.commit_file("x/ghost.txt", "w", "ghost work")
        lr.repo.git("switch", "-q", "main")
        lr.ctl("reconcile", "--apply")
        self.assertIn("dead/ghost-a1", names(lr))
        self.assertNotIn("feat/ghost-a1", names(lr))

    def test_the_sweep_keeps_a_feat_branch_whose_slug_and_attempt_a_live_record_still_names(self):
        lr = self.lr()
        loop = lr.fast_start()
        lr.repo.git("switch", "-q", "-c", "feat/slice-a-a1", "main")
        lr.repo.commit_file("x/a.txt", "w", "work")
        lr.repo.git("switch", "-q", "main")
        acts = branches.sweep(loop.ctx, ["blocked/slice-a-a1"])         # the record holds the blocked/ name only
        self.assertEqual([a for a in acts if a["branch"] == "feat/slice-a-a1"], [])
        acts = branches.sweep(loop.ctx, [])
        self.assertEqual([a["action"] for a in acts if a["branch"] == "feat/slice-a-a1"], ["rename-dead"])

    def test_live_branches_lists_in_flight_and_resting_items_and_their_shelved_branches_only(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B"), item_doc("C")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        st = lr.state()
        st.items["B"]["status"] = "blocked"
        st.items["B"].update({"branch": "feat/slice-b-a1", "parkedBranch": "blocked/slice-b-a1", "blockReason": "x"})
        st.items["C"].update({"branch": "feat/slice-c-a1"})            # pending: its old branch is not live (recovery resets it)
        live = sm.live_branches(st)
        self.assertEqual(sorted(live), sorted([st.items["A"]["branch"], "feat/slice-b-a1", "blocked/slice-b-a1"]))


if __name__ == "__main__":
    unittest.main()
