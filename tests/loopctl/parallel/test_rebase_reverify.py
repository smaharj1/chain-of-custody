"""After a parallel rebase every gate Evidence checks runs again on the new head (review fixes D4, GT-1, GT-11).

The stubs in `test_integrate.py` carry no `REQUIRED_FOR` and a stub Evidence, so they could not see that a rebased item failed Evidence as stale. Here the
other gates are stubs that record what they judged, but each carries its REAL `REQUIRED_FOR` and Evidence is the REAL gate (`gates/evidence_gate.py`),
which wants every gate that is `on` to have passed for the head it judges.
"""
from __future__ import annotations

import os

from loopctl import driver, gates
from loopctl.gates import GateResult, evidence_gate

from . import DriverCase, pitem

ITEMS = [pitem(i) for i in ("RA", "RB", "RC", "RD", "RM", "RN", "RO", "RP")]
OFF = {"g0": "off", "g2": "off"}                         # the pre-build gates have no recorded pass in a staged item


class Base(DriverCase):
    ITEMS = ITEMS
    CFG = {"gates": OFF}

    def setUp(self) -> None:
        self.lr.stubs.attrs.clear()                       # the class shares one repo and one stub set: start every test unarmed
        super(Base, self).setUp()
        self.addCleanup(self.lr.stubs.attrs.clear)

    def arm(self) -> None:
        """Called after the items are staged (staging steps through verifying -> reviewing without recorded gates): from here the stubs carry their
        real REQUIRED_FOR and Evidence is the real gate."""
        stubs = self.lr.stubs
        for n in gates.KNOWN_GATES:
            real = getattr(__import__("loopctl.gates." + MODULES[n], fromlist=["x"]), "REQUIRED_FOR", ()) if n in MODULES else ()
            stubs.attrs[n] = {"REQUIRED_FOR": real} if real else {}
        stubs.install()
        gates.register(evidence_gate)

    def verified(self, iid, **kw):
        """A finished worker whose gates have run at its head, as a real worker leaves it."""
        slot = self.stage(iid, **kw)
        self.arm()
        res = slot.worker.run_gates(iid, tuple(driver.VERIFY_GATES) + tuple(driver.REVIEW_GATES))
        self.assertFalse([n for n, r in res if r["blocking"]], res)
        return slot

    def recorded(self, iid):
        return self.lr.item(iid)["gates"]


MODULES = {"g1": "g1_acids", "g3": "g3_acceptance", "g5": "g5_integrity", "g6": "g6_verify", "g7": "g7_reviews", "g9": "g9_supply", "g11": "g11_secrets",
           "docs": "docs_delta", "evidence": "evidence_gate"}


class Rebased(Base):
    def test_a_rebased_item_has_every_gate_fresh_at_its_new_head_and_passes_evidence(self):
        a = self.stage("RA")
        b = self.stage("RB")
        self.arm()
        for slot in (a, b):
            slot.worker.run_gates(slot.iid, tuple(driver.VERIFY_GATES) + tuple(driver.REVIEW_GATES))
        old_head = self.lr.repo.git("-C", b.path, "rev-parse", "HEAD")
        self.assertIs(self.drv.integrate_one(), True)                         # RA: no rebase
        self.assertEqual(self.lr.order_merged(self.start_tip), ["RA"])
        self.lr.stubs.calls = []
        self.assertIs(self.drv.integrate_one(), True)                         # RB: rebased onto RA's merge, re-verified, merged
        self.assertEqual(self.lr.order_merged(self.start_tip), ["RA", "RB"])
        rebased = [c for c in self.lr.stubs.calls if c["item"] == "RB" and c["gate"] != "evidence"]
        ran = [c["gate"] for c in rebased]
        for g in list(driver.VERIFY_GATES) + list(driver.REVIEW_GATES):       # every verify gate and g7 ran again, in the item's own worktree
            self.assertIn(g, ran)
        self.assertEqual({c["worktree"] for c in rebased}, {b.path})
        self.assertNotIn(old_head, {c["head"] for c in rebased})
        self.assertEqual(len({c["head"] for c in rebased}), 1)                # all judged the same, new, head
        self.assertEqual(self.lr.item("RB")["status"], "merged")
        self.assertEqual(self.lr.item("RB")["gates"]["evidence"]["status"], "pass")

    def test_without_the_rerun_evidence_would_have_called_the_rebased_head_stale(self):
        """The failure the review found, shown directly: gates recorded for the old head do not satisfy Evidence at the new one."""
        b = self.verified("RC")
        self.lr.repo.commit_file("src/other.txt", "moved\n", "main moves")
        self.lr.repo.git("-C", b.path, "rebase", "--quiet", "main")
        new_head = self.lr.repo.git("-C", b.path, "rev-parse", "HEAD")
        gctx = self.drv.ctx.gate_context("RC", new_head, b.path, self.lr.item("RC"), None)
        res = gates.run_gate("evidence", gctx)
        self.assertTrue(res["blocking"])
        self.assertIn("evidence.stale", {f["id"] for f in res["findings"]})

    def test_a_gate_that_fails_only_on_the_rebased_code_sends_the_item_back_and_is_recorded(self):
        self.verified("RD", files={"src/rd/m.py": "x = 1\n"})
        self.lr.repo.commit_file("src/rd/clash.txt", "from main\n", "main adds a file the item's tests dislike")
        # g6 fails exactly when the merged-in file is present: green on the old code, red on the rebased tree
        slot = self.drv.slots["RD"]
        self.lr.stubs.results["g6"] = lambda ctx: GateResult.failed("breaks with main") if os.path.exists(os.path.join(ctx.worktree, "src", "rd", "clash.txt")) \
            else GateResult.passed(note="stub")
        tip = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)           # not merged
        r = self.lr.item("RD")
        self.assertNotEqual(r["status"], "merged")
        self.assertEqual(r["gates"]["g6"]["status"], "fail")                   # the failure is on the record
        self.assertNotEqual(r["gates"]["g6"]["head"], self.lr.item("RD")["headSha"])        # recorded at the rebased head, not the old one


class MergeCommits(Base):
    def test_an_item_branch_that_holds_a_merge_commit_is_parked_not_rebased(self):
        m = self.stage("RM")
        git = lambda *a: self.lr.repo.git("-C", m.path, *a)       # noqa: E731
        br = git("rev-parse", "--abbrev-ref", "HEAD")
        git("switch", "-q", "-c", "side")
        put_file = os.path.join(m.path, "src", "rm", "side.txt")
        with open(put_file, "w") as fh:
            fh.write("side\n")
        git("add", "-A")
        git("commit", "-q", "-m", "side work")
        git("switch", "-q", br)
        git("merge", "--no-ff", "-q", "-m", "Merge branch 'side'", "side")      # a conflict resolution could hide in a merge commit like this
        self.lr.repo.commit_file("src/elsewhere.txt", "main moved\n", "main moves")
        tip = self.lr.repo.git("rev-parse", "main")
        self.assertIs(self.drv.integrate_one(), True)
        self.assertEqual(self.lr.repo.git("rev-parse", "main"), tip)
        r = self.lr.item("RM")
        self.assertEqual((r["status"], r["blockReason"]), ("needs_human", "ESCALATION_NEEDED"))
        self.assertIn("rebases linear history only", r["history"][-1]["note"])


if __name__ == "__main__":
    import unittest
    unittest.main()
