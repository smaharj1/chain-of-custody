"""Where an item ends: infra always waits, a delivery other than local is the human's push, and a conflict on the integration branch
(report 6.1, 8.3, 10.7, R5; FI-18, FI-20).
"""
from __future__ import annotations

import os
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers.gitrepo import run as grun

from loopctl.gates import GateResult

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Delivery(DriverTestCase):
    def test_an_infra_item_always_waits_for_a_human(self):
        lr = self.dr(items=[item_doc("A", estimate={"loc": 100, "engineers": ["infra-engineer"]},
                                     touches={"domains": ["infra"], "paths": ["infra/**"], "schema": False, "deps_change": False})])
        lr.set_scenario(lr.happy(by_role={"infra-engineer": [lr.eng_run("A", domain="infra")]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "awaiting_human")
        self.assertTrue(lr.item("A")["humanGate"])
        self.assertIn("needs a human before it merges", r.out)
        self.assertEqual([c["role"] for c in lr.calls()], ["infra-engineer", "code-reviewer"])

    def test_delivery_other_than_local_never_merges_in_the_driver(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], cfg={"delivery": "push-branch"})
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertIn("a human pushes the branch", r.out)
        self.assertEqual(lr.item("A")["status"], "awaiting_human")
        self.assertEqual(lr.ctl("approve", "item", "A", "--by", "Pat").rc, 0)
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertIn("delivery is push-branch", r.out)
        self.assertEqual(lr.item("A")["status"], "awaiting_human")                           # still unmerged: the human pushes and `reconcile` records it
        self.assertEqual(lr.repo.git("log", "--merges", "--format=%H", "main"), "")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")

    def test_a_conflict_on_the_integration_branch_parks_the_item_and_leaves_main_clean(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [lr.eng_run("A", files={"README.md": "the engineer's version\n"})]}))
        lr.approve()

        def human_edit(ctx):
            """A human commits to main (in a second worktree) while the item is being verified: same file, other content."""
            wt = os.path.join(lr.repo.parent, "human-wt")
            grun(["worktree", "add", "-q", wt, "main"], lr.path, env=lr.repo.env)
            with open(os.path.join(wt, "README.md"), "w") as fh:
                fh.write("the human's version\n")
            grun(["commit", "-q", "-am", "human edit"], wt, env=lr.repo.env)
            grun(["worktree", "remove", "--force", wt], lr.path, env=lr.repo.env)
            return GateResult.passed()
        lr.stubs.results["g11"] = human_edit
        r = lr.run()
        self.assertEqual(r.rc, 1, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "merge_conflict"))
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertEqual(lr.repo.git("show", "main:README.md"), "the human's version")
        self.assertIn("blocked/slice-a-a1", lr.branch_names())                               # the work is kept
        self.assertEqual(lr.repo.git("log", "--merges", "--format=%H", "main"), "")
        self.assertEqual(lr.repo.git("show", "blocked/slice-a-a1:README.md"), "the engineer's version")


if __name__ == "__main__":
    unittest.main()
