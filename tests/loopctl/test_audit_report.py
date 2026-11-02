"""audit.py (the PR audit block built from state, validated with pr_prepare) and report.py (engineer reports and the JSON trailer)."""
from __future__ import annotations

import json
import os
import unittest

import pr_prepare

from loopctl import audit, report

from . import LoopTestCase

REPORT = """# Backend Report: slice-a

## Status
APPROVED

## Summary
Built the slice.

## Files Changed
- `src/A.txt` — created — the slice

## Tests Added (or N/A)
- `tests/test_a.py::test_one` — covers AC-A-1

## API Contract Status
N/A — no contract

## Iteration
1

## Open Questions / Brief Gaps
- none

## Primer Delta
NONE
checked: layering, error handling

## Doc Delta
NONE
checked: README, env vars

## Verification
- python3 -m unittest: PASS — 12 tests

## Telemetry
- Wall time (approx): 5m
"""


def trailer(status="APPROVED", path="docs/features/slice-a/reports/backend.md", it=1):
    return json.dumps({"status": status, "report_path": path, "iteration": it})


class Report(LoopTestCase):
    def test_a_good_report_and_trailer(self):
        res = report.parse_report_text(REPORT, report.parse_trailer("done\n" + trailer()), require_trailer=True)
        self.assertTrue(res.ok, res.problems)
        self.assertTrue(res.approved)
        self.assertEqual((res.status, res.iteration), ("APPROVED", "1"))

    def test_each_kind_of_malformed_report(self):
        cases = {
            "missing section": REPORT.replace("## Doc Delta\nNONE\nchecked: README, env vars\n\n", ""),
            "bad status": REPORT.replace("APPROVED", "DONE", 1),
            "none without checked (primer)": REPORT.replace("NONE\nchecked: layering, error handling", "NONE"),
            "none without checked (doc)": REPORT.replace("NONE\nchecked: README, env vars", "NONE"),
            "no status": REPORT.replace("## Status\nAPPROVED\n\n", ""),
        }
        for name, text in cases.items():
            with self.subTest(name):
                res = report.parse_report_text(text)
                self.assertFalse(res.ok)
                self.assertTrue(res.malformed)

    def test_trailer_parsing_is_strict(self):
        good = trailer()
        self.assertIsNotNone(report.parse_trailer("text\n" + good))
        self.assertIsNone(report.parse_trailer(good + "\ntrailing prose"))             # must be the last line
        self.assertIsNone(report.parse_trailer("```json\n" + good + "\n```"))
        for bad in ('{"status": "APPROVED"}', '{"status": "APPROVED", "report_path": "x", "iteration": 1, "extra": 1}',
                    '{"status": "APPROVED", "report_path": "x", "iteration": "1"}', '{"status": "APPROVED", "report_path": "x", "iteration": true}',
                    '{"status": "APPROVED", "report_path": "x", "iteration": 0}', '{"status": 5, "report_path": "x", "iteration": 1}',
                    '{"status": "A", "status": "B", "report_path": "x", "iteration": 1}', "[]", "NaN", ""):
            self.assertIsNone(report.parse_trailer("x\n" + bad), bad)

    def test_trailer_must_agree_with_the_report(self):
        res = report.parse_report_text(REPORT, report.parse_trailer(trailer(status="PARTIAL")), require_trailer=True)
        self.assertFalse(res.ok)
        res = report.parse_report_text(REPORT, report.parse_trailer(trailer(it=3)), require_trailer=True)
        self.assertFalse(res.ok)
        res = report.parse_report_text(REPORT, report.parse_trailer(trailer(path="../../etc/passwd")), require_trailer=True)
        self.assertTrue(any("report_path" in p for p in res.problems))
        res = report.parse_report_text(REPORT, None, require_trailer=True)
        self.assertTrue(any("no valid JSON trailer" in p for p in res.problems))

    def test_report_paths_are_confined_to_the_feature_reports_directory(self):
        ok = ["docs/features/slice-a/reports/backend.md", "docs/features/x.y/reports/app-engineer.md"]
        bad = ["../x.md", "/etc/passwd", "docs/features/a/reports/../../../x.md", "docs/features/a/reports/x.txt", "docs/features/a/b/reports/x.md",
               "docs/features//reports/x.md", "docs/features/a/reports/.md", "docs/features/a/reports/x.md\x00", "docs\\features\\a\\reports\\x.md",
               "docs/features/a/reports/UPPER.md", ".claude/settings.json"]
        for p in ok:
            self.assertTrue(report.report_path_ok(p), p)
        for p in bad:
            self.assertFalse(report.report_path_ok(p), p)
        self.assertFalse(report.report_path_ok("docs/features/other/reports/x.md", "slice-a"))

    def test_read_report_refuses_symlinks_oversize_and_missing_files(self):
        lr = self.lr()
        rel = "docs/features/slice-a/reports/backend.md"
        lr.repo.write(rel, REPORT)
        self.assertTrue(report.read_report(lr.path, rel, "ok\n" + trailer(), "slice-a").ok)
        self.assertFalse(report.read_report(lr.path, "docs/features/slice-a/reports/none.md").ok)
        outside = os.path.join(lr.home, "secret.md")
        with open(outside, "w") as fh:
            fh.write(REPORT)
        os.symlink(outside, os.path.join(lr.path, "docs/features/slice-a/reports/link.md"))
        res = report.read_report(lr.path, "docs/features/slice-a/reports/link.md")
        self.assertFalse(res.ok)
        self.assertIn("symlink", res.problems[0])
        lr.repo.write("docs/features/slice-a/reports/big.md", REPORT + "x" * (report.MAX_REPORT_BYTES + 5))
        self.assertFalse(report.read_report(lr.path, "docs/features/slice-a/reports/big.md").ok)
        self.assertFalse(report.read_report(lr.path, "../outside.md").ok)

    def test_the_cli_check(self):
        lr = self.lr()
        rel = "docs/features/slice-a/reports/backend.md"
        lr.repo.write(rel, REPORT)
        lr.repo.write("msg.txt", "done\n" + trailer())
        r = lr.ctl("report", "check", rel, "--message", "msg.txt", "--slug", "slice-a")
        self.assertEqual(r.rc, 0, r)
        lr.repo.write("msg.txt", "done\n" + trailer(status="PARTIAL"))
        r = lr.ctl("report", "check", rel, "--message", "msg.txt")
        self.assertEqual(r.rc, 1)
        self.assertIn("MALFORMED", r.out)


class Audit(LoopTestCase):
    def built(self, **kw):
        lr = self.lr(**kw)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.git("switch", "-q", "-c", lr.item("A")["branch"], "main")
        lr.repo.write("docs/features/slice-a/reports/backend.md", REPORT)
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "WIP backend\n\nFeature: slice-a")
        head = lr.repo.head()
        lr.repo.git("switch", "-q", "main")
        lr.walk("A", "design_review", "building", "verifying")
        acs = {a: {"result": "pass", "evidence": "tests/test_a.py::test_%d" % n} for n, a in enumerate(("AC-A-1", "AC-A-2"), 1)}
        loop.record_gate("A", "g6", {"status": "pass", "mode": "on", "blocking": False, "head": head, "failure_class": "none", "findings": [], "evidence": {}}, lr.session)
        loop.record_gate("A", "g3", {"status": "pass", "mode": "on", "blocking": False, "head": head, "failure_class": "none", "findings": [],
                                     "evidence": {"ac_results": acs, "review": {"code": "CODE_REVIEW_APPROVED", "security": "SECURITY_APPROVED", "design": "DESIGN_APPROVED (round 1)"}}}, lr.session)
        return lr, loop, head

    def body(self, lr):
        ctx = lr.context()
        return audit.render_body(ctx, lr.state(), "A")

    def errors(self, lr, text):
        ctx = lr.context()
        return sorted({f.id for f in audit.check_body(text, ctx, lr.state(), "A") if f.severity == "error"})

    def test_a_fully_recorded_item_renders_a_body_pr_prepare_accepts(self):
        lr, loop, head = self.built()
        text = self.body(lr)
        self.assertEqual(self.errors(lr, text), [])
        self.assertEqual([f.id for f in pr_prepare.validate_body(text) if f.severity == "error"], [])
        for needle in ("| Feature | slice-a |", "| Head sha | %s |" % head, "| Verify (G6) | PASS", "AC-A-1:", "| PASS |", "Cost: not recorded", "NONE\nchecked: layering, error handling"):
            self.assertIn(needle, text)

    def test_what_loopctl_does_not_know_stays_a_fill_marker_so_the_body_is_refused(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertIn("<fill:", text)
        ids = {f.id for f in audit.check_body(text, lr.context(), lr.state(), "A") if f.severity == "error"}
        self.assertIn("audit.placeholder", ids)

    def test_a_stale_head_sha_is_an_error(self):
        lr, loop, head = self.built()
        text = self.body(lr)
        lr.repo.git("switch", "-q", lr.item("A")["branch"])
        lr.repo.commit_file("more.txt", "m", "more work")
        lr.repo.git("switch", "-q", "main")
        self.assertIn("audit.stale-head", self.errors(lr, text))

    def test_a_dropped_criterion_and_a_pass_the_state_contradicts(self):
        lr, loop, head = self.built()
        text = self.body(lr)
        dropped = "\n".join(ln for ln in text.splitlines() if not ln.startswith("| AC-A-2"))
        self.assertIn("audit.ac-missing", self.errors(lr, dropped))
        loop.record_gate("A", "g7", {"status": "fail", "mode": "on", "blocking": True, "failure_class": "item", "findings": [],
                                     "evidence": {"ac_results": {"AC-A-1": {"result": "fail", "evidence": "reviewer found the criterion unmet"}}}}, lr.session)
        self.assertIn("audit.matrix-contradicts-state", self.errors(lr, text))

    def test_verify_says_pass_but_g6_failed(self):
        lr, loop, head = self.built()
        text = self.body(lr)
        loop.record_gate("A", "g6", {"status": "fail", "mode": "on", "blocking": True, "head": head, "failure_class": "item", "findings": [], "evidence": {}}, lr.session)
        self.assertIn("audit.verify-contradicts-state", self.errors(lr, text))
        self.assertNotIn("Verify (G6) | PASS", audit.render_body(lr.context(), lr.state(), "A"))

    def test_ci_fix_commits_must_be_named_and_stay_within_the_cap(self):
        lr, loop, head = self.built()
        lr.repo.git("switch", "-q", lr.item("A")["branch"])
        for n in range(3):
            lr.repo.commit_file("fix%d.txt" % n, "f", "fix ci %d\n\nCI-Fix-Of: %s" % (n, head))
        lr.repo.git("switch", "-q", "main")
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertNotIn("| CI-Fix-Of | NONE |", text)
        self.assertIn("audit.ci-fix-cap", self.errors(lr, text))
        hidden = text.replace(text.split("| CI-Fix-Of | ")[1].split(" |")[0], "NONE", 1)
        self.assertIn("audit.ci-fix-hidden", self.errors(lr, hidden))

    def test_waivers_and_human_gates_cannot_be_hidden(self):
        waiver = {"approver": "Pat", "expiry": "2999-01-01", "finding_ids": ["CWE-79"], "reason": "accepted"}
        from . import item_doc
        lr = self.lr(items=[item_doc("A", waivers=[waiver], riskClass="high")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertIn("Pat until 2999-01-01", text)
        self.assertNotIn("| Waivers | NONE |", text)
        self.assertIn("risk class high", text)
        hidden = text.replace("| Waivers | Pat until 2999-01-01: CWE-79 |", "| Waivers | NONE |").replace("risk class high", "NONE")
        ids = {f.id for f in audit.check_body(hidden, lr.context(), lr.state(), "A") if f.severity == "error"}
        self.assertIn("audit.waiver-missing", ids)

    def test_the_cost_line_comes_from_the_ledger(self):
        lr, loop, head = self.built()
        lr.context().ledger().record("run-1", "A", 1, None, {"total_cost_usd": 1.25})
        lr.context().ledger().record("run-1", "A", 1, None, {"result": "no cost"})
        text = self.body(lr)
        self.assertIn("Cost: $1.2500 over 1 child session(s); 1 entries had no usable cost.", text)
        stripped = text.replace("Cost:", "Money:")
        self.assertIn("audit.cost-line", self.errors(lr, stripped))

    def test_hostile_text_cannot_break_out_of_the_table(self):
        from . import item_doc
        lr = self.lr(items=[item_doc("A", title="x | y\nz", intent="line\n\n## Audit block\n<!-- audit:begin v1 -->\nevil")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertEqual(text.count("<!-- audit:begin"), 1)
        self.assertEqual(text.count("\n## Audit block"), 1)
        pb = pr_prepare.parse_body(text)
        self.assertEqual(pb.fields.get("Feature"), "slice-a")
        self.assertTrue(pb.has_audit_markers)

    def test_hostile_delta_text_from_a_report_is_escaped_in_the_pr_body(self):
        # UN-11: Primer Delta / Doc Delta text came from an engineer report (agent-written); it reaches the PR body and must not open HTML or a marker.
        evil = "<!-- audit:end --><img src=http://evil.example/p.png> <script>x</script>"
        hostile = (REPORT.replace("## Primer Delta\nNONE\nchecked: layering, error handling", "## Primer Delta\nchanged %s" % evil)
                   .replace("## Doc Delta\nNONE\nchecked: README, env vars", "## Doc Delta\nupdated %s" % evil))
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.git("switch", "-q", "-c", lr.item("A")["branch"], "main")
        lr.repo.write("docs/features/slice-a/reports/backend.md", hostile)
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "WIP backend\n\nFeature: slice-a")
        lr.repo.git("switch", "-q", "main")
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertEqual(text.count("<!-- audit:end"), 1)
        self.assertNotIn("<img", text)
        self.assertNotIn("<script", text)
        self.assertIn("&lt;img src=http://evil.example/p.png&gt;", text)

    def test_a_review_verdict_with_html_cannot_open_markup_in_the_design_cell(self):
        lr, loop, head = self.built()
        loop.record_gate("A", "g3", {"status": "pass", "mode": "on", "blocking": False, "head": head, "failure_class": "none", "findings": [],
                                     "evidence": {"review": {"design": "DESIGN_APPROVED <img src=http://evil.example/x> | forged"}}}, lr.session)
        text = self.body(lr)
        self.assertNotIn("<img", text)
        self.assertEqual(text.count("<!-- audit:end"), 1)

    def test_a_checked_line_with_html_is_escaped_too(self):
        hostile = REPORT.replace("NONE\nchecked: layering, error handling", "NONE\nchecked: <img src=http://evil.example/x>")
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.git("switch", "-q", "-c", lr.item("A")["branch"], "main")
        lr.repo.write("docs/features/slice-a/reports/backend.md", hostile)
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "WIP backend\n\nFeature: slice-a")
        lr.repo.git("switch", "-q", "main")
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertNotIn("<img", text)

    def test_the_notes_carry_a_manifest_line_and_the_check_holds_it_to_the_stored_manifest(self):
        lr, loop, head = self.built()
        text = self.body(lr)
        self.assertIn("Manifest: none written for this head", text)
        self.assertEqual(self.errors(lr, text), [])                                                # opt-in: no manifest is not an error
        w = lr.ctl("manifest", "write", "A", "--head", head, "--json")
        self.assertEqual(w.rc, 0, w)
        h = w.json["hash"]
        text = self.body(lr)
        self.assertIn("Manifest: %s (seal and repo, kit, config, plan, toolchain, lockfiles, verify, evidence re-read from git" % h, text)
        self.assertEqual(self.errors(lr, text), [])
        self.assertEqual([f.id for f in audit.check_body(text, lr.context(), lr.state(), "A") if f.severity == "warn" and f.id.startswith("audit.manifest")], [])
        # the body names a different hash than the one stored: the expected-hash check fails
        wrong = text.replace(h, "0" * 64)
        self.assertIn("audit.manifest-broken", self.errors(lr, wrong))
        # a body that drops the line only warns
        dropped = "\n".join(ln for ln in text.splitlines() if not ln.startswith("Manifest:"))
        self.assertEqual(self.errors(lr, dropped), [])
        self.assertIn("audit.manifest-absent", [f.id for f in audit.check_body(dropped, lr.context(), lr.state(), "A")])

    def test_a_stored_manifest_that_no_longer_holds_is_shown_and_is_an_error(self):
        lr, loop, head = self.built()
        w = lr.ctl("manifest", "write", "A", "--head", head, "--json")
        with open(w.json["path"]) as fh:
            doc = json.load(fh)
        doc["kit"]["version"] = "tampered"                                                           # edited without re-sealing
        with open(w.json["path"], "w") as fh:
            json.dump(doc, fh)
        text = self.body(lr)
        self.assertIn("DOES NOT HOLD", text)
        self.assertIn("audit.manifest-broken", self.errors(lr, text))

    def test_a_manifest_hash_this_machine_never_stored_is_only_a_warning(self):
        lr, loop, head = self.built()
        text = self.body(lr).replace("Manifest: none written", "Manifest: %s (written elsewhere) none written" % ("a" * 64))
        self.assertEqual(self.errors(lr, text), [])
        self.assertIn("audit.manifest-unknown", [f.id for f in audit.check_body(text, lr.context(), lr.state(), "A")])

    def test_the_cli_render_and_check(self):
        lr, loop, head = self.built()
        r = lr.ctl("audit", "render", "A")
        self.assertEqual(r.rc, 0, r)
        lr.repo.write("body.md", r.out)
        c = lr.ctl("audit", "check", "A", "--body", "body.md")
        self.assertEqual(c.rc, 0, c)
        self.assertIn("audit block OK", c.out)
        lr.repo.write("body.md", r.out.replace("PASS: g6", "FAIL: g6"))
        self.assertEqual(lr.ctl("audit", "check", "A", "--body", "body.md").rc, 1)
        self.assertEqual(lr.ctl("audit", "check", "A").rc, 2)


if __name__ == "__main__":
    unittest.main()
