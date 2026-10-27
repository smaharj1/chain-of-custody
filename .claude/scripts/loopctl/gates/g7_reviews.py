"""G7: independent reviews have happened, on THIS code, and are clean (report section 9 G7, L-24, L-28, CO-17, CO-24).

loopctl cannot be the reviewer: the conductor (or the driver's review child) dispatches `code-reviewer` and `security-reviewer` in a clean
context and saves each one's strict output verbatim, committed with the item, at

    docs/features/<slug>/reports/code-review.md        required
    docs/features/<slug>/reports/security-review.md    required when `security_review` is every_item, or `sensitive` and the item is sensitive
    docs/features/<slug>/reports/design-review.md      optional; a DESIGN_NEEDS_REVISION there blocks

This gate reads those files from git at HEAD (an uncommitted review is not evidence) and checks what the agents' own contract promises:

* exactly one verdict token, and it agrees with the findings (a `[BLOCKER]` under `CODE_REVIEW_APPROVED`, or a Critical or High finding
  under `SECURITY_APPROVED`, is an inconsistent review and fails);
* the review is **fresh**: no commit that changes anything outside the reports directory came after the commit that last touched the
  review file, so the verdict was given on the code that is about to merge;
* the code review ran the verify commands (`## Verification Run` with at least one PASS or FAIL line, CO-17) and names every acceptance
  criterion in its summary (rubric 7, traceability);
* a security review says which scanners ran or did not (CO-24); a blocked security verdict passes only under an unexpired waiver in the
  plan (the plan is read at the base sha, waivers are H6) that names every Critical and High finding id.

Verdicts go to `evidence["review"]` and from there to the item's state and the PR audit block. Owner metric: reviewer precision.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import common, locked, plan as plan_mod
from . import GateResult
from . import _lib

NAME = "g7"
OWNER_METRIC = "reviewer precision: blockers fixed vs overruled vs false positive"
REQUIRED_FOR = (("reviewing", "pr_open"), ("reviewing", "awaiting_human"), ("reviewing", "merged"))

_SENSITIVE_RE = ["**/auth/**", "**/auth*/**", "**/*auth*", "**/payment*/**", "**/billing/**", "**/checkout/**", "**/upload*/**", "**/session*/**",
                 "**/login*", "**/password*", "**/crypto*/**", "**/permissions/**", "**/acl/**"]
_VERDICT_RE = re.compile(r"(?ms)^##\s*Verdict\s*\n(.*?)(?=^##\s|\Z)")


def _section(text: str, title: str) -> str:
    m = re.search(r"(?ms)^##\s*%s\s*\n(.*?)(?=^##\s|\Z)" % re.escape(title), text)
    return m.group(1) if m else ""


def sensitive(ctx: Any) -> bool:
    ext = ctx.item.get("ext") if isinstance(ctx.item.get("ext"), dict) else {}
    if ext.get("sensitive") is True or ctx.item.get("riskClass") == "high":
        return True
    return any(_lib.match_any(_SENSITIVE_RE, p) for p in _lib.footprint(ctx.item))


def security_required(ctx: Any) -> Tuple[bool, str]:
    mode = str(ctx.cfg.get("security_review"))
    if mode == "every_item":
        return True, "security_review: every_item"
    if mode == "sensitive" and sensitive(ctx):
        return True, "the item is sensitive (auth, payments, uploads or user input in its footprint, or risk class high)"
    return False, ""


def _verdict(text: str, tokens: Tuple[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """(verdict, problem). The `## Verdict` section must hold exactly one of the two tokens."""
    m = _VERDICT_RE.search(text)
    if not m:
        return None, "no `## Verdict` section"
    found = [t for t in tokens if re.search(r"\b%s\b" % t, m.group(1))]
    if len(found) != 1:
        return None, "the Verdict section must name exactly one of %s" % " | ".join(tokens)
    return found[0], None


def _fresh(ctx: Any, rel: str, reports_prefix: str) -> Optional[str]:
    """None when fresh, else why not."""
    head = _lib.head_of(ctx)
    rv = (_lib.git_ok(ctx, ["log", "-1", "--format=%H", head, "--", rel]) or "").strip()
    if not rv:
        return "the review file is not committed"
    code = (_lib.git_ok(ctx, ["log", "-1", "--format=%H", "%s..%s" % (ctx.base_sha, head), "--", ".", ":(exclude)%s" % reports_prefix.rstrip("/")]) or "").strip()
    if not code:
        return None
    if code == rv:
        return None
    proc = ctx.git(["merge-base", "--is-ancestor", code, rv])
    return None if proc.returncode == 0 else "code changed in %s after the review was saved in %s" % (code[:10], rv[:10])


def _waived(ctx: Any, ids: List[str]) -> Optional[str]:
    today = common.iso()[:10]
    for w in ctx.item.get("waivers") or []:
        if isinstance(w, dict) and plan_mod.waiver_active(w, today):
            covered = set(w.get("finding_ids") or [])
            if ids and set(ids) <= covered:
                return common.clean_text(w.get("approver"), 40)
    return None


def run(ctx: Any) -> GateResult:
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    slug = ctx.item.get("slug", "")
    prefix = "docs/features/%s/reports/" % slug
    try:
        locked.add_gate_globs(_lib.runstate_of(ctx), "g7", _lib.review_globs(ctx))
    except (OSError, ValueError):
        pass
    findings = []  # type: List[Dict[str, Any]]
    review = {}  # type: Dict[str, str]
    ids = _lib.ac_ids_of(ctx.item)

    # -- code review (always)
    rel = prefix + "code-review.md"
    text = _lib.blob(ctx, head, rel)
    if text is None:
        findings.append(_lib.finding("g7.code-review-missing", "error", "%s is not committed: the independent code-reviewer has not reviewed this item" % rel))
    else:
        v, why = _verdict(text, ("CODE_REVIEW_APPROVED", "CHANGES_REQUESTED"))
        blockers = len(re.findall(r"(?m)^\s*[-*]\s*\[BLOCKER\]", text))
        if why:
            findings.append(_lib.finding("g7.code-review-verdict", "error", "code review: %s" % why))
        else:
            review["code"] = v or ""
            if v == "CHANGES_REQUESTED":
                findings.append(_lib.finding("g7.code-review-blocked", "error", "code review requests changes (%d blocker(s))" % blockers))
            elif blockers:
                findings.append(_lib.finding("g7.code-review-inconsistent", "error", "code review says CODE_REVIEW_APPROVED but lists %d [BLOCKER] finding(s)" % blockers))
        title = " ".join([ln for ln in text.splitlines() if ln.strip()][:2])
        if slug not in title and not re.search(r"\b%s\b" % re.escape(ctx.item_id), title):
            findings.append(_lib.finding("g7.code-review-wrong-item", "error", "the code review's title does not name this item (%s or %s): a review saved for "
                                         "another item is not a review of this one" % (slug, ctx.item_id)))
        vr = _section(text, "Verification Run")
        if not re.search(r"(?m)^\s*[-*].*:\s*(?:PASS|FAIL)\b", vr):
            findings.append(_lib.finding("g7.reviewer-did-not-run-verify", "error", "the code review has no `## Verification Run` entry with PASS or FAIL: the reviewer must run the "
                                         "verify commands itself (CO-17)"))
        missing = [a for a in ids if not _lib.ac_pattern(a).search(text)]
        if missing:
            findings.append(_lib.finding("g7.review-ac-trace", "error", "the code review does not name criterion(s) %s: rubric 7 (traceability) was not applied" % ", ".join(missing)))
        stale = _fresh(ctx, rel, prefix)
        if stale:
            findings.append(_lib.finding("g7.code-review-stale", "error", "the code review is stale: %s" % stale))

    # -- security review
    need, why_need = security_required(ctx)
    rel = prefix + "security-review.md"
    text = _lib.blob(ctx, head, rel)
    if need and text is None:
        findings.append(_lib.finding("g7.security-review-missing", "error", "%s is not committed and %s: the security-reviewer must review this item" % (rel, why_need)))
    if text is not None:
        v, why = _verdict(text, ("SECURITY_APPROVED", "SECURITY_BLOCKED"))
        sev_blocks = re.split(r"(?m)^\s*-?\s*\*\*SEVERITY\*\*", text)[1:]
        crit_ids = []  # type: List[str]
        n_high = 0
        for blk in sev_blocks:
            if re.match(r"\s*:?\s*\[?\s*(Critical|High)\b", blk, re.I):
                n_high += 1
                m = re.search(r"SEC-\d+", blk)
                if m:
                    crit_ids.append(m.group(0))
        if why:
            findings.append(_lib.finding("g7.security-review-verdict", "error", "security review: %s" % why))
        else:
            review["security"] = v or ""
            if v == "SECURITY_APPROVED" and n_high:
                findings.append(_lib.finding("g7.security-review-inconsistent", "error", "security review says SECURITY_APPROVED but lists %d Critical or High finding(s)" % n_high))
            elif v == "SECURITY_BLOCKED":
                who = _waived(ctx, crit_ids)
                if who:
                    findings.append(_lib.finding("g7.security-waived", "warn", "SECURITY_BLOCKED, waived by %s in the plan for %s: a human sees this at merge" % (who, ", ".join(crit_ids))))
                    review["security"] = "SECURITY_BLOCKED (waived)"
                else:
                    findings.append(_lib.finding("g7.security-review-blocked", "error", "security review blocks (%d Critical or High finding(s): %s). A waiver in the plan "
                                                 "must name every finding id and be unexpired" % (n_high, ", ".join(crit_ids) or "ids missing")))
        if not re.search(r"(?i)scanner|gitleaks|semgrep|osv-scanner|npm audit|pip-audit|trivy", text):
            findings.append(_lib.finding("g7.security-scanners-unstated", "error", "the security review does not say which scanners ran or did not (CO-24)"))
        stale = _fresh(ctx, rel, prefix)
        if stale:
            findings.append(_lib.finding("g7.security-review-stale", "error", "the security review is stale: %s" % stale))
    elif not need:
        findings.append(_lib.finding("g7.security-not-required", "info", "no security review required (security_review: %s, item not sensitive)" % ctx.cfg.get("security_review")))

    # -- design review (optional)
    text = _lib.blob(ctx, head, prefix + "design-review.md")
    if text is not None:
        v, why = _verdict(text, ("DESIGN_APPROVED", "DESIGN_NEEDS_REVISION"))
        if why:
            findings.append(_lib.finding("g7.design-review-verdict", "warn", "design review: %s" % why))
        else:
            review["design"] = v or ""
            if v == "DESIGN_NEEDS_REVISION":
                findings.append(_lib.finding("g7.design-review-blocked", "error", "the design review still needs revision"))
    ev = {"summary": ", ".join("%s %s" % (k, v) for k, v in sorted(review.items())) or "no review on record", "review": review}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
