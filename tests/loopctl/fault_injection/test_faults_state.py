"""Writes that race the driver, and a human who stops it (report 10.3, 10.7, RC-28, CS-19; FI-10 CAS conflict, FI-11 abort mid-item).

Every state write is a compare-and-swap that re-applies its mutation to the fresh state when it loses. A competing write that does not
matter changes nothing for the driver; one that makes the driver's step meaningless stops the driver and the human's write stands.
"""
from __future__ import annotations

import os
import threading
import time
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ...helpers.procs import wait_for

from kitlib import runstate
from loopctl import abort, state as state_mod

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Cas(DriverTestCase):
    def race(self, lr, compete, fire):
        """Before a compare-and-swap for which `fire(committed state)` is true, run `compete()`: a competing write by someone else."""
        orig = state_mod.StateStore.__init__
        busy = {"on": False}

        def init(store, *a, **k):
            orig(store, *a, **k)

            def hook():
                if busy["on"]:
                    return
                busy["on"] = True
                try:
                    st, _ = lr.context().store().read()
                    if st is not None and fire(st):
                        compete()
                finally:
                    busy["on"] = False
            store._before_cas = hook
        state_mod.StateStore.__init__ = init
        self.addCleanup(setattr, state_mod.StateStore, "__init__", orig)

    def test_a_competing_write_that_does_not_matter_is_retried_over_and_nothing_is_lost(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        n = {"writes": 0}
        other = lr.loop(fresh=True)                                                          # another process's Loop, built once

        def compete():
            n["writes"] += 1
            other.approve("cost", "cost", "Pat", note="race %d" % n["writes"])
        self.race(lr, compete, lambda st: st.items["A"]["status"] in ("designing", "building", "verifying") and n["writes"] < 3)
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(n["writes"], 3)
        st = lr.state()
        self.assertEqual(st.items["A"]["status"], "merged")
        notes = [a["note"] for a in st.d["approvals"] if a["scope"] == "cost"]
        self.assertEqual(notes, ["race 1", "race 2", "race 3"])                               # the competing writes all survived
        hist = [(h["from"], h["to"]) for h in st.items["A"]["history"] if h["from"] != h["to"]]
        self.assertEqual(hist.count(("pending", "designing")), 1)                             # and the driver's own steps happened exactly once each
        self.assertEqual(hist.count(("building", "verifying")), 1)
        seqs = [h["seq"] for h in st.items["A"]["history"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(lr.state().d["seq"], len(lr.repo.git("rev-list", "refs/loopctl/state/demo").splitlines()))   # a linear chain: no write lost, none duplicated

    def test_a_competing_abort_makes_the_drivers_step_meaningless_and_the_abort_stands(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        done = []

        loop = lr.loop(fresh=True)

        def compete():
            done.append(1)
            st, _ = loop.store.read()
            abort.execute(loop, abort.plan_abort(loop.ctx, st, loop.plan, None), "Pat", "stop in the middle of a step", None)
        self.race(lr, compete, lambda st: st.items["A"]["status"] == "building" and not done)
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"]), ("blocked", "aborted"))                   # the human's write stands
        self.assertEqual(lr.state().header["runStatus"], "aborted")
        self.assertEqual([c["role"] for c in lr.calls()], ["backend-engineer"])                       # nothing was launched on a stale view
        self.assertEqual(lr.item("B")["status"], "pending")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")                              # settled on the way out
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        self.assertFalse(lr.rs.lock_held())
        self.assertIn("not a forward step", r.out)

    def test_a_state_that_keeps_moving_exhausts_the_retries_and_changes_nothing(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        k = {"n": 0}
        other = lr.loop(fresh=True)

        def compete():
            k["n"] += 1
            other.approve("cost", "cost", "Pat", note="again %d" % k["n"])
        self.race(lr, compete, lambda st: st.items["A"]["status"] == "building")
        r = lr.run()
        self.assertEqual(r.rc, 3, r)
        self.assertIn("moved under this command 8 times", r.out)
        self.assertEqual(lr.item("A")["status"], "building")                                           # the step never landed
        self.assertEqual(k["n"], 8)


class Stop(DriverTestCase):
    def stalled_run(self, lr, seconds=60):
        pidfile = os.path.join(lr.home, "child.pid")
        lr.set_scenario(lr.happy(by_role={"backend-engineer": [{"behavior": "stall", "seconds": seconds, "pidfile": pidfile}, lr.eng_run("A")]}))
        lr.approve()
        return pidfile

    def after_child_starts(self, pidfile, action):
        def go():
            wait_for(lambda: os.path.exists(pidfile) and open(pidfile).read().strip(), 10)
            action()
        t = threading.Thread(target=go)
        t.start()
        return t

    def test_halt_mid_item_kills_the_child_stops_the_run_and_resume_lets_it_finish(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)], timing={"stall_s": 60})
        pidfile = self.stalled_run(lr)
        t = self.after_child_starts(pidfile, lambda: lr.rs.set_halt("stop now", "Pat"))
        t0 = time.time()
        r = lr.run()
        t.join()
        self.assertEqual(r.rc, 3, r)
        self.assertLess(time.time() - t0, 8)
        self.assertIn("HALT is set (stop now)", r.out)
        with open(pidfile) as fh:
            pid = int(fh.read())
        self.assertTrue(wait_for(lambda: not runstate.pid_alive(pid), 3))
        self.assertTrue(lr.rs.is_halted())                                                             # the driver never clears HALT: that is the human's
        self.assertFalse(lr.rs.lock_held())
        self.assertEqual(lr.children_left(), [])
        self.assertEqual(lr.item("A")["status"], "building")
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        r = lr.run()
        self.assertEqual(r.rc, 3, r)                                                                   # refused while HALT stands
        self.assertIn("HALT", r.err)
        self.assertEqual(lr.ctl("resume").rc, 0)
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["status"], "merged")

    def test_abort_mid_item_stops_the_driver_and_nothing_merges_afterwards(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", ["A"], estimate=ENG)], timing={"stall_s": 60})
        pidfile = self.stalled_run(lr)

        def do_abort():
            loop = lr.loop(fresh=True)
            st, _ = loop.store.read()
            abort.execute(loop, abort.plan_abort(loop.ctx, st, loop.plan, None), "Pat", "the plan is wrong", None)
        t = self.after_child_starts(pidfile, do_abort)
        r = lr.run()
        t.join()
        self.assertEqual(r.rc, 3, r)
        with open(pidfile) as fh:
            pid = int(fh.read())
        self.assertTrue(wait_for(lambda: not runstate.pid_alive(pid), 3))
        st = lr.state()
        self.assertEqual((st.header["runStatus"], st.items["A"]["status"], st.items["A"]["blockReason"]), ("aborted", "blocked", "aborted"))
        self.assertEqual(st.items["B"]["status"], "pending")
        self.assertTrue(lr.rs.is_halted())
        self.assertEqual(lr.repo.git("log", "--merges", "--format=%H", "main"), "")
        self.assertEqual(lr.children_left(), [])
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertEqual(lr.run().rc, 3)                                                               # and it does not start again by itself
        self.assertEqual(len(lr.calls()), 1)
        self.assertTrue(any(b.startswith("blocked/") for b in lr.branch_names()))                      # abort keeps stopped work as blocked/*


if __name__ == "__main__":
    unittest.main()
