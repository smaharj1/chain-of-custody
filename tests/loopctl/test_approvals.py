"""Approvals are bound to a kind, an item, an attempt and a head, and every other kind is consumed when it is written (review fixes D2, LC-1);
the head a transition judges is the branch tip unless named, and a merge must contain exactly that head (LC-5, GT-4, GT-9); LC-10, LC-13, LC-14.

These use the real `Loop` against a temp repository (no stub of the approval logic).
"""
from __future__ import annotations

import types
import unittest

from loopctl import gates, state as sm
from loopctl.common import Refused
from loopctl.gates import GateResult

from . import LoopTestCase, item_doc


def gate_mod(name="g6", **attrs):
    m = types.SimpleNamespace(NAME=name, OWNER_METRIC="x", run=lambda ctx: GateResult.passed(note="ok"), __name__="fake_" + name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class Base(LoopTestCase):
    def setUp(self):
        for n in list(gates._registered):
            gates.unregister(n)
        self.addCleanup(lambda: [gates.unregister(n) for n in list(gates._registered)])

    def at_awaiting(self, **kw):
        lr = self.lr(items=[item_doc("A", **kw), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        return lr, loop

    def approvals(self, lr):
        return lr.state().d["approvals"]

    def more_work(self, lr, iid="A", name="more.txt"):
        """A new commit on the item's branch: its tip is a new head."""
        br = lr.item(iid)["branch"]
        lr.repo.git("switch", "-q", br)
        lr.repo.commit_file("src/%s" % name, "more\n", "more work")
        tip = lr.repo.head()
        lr.repo.git("switch", "-q", "main")
        return tip


class Leaks(Base):
    def test_an_un_park_approval_cannot_later_satisfy_the_merge_gate(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.park("A", "ESCALATION_NEEDED", "needs a person", lr.session)
        ap = loop.approve("item", "A", "Pat", "approve", "looked at it")
        self.assertEqual((ap["kind"], ap["used"]), ("unpark", True))               # consumed when written
        self.assertEqual(lr.item("A")["status"], "pending")
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        st = lr.state()
        self.assertIsNone(sm._find_approval(st, "A", st.items["A"]["attempt"], st.items["A"]["headSha"]))
        mc = lr.do_merge("A")
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)

    def test_an_approval_made_before_a_ci_fix_round_trip_is_void_for_the_new_head(self):
        lr, loop = self.at_awaiting()
        ap = loop.approve("item", "A", "Pat")
        self.assertEqual((ap["kind"], ap["used"], ap["headSha"]), ("merge", False, lr.item("A")["headSha"]))
        loop.fail("A", "item", "ci_failed", lr.session, stage="awaiting_human")           # back to building
        self.assertEqual(lr.item("A")["status"], "building")
        old = [a for a in self.approvals(lr) if a["kind"] == "merge"][0]
        self.assertTrue(old["used"])
        self.assertTrue(old["voided"].startswith("status:awaiting_human->building"))
        new_head = self.more_work(lr)
        lr.walk("A", "verifying")
        loop.advance("A", "reviewing", lr.session, head=new_head)
        loop.advance("A", "awaiting_human", lr.session, head=new_head)
        mc = lr.do_merge("A")
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)                       # the old approval is gone
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertEqual(lr.item("A")["status"], "merged")

    def test_an_approval_for_one_head_cannot_merge_another(self):
        lr, loop = self.at_awaiting()
        approved = lr.item("A")["headSha"]
        loop.approve("item", "A", "Pat")
        other = self.more_work(lr)
        self.assertNotEqual(approved, other)
        mc = lr.do_merge("A")                                                              # merges the branch at its NEW tip
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc, head=other)
        self.assertTrue([a for a in self.approvals(lr) if a["kind"] == "merge"][0]["used"] is False)       # nothing consumed by the refusal
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)                       # head defaults to the branch tip: also the other one

    def test_a_checkpoint_approval_on_an_item_in_flight_is_refused_and_never_merges(self):
        lr, loop = self.at_awaiting()
        with self.assertRaises(Refused):
            loop.approve("checkpoint", "A", "Pat")
        self.assertEqual([a for a in self.approvals(lr) if a["scope"] == "checkpoint"], [])
        # even when a checkpoint is outstanding for the item, the record cannot stand in for a merge approval
        def park(st):
            st.header.update({"runStatus": "parked", "parked": {"item": "A", "reason": "checkpoint", "at": "2026-10-20T00:00:00Z"}})
        loop.store.update(park, "test")
        ap = loop.approve("checkpoint", "A", "Pat")
        self.assertEqual((ap["kind"], ap["used"]), ("checkpoint", False))
        st = lr.state()
        self.assertIsNone(sm._find_approval(st, "A", 1, st.items["A"]["headSha"]))
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=lr.do_merge("A"))

    def test_a_record_written_before_kinds_existed_does_not_merge(self):
        lr, loop = self.at_awaiting()
        head = lr.item("A")["headSha"]

        def legacy(st):
            st.d["approvals"].append({"id": "ap-9", "scope": "item", "target": "A", "by": "Pat", "choice": "approve", "at": "2026-10-20T00:00:00Z",
                                      "note": "", "attempt": 1, "used": False})
            st.d["approvals"].append({"id": "ap-10", "scope": "item", "target": "A", "by": "Pat", "choice": "approve", "at": "2026-10-20T00:00:00Z",
                                      "note": "", "attempt": 1, "used": False, "headSha": head})
        loop.store.update(legacy, "test")
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=lr.do_merge("A"))

    def test_acknowledgements_and_other_scopes_are_written_consumed(self):
        lr, loop = self.at_awaiting()
        kinds = {}
        kinds["env"] = loop.approve("env", "env", "Pat")
        kinds["breaker"] = loop.approve("breaker", "breaker", "Pat")
        kinds["cost"] = loop.approve("cost", "cost", "Pat")
        for scope, ap in kinds.items():
            self.assertEqual((ap["kind"], ap["used"]), (scope, True), scope)
        stop = loop.approve("item", "A", "Pat", "stop")
        self.assertEqual((stop["kind"], stop["used"]), ("ack", True))
        self.assertTrue(all(a["used"] for a in self.approvals(lr)))               # the plan approval and every one above: nothing unused is left to be reused
        rej = loop.approve("item", "A", "Pat", "reject", "no")
        self.assertEqual((rej["kind"], rej["used"]), ("ack", True))

    def test_a_merge_approval_needs_a_recorded_head(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        def blank(st):
            st.item("A")["status"] = "reviewing"          # a hand-edited record: reviewing without a head
        loop.store.update(blank, "test")
        with self.assertRaises(Refused) as cm:
            loop.approve("item", "A", "Pat")
        self.assertIn("no head is recorded", str(cm.exception))

    def test_a_reject_voids_a_standing_approval(self):
        lr, loop = self.at_awaiting()
        loop.approve("item", "A", "Pat")
        loop.approve("item", "A", "Pat", "reject", "redo it")
        self.assertTrue(all(a["used"] for a in self.approvals(lr)))


class Heads(Base):
    def test_the_head_defaults_to_the_branch_tip_and_a_missing_branch_refuses(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        with self.assertRaises(Refused):
            loop.advance("A", "reviewing", lr.session)                                     # no branch, no --head
        tip = lr.item_branch("A")
        rec = loop.advance("A", "reviewing", lr.session)
        self.assertEqual(rec["headSha"], tip)

    def test_a_merge_commit_that_is_not_a_merge_of_the_judged_head_is_refused(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        judged = lr.item("A")["headSha"]
        self.more_work(lr)                                                                  # the branch moved after the review
        mc = lr.do_merge("A")
        self.assertNotEqual(lr.repo.git("rev-parse", mc + "^2"), judged)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=mc, head=judged)
        self.assertIn("second parent", str(cm.exception))

    def test_a_gate_result_for_another_head_or_none_is_stale(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        gates.register(gate_mod("g6", REQUIRED_FOR=(("verifying", "reviewing"),)))
        tip = lr.item_branch("A")
        loop = lr.loop(fresh=True)
        loop.record_gate("A", "g6", {"status": "pass", "mode": "on", "blocking": False, "head": None, "findings": [], "evidence": {}}, lr.session)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "reviewing", lr.session)
        self.assertIn("stale", str(cm.exception))                                           # a record with no head is stale outside the pre-build gates
        loop.record_gate("A", "g6", {"status": "pass", "mode": "on", "blocking": False, "head": "b" * 40, "findings": [], "evidence": {}}, lr.session)
        with self.assertRaises(Refused):
            loop.advance("A", "reviewing", lr.session)                                      # another head
        loop.record_gate("A", "g6", {"status": "pass", "mode": "on", "blocking": False, "head": tip, "findings": [], "evidence": {}}, lr.session)
        loop.advance("A", "reviewing", lr.session)                                          # the branch tip, defaulted

    def test_the_platform_counter_counts_a_row(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.fail("A", "platform", "overloaded", lr.session)
        loop.fail("A", "platform", "overloaded", lr.session)
        self.assertEqual(lr.item("A")["counters"]["platform"], 2)
        lr.walk("A", "design_review")                                                       # a forward step ends the row
        self.assertEqual(lr.item("A")["counters"]["platform"], 0)
        loop.fail("A", "platform", "overloaded", lr.session)
        self.assertEqual(lr.item("A")["counters"]["platform"], 1)


class Locks(Base):
    def test_a_gate_result_is_recorded_only_by_the_session_that_holds_the_lock(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        res = {"status": "pass", "mode": "on", "blocking": False, "head": "a" * 40, "findings": [], "evidence": {}}
        for who in (None, "another-session"):
            with self.assertRaises(Refused):
                loop.record_gate("A", "g6", res, who)
            with self.assertRaises(Refused):
                loop.record_gates("A", [("g6", res)], who)
        loop.record_gate("A", "g6", res, lr.session)
        loop.ctx.rs.release_lock(lr.session)
        with self.assertRaises(Refused):
            loop.record_gate("A", "g6", res, lr.session)                                    # no lock held at all

    def test_park_still_works_without_the_lock(self):
        lr = self.lr(items=[item_doc("A")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.ctx.rs.release_lock(lr.session)
        loop.park("A", "ESCALATION_NEEDED", "a person parks it", None)
        self.assertEqual(lr.item("A")["status"], "needs_human")


class Preflight(Base):
    def test_preflight_refuses_halt_a_missing_approval_and_a_stale_head(self):
        lr, loop = self.at_awaiting()
        head = lr.item("A")["headSha"]
        with self.assertRaises(Refused):
            loop.preflight_merge("A", head, lr.session)                                     # no approval yet
        loop.approve("item", "A", "Pat")
        loop.preflight_merge("A", head, lr.session)                                         # fine, and nothing was written
        self.assertFalse([a for a in self.approvals(lr) if a["kind"] == "merge"][0]["used"])
        with self.assertRaises(Refused):
            loop.preflight_merge("A", "c" * 40, lr.session)                                 # the approval is for another head
        with self.assertRaises(Refused):
            loop.preflight_merge("A", head, "another-session")
        lr.ctl("halt", "--reason", "stop")
        with self.assertRaises(Refused):
            loop.preflight_merge("A", head, lr.session)


if __name__ == "__main__":
    unittest.main()
