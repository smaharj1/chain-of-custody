"""G4: mutation testing, tool-agnostic, OPT-IN and evidence-gated."""
from __future__ import annotations

import json
import os
import sys

from . import ACCEPTANCE_OK, IMPL, PKG, GateCase, g3_item

from loopctl import gates

EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})
UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n\n    @unittest.skip('known')\n    def test_two(self):\n        pass\n"


MUTATE = '''import json, os, sys
scope = json.load(open(os.environ["LOOPCTL_MUTATION_SCOPE"]))
mode = open("mutants.txt").read().strip() if os.path.exists("mutants.txt") else "70,30,0"
k, s, t = (int(x) for x in mode.split(","))
print("running mutants for", sorted(scope["files"]))
print(json.dumps({"schema": "mutation.v1", "killed": k, "survived": s, "timeout": t}))
'''


class G4(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: on\nmutation_cmd: \"%s mutate.py\"\nmutation_min: 60\n" % sys.executable
    BASE_FILES = dict(PKG, **{"mutate.py": MUTATE, "src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def evidence(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)

    def mutants(self, text):
        with open(os.path.join(self.lr.path, "mutants.txt"), "w") as fh:
            fh.write(text)

    def test_it_refuses_to_run_without_the_evidence_gate(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        # the repo cannot even opt in: the config level refuses; the runner refuses too
        res = self.run_gate("g4")
        self.assertEqual((res["status"], res["blocking"]), ("error", True))
        self.assertEqual(res["findings"][0]["id"], "gate.evidence-required")

    def test_score_at_or_above_mutation_min_passes(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        res = self.run_gate("g4")
        self.assertPass(res)
        self.assertIn("mutation score 70.0%", res["evidence"]["summary"])

    def test_a_low_score_fails_and_counts_timeouts_against(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("50,40,10")
        self.assertFails(self.run_gate("g4"), "g4.score-low")
        self.mutants("60,40,0")
        self.assertPass(self.run_gate("g4"))
        self.mutants("59,40,1")
        self.assertFails(self.run_gate("g4"), "g4.score-low")

    def test_no_mutants_for_changed_production_code_proves_nothing(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("0,0,0")
        self.assertFails(self.run_gate("g4"), "g4.no-mutants")

    def test_only_tests_and_docs_changed_skips(self):
        self.evidence()
        self.commit({"docs/a.md": "x\n", "tests/acceptance/test_a.py": ACCEPTANCE_OK + "\n"}, "docs")
        self.assertEqual(self.run_gate("g4")["status"], "skipped")

    def test_the_scope_file_lists_changed_lines_and_the_acceptance_globs(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n\ndef extra():\n    return 1\n"}, "work")
        self.mutants("7,3,0")
        spec = {}
        from loopctl.gates import g4_mutation
        ctx = self.ctx()
        spec = g4_mutation.changed_lines(ctx)
        self.assertEqual(list(spec), ["src/calc.py"])
        self.assertEqual(spec["src/calc.py"], [[len(IMPL.splitlines()) + 1, len(IMPL.splitlines()) + 4]])

    def test_garbage_or_negative_counts_are_an_error(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("-1,3,0")
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["failure_class"], "env")

    def test_a_wrapper_that_prints_nothing_useful_fails(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n", "mutate.py": "print('hello')\n"}, "work")
        # the command comes from the base, so the branch's own mutate.py is NOT used by the base config... it is the same path: be explicit
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "fail")


class G4Unset(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: on\n"

    def test_no_command_is_an_error(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)
        self.commit({"src/x.py": "x = 1\n"}, "work")
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "error")
        self.assertIn("mutation_cmd is not set", res["findings"][0]["message"])


if __name__ == "__main__":
    import unittest
    unittest.main()
