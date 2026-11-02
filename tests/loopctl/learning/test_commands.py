"""The learning commands through `loopctl.cli.main`, in a temp repo with a plan, engineer reports, reviews and a seeded event log (report 16.1 row 10)."""
from __future__ import annotations

import json
import os
import unittest

from .. import LoopTestCase, item_doc
from . import fixtures

from kitlib import runstate

TOUCHES = {"domains": ["backend"], "paths": ["src/api/**", "tests/api/**"], "schema": False, "deps_change": False}
REPORT = ("# Backend Report\n\n## Status\nAPPROVED\n\n## Summary\nDid it.\n\n## Files Changed\n- `src/api/x.py` — modified\n\n## Tests Added (or N/A)\n- N/A\n\n"
          "## API Contract Status\nAS_SPECIFIED\n\n## Iteration\n1\n\n## Open Questions / Brief Gaps\n%s\n\n## Primer Delta\n%s\n\n## Doc Delta\nNONE\nchecked: README\n\n"
          "## Verification\n- python3 -m unittest: PASS\n")
REVIEW = "# Code Review — slice (iteration %d)\n\n## Verdict\n%s\n\n## Summary\nx\n\n## Blockers\n%s\n## Verification Run\n- python3 -m unittest: PASS\n"


class Cmds(LoopTestCase):
    def setUp(self):
        self.lr = self.lr_(items=[item_doc("A", touches=TOUCHES), item_doc("B", ["A"], touches=TOUCHES, milestone="M2")], milestones=["M1", "M2"], real_gates=True)      # `harness` reads the installed gates
        self.rs = runstate.RunState.for_path(self.lr.path)
        self.snap = lambda: (self.lr.repo.git("for-each-ref", "--format=%(refname) %(objectname)"), self.lr.repo.git("status", "--porcelain"), self.lr.repo.git("branch", "--list"))

    def lr_(self, **kw):
        return self.lr(**kw)

    def seed(self, gaps=True):
        gap = "- which status code for a validation failure? I guessed 422" if gaps else "NONE"
        for slug in ("slice-a", "slice-b"):
            self.lr.repo.write("docs/features/%s/reports/backend.md" % slug, REPORT % (gap, "`.claude/context/backend.md` cites a file that is gone" if gaps else "NONE\nchecked: layering"))
        self.lr.repo.write("docs/features/slice-a/reviews/code-reviewer-r1.md", REVIEW % (1, "CHANGES_REQUESTED", "- [BLOCKER] src/api/x.py:3 — returns 400\n  Why: x\n"))
        self.lr.repo.write("docs/features/slice-a/reviews/code-reviewer-r2.md", REVIEW % (2, "CODE_REVIEW_APPROVED", "- none\n"))
        for iid, n in (("A", 0), ("B", 100)):
            self.rs.append_event("loopctl", {"event": "begin", "item": iid, "runId": "run-1", "source": "driver"})
            self.rs.append_event("loopctl", {"event": "gate", "item": iid, "gate": "g5", "status": "fail", "mode": "on", "blocking": True, "ids": ["g5.protected-path"], "runId": "run-1", "source": "driver"})
            self.rs.append_event("loopctl", {"event": "fail", "item": iid, "failure_class": "item", "reason": "tests_failed", "runId": "run-1", "source": "driver"})
        self.rs.append_event("notify", {"source": "driver", "kind": "stop", "item": "A"})

    def test_retro_prints_the_numbers_as_text_and_json_and_writes_nothing(self):
        self.seed()
        before = self.snap()
        r = self.lr.ctl("retro")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("# Retro: milestone all", r.out)
        self.assertIn("tests_failed 2", r.out)
        self.assertIn("g5: 2 run(s)", r.out)
        j = self.lr.ctl("retro", "--json").json
        self.assertEqual((j["schema"], j["items"], j["failures"]["total"], j["gates"]["g5"]["fail"]), ("loopctl.retro.v1", ["A", "B"], 2, 2))
        self.assertEqual(j["reports"]["brief_gaps_by_domain"], {"backend": 2})
        m2 = self.lr.ctl("retro", "--json", "--milestone", "M2").json
        self.assertEqual((m2["items"], m2["failures"]["total"]), (["B"], 1))
        self.assertEqual(self.lr.ctl("retro", "--milestone", "M9").rc, 2)
        self.assertEqual(self.snap(), before)

    def test_gaps_shows_then_drafts_and_never_touches_git(self):
        self.seed()
        before = self.snap()
        r = self.lr.ctl("gaps")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("would draft (nothing written; add --write)", r.out)
        self.assertFalse(os.path.exists(os.path.join(self.rs.root, "gaps")))
        w = self.lr.ctl("gaps", "--write")
        self.assertEqual(w.rc, 0, w)
        self.assertIn("loopctl has not edited, committed, pushed or opened anything", w.out)
        self.assertIn("git switch -c learning/gaps-", w.out)
        self.assertIn("gh pr create --draft", w.out)
        self.assertEqual(self.snap(), before)                                                      # no commit, no branch, no ref, no file in the work tree
        dirs = os.listdir(os.path.join(self.rs.root, "gaps"))
        self.assertEqual(len(dirs), 1)
        j = self.lr.ctl("gaps", "--write", "--json").json
        self.assertFalse(j["written"])                                                             # the same findings: not drafted twice
        self.assertEqual(j["id"], dirs[0])
        rule = os.path.join(self.rs.root, "gaps", dirs[0], "files", ".claude", "rules")
        self.assertIn("backend-brief-gaps.md", os.listdir(rule))
        with open(os.path.join(self.rs.root, "gaps", dirs[0], "COMMANDS.txt")) as fh:
            self.assertIn("never runs them", fh.read())

    def test_nothing_repeats_in_a_clean_run(self):
        self.seed(gaps=False)
        for iid in ("A", "B"):
            self.rs.append_event("loopctl", {"event": "begin", "item": iid, "runId": "run-1", "source": "driver"})
        r = self.lr.ctl("gaps", "--write", "--min", "3")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("nothing repeats", r.out)
        self.assertFalse(os.path.exists(os.path.join(self.rs.root, "gaps")))
        self.assertEqual(self.lr.ctl("gaps", "--min", "0").rc, 2)

    def test_calibration_scan_report_and_record(self):
        self.seed()
        r = self.lr.ctl("calibration", "scan")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("1 new blocker(s), 1 outcome(s) derived", r.out)
        self.assertFalse(os.path.exists(os.path.join(self.rs.root, "calibration.jsonl")))
        self.assertEqual(self.lr.ctl("calibration", "scan", "--write").rc, 0)
        rep = self.lr.ctl("calibration", "report", "--json").json
        c = rep["critics"][0]
        self.assertEqual((c["critic"], c["logged"], c["fixed"], c["derived"], c["verdict"]), ("code-reviewer", 1, 1, 1, "pending"))
        with open(os.path.join(self.rs.root, "calibration.jsonl")) as fh:
            bid = json.loads(fh.readline())["id"]
        self.assertEqual(self.lr.ctl("calibration", "scan", "--write", "--json").json["new_blockers"], 0)
        rec = self.lr.ctl("calibration", "record", "--blocker", bid, "--outcome", "false_positive", "--engineer-hit", "no", "--by", "Pat", "--note", "the contract says 400")
        self.assertEqual(rec.rc, 0, rec)
        rep = self.lr.ctl("calibration", "report", "--json").json["critics"][0]
        self.assertEqual((rep["fixed"], rep["false_positive"], rep["precision"]), (0, 1, 0.0))
        self.assertEqual(self.lr.ctl("calibration", "record", "--blocker", "bk-000000000000", "--outcome", "fixed", "--by", "Pat").rc, 2)
        self.assertEqual(self.lr.ctl("calibration", "record", "--blocker", bid, "--outcome", "fixed").rc, 2)             # --by is required
        self.assertEqual(self.lr.ctl("calibration", "record", "--write", "--blocker", bid, "--outcome", "fixed", "--by", "Pat").rc, 2)

    def test_the_primer_streak_is_read_from_the_merge_commits(self):
        """A's merge adds a new top-level directory (src/), B's and C's do not: only A is structural, and a report with a real Primer Delta is not empty."""
        self.seed(gaps=False)
        self.lr.fast_start()
        self.lr.quick_merge("A")
        self.lr.quick_merge("B")
        from loopctl.context import Context
        from loopctl.learning import commands, data as data_mod
        ctx = Context.build(self.lr.path, self.lr.env)
        d = data_mod.load(ctx)
        rows = commands.structural_items(ctx, d)
        self.assertEqual([(r["id"], r["structural"], r["delta_empty"]) for r in rows], [("A", True, True), ("B", False, True)])
        self.assertEqual(rows[0]["why"], "new-top-level:src")
        j = self.lr.ctl("retro", "--json").json
        self.assertEqual(j["primer"], {"streak": 1, "flag": False, "items": ["A"], "threshold": 3})
        self.assertNotIn("primer", self.lr.ctl("retro", "--json", "--no-git").json)

    def test_report_scan_says_whether_block_is_ready(self):
        self.seed()
        r = self.lr.ctl("report", "scan")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("report_trailers: warn", r.out)
        self.assertIn("no engineer report has been read yet", r.out)
        j = self.lr.ctl("report", "scan", "--json").json
        self.assertEqual((j["mode"], j["block_ready"], j["reports_on_disk"]["checked"]), ("warn", False, 2))
        for n in range(10):
            self.rs.append_event("loopctl", {"event": "engineer_report", "item": "A", "role": "backend-engineer", "status": "APPROVED", "runId": "r"})
        self.assertTrue(self.lr.ctl("report", "scan", "--json").json["block_ready"])
        self.assertEqual(self.lr.ctl("report", "check").rc, 2)                                                           # check still needs a path

    def test_harness_prints_owner_metrics_and_reads_an_ablation_file_from_inside_the_repo_only(self):
        self.seed()
        r = self.lr.ctl("harness")
        self.assertEqual(r.rc, 0, r)
        self.assertIn("g5", r.out)
        self.assertIn("no ablation file given", r.out)
        doc = self.lr.ctl("harness", "--json", "--model", "m-next").json
        self.assertEqual((doc["schema"], doc["model"], doc["ablation_present"]), ("loopctl.harness.v1", "m-next", False))
        rows = {g["gate"]: g for g in doc["gates"]}
        self.assertEqual((rows["g5"]["runs"], rows["g5"]["blocked"], rows["docs"]["mode"]), (2, 2, "report"))
        ab = {"schema": "kit.ablation.v1", "parts": [{"id": "hook:guard-git", "class": "safety", "cases": [{"id": "S-no-push", "class": "safety", "base_rate": 1.0, "ablated_rate": 0.0, "ablated_runs": 3}]}]}
        self.lr.repo.write("ab.json", json.dumps(ab))
        ok = self.lr.ctl("harness", "--ablation", "ab.json", "--json").json
        self.assertEqual([p["verdict"] for p in ok["other_parts"]], ["keep"])
        outside = os.path.join(self.lr.home, "ab.json")
        with open(outside, "w") as fh:
            fh.write(json.dumps(ab))
        self.assertEqual(self.lr.ctl("harness", "--ablation", "../" * 6 + outside.lstrip("/")).rc, 2)
        os.symlink(outside, os.path.join(self.lr.path, "link.json"))
        self.assertEqual(self.lr.ctl("harness", "--ablation", "link.json").rc, 2)
        self.lr.repo.write("bad.json", json.dumps({"schema": "something-else"}))
        self.assertEqual(self.lr.ctl("harness", "--ablation", "bad.json").rc, 2)
        self.lr.repo.write("dup.json", '{"schema": "kit.ablation.v1", "schema": "x"}')
        self.assertEqual(self.lr.ctl("harness", "--ablation", "dup.json").rc, 2)


if __name__ == "__main__":
    unittest.main()
