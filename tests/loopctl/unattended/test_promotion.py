"""The promotion bar for L4 (report 6.3): server-side facts only, advice only, and never without the evidence gate."""
from __future__ import annotations

import json
import os
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from .. import LoopRepo

from loopctl import promotion

from . import UnattendedRepo


def pr(n, **kw):
    d = {"number": n, "merged": True, "level": "L3", "reverted": False, "correctness_changes_requested": 0, "test_integrity_flags": 0}
    d.update(kw)
    return d


def facts(prs=None, **kw):
    d = {"schema": promotion.SCHEMA, "source": "github", "collected_by": "Pat", "collected_at": "2026-10-20T10:00:00Z",
         "prs": prs if prs is not None else [pr(n) for n in range(1, 13)], "eval_pass_k": {"suite": "safety", "k": 3, "passed": True}}
    d.update(kw)
    return d


BAR = promotion.Bar()


class Evaluate(unittest.TestCase):
    def ev(self, f, evidence=True, bar=BAR):
        return promotion.evaluate(promotion.parse_facts(json.dumps(f)), bar, evidence)

    def test_ten_clean_draft_prs_and_the_evidence_gate_are_eligible(self):
        res = self.ev(facts())
        self.assertEqual((res["eligible"], res["reasons"]), (True, []))
        self.assertEqual(res["stats"]["window"], 10)
        self.assertEqual(res["bar"], {"min_prs": 10, "max_correctness_changes": 1, "max_reverts": 0, "max_integrity_flags": 0})

    def test_each_unmet_condition_is_named(self):
        cases = {
            "consecutive merged": facts([pr(n) for n in range(1, 9)]),
            "correctness changes": facts([pr(n, correctness_changes_requested=1 if n > 10 else 0) for n in range(1, 13)] + [pr(13, correctness_changes_requested=1)]),
            "reverted": facts([pr(n, reverted=(n == 12)) for n in range(1, 13)]),
            "test-integrity": facts([pr(n, test_integrity_flags=1 if n == 12 else 0) for n in range(1, 13)]),
            "pass^k": facts(eval_pass_k={"suite": "safety", "k": 3, "passed": False}),
        }
        for needle, f in cases.items():
            with self.subTest(needle):
                res = self.ev(f)
                self.assertFalse(res["eligible"])
                self.assertTrue(any(needle in r for r in res["reasons"]), res["reasons"])
        self.assertTrue(any("pass^k" in r for r in self.ev({k: v for k, v in facts().items() if k != "eval_pass_k"})["reasons"]))

    def test_an_unmerged_pr_ends_the_streak_and_other_levels_do_not_count(self):
        prs = [pr(n) for n in range(1, 10)] + [pr(10, merged=False)] + [pr(n) for n in range(11, 15)]
        res = self.ev(facts(prs))
        self.assertEqual(res["stats"]["consecutive_merged"], 4)
        self.assertFalse(res["eligible"])
        prs = [pr(n, level="L1") for n in range(1, 40)]                                        # supervised interactive PRs are not evidence for L4
        self.assertEqual(self.ev(facts(prs))["stats"]["counted_prs"], 0)
        self.assertEqual(self.ev(facts([pr(n, level="L2" if n % 2 else "L3") for n in range(1, 11)]))["eligible"], True)

    def test_without_the_evidence_gate_nothing_is_eligible_however_good_the_facts(self):
        res = self.ev(facts(), evidence=False)
        self.assertFalse(res["eligible"])
        self.assertIn("evidence gate", res["reasons"][0])

    def test_facts_that_do_not_come_from_the_server_are_refused(self):
        for src in ("events", "loopctl", "local", "attested", "", "GitHub", None):
            res = self.ev(facts(source=src))
            self.assertFalse(res["eligible"], src)
            self.assertTrue(any("forgeable" in r for r in res["reasons"]), src)
        self.assertTrue(self.ev(facts(source="ci"))["eligible"])

    def test_the_bar_comes_from_the_config_and_can_be_stricter(self):
        res = self.ev(facts(), bar=promotion.Bar(min_prs=12, max_correctness=0, max_reverts=0, max_integrity=0))
        self.assertTrue(res["eligible"])
        self.assertFalse(self.ev(facts(), bar=promotion.Bar(min_prs=13))["eligible"])

    def test_the_parser_is_strict(self):
        good = json.dumps(facts())
        promotion.parse_facts(good)
        for bad in ("not json", "[]", json.dumps(dict(facts(), schema="other")), json.dumps(dict(facts(), extra=1)), json.dumps(dict(facts(), prs="x")),
                    json.dumps(facts([dict(pr(1), merged="yes")])), json.dumps(facts([dict(pr(1), level="L9")])), json.dumps(facts([dict(pr(1), number=-1)])),
                    json.dumps(facts([dict(pr(1), unknown=1)])), json.dumps(facts([dict(pr(1), correctness_changes_requested=True)])),
                    good.replace('"source": "github"', '"source": "github", "source": "ci"'), '{"schema": NaN}'):
            with self.subTest(bad[:50]):
                with self.assertRaises(ValueError):
                    promotion.parse_facts(bad)


class Cli(unittest.TestCase):
    def facts_file(self, lr, f):
        p = os.path.join(lr.path, "facts.json")
        with open(p, "w") as fh:
            json.dump(f, fh)
        return "facts.json"

    def test_without_evidence_the_cli_says_not_yet_and_changes_nothing(self):
        lr = LoopRepo()
        self.addCleanup(lr.cleanup)
        before = lr.repo.git("status", "--porcelain")
        r = lr.ctl("unattended", "promotion", "--facts", self.facts_file(lr, facts()), "--json")
        self.assertEqual(r.rc, 1, r)
        self.assertFalse(r.json["eligible"])
        self.assertIn("evidence gate", r.json["reasons"][0])
        self.assertIn("nothing was changed", r.json["note"])
        self.assertEqual(lr.repo.git("status", "--porcelain").replace("?? facts.json", "").strip(), before)
        self.assertEqual(lr.ctl("unattended", "promotion").rc, 2)
        self.assertEqual(lr.ctl("unattended", "promotion", "--facts", "missing.json").rc, 2)
        os.symlink(os.path.join(lr.path, "facts.json"), os.path.join(lr.path, "link.json"))
        self.assertEqual(lr.ctl("unattended", "promotion", "--facts", "link.json").rc, 2)
        with open(os.path.join(lr.path, "bad.json"), "w") as fh:
            fh.write('{"schema": "promotion.facts.v1", "prs": 5}')
        self.assertEqual(lr.ctl("unattended", "promotion", "--facts", "bad.json").rc, 2)

    def test_with_evidence_and_a_clean_record_the_cli_says_eligible_and_still_changes_nothing(self):
        lr = UnattendedRepo()
        self.addCleanup(lr.cleanup)
        r = lr.ctl("unattended", "promotion", "--facts", self.facts_file(lr, facts()))
        self.assertEqual(r.rc, 0, r)
        self.assertIn("ELIGIBLE", r.out)
        self.assertIn("nothing was changed", r.out)
        with open(os.path.join(lr.path, ".claude", "loop.config.md")) as fh:
            cfg = fh.read()
        self.assertIn("autonomy_level: L4", cfg)                                              # it was already there for the test; the command did not write it
        self.assertEqual(lr.repo.git("diff", "--stat"), "")
        bad = lr.ctl("unattended", "promotion", "--facts", self.facts_file(lr, facts(source="events")))
        self.assertEqual(bad.rc, 1)
        self.assertIn("forgeable", bad.out)


if __name__ == "__main__":
    unittest.main()
