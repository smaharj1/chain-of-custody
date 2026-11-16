"""The humans in the loop: checkpoints per the autonomy dial, items that wait for approval, and an approval that does not cover moved work
(report 6, 10.4; FI-18). The checkpoint rule itself is in test_driver_helpers.
"""
from __future__ import annotations

import json
import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import driver

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Human(DriverTestCase):
    def test_per_feature_autonomy_pauses_after_each_item_until_a_checkpoint_is_approved(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG, requiresEnv=["B_NEEDS_THIS"])], cfg={"autonomy": "per-feature"})
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run("--json")
        self.assertEqual((r.rc, json.loads(r.out)["outcome"]), (0, "checkpoint"), r)
        self.assertEqual((lr.item("A")["status"], lr.item("B")["status"]), ("merged", "pending"))
        st = lr.state()
        self.assertEqual((st.header["runStatus"], st.header["parked"]["reason"], st.header["parked"]["item"]), ("parked", "checkpoint", "A"))
        n = len(lr.calls())
        r = lr.run()                                                                         # started again without an answer
        self.assertEqual(r.rc, 3, r)
        self.assertIn("approve checkpoint A", r.out + r.err)
        self.assertEqual((lr.item("B")["status"], len(lr.calls())), ("pending", n))
        self.assertEqual(lr.ctl("approve", "checkpoint", "A", "--by", "Pat").rc, 0)
        r = lr.run("--json")                                                                 # the run goes on: B is next (and needs a variable nobody set)
        self.assertEqual(r.rc, 1, r)
        self.assertEqual((lr.item("B")["status"], lr.item("B")["blockReason"]), ("blocked", "needs_provisioning"))
        self.assertEqual(len(lr.calls()), n)
        self.assertTrue([a for a in lr.state().d["approvals"] if a["scope"] == "checkpoint"][0]["used"])      # consumed once

    def test_a_human_gated_item_stops_at_awaiting_human_and_merges_only_after_the_approval(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG, riskClass="high")])
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run("--json")
        self.assertEqual((r.rc, json.loads(r.out)["outcome"]), (0, "awaiting_human"), r)
        a = lr.item("A")
        self.assertEqual(a["status"], "awaiting_human")
        self.assertTrue(a["humanGate"])
        self.assertEqual(a["headSha"], lr.repo.git("rev-parse", "feat/slice-a-a1"))
        n = len(lr.calls())
        r = lr.run("--json")                                                                 # again, no approval: nothing happens, nothing is launched
        self.assertEqual((r.rc, json.loads(r.out)["outcome"]), (0, "awaiting_human"), r)
        self.assertEqual(len(lr.calls()), n)
        self.assertIn("feat/slice-a-a1", lr.branch_names())
        self.assertEqual(lr.ctl("approve", "item", "A", "--by", "Pat").rc, 0)
        r = lr.run("--json")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(json.loads(r.out)["merged_this_run"], ["A"])
        self.assertEqual(json.loads(r.out)["children"], 0)                                   # the merge needed no new child
        self.assertEqual(lr.item("A")["status"], "merged")
        self.assertTrue([a for a in lr.state().d["approvals"] if a["scope"] == "item"][0]["used"])

    def test_an_approval_does_not_cover_a_branch_that_moved_afterwards(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG, riskClass="high")])
        drv = lr.driver_at("A", "design_review", "building", "verifying", "reviewing")
        lr.repo.commit_file("src/a.py", "x = 1\n", "the item's work")
        head = lr.repo.head()
        drv.loop.advance("A", "awaiting_human", lr.session, head=head)
        self.assertEqual(lr.ctl("approve", "item", "A", "--by", "Pat").rc, 0)
        lr.repo.commit_file("src/sneaky.py", "evil = True\n", "after the approval")
        with self.assertRaises(driver._Stop) as cm:
            drv.merge_approved("A")
        self.assertEqual(cm.exception.outcome, "parked")
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("needs_human", "head_changed"))
        self.assertNotIn("sneaky", lr.repo.git("ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(lr.repo.git("log", "--merges", "--format=%H", "main"), "")


if __name__ == "__main__":
    unittest.main()
