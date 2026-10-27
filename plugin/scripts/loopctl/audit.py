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

The Notes also carry a `Manifest:` line (Phase 11b's carry-over): the hash of the reproducibility manifest `loopctl manifest write` stored for this
item's head, and whether `manifest.verify` still holds against git. The manifest is opt-in, so none written is only a note; a stored one that does
not verify, or a body whose `Manifest:` hash is not the stored one, is an error. That puts the hash where the writer of the manifest cannot edit it.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    reports = _reports_at(ctx, head, slug) if head else pr_prepare.read_reports(repo, slug)
    waivers = [w for w in item.get("waivers") or [] if isinstance(w, dict) and plan_mod.waiver_active(w, common.iso()[:10])]
    run = None
    try:
        run = ctx.rs.read_run()
    except common.READ_ERRORS:
        pass
    cost = None
    if run and isinstance(run.get("runId"), str):
        cost = ctx.ledger().totals(run_id=run["runId"], item=iid)
    return {"manifest": _stored_manifest(ctx, iid, head), "item": item, "rec": rec, "slug": slug, "lane": lane, "delivery": ctx.cfg.get("delivery"), "branch": branch, "base": base,
            "base_sha": base_sha, "head": head, "commits": commits, "ci_fix": ci_fix, "reports": reports, "waivers": waivers, "cost": cost,
            "max_ci_fix": ctx.cfg.get_int("max_ci_fix_attempts")}


def _stored_manifest(ctx: Any, iid: str, head: Optional[str]) -> Optional[Dict[str, Any]]:
    """The newest reproducibility manifest stored for this item at this head: {doc, name}, or None. Never raises: the manifest is opt-in."""
    if not head:
        return None
    from . import manifest as mf
    try:
        d = mf.manifests_dir(ctx.rs)
        names = [n for n in os.listdir(d) if mf._NAME_RE.match(n) and n.endswith("-%s.json" % head[:12])]
    except OSError:
        return None
    best = None  # type: Optional[Tuple[float, str, Dict[str, Any]]]
    for n in names:
        try:
            doc = mf.read_file(os.path.join(d, n))
            mtime = os.stat(os.path.join(d, n)).st_mtime
        except (common.LoopctlError, OSError):
            continue
        if (doc.get("plan") or {}).get("item") != iid or (doc.get("repo") or {}).get("head") != head:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, n, doc)
    return {"name": best[1], "doc": best[2]} if best else None


def manifest_result(ctx: Any, stored: Optional[Dict[str, Any]], expect_hash: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`manifest.verify` of a stored manifest (None when there is none). A verify that cannot run counts as not holding."""
    if not stored:
        return None
    from . import manifest as mf
    try:
        return mf.verify(stored["doc"], ctx.repo, expect_hash)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "problems": ["the manifest could not be checked: %s" % common.clean_text(exc, 80)], "facts": [], "checked": []}


def _manifest_line(ctx: Any, stored: Optional[Dict[str, Any]]) -> str:
    if not stored:
        return ("Manifest: none written for this head (optional: `loopctl manifest write ITEM --head <head sha>`, then render this block again; "
                "the hash here is what `manifest verify --expect-hash` needs).")
    h = common.clean_text(stored["doc"].get("hash"), 70)
    res = manifest_result(ctx, stored) or {}
    if res.get("ok"):
        return "Manifest: %s (seal and %s re-read from git at the recorded commits: they hold)." % (h, ", ".join(res.get("checked") or ["nothing"]))
    why = "; ".join(common.md_text(p, 120) for p in (res.get("problems") or ["unknown"])[:2])
    return "Manifest: %s DOES NOT HOLD (%s)." % (h, why)


def _reports_at(ctx: Any, head: str, slug: str) -> List[Dict[str, Any]]:
    """The engineer reports as committed on the audited branch (docs/features/<slug>/reports/*.md at `head`), parsed with pr_prepare."""
    import pr_prepare
    from . import gitio
    base = "docs/features/%s/reports" % slug
    out = []  # type: List[Dict[str, Any]]
    try:
        rows = gitio.ls_tree(ctx.repo, head, [base + "/"], recursive=False)
    except ValueError:
        return out
    pick = [r for r in rows if r["type"] == "blob" and r["mode"] in ("100644", "100755") and str(r["path"]).endswith(".md") and 0 <= int(r["size"]) <= 1 << 20]
    blobs = gitio.cat_blobs(ctx.repo, [str(r["sha"]) for r in pick], 1 << 20)
    for r in sorted(pick, key=lambda r: str(r["path"])):
        data = blobs.get(str(r["sha"]))
        if data is None:
            continue
        rep = pr_prepare.parse_report(data.decode("utf-8", "replace"))
        rep["engineer"] = os.path.basename(str(r["path"]))[:-3]
        rep["path"] = str(r["path"])
        out.append(rep)
    return out


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
    design = (common.md_cell(review.get("design"), 60) if review.get("design")
              else (_fill("DESIGN_APPROVED (round N), or N/A (quick lane)") if lane == "full" else "N/A (quick lane)"))
    if lane == "quick":
        design = "N/A (quick lane)"
    gated = rec.get("humanGate") or []
    cif = ", ".join(s[:12] for s in f["ci_fix"]) or "NONE"
    waivers = "; ".join("%s until %s: %s" % (common.md_cell(w["approver"], 40), w["expiry"], ", ".join(w["finding_ids"][:4])) for w in f["waivers"]) or "NONE"
    title = common.md_text(item.get("title"), 72)
    L = ["<!-- kit:pr-body v1. Built by loopctl audit from the run state; `python3 .claude/scripts/pr_prepare.py check` still validates it. -->",
         "# %s" % title, "", "## Summary", "", common.md_text(item.get("intent"), 600), "", "## Audit block", "", "<!-- audit:begin v1 -->",
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
          _notes(f, rec, _manifest_line(ctx, f["manifest"])), "", "---", "Prepared by an agent from a feature branch. A human pushed it and a human merges it.", ""]
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
                checked.append(common.md_text(m.group(1), 200))
        else:
            parts.append("%s: %s" % (common.md_text(r["engineer"], 40), common.md_text(body, 400)))      # UN-11: these lines reach the PR body: no < or >
    if parts:
        return "\n".join(parts)
    if checked and len(checked) == len([r for r in reports if r.get("sections", {}).get(name, "").strip()]):
        return "NONE\nchecked: " + "; ".join(sorted(set(checked)))
    return ""


def _notes(f: Dict[str, Any], rec: Dict[str, Any], manifest_line: str = "") -> str:
    L = []  # type: List[str]
    c = f["cost"]
    if c is None or (c["entries"] == 0 and not c["unknown"]):
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
    if manifest_line:
        L.append(manifest_line)
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
    stored = facts["manifest"]
    named = re.search(r"^Manifest:\s*([0-9a-f]{64})\b", pb.sections.get("Notes", ""), re.M)
    if stored:
        res = manifest_result(ctx, stored, named.group(1) if named else None)
        if res and not res.get("ok"):
            err("audit.manifest-broken", "the stored reproducibility manifest for this head does not verify (%s)"
                % common.clean_text("; ".join(res.get("problems") or ["unknown"]), 160))
        elif not named:
            out.append(Finding("warn", "audit.manifest-absent", "a reproducibility manifest is stored for this head but the Notes carry no 'Manifest: <hash>' line", iid))
    elif named:
        out.append(Finding("warn", "audit.manifest-unknown", "the Notes name a manifest hash this machine has no stored manifest for (written elsewhere?): not checked", iid))
    hg = rec.get("humanGate") or []
    if hg and pb.fields.get("Human-gated paths", "").split(None, 1)[0:1] == ["NONE"]:
        err("audit.human-gate-hidden", "the item is human-gated (%s) but the audit block says NONE" % common.clean_text("; ".join(hg[:2]), 80))
    return out
