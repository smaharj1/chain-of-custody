"""reconcile: crash recovery, missing merges, human reverts, orphans, the PR overlay (report section 10.2, 19.7 L-3, L-4, L-5, CS-20)."""
from __future__ import annotations

import json
import unittest

from loopctl import branches, reconcile

from . import LoopTestCase, item_doc


class Reconcile(LoopTestCase):
    def scan(self, lr, prs=None, suites=None):
        loop = lr.loop(fresh=True)
        st, _ = loop.store.read()
        return reconcile.scan(loop.ctx, st, loop.ctx.plan, prs, suites)

    def ops(self, rep):
        return sorted((a["op"], a.get("item") or a.get("branch") or "") for a in rep.actions)

    def test_a_clean_state_has_no_drift(self):
        lr = self.lr()
        loop = lr.fast_start()
        lr.quick_merge("A")
        self.assertFalse(self.scan(lr).drift)
        self.assertEqual(lr.ctl("reconcile", "--dry-run").rc, 0)

    def test_a_crash_between_merge_and_record_is_recovered_from_the_trailer(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        mc = lr.do_merge("A")                                   # the merge happened, loopctl never heard
        rep = self.scan(lr)
        self.assertEqual(self.ops(rep), [("delete-merged", "feat/slice-a-a1"), ("merged-recovered", "A")])
        self.assertEqual(lr.ctl("reconcile", "--dry-run").rc, 1)
        self.assertEqual(lr.item("A")["status"], "reviewing")    # dry-run wrote nothing
        r = lr.ctl("reconcile", "--apply")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["mergeCommit"]), ("merged", mc))
        self.assertFalse(self.scan(lr).drift)                    # idempotent
        loop.begin("B", lr.session)

    def test_a_forged_trailer_is_not_trusted(self):
        lr = self.lr()
        lr.fast_start()
        # a commit that merely names the item in its message, on main, not a merge of feat/slice-a-a<n>
        lr.repo.git("switch", "-q", "-c", "topic", "main")
        lr.repo.commit_file("t.txt", "t", "topic work")
        lr.repo.git("switch", "-q", "main")
        lr.repo.git("merge", "--no-ff", "-q", "-m", "Merge branch 'topic'\n\nLoop-Item: A", "topic")
        rep = self.scan(lr)
        self.assertEqual(self.ops(rep), [])
        self.assertIn("reconcile.trailer-unverified", {f.id for f in rep.findings})
        # and a non-merge commit with the trailer is not even looked at
        lr.repo.commit_file("u.txt", "u", "fake\n\nLoop-Item: A")
        self.assertEqual(self.ops(self.scan(lr)), [])
        lr.ctl("reconcile", "--apply")
        self.assertEqual(lr.item("A")["status"], "pending")

    def test_a_trailer_for_an_item_the_state_already_merged_changes_nothing(self):
        lr = self.lr()
        lr.fast_start()
        mc = lr.quick_merge("A")
        self.assertEqual(lr.item("A")["mergeCommit"], mc)
        lr.do_merge("A", attempt=2, file_content="again\n")
        self.assertEqual([o for o in self.ops(self.scan(lr)) if o[0] != "delete-merged"], [])

    def test_a_human_revert_sends_the_item_to_a_human_never_back_to_work(self):
        lr = self.lr()
        lr.fast_start()
        mc = lr.quick_merge("A")
        lr.repo.git("revert", "-m", "1", "--no-edit", mc)
        rep = self.scan(lr)
        self.assertEqual(self.ops(rep), [("human-revert", "A")])
        lr.ctl("reconcile", "--apply")
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("needs_human", "human_revert"))
        with self.assertRaises(Exception):
            lr.loop(fresh=True).begin("B", lr.session)                # B depends on A, which is not merged any more

    def test_a_merge_commit_that_left_the_integration_branch(self):
        lr = self.lr()
        lr.fast_start()
        mc = lr.quick_merge("A")
        lr.repo.git("reset", "-q", "--hard", "HEAD~1")             # a human rewound main
        rep = self.scan(lr)
        self.assertEqual(self.ops(rep), [("merge-missing", "A")])
        self.assertEqual(rep.findings[0].severity, "error")

    def test_in_flight_items_with_no_live_lock_go_back_to_pending(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building")
        self.assertEqual(self.ops(self.scan(lr)), [])              # the lock is held and fresh
        lr.context().lock().release(lr.session)
        self.assertEqual(self.ops(self.scan(lr)), [("reset-inflight", "A")])
        lr.ctl("reconcile", "--apply")
        self.assertEqual(lr.item("A")["status"], "pending")

    def test_orphan_branches_are_renamed_dead_and_blocked_branches_are_never_touched(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        live = lr.item("A")["branch"]
        for b in (live, "feat/ghost-a1", "blocked/old-a3", "dead/older-a1", "feature/not-ours"):
            lr.repo.git("switch", "-q", "-c", b, "main")                # each carries a commit that is on no other branch
            lr.repo.commit_file("x/%s.txt" % b.replace("/", "_"), "w", "work on " + b)
        lr.repo.git("switch", "-q", "main")
        rep = self.scan(lr)
        ops = self.ops(rep)
        self.assertIn(("orphan-branch", "feat/ghost-a1"), ops)
        self.assertNotIn(("orphan-branch", live), ops)
        self.assertNotIn(("orphan-branch", "blocked/old-a3"), ops)
        lr.ctl("reconcile", "--apply")
        names = {b["name"] for b in branches.list_branches(lr.path)}
        self.assertIn("dead/ghost-a1", names)
        self.assertIn("blocked/old-a3", names)
        self.assertIn(live, names)
        self.assertIn("feature/not-ours", lr.repo.git("branch", "--list"))
        # dead branches older than orphan_retention_days are pruned (the test repo's commits are dated January)
        self.assertNotIn("dead/older-a1", names)
        self.assertFalse(any(n.startswith("feat/ghost") for n in names))

    def test_the_branch_name_a_hostile_ref_cannot_become_an_argument(self):
        lr = self.lr()
        lr.fast_start()
        lr.repo.git("branch", "feat/-oProxyCommand-a1", "main", check=False)
        lr.repo.git("branch", "feat/a;rm-a1", "main", check=False)
        rep = self.scan(lr)
        self.assertEqual([a for a in rep.actions if a["op"] == "orphan-branch"], [])

    def test_pr_overlay_merged_closed_open_and_failing(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B"), item_doc("C")])
        loop = lr.fast_start()
        mc = None
        # A: PR merged on GitHub, merge commit present locally
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "pr_open")
        mc = lr.do_merge("A")
        prs = [{"number": 11, "state": "MERGED", "isDraft": False, "headRefName": "feat/slice-a-a1", "url": "https://github.example/o/r/pull/11",
                "mergeCommit": {"oid": mc}, "statusCheckRollup": [{"conclusion": "SUCCESS"}]}]
        rep = self.scan(lr, reconcile.normalize_prs(prs))
        self.assertIn(("pr-merged", "A"), self.ops(rep))
        lr.repo.write("prs.json", json.dumps(prs))
        lr.repo.git("add", "prs.json")
        lr.repo.git("commit", "-q", "-m", "prs")
        self.assertEqual(lr.ctl("reconcile", "--apply", "--prs", "prs.json").rc, 0)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["mergeCommit"]), ("merged", mc))
        self.assertEqual(lr.item("A")["overlay"]["pr"], 11)
        # B: PR merged but the commit is not here yet: only the overlay, and a request to fetch
        loop.begin("B", lr.session)
        lr.walk("B", "design_review", "building", "verifying", "reviewing", "pr_open")
        prs2 = reconcile.normalize_prs([{"number": 12, "state": "MERGED", "headRefName": "feat/slice-b-a1", "mergeCommit": {"oid": "9" * 40}}])
        rep = self.scan(lr, prs2)
        self.assertIn("reconcile.pr-merged-unfetched", {f.id for f in rep.findings})
        self.assertNotIn(("pr-merged", "B"), self.ops(rep))
        # C-like: a PR closed without merging while B is in flight
        prs3 = reconcile.normalize_prs([{"number": 12, "state": "CLOSED", "headRefName": "feat/slice-b-a1"}])
        rep = self.scan(lr, prs3)
        self.assertIn(("pr-closed", "B"), self.ops(rep))
        reconcile.apply(lr.loop(fresh=True), rep)
        self.assertEqual((lr.item("B")["status"], lr.item("B")["blockReason"]), ("needs_human", "pr_closed"))

    def test_open_pr_with_failing_checks_is_flagged_for_a_ci_fix(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        prs = reconcile.normalize_prs([{"number": 5, "state": "OPEN", "isDraft": True, "headRefName": "feat/slice-a-a1", "url": "https://github.example/o/r/pull/5",
                                        "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]}])
        self.assertEqual(prs[0]["checks"], "fail")
        rep = self.scan(lr, prs)
        self.assertIn("reconcile.ci-failed", {f.id for f in rep.findings})
        reconcile.apply(lr.loop(fresh=True), rep)
        rec = lr.item("A")
        self.assertEqual((rec["status"], rec["overlay"]["checks"], rec["prUrl"]), ("pr_open", "fail", "https://github.example/o/r/pull/5"))

    def test_normalize_prs_rejects_junk(self):
        self.assertEqual(reconcile.normalize_prs("nope"), [])
        out = reconcile.normalize_prs([{"number": "x"}, {"number": 1, "state": "WEIRD"}, 5, None, {"number": 2, "state": "open", "url": "javascript:alert(1)",
                                                                                                    "mergeCommit": {"oid": "not-a-sha"}}])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["state"], out[0]["url"], out[0]["merge_commit"]), ("OPEN", None, None))

    def test_live_gh_argv_is_fixed(self):
        argv = reconcile.live_prs_argv()
        self.assertEqual(argv[:3], ["gh", "pr", "list"])
        self.assertTrue(all(isinstance(a, str) for a in argv))

    def test_live_refuses_inside_a_claude_code_session(self):
        lr = self.lr()
        lr.fast_start()
        r = lr.ctl("reconcile", "--dry-run", "--live", env={"CLAUDECODE": "1"})
        self.assertEqual(r.rc, 3, r)

    def test_items_in_state_but_not_in_the_plan_are_reported(self):
        lr = self.lr()
        lr.fast_start()
        lr.write_plan([item_doc("A")])
        lr.commit_all("drop B and C without re-approval")
        rep = self.scan(lr)
        self.assertEqual({f.item for f in rep.findings if f.id == "reconcile.not-in-plan"}, {"B", "C"})


if __name__ == "__main__":
    unittest.main()
