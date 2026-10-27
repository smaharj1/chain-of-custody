"""G7: the independent reviews exist, are about this code, agree with their own findings, and are clean."""
from __future__ import annotations

from .. import item_doc
from . import GateCase

CODE_OK = """# Code Review - slice-a (iteration 1)

## Verdict
CODE_REVIEW_APPROVED

## Summary
- AC-A-1 is proved by test_ac_a_1; AC-A-2 by test_ac_a_2.

## Blockers (must fix before this item can reach `done`)
None.

## Verification Run
- python3 -m unittest: PASS - 12 tests
- ruff check: PASS

## Files Reviewed
- src/calc.py
"""
CODE_BLOCKED = CODE_OK.replace("CODE_REVIEW_APPROVED", "CHANGES_REQUESTED").replace("None.", "- [BLOCKER] src/calc.py:3 - off by one\n  Why: AC-A-1\n  Fix: x")
SEC_OK = """# Security Review

## Verdict
SECURITY_APPROVED

Scanners: gitleaks ran (0 findings); semgrep did not run (not installed).

- **SEVERITY**: Low
- **ID**: SEC-1
"""
SEC_BLOCKED = """# Security Review

## Verdict
SECURITY_BLOCKED

Scanners: gitleaks ran.

- **SEVERITY**: High
- **LOCATION**: src/auth/login.py:10
- **ID**: SEC-7
- **CWE**: CWE-89
"""
REPORTS = "docs/features/slice-a/reports/"


def put(case, code=CODE_OK, sec=None, extra=None):
    files = {REPORTS + "code-review.md": code}
    if sec is not None:
        files[REPORTS + "security-review.md"] = sec
    files.update(extra or {})
    return case.commit(files, "reviews")


class Reviews(GateCase):
    ITEMS = [item_doc("A", acs=2, touches={"domains": ["backend"], "paths": ["src/**"], "schema": False, "deps_change": False})]
    BASE_FILES = {"src/app.py": "x = 1\n"}

    def test_a_clean_code_review_of_a_non_sensitive_item_passes(self):
        self.commit({"src/app.py": "x = 2\n"}, "work")
        put(self)
        res = self.run_gate("g7")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["review"], {"code": "CODE_REVIEW_APPROVED"})
        self.assertIn("g7.security-not-required", self.ids(res))

    def test_a_missing_review_fails(self):
        self.assertFails(self.run_gate("g7"), "g7.code-review-missing")

    def test_an_uncommitted_review_is_not_evidence(self):
        import os
        os.makedirs(os.path.join(self.lr.path, REPORTS))
        with open(os.path.join(self.lr.path, REPORTS, "code-review.md"), "w") as fh:
            fh.write(CODE_OK)
        self.assertFails(self.run_gate("g7"), "g7.code-review-missing")

    def test_changes_requested_blocks(self):
        put(self, code=CODE_BLOCKED)
        res = self.run_gate("g7")
        self.assertFails(res, "g7.code-review-blocked")
        self.assertEqual(res["evidence"]["review"]["code"], "CHANGES_REQUESTED")

    def test_an_approval_that_lists_a_blocker_is_inconsistent(self):
        put(self, code=CODE_OK.replace("None.", "- [BLOCKER] src/app.py:1 - broken"))
        self.assertFails(self.run_gate("g7"), "g7.code-review-inconsistent")

    def test_both_verdict_tokens_or_none_is_not_a_verdict(self):
        put(self, code=CODE_OK.replace("CODE_REVIEW_APPROVED", "CODE_REVIEW_APPROVED | CHANGES_REQUESTED"))
        self.assertFails(self.run_gate("g7"), "g7.code-review-verdict")
        self.setUp()
        put(self, code=CODE_OK.replace("## Verdict\nCODE_REVIEW_APPROVED\n", ""))
        self.assertFails(self.run_gate("g7"), "g7.code-review-verdict")

    def test_the_reviewer_must_have_run_the_verify_commands(self):
        put(self, code=CODE_OK.replace("- python3 -m unittest: PASS - 12 tests\n- ruff check: PASS", "- python3 -m unittest: not run - no time"))
        self.assertFails(self.run_gate("g7"), "g7.reviewer-did-not-run-verify")

    def test_every_criterion_must_be_traced_in_the_review(self):
        put(self, code=CODE_OK.replace("AC-A-2", "AC-B-2").replace("test_ac_a_2", "test_x"))
        res = self.run_gate("g7")
        self.assertFails(res, "g7.review-ac-trace")
        self.assertIn("AC-A-2", res["findings"][0]["message"])

    def test_code_changed_after_the_review_makes_it_stale(self):
        put(self)
        self.commit({"src/app.py": "x = 3\n"}, "late change")
        self.assertFails(self.run_gate("g7"), "g7.code-review-stale")

    def test_a_report_only_commit_after_the_review_does_not_stale_it(self):
        put(self)
        self.commit({REPORTS + "backend-engineer.md": "# report\n"}, "engineer report")
        self.assertPass(self.run_gate("g7"))

    def test_a_review_saved_for_another_item_is_not_this_items(self):
        put(self, code=CODE_OK.replace("slice-a", "slice-z"))
        self.assertFails(self.run_gate("g7"), "g7.code-review-wrong-item")

    def test_the_review_files_are_pinned_from_the_first_run_so_an_engineer_cannot_write_them(self):
        from loopctl import locked
        from loopctl.gates import _lib
        self.run_gate("g7")
        globs = locked.read(_lib.runstate_of(self.ctx()))["globs"]
        for n in ("code-review.md", "security-review.md", "design-review.md"):
            self.assertIn(REPORTS + n, globs)

    def test_a_design_review_that_needs_revision_blocks(self):
        put(self, extra={REPORTS + "design-review.md": "## Verdict\nDESIGN_NEEDS_REVISION\n"})
        self.assertFails(self.run_gate("g7"), "g7.design-review-blocked")


class Sensitive(GateCase):
    ITEMS = [item_doc("A", acs=2, touches={"domains": ["backend"], "paths": ["src/auth/**"], "schema": False, "deps_change": False}),
             item_doc("B", acs=2, slug="slice-b", riskClass="high", touches={"domains": ["backend"], "paths": ["src/x/**"], "schema": False, "deps_change": False}),
             item_doc("C", acs=2, slug="slice-c", ext={"sensitive": True}),
             item_doc("W", acs=2, slug="slice-w", touches={"domains": ["backend"], "paths": ["src/auth/**"], "schema": False, "deps_change": False},
                      waivers=[{"approver": "Pat", "expiry": "2999-01-01", "finding_ids": ["SEC-7"], "reason": "accepted risk, ticket 12"}])]
    BASE_FILES = {"src/app.py": "x = 1\n"}

    def reviews(self, slug, iid, code=None, sec=None):
        base = "docs/features/%s/reports/" % slug
        files = {base + "code-review.md": (code or CODE_OK).replace("AC-A", "AC-" + iid).replace("slice-a", slug).replace("test_ac_a", "test_ac_" + iid.lower())}
        if sec is not None:
            files[base + "security-review.md"] = sec
        self.commit(files, "reviews")

    def test_a_sensitive_item_needs_a_security_review(self):
        for iid, slug in (("A", "slice-a"), ("B", "slice-b"), ("C", "slice-c")):
            with self.subTest(iid):
                self.setUp()
                self.reviews(slug, iid)
                self.assertFails(self.run_gate("g7", iid), "g7.security-review-missing")

    def test_a_clean_security_review_passes(self):
        self.reviews("slice-a", "A", sec=SEC_OK)
        res = self.run_gate("g7")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["review"]["security"], "SECURITY_APPROVED")

    def test_security_blocked_fails_and_names_the_findings(self):
        self.reviews("slice-a", "A", sec=SEC_BLOCKED)
        res = self.run_gate("g7")
        self.assertFails(res, "g7.security-review-blocked")
        self.assertIn("SEC-7", res["findings"][0]["message"] + str(res["findings"]))

    def test_approved_with_a_high_finding_is_inconsistent(self):
        self.reviews("slice-a", "A", sec=SEC_BLOCKED.replace("SECURITY_BLOCKED", "SECURITY_APPROVED"))
        self.assertFails(self.run_gate("g7"), "g7.security-review-inconsistent")

    def test_the_security_review_must_say_which_scanners_ran(self):
        self.reviews("slice-a", "A", sec=SEC_OK.replace("Scanners: gitleaks ran (0 findings); semgrep did not run (not installed).", "Looks fine."))
        self.assertFails(self.run_gate("g7"), "g7.security-scanners-unstated")

    def test_an_unexpired_waiver_in_the_plan_covers_a_blocked_review(self):
        self.reviews("slice-w", "W", sec=SEC_BLOCKED)
        res = self.run_gate("g7", "W")
        self.assertPass(res)
        self.assertIn("g7.security-waived", self.ids(res))
        self.assertEqual(res["evidence"]["review"]["security"], "SECURITY_BLOCKED (waived)")

    def test_a_waiver_that_misses_a_finding_does_not_cover_it(self):
        self.reviews("slice-w", "W", sec=SEC_BLOCKED + "\n- **SEVERITY**: Critical\n- **ID**: SEC-8\n")
        self.assertFails(self.run_gate("g7", "W"), "g7.security-review-blocked")

    def test_a_security_review_that_is_stale_fails(self):
        self.reviews("slice-a", "A", sec=SEC_OK)
        self.commit({"src/app.py": "x = 5\n"}, "late change")
        res = self.run_gate("g7")
        self.assertFails(res, "g7.security-review-stale")


class SecurityEveryItem(GateCase):
    ITEMS = [item_doc("A", acs=2)]
    CFG = "security_review: every_item\n"

    def test_every_item_means_every_item(self):
        put(self)
        self.assertFails(self.run_gate("g7"), "g7.security-review-missing")


class SecurityOff(GateCase):
    ITEMS = [item_doc("A", acs=2, ext={"sensitive": True})]
    CFG = "security_review: \"off\"\n"

    def test_off_means_off(self):
        put(self)
        self.assertPass(self.run_gate("g7"))


if __name__ == "__main__":
    import unittest
    unittest.main()
