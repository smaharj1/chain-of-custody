"""The scheduler's decisions, one at a time and without a child session: admission, the claim all-or-nothing, the budget reservation, the reject
cascade's reach into a run in progress, how a worker's end is settled (report 11.5 item 3, DEC-34, L-6)."""
from __future__ import annotations

import threading
import types

from loopctl import branches, state as sm
from loopctl.driver import _Stop
from loopctl.parallel import cascade, mutex, ports, scheduler, worktrees

from . import DriverCase, pitem

PLAN = [pitem("P1"), pitem("P2"), pitem("P3", ["P1"]), pitem("P4", paths=["src/**"]), pitem("P5", paths=[]),
        pitem("S1", schema=True), pitem("S2", schema=True), pitem("DEP1", deps_change=True), pitem("DEP2", deps_change=True),
        pitem("BO"), pitem("ENV", requiresEnv=["NOPE_VAR_FOR_TESTS"]), pitem("WH", paths=["src/wh/**"]), pitem("WHC", paths=["src/wh/inner/**"]),
        pitem("Z1"), pitem("Z2"), pitem("Z3"), pitem("Z4"), pitem("L1"), pitem("L2"), pitem("L3"), pitem("L4"),
        pitem("Q1"), pitem("Q2", ["Q1"]), pitem("Q5"), pitem("Q3", ["Q5"]), pitem("Q6"), pitem("Q4", ["Q6"]),
        pitem("R1"), pitem("R2"), pitem("R3"), pitem("B1"), pitem("B2"), pitem("B3")]


def fake_slot(iid, running=True):
    s = types.SimpleNamespace(iid=iid, cancel=threading.Event(), done=threading.Event(), path="/nowhere/" + iid, branch=None,
                              block=types.SimpleNamespace(env=lambda: {"KIT_PORT_OFFSET": "0"}), ready=False, lost=0)
    if not running:
        s.done.set()
    return s


class Admission(DriverCase):
    ITEMS = PLAN

    def admit(self, order, running=(), merged=()):
        for m in merged:
            self.lr.quick_merge(m)
        for r in running:
            self.drv.slots[r] = fake_slot(r)
        started = []
        def fake_start(iid):
            started.append(iid)
            self.drv.slots[iid] = fake_slot(iid)
            return True
        self.drv.start_item = fake_start
        st = self.drv.state()
        self.drv.admit(st, order, self.drv.loop._deps())
        return started

    def waits(self):
        return [e["item"] for e in self.lr.events() if e.get("event") == "admit_wait"][-5:]

    def test_disjoint_items_start_together_and_one_whose_dependency_has_not_merged_does_not(self):
        self.assertEqual(self.admit(["P1", "P2", "P3"]), ["P1", "P2"])
        self.drv.slots.clear()
        self.assertEqual(self.admit(["P3"], merged=["P1"]), ["P3"])        # P1 merged on the integration branch: P3 is eligible

    def test_an_item_whose_footprint_may_meet_a_running_ones_waits_and_does_not_let_a_later_one_overtake_it(self):
        self.assertEqual(self.admit(["P4", "P2"], running=["Z1"]), [])        # P4 (src/**) meets the running Z1; P2 conflicts with P4, which came first
        self.assertEqual(self.waits()[-2:], ["P4", "P2"])
        self.drv.slots.clear()
        self.assertEqual(self.admit(["P2"], running=["Z1"]), ["P2"])                                  # without P4 in front, P2 is disjoint from Z1

    def test_an_item_that_declares_no_footprint_runs_alone_whichever_comes_first(self):
        self.assertEqual(self.admit(["P5", "L1"]), ["P5"])
        self.drv.slots.clear()
        self.assertEqual(self.admit(["L1", "P5"]), ["L1"])

    def test_the_schema_and_dependency_lanes_hold_one_item_each_and_do_not_hold_each_other(self):
        self.assertEqual(self.admit(["S1", "S2"]), ["S1"])
        self.drv.slots.clear()
        self.assertEqual(self.admit(["DEP1", "DEP2"]), ["DEP1"])
        self.drv.slots.clear()
        self.assertEqual(self.admit(["S1", "DEP1"]), ["S1", "DEP1"])

    def test_no_more_than_max_parallel_worktrees(self):
        self.drv.max_parallel = 2
        self.assertEqual(self.admit(["Z1", "Z2", "Z3", "Z4"]), ["Z1", "Z2"])
        self.drv.slots.clear()
        self.drv.max_parallel = 1
        self.assertEqual(self.admit(["Z3", "Z4"]), ["Z3"])

    def test_an_item_waiting_for_a_human_still_holds_its_footprint(self):
        def mutate(st):
            st.items["WH"]["status"] = "awaiting_human"
        self.drv.loop.store.update(mutate, "test: WH waits for a human")
        self.assertEqual(self.admit(["WHC", "L2"]), ["L2"])

    def test_an_item_backing_off_does_not_hold_anything_back(self):
        from loopctl import common

        def mutate(st):
            st.items["BO"]["notBefore"] = common.iso(common.now() + 600)
        self.drv.loop.store.update(mutate, "test: BO backs off")
        self.assertEqual(self.admit(["BO", "L3"]), ["L3"])

    def test_an_item_that_needs_an_environment_variable_that_is_missing_is_parked_not_started(self):
        self.drv.env.pop("NOPE_VAR_FOR_TESTS", None)
        self.assertEqual(self.admit(["ENV", "L4"]), ["L4"])
        r = self.lr.item("ENV")
        self.assertEqual((r["status"], r["blockReason"]), ("blocked", "needs_provisioning"))
        self.assertTrue(self.drv.draining)


class Claims(DriverCase):
    ITEMS = PLAN

    def other(self):
        return mutex.LeaseStore(self.drv.rs, ttl_s=60)

    def test_two_workers_cannot_claim_one_item_and_nothing_is_left_behind_by_the_loser(self):
        theirs = self.other().acquire(mutex.item_lease("P1"), "another driver")
        self.assertIs(self.drv.start_item("P1"), False)
        self.assertEqual([r["name"] for r in self.drv.leases.list()], ["item-p1"])
        self.assertEqual(self.lr.item("P1")["status"], "pending")                    # nothing was begun
        self.assertTrue(self.other().holds(theirs))

    def test_a_lane_that_is_taken_gives_back_the_item_lease_already_taken(self):
        self.other().acquire(mutex.mutex_lease("schema"), "S0")
        self.assertIs(self.drv.start_item("S1"), False)
        self.assertEqual([r["name"] for r in self.drv.leases.list()], ["mutex-schema"])
        self.assertEqual(self.lr.item("S1")["status"], "pending")

    def test_with_every_port_slot_taken_the_item_does_not_start_and_leaks_no_lease(self):
        store = self.other()
        [store.acquire(mutex.ports_lease(n), "x") for n in range(ports.MAX_SLOTS)]
        self.assertIs(self.drv.start_item("P2"), False)
        self.assertEqual(len(self.drv.leases.list()), ports.MAX_SLOTS)
        self.assertEqual(self.lr.item("P2")["status"], "pending")
        self.assertTrue(any("no free port slot" in str(e.get("why")) for e in self.lr.events() if e.get("event") == "admit_wait"))

    def test_a_port_in_use_by_a_stranger_moves_the_worker_to_the_next_slot(self):
        self.drv.ports.base = [24100]
        self.drv.ports.busy = lambda p: p == 24100
        blk = self.drv.ports.allocate("w")
        self.assertEqual((blk.slot, blk.ports), (1, [24200]))


class Money(DriverCase):
    ITEMS = PLAN

    def worker(self, iid):
        return scheduler.Worker(self.drv, fake_slot(iid))

    def test_children_alive_together_are_never_promised_more_than_what_is_left(self):
        for i in ("R1", "R2"):
            self.drv.slots[i] = fake_slot(i)
        led = self.drv.ctx.ledger()
        w1, w2 = self.worker("R1"), self.worker("R2")
        a1 = self.drv.allowance_for(w1, led, "R1")
        a2 = self.drv.allowance_for(w2, led, "R2")
        self.assertEqual((a1, a2), (5.0, 5.0))                               # run_budget_usd 10 over two workers
        self.assertLessEqual(a1 + a2, 10.0)
        self.drv.release_allowance("R1")
        self.assertEqual(self.drv.allowance_for(w1, led, "R1"), 5.0)

    def test_a_third_child_waits_until_one_ends_rather_than_overspending(self):
        for i in ("R1", "R2", "R3"):
            self.drv.slots[i] = fake_slot(i)
        led = self.drv.ctx.ledger()
        self.drv.reserved.update({"R1": 5.0, "R2": 5.0})                     # two live children hold all of the 10
        got = []
        t = threading.Thread(target=lambda: got.append(self.drv.allowance_for(self.worker("R3"), led, "R3")))
        t.start()
        self.drv.release_allowance("R1")
        t.join(60)
        self.assertEqual(got, [3.3333])                                       # a third of what is left, which is what R2 has not been promised
        self.assertLessEqual(sum(self.drv.reserved.values()), 10.0)

    def test_a_drain_ends_the_wait(self):
        for i in ("R1", "R2"):
            self.drv.slots[i] = fake_slot(i)
        led = self.drv.ctx.ledger()
        self.drv.reserved.update({"R1": 10.0})
        errs = []

        def go():
            try:
                self.drv.allowance_for(self.worker("R2"), led, "R2")
            except scheduler._Drain as exc:
                errs.append(exc)
        t = threading.Thread(target=go)
        t.start()
        self.drv.start_drain("test")
        t.join(60)
        self.assertEqual(len(errs), 1)


class Cascading(DriverCase):
    ITEMS = PLAN

    def flip(self, iid, status="needs_human"):
        def mutate(st):
            sm._force_status(st, iid, status, "test", "a human reverted it", "human_revert" if status in sm.RESTING else None)
        self.drv.loop.store.update(mutate, "test: %s %s" % (iid, status))

    def test_an_item_whose_ancestor_was_reverted_is_put_back_and_its_work_kept(self):
        self.lr.quick_merge("Q1")
        q2 = self.stage("Q2")
        self.flip("Q1")
        self.drv.invalidate(self.drv.state())
        r = self.lr.item("Q2")
        self.assertEqual((r["status"], r["attempt"], r["branch"]), ("pending", 2, None))
        self.assertFalse(__import__("os").path.lexists(q2.path))
        self.assertIn("dead/slice-q2-a1", [b["name"] for b in branches.list_branches(self.lr.path)])
        self.assertEqual(self.drv.slots, {})
        self.assertEqual(self.drv.invalidating, set())
        self.drv.invalidate(self.drv.state())                                           # and nothing more happens
        self.assertEqual(self.lr.item("Q2")["attempt"], 2)

    def test_a_worker_still_running_is_cancelled_not_cut_off_and_the_item_is_reset_when_it_stops(self):
        self.lr.quick_merge("Q5")
        q3 = self.stage("Q3")
        q3.done.clear()                                                                  # its thread is still running
        self.flip("Q5")
        self.drv.invalidate(self.drv.state())
        self.assertTrue(q3.cancel.is_set())
        self.assertEqual(self.lr.item("Q3")["status"], "reviewing")                       # untouched until the worker stops
        self.assertIn("Q3", self.drv.slots)
        q3.result, q3.harvested = {"item": "Q3", "outcome": "cancelled"}, False
        q3.done.set()
        self.drv.harvest()
        self.assertEqual((self.lr.item("Q3")["status"], self.lr.item("Q3")["attempt"]), ("pending", 2))
        self.assertEqual(self.drv.slots, {})

    def test_an_item_invalidated_too_many_times_is_parked_for_a_human_instead_of_going_round_again(self):
        self.lr.quick_merge("Q6")
        self.stage("Q4")
        self.flip("Q6")
        self.drv.cascades["Q4"] = cascade.MAX_CASCADES
        self.drv.invalidate(self.drv.state())
        r = self.lr.item("Q4")
        self.assertEqual((r["status"], r["blockReason"]), ("needs_human", "cascade_loop"))
        self.assertTrue(self.drv.draining)
        self.assertEqual(self.drv.slots, {})


class Harvest(DriverCase):
    ITEMS = PLAN

    def ended(self, iid, **result):
        slot = self.stage(iid)
        slot.ready = False
        slot.result = dict(result, item=iid)
        slot.harvested = False
        return slot

    def test_a_worker_that_finished_its_item_is_ready_for_the_integrator(self):
        slot = self.ended("B1", outcome="ready")
        self.drv.harvest()
        self.assertTrue(slot.ready)
        self.assertIn("B1", self.drv.slots)

    def test_a_run_wide_stop_in_a_worker_is_handed_to_the_main_thread_and_starts_a_drain(self):
        stop = _Stop("budget", "ceiling", 1)
        slot = self.ended("B2", outcome="stop", stop=stop)
        self.drv.harvest()
        self.assertIs(self.drv.stop_exc, stop)
        self.assertTrue(self.drv.drain_event.is_set())
        self.assertNotIn("B2", self.drv.slots)                                              # an item left in flight is retired: worktree off, branch kept for recovery
        self.assertFalse(__import__("os").path.lexists(slot.path))
        self.assertEqual(self.lr.item("B2")["status"], "reviewing")
        self.assertIn("feat/slice-b2-a1", [b["name"] for b in branches.list_branches(self.lr.path)])

    def test_finding_a_lost_merge_never_touches_what_the_other_workers_are_doing(self):
        slot = self.stage("B3")
        self.drv.recover_merge()                                  # the serial driver's recover() would put every item in flight back to pending
        self.assertEqual(self.lr.item("B3")["status"], "reviewing")
        self.assertTrue(__import__("os").path.isdir(slot.path))
        self.assertEqual([r["name"] for r in self.drv.leases.list()], ["item-b3", "ports-0"])


if __name__ == "__main__":
    import unittest
    unittest.main()
