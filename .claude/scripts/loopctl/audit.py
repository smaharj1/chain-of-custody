"""The PR audit block, taken over from the conductor's template (report section 10.3 Audit; Phase 3's `pr-body.md`; CS-20).

Until the loopctl MVP the audit block was a template the conductor filled in by hand. From here loopctl builds it from facts it
holds (state, plan, gate results, engineer reports, the ledger, git) and leaves a `<fill: ...>` marker where it does NOT have
the fact, so `pr_prepare.validate_body` (Phase 3, reused, not copied) still refuses an incomplete block. The PR is the unit of
audit; squash merges cannot destroy a block that is in the PR body.

    render_body(ctx, st, iid)   -> markdown in the exact shape of `.claude/templates/pr-body.md`
    check_body(text, ctx, st, iid) -> findings: pr_prepare's textual rules plus cross-checks against state and git

Cross-checks (all `error`): the body's Head sha is the branch tip now; every plan acceptance criterion id appears in the matrix;
no row says PASS where state holds a failing result; Verify (G6) says PASS only if the recorded g6 result passed (when one exists);
Waivers lists every unexpired waiver of the item; the Notes carry the cost line (cost and model per agent are the audit's job,
§10.5); `CI-Fix-Of` names the commits that carry the trailer and their count stays within `max_ci_fix_attempts`.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from kitlib import gitutil

from . import common, plan as plan_mod
from .common import Finding

__all__ = ["render_body", "check_body", "collect_facts"]

_FILL = "<fill: %s>"


def _fill(what: str) -> str:
    return _FILL % what


def collect_facts(ctx: Any, st: Any, iid: str, lane: str = "full") -> Dict[str, Any]:
    import pr_prepare
    item = ctx.plan.items[iid]
    rec = st.items[iid]
    repo = ctx.repo
    slug = item["slug"]
    branch = rec.get("branch") or rec.get("parkedBranch")
    head = rec.get("headSha")
    if branch and gitutil.valid_ref("refs/heads/" + branch):
        head = gitutil.rev_parse("refs/heads/" + branch + "^{commit}", repo, isolated=True) or head
    base = ctx.integration_branch() or "main"
    base_sha = ctx.base_sha
    commits = []  # type: List[Dict[str, Any]]
    if head and base_sha:
        try:
            commits = pr_prepare.log_commits(repo, "%s..%s" % (base_sha, head))
        except Exception:  # noqa: BLE001
            commits = []
    ci_fix = [c["sha"] for c in commits if c["trailers"].get("ci-fix-of")]
    reports = pr_prepare.read_reports(repo, slug)
    waivers = [w for w in item.get("waivers") or [] if isinstance(w, dict) and plan_mod.waiver_active(w, common.iso()[:10])]
    run = None
    try:
        run = ctx.rs.read_run()
    except common.READ_ERRORS:
        pass
    cost = None
    if run and isinstance(run.get("runId"), str):
        cost = ctx.ledger().totals(run_id=run["runId"], item=iid)
    return {"item": item, "rec": rec, "slug": slug, "lane": lane, "delivery": ctx.cfg.get("delivery"), "branch": branch, "base": base,
            "base_sha": base_sha, "head": head, "commits": commits, "ci_fix": ci_fix, "reports": reports, "waivers": waivers, "cost": cost,
            "max_ci_fix": ctx.cfg.get_int("max_ci_fix_attempts")}


def _matrix_rows(item: Dict[str, Any], rec: Dict[str, Any]) -> List[str]:
    rows = []
    for ac in item.get("acceptance", []):
        cells = rec.get("ac", {}).get(ac["id"], {})
        passed = [c for c in cells.values() if c.get("result") == "pass"]
        failed = [c for c in cells.values() if c.get("result") == "fail"]
        crit = "%s: %s" % (ac["id"], common.md_cell(ac["text"], 160))
        if failed:
            rows.append("| %s | %s | FAIL |" % (crit, common.md_cell(", ".join(c.get("evidence") or g for g, c in cells.items() if c.get("result") == "fail"), 120)))
        elif passed:
            ev = ", ".join(sorted({"%s (%s)" % (c.get("evidence") or "executed", g) for g, c in cells.items() if c.get("result") == "pass"}))
            rows.append("| %s | %s | PASS |" % (crit, common.md_cell(ev, 160)))
        else:
            rows.append("| %s | %s | %s |" % (crit, _fill("the test or command that proves it"), _fill("PASS")))
    return rows


def render_body(ctx: Any, st: Any, iid: str, lane: str = "full") -> str:
    f = collect_facts(ctx, st, iid, lane)
    item, rec = f["item"], f["rec"]
    g6 = rec.get("gates", {}).get("g6")
    verify = ("PASS: g6 passed for %s" % (g6.get("head") or "")[:12]) if g6 and g6.get("status") == "pass" else _fill("PASS, then the verify.py run")
    sec = rec.get("review", {}).get("security")
    review = rec.get("review", {})
    design = review.get("design") or (_fill("DESIGN_APPROVED (round N), or N/A (quick lane)") if lane == "full" else "N/A (quick lane)")
    if lane == "quick":
        design = "N/A (quick lane)"
    gated = rec.get("humanGate") or []
    cif = ", ".join(s[:12] for s in f["ci_fix"]) or "NONE"
    waivers = "; ".join("%s until %s: %s" % (common.md_cell(w["approver"], 40), w["expiry"], ", ".join(w["finding_ids"][:4])) for w in f["waivers"]) or "NONE"
    title = common.clean_text(item.get("title"), 72)
    L = ["<!-- kit:pr-body v1. Built by loopctl audit from the run state; `python3 .claude/scripts/pr_prepare.py check` still validates it. -->",
         "# %s" % title, "", "## Summary", "", common.clean_text(item.get("intent"), 600), "", "## Audit block", "", "<!-- audit:begin v1 -->",
         "| Field | Value |", "|---|---|",
         "| Feature | %s |" % f["slug"], "| Lane | %s |" % lane, "| Delivery | %s |" % f["delivery"],
         "| Branch | %s |" % (f["branch"] or _fill("the feature branch")), "| Base | %s |" % f["base"],
         "| Base sha | %s |" % (f["base_sha"] or _fill("merge-base, 40 hex")), "| Head sha | %s |" % (f["head"] or _fill("the branch tip, 40 hex")),
         "| Design critic | %s |" % design, "| Verify (G6) | %s |" % verify,
         "| Security review | %s |" % (common.md_cell(sec, 60) if sec else _fill("SECURITY_APPROVED, or N/A (why), or NOT RUN (why)")),
         "| Human-gated paths | %s |" % (common.md_cell("; ".join(gated), 300) if gated else "NONE"),
         "| CI-Fix-Of | %s |" % cif, "| Waivers | %s |" % waivers,
         "| CI evidence | PENDING: the CI check runs on this pull request are the evidence |", "<!-- audit:end -->", "",
         "## Engineers", "", "| Engineer | Status | Iterations | Code review | Report | Commits |", "|---|---|---|---|---|---|"]
    code_review = review.get("code") or _fill("CODE_REVIEW_APPROVED")
    short = ", ".join(c["sha"][:7] for c in f["commits"][:6]) or _fill("short shas of the WIP commits")
    if f["reports"]:
        for r in f["reports"]:
            L.append("| %s | %s | %s | %s | %s | %s |" % (common.md_cell(r["engineer"], 30), common.md_cell(r["status"] or _fill("APPROVED"), 30),
                                                          common.md_cell(r.get("iteration") or _fill("number"), 4), common.md_cell(code_review, 40),
                                                          common.md_cell(r["path"], 120), short))
    else:
        L.append("| %s | %s | %s | %s | %s | %s |" % (_fill("engineer"), _fill("APPROVED"), _fill("number"), common.md_cell(code_review, 40),
                                                      _fill("report path, or inline"), short))
    L += ["", "## Acceptance matrix", "", "Every criterion needs named evidence (a test, a command, a route) and PASS, from the gates' recorded results.", "",
          "| Criterion | Evidence | Result |", "|---|---|---|"] + _matrix_rows(item, rec)
    primer = _deltas(f["reports"], "Primer Delta")
    docd = _deltas(f["reports"], "Doc Delta")
    L += ["", "## Doc Delta", "", docd or _fill("NONE with a checked: line, or each doc that must change"), "", "## Primer Delta", "",
          primer or _fill("NONE with a checked: line, or each primer change applied"), "", "## Notes", "",
          _notes(f, rec), "", "---", "Prepared by an agent from a feature branch. A human pushed it and a human merges it.", ""]
    return "\n".join(L)


def _deltas(reports: Sequence[Dict[str, Any]], name: str) -> str:
    parts = []  # type: List[str]
    checked = []  # type: List[str]
    for r in reports:
        body = r.get("sections", {}).get(name, "").strip()
        if not body:
            continue
        if body.split(None, 1)[0] == "NONE":
            m = re.search(r"checked:\s*(.+)", body)
            if m:
                checked.append(m.group(1).strip())
        else:
            parts.append("%s: %s" % (r["engineer"], common.clean_text(body, 400)))
    if parts:
        return "\n".join(parts)
    if checked and len(checked) == len([r for r in reports if r.get("sections", {}).get(name, "").strip()]):
        return "NONE\nchecked: " + "; ".join(sorted(set(checked)))
    return ""


def _notes(f: Dict[str, Any], rec: Dict[str, Any]) -> str:
    L = []  # type: List[str]
    c = f["cost"]
    if c is None:
        L.append("Cost: not recorded (no ledger entries for this run; sequential runs have no per-child cost).")
    else:
        L.append("Cost: $%.4f over %d child session(s)%s." % (c["usd"], c["entries"], "; %d entries had no usable cost" % c["unknown"] if c["unknown"] else ""))
    gates = rec.get("gates", {})
    if gates:
        L.append("Gates: " + ", ".join("%s %s" % (g, v.get("status")) + ("" if v.get("blocking") or v.get("mode") == "on" else " (report only)")
                                       for g, v in sorted(gates.items())) + ".")
    counters = rec.get("counters", {})
    L.append("Attempt %d; retries used: verify %d, review %d, partial %d." % (rec.get("attempt", 1), counters.get("verify", 0),
                                                                              counters.get("review", 0), counters.get("partial", 0)))
    return "\n".join(L)


def check_body(text: str, ctx: Any, st: Any, iid: str, lane: Optional[str] = None) -> List[Finding]:
    import pr_prepare
    out = []  # type: List[Finding]
    for f in pr_prepare.validate_body(text, lane):
        out.append(Finding(f.severity, f.id, f.message, iid))
    pb = pr_prepare.parse_body(text)
    facts = collect_facts(ctx, st, iid, lane or pb.fields.get("Lane", "full"))
    item, rec = facts["item"], facts["rec"]

    def err(fid: str, msg: str) -> None:
        out.append(Finding("error", fid, msg, iid))

    head = pb.fields.get("Head sha", "")
    if facts["head"] and head and head != facts["head"]:
        err("audit.stale-head", "the body's Head sha %s is not the branch tip %s: the audit block is stale" % (head[:12], facts["head"][:12]))
    if facts["base_sha"] and pb.fields.get("Base sha") and pb.fields["Base sha"] != facts["base_sha"]:
        err("audit.base-sha", "the body's Base sha is not the sha loopctl read the config from")
    crit_text = " ".join(r.get("Criterion", "") for r in pb.matrix)
    for ac in item.get("acceptance", []):
        if ac["id"] not in crit_text:
            err("audit.ac-missing", "acceptance criterion %s is not in the Acceptance matrix" % ac["id"])
    for r in pb.matrix:
        m = re.match(r"^(AC-[A-Za-z0-9]+-\d+)", r.get("Criterion", ""))
        if m and r.get("Result", "").strip() == "PASS":
            cells = rec.get("ac", {}).get(m.group(1), {})
            if any(c.get("result") == "fail" for c in cells.values()):
                err("audit.matrix-contradicts-state", "%s says PASS but a gate recorded a failure for it" % m.group(1))
            if not any(c.get("result") == "pass" for c in cells.values()):
                out.append(Finding("warn", "audit.matrix-unrecorded", "%s says PASS with no gate result recorded in the state" % m.group(1), iid))
    g6 = rec.get("gates", {}).get("g6")
    if pb.fields.get("Verify (G6)", "").startswith("PASS") and g6 and g6.get("status") != "pass":
        err("audit.verify-contradicts-state", "Verify (G6) says PASS but the recorded g6 result is %s" % g6.get("status"))
    if len(facts["ci_fix"]) > facts["max_ci_fix"]:
        err("audit.ci-fix-cap", "%d CI-fix commits exceed max_ci_fix_attempts %d" % (len(facts["ci_fix"]), facts["max_ci_fix"]))
    cif = pb.fields.get("CI-Fix-Of", "")
    if facts["ci_fix"] and cif.split(None, 1)[0:1] == ["NONE"]:
        err("audit.ci-fix-hidden", "commits carry CI-Fix-Of trailers but the audit block says NONE")
    for w in facts["waivers"]:
        if w["approver"].split()[0] not in pb.fields.get("Waivers", ""):
            err("audit.waiver-missing", "the waiver by %s is not listed in the audit block" % common.clean_text(w["approver"], 40))
    if "Cost:" not in pb.sections.get("Notes", ""):
        err("audit.cost-line", "the Notes carry no 'Cost:' line (cost per PR is part of the audit, report section 10.5)")
    hg = rec.get("humanGate") or []
    if hg and pb.fields.get("Human-gated paths", "").split(None, 1)[0:1] == ["NONE"]:
        err("audit.human-gate-hidden", "the item is human-gated (%s) but the audit block says NONE" % common.clean_text("; ".join(hg[:2]), 80))
    return out
