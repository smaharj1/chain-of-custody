<!-- kit:pr-body v1. The conductor fills this in. `python3 .claude/scripts/pr_prepare.py check` refuses it while any fill marker remains or a field is empty. NONE and PENDING are valid only where a field says so. This comment is invisible on GitHub. -->
# <fill: PR title, one line, at most 72 characters>

## Summary

<fill: two or three sentences: what changed and why, written for a reviewer who has not seen the feature folder>

## Audit block

<!-- audit:begin v1 -->
| Field | Value |
|---|---|
| Feature | <fill: feature slug> |
| Lane | <fill: full or quick> |
| Delivery | <fill: local, push-branch or draft-pr> |
| Branch | <fill: the feature branch, for example feat/bookmarks-page> |
| Base | <fill: the branch this merges into, for example main> |
| Base sha | <fill: merge-base of the branch and Base, 40 hex characters> |
| Head sha | <fill: the branch tip this block was written for, 40 hex characters> |
| Design critic | <fill: DESIGN_APPROVED (round N), or N/A (quick lane)> |
| Verify (G6) | <fill: PASS, then the verify.py run: base sha, stages, log> |
| Security review | <fill: SECURITY_APPROVED, or N/A (why nothing here is sensitive), or NOT RUN (why)> |
| Human-gated paths | <fill: NONE, or each changed path a human must review and why> |
| CI-Fix-Of | <fill: NONE, or the sha of each failing commit a CI-fix commit follows> |
| Waivers | <fill: NONE, or who granted which waiver and what it covers> |
| CI evidence | PENDING: the CI check runs on this pull request are the evidence |
<!-- audit:end -->

## Engineers

| Engineer | Status | Iterations | Code review | Report | Commits |
|---|---|---|---|---|---|
| <fill: database, backend, app, admin-app or infra> | <fill: APPROVED> | <fill: number> | <fill: CODE_REVIEW_APPROVED, or SKIPPED (trivial change) for the quick lane> | <fill: docs/features/slug/reports/engineer.md, or inline> | <fill: short shas of the WIP commits> |

## Acceptance matrix

Every criterion needs named evidence (a test, a command, a route) and PASS, checked by the code-reviewer and not only by the engineer.

| Criterion | Evidence | Result |
|---|---|---|
| <fill: the acceptance criterion, copied from requirements.md> | <fill: the test name, command or route that proves it> | <fill: PASS> |

## Doc Delta

<fill: NONE with a checked: line, or each doc that must change and why (README, API reference, env vars, runbook)>

## Primer Delta

<fill: NONE with a checked: line, or each primer change that was applied>

## Notes

<fill: NONE, or open questions, known gaps and anything the reviewer should look at first>

---
Prepared by an agent from a feature branch. A human pushed it and a human merges it.
