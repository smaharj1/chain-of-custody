"""The gates inside loopctl: `advance` enforces REQUIRED_FOR, `gate` records, `status --matrix` shows, the registry refuses the OPT-IN keys."""
from __future__ import annotations

import json
import os
import sys

from .. import LoopTestCase, item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, STUB, g3_item

from loopctl.common import Refused
from kitlib import config as kconfig, registry

CFG = ("verify_tests: \"%s -m unittest discover -v -s tests -t .\"\nsecurity_review: \"off\"\ndb_local: sqlite\n" % sys.executable)
UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n"

CODE = """# Code Review - slice-a (iteration 1)

## Verdict
CODE_REVIEW_APPROVED

## Summary
- AC-A-1 and AC-A-2 are proved by the acceptance tests.

## Verification Run
- python3 -m unittest: PASS - 2 tests

## Files Reviewed
- src/calc.py
"""


class FullWalk(LoopTestCase):
    """One item through every gate with the real implementations, the way the conductor would drive it at L1."""

    def test_the_item_cannot_move_forward_until_its_gates_have_passed_for_this_head(self):
        lr = self.lr(items=[g3_item("A")], extra_cfg=CFG, real_gates=True)
        lr.repo.write(".gitignore", "__pycache__/\n*.pyc\n")
        lr.repo.write("tests/__init__.py", "")
        lr.repo.write("tests/unit/__init__.py", "")
        lr.repo.write("tests/unit/test_u.py", UNIT)
        lr.commit_all("an existing suite, and ignore bytecode")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        r = lr.repo
        r.git("switch", "-q", "-c", "feat/slice-a-a1", "main")

        def gate(name, expect_rc=0):
            res = lr.c("gate", name, "A")
            self.assertEqual(res.rc, expect_rc, "%s: %s %s" % (name, res.out, res.err))
            return res

        def advance(to, **kw):
            lr.loop(fresh=True).advance("A", to, lr.session, **kw)

        # designing -> design_review needs G0
        with self.assertRaises(Refused) as cm:
            advance("design_review")
        self.assertIn("gate g0 must pass", str(cm.exception))
        gate("g0")
        advance("design_review")

        # the acceptance author stubs and writes the tests; building needs G2
        lr.repo.write("src/calc.py", STUB)
        for k, v in PKG.items():
            lr.repo.write(k, v)
        lr.commit_all("stubs")
        lr.repo.write("tests/acceptance/test_a.py", ACCEPTANCE_OK)
        lr.commit_all("acceptance tests")
        advance("stubbing")
        with self.assertRaises(Refused) as cm:
            advance("building")
        self.assertIn("gate g2 must pass", str(cm.exception))
        gate("g2")
        advance("building")

        # the engineer builds; verifying -> reviewing needs g1 g3 g5 g6 g9 g11
        lr.repo.write("src/calc.py", IMPL)
        lr.commit_all("implementation")
        advance("verifying")
        head = r.head()
        for blocked in ("g1", "g3", "g5", "g6", "g9", "g11"):
            with self.assertRaises(Refused, msg=blocked):
                advance("reviewing", head=head)
            gate("g6" if blocked == "g1" else blocked)       # g1 reads g6's and g3's results: run g6 first, then g3, then g1
        gate("g1")
        advance("reviewing", head=head)

        # reviewing -> merged needs G7 and Evidence
        lr.repo.write("docs/features/slice-a/reports/code-review.md", CODE)
        lr.commit_all("code review saved")
        head = r.head()
        gate("g7")
        r.git("switch", "-q", "main")
        r.git("merge", "--no-ff", "-q", "-m", "Merge branch 'feat/slice-a-a1'\n\nLoop-Item: A\nLoop-Attempt: 1", "feat/slice-a-a1")
        merge = r.head()
        with self.assertRaises(Refused) as cm:
            advance("merged", merge_commit=merge, head=head)
        self.assertIn("gate evidence must pass", str(cm.exception))
        r.git("switch", "-q", "feat/slice-a-a1")
        gate("evidence")
        r.git("switch", "-q", "main")
        advance("merged", merge_commit=merge, head=head)
        st = lr.item("A")
        self.assertEqual(st["status"], "merged")
        self.assertEqual({n: g["status"] for n, g in st["gates"].items()},
                         {"g0": "pass", "g2": "pass", "g1": "pass", "g3": "pass", "g5": "pass", "g6": "pass", "g9": "pass", "g11": "pass", "g7": "pass", "evidence": "pass"})
        self.assertEqual(st["review"], {"code": "CODE_REVIEW_APPROVED"})
        m = lr.ctl("status", "--matrix")
        self.assertIn("AC-A-1", m.out)
        self.assertIn("coverage: 2/2", m.out)
        for col in ("g1", "g3", "g6"):
            self.assertIn(col, m.out.splitlines()[0])


class Registry(LoopTestCase):
    def test_the_opt_in_keys_are_registered_and_evidence_gated(self):
        reg = registry.load_registry()
        for name in ("gates.g4", "g5_ast_lint"):
            self.assertIn(name, reg.evidence_gated_keys(), name)
        for name in ("heldout_cmd", "mutation_cmd", "mutation_min", "mutation_timeout_s", "env_check_cmd", "migrate_check_cmd", "dep_check_cmd",
                     "license_allowlist", "secrets_scan_cmd"):
            k = reg.get(name)
            self.assertIsNotNone(k, name)
            self.assertTrue(k.h6_protected, name)
            self.assertEqual(k.owner_phase, "4b-gates")
            self.assertFalse(k.evidence_gated, name)
        self.assertEqual(reg.get("gates.g4").default, "off")
        self.assertEqual(reg.get("mutation_min").default, 60)

    def test_switching_g4_or_the_ast_lint_on_is_refused_until_the_evidence_gate_passes(self):
        reg = registry.load_registry()
        for text, key in (("gates:\n  g4: on\n", "gates.g4"), ("gates:\n  g4: report\n", "gates.g4"), ("g5_ast_lint:\n  - python\n", "g5_ast_lint")):
            cfg = kconfig.load_text("```yaml\n%s```\n" % text, reg)
            self.assertEqual(cfg.evidence_gated_violations(), [key], text)
        cfg = kconfig.load_text("```yaml\ngates:\n  g4: off\n```\n", reg)
        self.assertEqual(cfg.evidence_gated_violations(), [])

    def test_loopctl_refuses_tier2_and_validate_reports_it(self):
        lr = self.lr(extra_cfg="gates:\n  g4: on\n", real_gates=True)
        r = lr.ctl("validate")
        self.assertIn("config.evidence-gated", r.out)
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("gates.g4", r.err)

    def test_the_generated_config_lists_the_new_keys(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".claude", "loop.config.md")) as fh:
            text = fh.read()
        for k in ("g4: off", "g5_ast_lint: []", "heldout_cmd:", "mutation_min: 60", "migrate_check_cmd:", "dep_check_cmd:", "secrets_scan_cmd:"):
            self.assertIn(k, text)


class CliGate(LoopTestCase):
    def test_gate_records_the_result_the_status_matrix_shows_and_json_is_printed(self):
        lr = self.lr(items=[item_doc("A", acs=2, ext={"g3": {"exempt": "docs only"}})], extra_cfg=CFG, real_gates=True)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        r = lr.c("gate", "g3", "A", "--json")
        self.assertEqual(r.rc, 0, r)
        doc = r.json
        self.assertEqual((doc["name"], doc["status"], doc["mode"]), ("g3", "pass", "on"))
        self.assertEqual(lr.item("A")["gates"]["g3"]["status"], "pass")
        r = lr.c("gate", "g1", "A", "--json")
        self.assertEqual(r.json["status"], "pass")                      # an exempt item: nothing to trace, flagged for a human
        self.assertIn("g1.exempt", [f["id"] for f in r.json["findings"]])

    def test_a_failing_gate_exits_1_and_blocks(self):
        lr = self.lr(items=[item_doc("A", acs=2)], extra_cfg=CFG, real_gates=True)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        r = lr.c("gate", "g2", "A")
        self.assertEqual(r.rc, 1, r)
        self.assertIn("BLOCKING", r.out)
        lr.ctl("gate", "g2", "A")


if __name__ == "__main__":
    import unittest
    unittest.main()
