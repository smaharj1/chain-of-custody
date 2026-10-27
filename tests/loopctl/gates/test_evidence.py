"""Evidence (the aggregator) and gate discovery."""
from __future__ import annotations

import json
import os

from . import GateCase, g3_item

from loopctl import gates

EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})
UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n\n    @unittest.skip('known')\n    def test_two(self):\n        pass\n"


class EvidenceGate(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g0: report\n  g2: report\n"

    def record(self, head, **statuses):
        gs = {}
        for name, status in statuses.items():
            gs[name] = {"status": status, "head": head, "at": "2026-01-01T00:00:00Z"}
        return {"status": "reviewing", "attempt": 1, "gates": gs, "ac": {"AC-A-1": {"g3": {"result": "pass"}}}, "review": {"code": "CODE_REVIEW_APPROVED"}}

    ON = ("g1", "g3", "g5", "g6", "g7", "g9", "g11")

    def test_all_gates_recorded_for_this_head_pass_and_the_local_file_is_written(self):
        h = self.head()
        res = self.run_gate("evidence", state=self.record(h, **{n: "pass" for n in self.ON}))
        self.assertPass(res)
        self.assertIn("7 gate(s) recorded", res["evidence"]["summary"])
        from loopctl.gates import _lib
        path = os.path.join(_lib.runstate_of(self.ctx()).root, "evidence", "A-a1.json")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["head"], doc["base"], doc["ci"]), (h, self.base, "n/a"))
        self.assertEqual(len(doc["digest"]), 64)
        self.assertIn("evidence.local-only", self.ids(res))

    def test_a_gate_that_never_ran_or_failed_is_missing(self):
        h = self.head()
        st = self.record(h, **{n: "pass" for n in self.ON if n != "g6"})
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        st = self.record(h, **{n: ("fail" if n == "g5" else "pass") for n in self.ON})
        res = self.run_gate("evidence", state=st)
        self.assertFails(res, "evidence.gate-missing")
        self.assertIn("g5", json.dumps(res["findings"]))

    def test_a_pass_for_an_older_head_is_stale(self):
        old = self.head()
        self.commit({"src/x.py": "x = 1\n"}, "more work")
        res = self.run_gate("evidence", state=self.record(old, **{n: "pass" for n in self.ON}))
        self.assertFails(res, "evidence.stale")

    def test_pre_build_gates_judged_the_start_and_only_need_to_have_passed(self):
        old = self.head()
        self.commit({"src/x.py": "x = 1\n"}, "more work")
        st = self.record(self.head(), **{n: "pass" for n in self.ON})
        st["gates"]["g0"] = {"status": "pass", "head": old}
        st["gates"]["g2"] = {"status": "pass", "head": old}
        self.assertPass(self.run_gate("evidence", state=st))

    def test_gates_in_report_or_off_mode_are_not_required(self):
        h = self.head()
        st = self.record(h, **{n: "pass" for n in self.ON})
        self.assertNotIn("g0", st["gates"])
        self.assertPass(self.run_gate("evidence", state=st))


class EvidenceWithG4(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g0: report\n  g2: report\n  g4: \"on\"\n"

    def test_a_mutation_gate_that_skipped_because_nothing_needed_mutating_is_not_missing(self):
        h = self.head()
        st = {"status": "reviewing", "attempt": 1, "gates": {n: {"status": "pass", "head": h} for n in EvidenceGate.ON}}
        st["gates"]["g4"] = {"status": "skipped", "head": h}
        self.assertPass(self.run_gate("evidence", state=st))
        st["gates"]["g4"] = {"status": "fail", "head": h}
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        del st["gates"]["g4"]
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        st["gates"]["g1"] = {"status": "skipped", "head": h}                  # only G4 may skip
        st["gates"]["g4"] = {"status": "pass", "head": h}
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")


class EvidenceCI(GateCase):
    ITEMS = [g3_item("A")]
    DELIVERY = "draft-pr"
    CFG = "gates:\n" + "".join("  %s: off\n" % g for g in ("g0", "g1", "g2", "g3", "g5", "g6", "g7", "g9", "g11"))

    def state(self, overlay, status="pr_open"):
        return {"status": status, "attempt": 1, "gates": {}, "overlay": overlay}

    def test_from_pr_open_the_check_run_is_the_gate(self):
        green = {"pr": 7, "state": "OPEN", "draft": True, "checks": "pass", "url": "https://github.com/x/y/pull/7"}
        res = self.run_gate("evidence", state=self.state(green))
        self.assertPass(res)
        self.assertIn("ci pass", res["evidence"]["summary"])
        for checks in ("fail", "pending"):
            self.assertFails(self.run_gate("evidence", state=self.state(dict(green, checks=checks))), "evidence.ci-not-green")

    def test_no_recorded_check_state_is_not_a_pass(self):
        self.assertFails(self.run_gate("evidence", state=self.state(None)), "evidence.ci-unknown")

    def test_before_the_pr_exists_there_is_no_ci_to_wait_for(self):
        self.assertPass(self.run_gate("evidence", state=self.state(None, status="reviewing")))


class Discovery(GateCase):
    ITEMS = [g3_item("A")]

    def test_every_gate_of_the_phase_is_installed_with_the_contract(self):
        self.assertEqual(gates.installed(), ["evidence", "g0", "g1", "g11", "g2", "g3", "g4", "g5", "g6", "g7", "g9"])
        for name in gates.installed():
            mod = gates.get(name)
            self.assertTrue(callable(mod.run))
            self.assertTrue(mod.OWNER_METRIC)
            self.assertEqual(mod.NAME, name)

    def test_g4_is_the_only_off_by_default_evidence_gated_gate(self):
        for name in gates.installed():
            mod = gates.get(name)
            self.assertEqual(getattr(mod, "EVIDENCE_GATED", False), name == "g4", name)
            self.assertEqual(getattr(mod, "DEFAULT_MODE", "on"), "off" if name == "g4" else "on", name)

    def test_required_transitions(self):
        want = {"g0": [("designing", "design_review")], "g2": [("design_review", "building"), ("stubbing", "building")],
                "g1": [("verifying", "reviewing")], "g3": [("verifying", "reviewing")], "g5": [("verifying", "reviewing")],
                "g6": [("verifying", "reviewing")], "g9": [("verifying", "reviewing")], "g11": [("verifying", "reviewing")]}
        for name, trans in want.items():
            self.assertEqual(list(gates.get(name).REQUIRED_FOR), trans, name)
        self.assertIn(("reviewing", "pr_open"), gates.get("g7").REQUIRED_FOR)
        self.assertIn(("awaiting_human", "merged"), gates.get("evidence").REQUIRED_FOR)
        self.assertEqual(getattr(gates.get("g4"), "REQUIRED_FOR", ()), ())


if __name__ == "__main__":
    import unittest
    unittest.main()
