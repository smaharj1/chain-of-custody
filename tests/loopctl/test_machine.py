"""The item state machine through the Loop API (report section 10.4): transitions, preconditions, approvals, frozen ACs."""
from __future__ import annotations

import json
import unittest

from loopctl import common, state as sm
from loopctl.common import Refused, UsageError

from . import LoopTestCase, item_doc


class Edges(unittest.TestCase):
    def test_the_enum_is_closed_and_every_edge_goes_somewhere_legal(self):
        self.assertEqual(len(sm.STATUSES), 13)
        for frm, tos in sm.TRANSITIONS.items():
            self.assertIn(frm, sm.STATUSES)
            for t in tos:
                self.assertIn(t, sm.STATUSES)
        for frm, tos in sm.FORWARD.items():
            for t in tos:
                self.assertIn(t, sm.TRANSITIONS[frm], "%s -> %s is forward but not an edge" % (frm, t))
        self.assertEqual(sm.TRANSITIONS["abandoned"], ())
        self.assertNotIn("pending", sm.FORWARD)

    def test_blocked_and_needs_human_need_a_reason(self):
        st = sm.State.new("demo")
        st.items["A"] = sm.new_item_record()
        with self.assertRaises(Refused):
            st.set_status("A", "blocked", "t")
        with self.assertRaises(Refused):
            st.set_status("A", "needs_human", "t", block_reason="")
        st.set_status("A", "needs_human", "t", block_reason="why")
        self.assertEqual(st.items["A"]["blockReason"], "why")
        with self.assertRaises(Refused):
            st.set_status("A", "merged", "t")

    def test_validate_state_doc_rejects_garbage(self):
        good = sm.State.new("demo")
        good.items["A"] = sm.new_item_record()
        self.assertTrue(sm.validate_state_doc(json.loads(json.dumps(good.d)), "demo"))

        def broken(mut):
            d = json.loads(json.dumps(good.d))
            mut(d)
            return d
        cases = {
            "status outside the enum": lambda d: d["items"]["A"].__setitem__("status", "done"),
            "blocked without a reason": lambda d: d["items"]["A"].update(status="blocked", blockReason=None),
            "merged without a commit": lambda d: d["items"]["A"].update(status="merged"),
            "bad sha": lambda d: d["items"]["A"].update(status="merged", mergeCommit="HEAD"),
            "item id with a slash": lambda d: d["items"].__setitem__("../x", sm.new_item_record()),
            "wrong plan": lambda d: d.__setitem__("plan", "other"),
            "schema": lambda d: d.__setitem__("schema", "v0"),
            "counter not int": lambda d: d["items"]["A"]["counters"].__setitem__("verify", "3"),
            "negative counter": lambda d: d["items"]["A"]["counters"].__setitem__("verify", -1),
            "attempt zero": lambda d: d["items"]["A"].__setitem__("attempt", 0),
            "branch with dots": lambda d: d["items"]["A"].__setitem__("branch", "feat/../x"),
            "extra top level": lambda d: d.__setitem__("evil", 1),
            "approvals not a list": lambda d: d.__setitem__("approvals", {}),
            "seq not int": lambda d: d.__setitem__("seq", "1"),
        }
        for name, mut in cases.items():
            with self.subTest(name):
                with self.assertRaises(sm.StateCorrupt):
                    sm.validate_state_doc(broken(mut), "demo")


class Flow(LoopTestCase):
    def test_the_whole_forward_path_and_a_merge(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "stubbing", "building", "verifying", "reviewing")
        mc = lr.do_merge("A")
        loop.advance("A", "merged", lr.session, merge_commit=mc)
        rec = lr.item("A")
        self.assertEqual((rec["status"], rec["mergeCommit"]), ("merged", mc))
        self.assertEqual([h["to"] for h in rec["history"]][:3], ["designing", "design_review", "stubbing"])
        self.assertEqual(loop.store.read()[0].header["breaker"], 0)

    def test_advance_refuses_skips_and_backward_steps(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        for bad in ("building", "verifying", "merged", "design_review_x"):
            with self.subTest(bad):
                with self.assertRaises((Refused, UsageError)):
                    loop.advance("A", bad, lr.session)
        for bad in ("pending", "blocked", "needs_human", "abandoned"):
            with self.assertRaises(UsageError):
                loop.advance("A", bad, lr.session)
        self.assertEqual(lr.item("A")["status"], "designing")
        lr.walk("A", "design_review", "building")
        with self.assertRaises(Refused):
            loop.advance("A", "design_review", lr.session)
        with self.assertRaises(Refused):
            loop.advance("A", "building", lr.session)

    def test_design_review_may_skip_stubbing_but_not_when_g2_is_off_and_asked_for(self):
        lr = self.lr(extra_cfg="gates:\n  g2: off")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review")
        with self.assertRaises(Refused):
            loop.advance("A", "stubbing", lr.session)
        loop.advance("A", "building", lr.session)

    def test_begin_needs_an_approved_plan_the_lock_and_dependencies(self):
        lr = self.lr()
        loop = lr.loop()
        with self.assertRaises(Refused):
            loop.begin("A", lr.session)                       # no lock, no state
        loop = lr.fast_start()
        with self.assertRaises(Refused) as cm:
            loop.begin("B", lr.session)
        self.assertIn("depends on A", str(cm.exception))
        loop.begin("A", lr.session)
        with self.assertRaises(Refused) as cm:
            loop.begin("A", lr.session)
        self.assertIn("is designing", str(cm.exception))

    def test_only_one_item_runs_at_a_time(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        with self.assertRaises(Refused) as cm:
            loop.begin("B", lr.session)
        self.assertIn("one item at a time", str(cm.exception))

    def test_a_session_that_does_not_hold_the_lock_cannot_write(self):
        lr = self.lr()
        loop = lr.fast_start()
        with self.assertRaises(Refused) as cm:
            loop.begin("A", "someone-else")
        self.assertIn("belongs to session", str(cm.exception))
        with self.assertRaises(Refused):
            loop.begin("A", None)
        self.assertEqual(lr.item("A")["status"], "pending")

    def test_halt_stops_forward_moves_but_not_the_record_of_a_failure(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.context().rs.set_halt("stop now", "Pat")
        with self.assertRaises(Refused):
            loop.advance("A", "design_review", lr.session)
        with self.assertRaises(Refused):
            loop.begin("B", lr.session)
        d = loop.fail("A", "platform", "overloaded", lr.session)
        self.assertEqual(d.action, "retry")

    def test_a_merge_commit_must_be_real_on_the_branch_and_carry_the_trailer(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        fake = "0123456789abcdef0123456789abcdef01234567"
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=fake)
        with self.assertRaises(UsageError):
            loop.advance("A", "merged", lr.session)
        lr.repo.git("switch", "-q", "-c", "other", "main")
        other = lr.repo.commit_file("o.txt", "o", "off the integration branch")
        lr.repo.git("switch", "-q", "main")
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=other)
        self.assertIn("not on the integration branch", str(cm.exception))
        untagged = lr.do_merge("A", trailer=False)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=untagged)
        self.assertIn("Loop-Item", str(cm.exception))
        self.assertEqual(lr.item("A")["status"], "reviewing")

    def test_a_human_gated_item_needs_an_approval_that_is_used_once(self):
        lr = self.lr(items=[item_doc("A", riskClass="high"), item_doc("B", ["A"])])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        mc = lr.do_merge("A")
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertIn("needs a human approval", str(cm.exception))
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertTrue(lr.state().d["approvals"][-1]["used"])

    def test_awaiting_human_needs_the_approval_even_for_an_ungated_item(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        mc = lr.do_merge("A")
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=mc)

    def test_an_approval_is_for_one_attempt_and_one_item(self):
        lr = self.lr(items=[item_doc("A", riskClass="high"), item_doc("B", riskClass="high")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        mc = lr.do_merge("A")
        with self.assertRaises(Refused):
            loop.approve("item", "B", "Pat")                    # B is pending: there is nothing there to approve
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=mc)

    def test_checkpoint_reject_resets_the_item_with_the_feedback(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        loop.approve("checkpoint", "A", "Pat", "reject", "wrong colour")
        rec = lr.item("A")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["rework"][-1]["note"], "wrong colour")
        self.assertIsNone(rec["branch"])

    def test_a_merged_item_cannot_be_rejected_by_an_approval(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=lr.do_merge("A"))
        with self.assertRaises(Refused):
            loop.approve("checkpoint", "A", "Pat", "reject")

    def test_clarifications_are_capped_at_two_rounds(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        self.assertEqual(loop.clarify("A", lr.session), 1)
        self.assertEqual(loop.clarify("A", lr.session), 2)
        self.assertEqual(loop.clarify("A", lr.session), 3)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("blocked", "NEEDS_CLARIFICATION"))

    def test_park_and_the_unblock_review(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.park("A", "ESCALATION_NEEDED", "needs a human call", lr.session)
        self.assertEqual(lr.item("A")["status"], "needs_human")
        loop.approve("unblock", "A", "Pat", "keep")
        self.assertEqual(lr.item("A")["status"], "needs_human")
        loop.approve("unblock", "A", "Pat", "reset")
        self.assertEqual(lr.item("A")["status"], "pending")
        with self.assertRaises(Refused):
            loop.approve("unblock", "A", "Pat", "reset")

    def test_approve_needs_a_named_human_and_a_known_scope(self):
        lr = self.lr()
        loop = lr.fast_start()
        for by in ("", "   "):
            with self.assertRaises(UsageError):
                loop.approve("env", "env", by)
        with self.assertRaises(UsageError):
            loop.approve("root", "x", "Pat")
        with self.assertRaises(UsageError):
            loop.approve("item", "A", "Pat", "yolo")

    def test_acceptance_criteria_are_frozen_at_approval(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building")
        revised = [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"])]
        revised[0]["acceptance"][0]["text"] = "`GET /a/1` returns 201"
        revised[1]["acceptance"][0]["text"] = "`GET /b/1` returns 201"
        lr.write_plan(revised)
        lr.commit_all("revise plan")
        loop2 = lr.loop(fresh=True)                              # the plan is read at the new base
        with self.assertRaises(Refused) as cm:
            loop2.advance("A", "verifying", lr.session)
        self.assertIn("acceptance criteria differ", str(cm.exception))
        loop2.approve_plan("Pat")
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("needs_human", "ac_changed"))
        self.assertEqual(lr.item("B")["status"], "pending")      # not started: adopts the new criteria

    def test_a_changed_plan_must_be_reapproved_before_begin(self):
        lr = self.lr()
        lr.fast_start()
        revised = [item_doc("A", title="Renamed"), item_doc("B", ["A"]), item_doc("C", ["B"])]
        lr.write_plan(revised)
        lr.commit_all("rename A")
        with self.assertRaises(Refused) as cm:
            lr.loop(fresh=True).begin("A", lr.session)
        self.assertIn("plan changed since it was approved", str(cm.exception))

    def test_a_plan_revision_that_drops_items(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.write_plan([item_doc("A")])
        lr.commit_all("drop B and C")
        lr.loop(fresh=True).approve_plan("Pat")
        self.assertEqual(lr.item("B")["status"], "abandoned")
        self.assertEqual(lr.item("C")["status"], "abandoned")
        self.assertEqual(lr.item("A")["status"], "designing")

    def test_a_v1_seed_marks_done_items_merged_only_when_the_commit_exists(self):
        lr = self.lr()
        mc = lr.do_merge("A")
        loop = lr.loop(fresh=True)
        seed = {"A": {"status": "merged", "mergeCommit": mc}, "B": {"status": "merged", "mergeCommit": "3" * 40},
                "C": {"status": "blocked", "blockReason": "budget_halted"}}
        loop.approve_plan("Pat", seed=seed)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["mergeCommit"]), ("merged", mc))
        self.assertEqual((lr.item("B")["status"], lr.item("B")["blockReason"]), ("needs_human", "merge_missing"))
        self.assertEqual((lr.item("C")["status"], lr.item("C")["blockReason"]), ("blocked", "budget_halted"))

    def test_an_invalid_plan_cannot_be_approved(self):
        lr = self.lr(items=[item_doc("A", size="L")])
        with self.assertRaises(Refused) as cm:
            lr.loop().approve_plan("Pat")
        self.assertIn("does not validate", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
