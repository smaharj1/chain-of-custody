"""`loopctl parallel check|status|leases|cleanup|cascade` (docs/parallel.md)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from loopctl.parallel import mutex, worktrees

from . import ParTestCase, pitem, put


class Cli(ParTestCase):
    def setUp(self):
        super(Cli, self).setUp()
        self.lr = self.pr(items=[pitem("A"), pitem("B"), pitem("C", paths=[]), pitem("D", ["A"], paths=["src/a/**"])])
        self.lr.approve()

    def test_check_says_what_would_stop_a_run_and_which_items_can_run_together(self):
        r = self.lr.ctl("parallel", "check", "--json")
        self.assertEqual(r.rc, 0, r)
        doc = r.json
        self.assertEqual((doc["enabled"], doc["max_parallel"]), (True, 2))
        self.assertTrue(all(row["ok"] for row in doc["rows"]))
        self.assertIn(["A", "B"], doc["groups"])
        why = {(c["a"], c["b"]): c["why"] for c in doc["conflicts"]}
        self.assertIn("runs alone", why[("A", "C")])
        self.assertIn("footprints may meet", why[("A", "D")])
        self.assertEqual([i for i in doc["items"] if i["id"] == "C"][0]["declared"], False)
        text = self.lr.ctl("parallel", "check").out
        self.assertIn("can run together: A, B", text)

    def test_check_fails_when_the_evidence_gate_is_not_met(self):
        os.unlink(os.path.join(self.lr.path, "docs", "baseline", "evidence.json"))
        r = self.lr.ctl("parallel", "check", "--json")
        self.assertEqual(r.rc, 1)
        self.assertFalse({row["id"]: row for row in r.json["rows"]}["parallel.evidence"]["ok"])

    def test_leases_and_worktrees_are_listed_without_their_tokens(self):
        store = mutex.LeaseStore(self.lr.rs)
        store.acquire("item-a", "worker:A", item="A")
        self.lr.repo.git("branch", "feat/slice-a-a1", "main")
        worktrees.create(self.lr.path, "slice-a-a1", "feat/slice-a-a1")
        doc = self.lr.ctl("parallel", "leases", "--json").json
        self.assertEqual([l["name"] for l in doc["leases"]], ["item-a"])
        self.assertNotIn("token", doc["leases"][0])
        self.assertEqual([w["path"] for w in doc["worktrees"]], [".claude/worktrees/slice-a-a1"])
        self.assertIn("lease item-a", self.lr.ctl("parallel", "status").out)

    def fabricate_a_crash(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        mutex.LeaseStore(self.lr.rs, pid=p.pid).acquire("item-a", "dead")
        self.lr.repo.git("branch", "feat/slice-a-a1", "main")
        wt = worktrees.create(self.lr.path, "slice-a-a1", "feat/slice-a-a1")
        put(os.path.join(wt, "unsaved.txt"), "x\n")
        return wt

    def test_cleanup_shows_what_it_would_remove_and_removes_it_only_when_told_and_no_run_holds_the_lock(self):
        wt = self.fabricate_a_crash()
        d = self.lr.ctl("parallel", "cleanup", "--json").json
        self.assertEqual((d["applied"], d["worktrees"], d["leases"]), (False, [".claude/worktrees/slice-a-a1"], ["item-a"]))
        self.assertTrue(os.path.isdir(wt))
        self.lr.fast_start()                                                           # a run holds the lock
        r = self.lr.ctl("parallel", "cleanup", "--apply")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("a run holds the lock", r.err)
        self.assertTrue(os.path.isdir(wt))
        self.lr.rs.release_lock("sess-1")
        r = self.lr.ctl("parallel", "cleanup", "--apply", "--json")
        self.assertEqual((r.rc, r.json["applied"]), (0, True))
        self.assertEqual((self.lr.worktrees_left(), self.lr.leases_left()), ([], []))
        self.assertIn("unsaved.txt", self.lr.repo.git("show", "--stat", "--format=", "feat/slice-a-a1"))

    def test_cleanup_removes_an_unreadable_lease_only_with_apply(self):
        os.makedirs(os.path.join(self.lr.rs.root, "leases"), exist_ok=True)
        put(os.path.join(self.lr.rs.root, "leases", "item-b.json"), "{broken")
        d = self.lr.ctl("parallel", "cleanup", "--json").json
        self.assertEqual(d["corrupt_leases"], ["item-b"])
        self.assertEqual(self.lr.leases_left(), ["item-b.json"])
        self.lr.ctl("parallel", "cleanup", "--apply")
        self.assertEqual(self.lr.leases_left(), [])

    def test_cascade_shows_the_plan_and_rejects_only_with_apply_by_a_named_human_and_a_lock_nobody_else_holds(self):
        for i in ("A", "D"):
            self.lr.quick_merge(i)
        d = self.lr.ctl("parallel", "cascade", "A", "--json")
        self.assertEqual(d.rc, 0, d)
        self.assertEqual((d.json["applied"], [m["item"] for m in d.json["merged"]]), (False, ["D", "A"]))
        self.assertEqual(self.lr.item("A")["status"], "merged")
        self.assertEqual(self.lr.ctl("parallel", "cascade", "A", "--apply").rc, 2)             # --by is required
        self.assertEqual(self.lr.ctl("parallel", "cascade").rc, 2)                              # and an item
        self.lr.fast_start()
        self.assertEqual(self.lr.ctl("parallel", "cascade", "A", "--apply", "--by", "Pat").rc, 3)       # a run holds the lock
        r = self.lr.ctl("parallel", "cascade", "A", "--apply", "--by", "Pat", "--reason", "wrong", "--session", "sess-1", "--json")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual([x["item"] for x in r.json["reverted"]], ["D", "A"])
        self.assertEqual((self.lr.item("A")["status"], self.lr.item("D")["status"]), ("pending", "pending"))
        self.assertEqual([f for f in self.lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines() if f.startswith("src/")], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
