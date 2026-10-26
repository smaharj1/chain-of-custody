"""`status`: where the run is, what waits on a human, the AC x evidence matrix, the bypass log (report section 10.2).

    status [--json]            the run: plan, items, caps, spend, lock, HALT, config drift
    status --pending-human     only what is waiting on a person, each with a link and the exact command (CO-44)
    status --matrix [ITEM]     the AC x evidence matrix: G8's view over G3 and G7 results (it is a view, not a gate)
    status --bypass            the bypass log: GitHub ruleset bypasses ingested by `reconcile --apply` (RC-9)

Read-only. It never creates the state ref, takes no lock and writes nothing. If the state ref is not a valid loopctl state
the document says so (`state.error`) instead of showing anything made up.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from kitlib import runstate

from . import common, ledger as ledger_mod, plan as plan_mod, state as state_mod

__all__ = ["collect", "pending_human", "ac_matrix", "format_text", "format_pending", "format_matrix", "format_bypass"]

GATE_ORDER = ("g1", "g3", "g5", "g6", "g7", "g9", "g11")


def _read_state(ctx: Any) -> Dict[str, Any]:
    out = {"state": None, "head": None, "error": None}  # type: Dict[str, Any]
    if ctx.plan is None or not ctx.plan.name:
        out["error"] = "no plan"
        return out
    try:
        st, head = ctx.store().read()
        out["state"], out["head"] = st, head
    except state_mod.StateCorrupt as exc:
        out["error"] = str(exc)
    except common.LoopctlError as exc:
        out["error"] = str(exc)
    return out


def ac_matrix(lp: Any, st: Any, only: Optional[str] = None) -> Dict[str, Any]:
    """Rows per AC, one cell per gate that reported on it. `coverage` is G1's owner metric: the share of ACs with at least one
    executed passing test and no failing one."""
    rows = []  # type: List[Dict[str, Any]]
    gates_seen = set()  # type: set
    covered = total = 0
    for iid in lp.ids():
        if only and iid != only:
            continue
        rec = st.items.get(iid) if st is not None else None
        for ac in lp.items[iid].get("acceptance", []):
            if not isinstance(ac, dict) or not isinstance(ac.get("id"), str):
                continue
            cells = {}  # type: Dict[str, Dict[str, Any]]
            if rec:
                for g, cell in rec.get("ac", {}).get(ac["id"], {}).items():
                    cells[g] = cell
                    gates_seen.add(g)
            results = [c.get("result") for c in cells.values()]
            verdict = "fail" if "fail" in results else ("pass" if "pass" in results else "none")
            total += 1
            covered += 1 if verdict == "pass" else 0
            rows.append({"item": iid, "ac": ac["id"], "text": common.clean_text(ac.get("text"), 120), "cells": cells, "verdict": verdict,
                         "item_status": rec["status"] if rec else "pending"})
    cols = [g for g in GATE_ORDER if g in gates_seen] + sorted(gates_seen - set(GATE_ORDER))
    return {"columns": cols, "rows": rows, "coverage": {"covered": covered, "total": total,
                                                       "percent": round(100.0 * covered / total, 1) if total else 0.0}}


def pending_human(ctx: Any, st: Any, lp: Any, doc_state_error: Optional[str] = None) -> List[Dict[str, Any]]:
    """Everything waiting on a human, each with the command to answer it and a link when there is one."""
    out = []  # type: List[Dict[str, Any]]

    def add(kind: str, item: Optional[str], why: str, cmd: str, link: Optional[str] = None) -> None:
        out.append({"kind": kind, "item": item, "why": common.clean_text(why, 220), "command": cmd, "link": link})

    if doc_state_error:
        add("state-unreadable", None, doc_state_error, "inspect refs/loopctl/state/%s with git; do not continue on it" % (lp.name or "<plan>"))
        return out
    try:
        halted = ctx.rs.is_halted()
    except OSError:
        halted = True
    if halted:
        h = ctx.rs.read_halt() or {}
        add("halt", None, "HALT is set: %s (by %s)" % (h.get("reason", "no reason recorded"), h.get("by", "?")), "loopctl resume   (from a plain terminal)")
    if st is None:
        if lp.plan is not None:
            add("approve-plan", None, "the plan is not approved yet (H1): its acceptance criteria are what you approve",
                "loopctl approve plan --by <you>")
        return out
    ap = st.header.get("approvedPlan")
    cur = plan_mod.plan_digest(lp) if lp.plan is not None and lp.items else None
    if ap and cur and ap["digest"] != cur:
        add("approve-plan", None, "the plan changed since you approved it (approved %s, now %s) (H2)" % (ap["digest"][:12], cur[:12]),
            "loopctl approve plan --by <you>")
    elif not ap:
        add("approve-plan", None, "the plan is not approved yet (H1)", "loopctl approve plan --by <you>")
    for iid in st.by_status("awaiting_human"):
        r = st.items[iid]
        gate = r.get("humanGate") or []
        add("approve-item", iid, "item %s is waiting for your review%s" % (iid, (" (human gate: " + "; ".join(gate[:2]) + ")") if gate else ""),
            "loopctl approve item %s --by <you>   (or --choice reject --note ...)" % iid, r.get("prUrl"))
    for iid in st.by_status("needs_human", "blocked"):
        r = st.items[iid]
        add("unblock", iid, "item %s is %s: %s" % (iid, r["status"], r.get("blockReason")),
            "loopctl approve unblock %s --choice reset --by <you>   (or fix it, or `keep`, or `replan`)" % iid, r.get("prUrl"))
    if st.header.get("levelCap"):
        lc = st.header["levelCap"]
        add("env", lc.get("item"), "the effective level is capped to %s: %s" % (lc.get("level"), lc.get("reason")),
            "fix the environment, run `loopctl doctor`, then `loopctl approve env --by <you>`")
    if st.header.get("runStatus") == "stopped":
        add("breaker", None, "the circuit breaker tripped after %d consecutive blocked items" % st.header.get("breaker", 0),
            "loopctl approve breaker --by <you>")
    verdict = None
    run = None
    try:
        run = ctx.rs.read_run()
    except common.READ_ERRORS:
        run = None
    if run and isinstance(run.get("runId"), str):
        verdict = ctx.ledger().verdict(ctx.cfg, run["runId"])
    elif ctx.rs.exists():
        tot = ctx.ledger().totals()
        if tot["unknown"]:
            verdict = ledger_mod.Verdict()
            verdict.fail("cost_unknown", "%d spend entries have no usable cost" % tot["unknown"])
    if verdict is not None and not verdict.ok:
        add("cost", None, "; ".join(verdict.reasons), "check total_cost_usd in the child result, then `loopctl approve cost --by <you>`; "
            "raise the budget in loop.config (H6) if it is a ceiling")
    today = common.iso()[:10]
    for iid in lp.ids():
        for w in lp.items[iid].get("waivers") or []:
            if isinstance(w, dict) and isinstance(w.get("expiry"), str):
                if w["expiry"] < today and st.items.get(iid, {}).get("status") not in ("merged", "abandoned"):
                    add("waiver", iid, "the security waiver by %s on item %s expired %s" % (common.clean_text(w.get("approver"), 40), iid, w["expiry"]),
                        "renew it through a plan PR (H6) or resolve the findings")
    if ctx.config_drift:
        names = ", ".join(n for n, _b, _h in ctx.config_drift[:6])
        add("config-drift", None, "protected loop.config keys differ in the working tree from the base sha (%s): not in effect until merged" % names,
            "review the change (H6); loopctl keeps using the base sha's values")
    ab = st.header.get("abort")
    if isinstance(ab, dict) and ab.get("revertPlan"):
        add("revert-plan", None, "an abort left a revert plan for %d item(s) that already reached main or a shared database" % len(ab["revertPlan"]),
            "run the plan by hand: loopctl abort --dry-run shows it; it is in %s" % ab.get("planFile", "the abort record"))
    lk = ctx.lock().status()
    if lk["held"] and (lk["corrupt"] or lk["stale"]):
        add("stale-lock", None, "the run lock is %s" % ("unreadable" if lk["corrupt"] else "stale (no heartbeat for %s min)" % lk["age_min"]),
            "loopctl lock steal" + (" --corrupt" if lk["corrupt"] else ""))
    return out


def collect(ctx: Any, include_level: bool = False) -> Dict[str, Any]:
    lp = ctx.plan
    rep = plan_mod.validate(lp, max_item_loc=ctx.cfg.get_int("max_item_loc"), roster=ctx.roster(),
                            always_human_paths=ctx.cfg.get_list("always_human_paths"))
    rs = _read_state(ctx)
    st = rs["state"]
    lk = ctx.lock().status()
    doc = {"schema": "loopctl.status.v1", "plan": lp.name, "plan_source": ctx.plan_source, "plan_trusted": ctx.plan_trusted,
           "base_sha": ctx.base_sha, "config": {"note": ctx.cfg_note, "trusted": ctx.cfg_trusted, "tier2": ctx.tier2, "runner": ctx.runner,
                                                "drift": [n for n, _b, _h in ctx.config_drift]},
           "plan_ok": rep.ok, "plan_errors": [f.to_dict() for f in rep.errors][:10],
           "state": {"present": st is not None, "commit": rs["head"], "seq": st.seq if st else None, "error": rs["error"]},
           "evidence": {"passed": ctx.evidence_passed(), "reason": ctx.evidence().reason},
           "run": {"lock": {k: v for k, v in lk.items() if k != "record"}, "lock_record": lk.get("record"),
                   "halt": (ctx.rs.read_halt() if ctx.rs.is_halted() else None)}}
    run_rec = None
    try:
        run_rec = ctx.rs.read_run()
    except common.READ_ERRORS:
        pass
    doc["run"]["record"] = run_rec
    items = []  # type: List[Dict[str, Any]]
    if st is not None:
        h = st.header
        doc["run"].update({"status": h.get("runStatus"), "breaker": h.get("breaker"), "parked": h.get("parked"), "level_cap": h.get("levelCap"),
                           "plan_approved": bool(h.get("approvedPlan")), "approved_by": (h.get("approvedPlan") or {}).get("by"),
                           "abort": h.get("abort")})
        for iid in rep.order or lp.ids():
            r = st.items.get(iid)
            if r is None:
                items.append({"id": iid, "status": "pending", "tracked": False})
                continue
            items.append({"id": iid, "tracked": True, "status": r["status"], "blockReason": r.get("blockReason"), "attempt": r["attempt"],
                          "branch": r.get("branch"), "mergeCommit": r.get("mergeCommit"), "prUrl": r.get("prUrl"),
                          "counters": r["counters"], "lastFailure": r.get("lastFailure"), "overlay": r.get("overlay"),
                          "gates": {g: v.get("status") for g, v in r.get("gates", {}).items()}, "humanGate": r.get("humanGate")})
        deps = {i: [d for d in lp.items[i].get("dependsOn", []) if isinstance(d, str)] for i in lp.items}
        elig = st.eligible(rep.order, deps)
        doc["eligible"] = elig
        remaining = [i for i in st.items if st.items[i]["status"] not in ("merged", "abandoned")]
        doc["deadlock"] = bool(remaining) and not elig and not st.by_status(*state_mod.IN_FLIGHT)   # DEADLOCK is run-level, not an item status
        blocked = set(st.by_status("blocked", "needs_human", "abandoned"))
        stuck = [i for i in remaining if st.items[i]["status"] == "pending" and any(d in blocked for d in plan_mod.ancestors(lp.items, i) | set(deps.get(i, [])))]
        doc["blocked_downstream"] = sorted(stuck, key=common.natural_key)
        doc["counts"] = st.counts()
        doc["bypass"] = list(h.get("bypass") or [])[-50:]
    else:
        items = [{"id": i, "status": "pending", "tracked": False} for i in (rep.order or lp.ids())]
        doc["counts"] = {s: (len(items) if s == "pending" else 0) for s in state_mod.STATUSES}
        doc["bypass"] = []
    doc["items"] = items
    doc["spend"] = _spend(ctx, run_rec)
    doc["pending_human"] = pending_human(ctx, st, lp, rs["error"])
    doc["matrix"] = ac_matrix(lp, st)
    if include_level:
        doc["level"] = ctx.effective_level()
    return doc


def _spend(ctx: Any, run_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    led = ctx.ledger()
    run_id = run_rec.get("runId") if isinstance(run_rec, dict) else None
    out = {"ceilings": {"run": ctx.cfg.get("run_budget_usd"), "item": ctx.cfg.get("item_budget_usd"), "day": ctx.cfg.get("day_budget_usd")},
           "day": led.totals(day=common.iso()[:10])}  # type: Dict[str, Any]
    if isinstance(run_id, str):
        out["run"] = led.totals(run_id=run_id)
        out["verdict"] = led.verdict(ctx.cfg, run_id).to_dict()
    return out


# -- text -----------------------------------------------------------------------------------------------------------------------

def format_text(doc: Dict[str, Any]) -> str:
    L = []  # type: List[str]
    L.append("loopctl status: plan %s (%s)" % (common.clean_text(doc["plan"], 40), common.clean_text(doc["plan_source"], 70)))
    L.append("  base %s; %s" % ((doc["base_sha"] or "none")[:12], common.clean_text(doc["config"]["note"], 100)))
    if not doc["plan_ok"]:
        L.append("  PLAN HAS ERRORS: " + "; ".join(common.clean_text(e["message"], 80) for e in doc["plan_errors"][:3]))
    st = doc["state"]
    if st["error"]:
        L.append("  STATE UNREADABLE: %s" % common.clean_text(st["error"], 200))
    elif not st["present"]:
        L.append("  no state yet (`loopctl approve plan --by <you>` creates it)")
    else:
        run = doc["run"]
        L.append("  state seq %s; run %s; plan %s; breaker %s" % (st["seq"], run.get("status"), "approved" if run.get("plan_approved") else "NOT approved",
                                                                run.get("breaker")))
    L.append("  Tier 2 writes: %s; runner: %s; evidence gate: %s" % ("on" if doc["config"]["tier2"] else "off", doc["config"]["runner"],
                                                                  "passed" if doc["evidence"]["passed"] else "not passed (level capped at L3)"))
    lk = doc["run"]["lock"]
    L.append("  lock: %s" % ("none" if not lk["held"] else ("UNREADABLE" if lk["corrupt"] else
             "held by %s (heartbeat %s min ago%s)" % (common.clean_text((doc["run"].get("lock_record") or {}).get("sessionId"), 30), lk["age_min"],
                                                      ", STALE" if lk["stale"] else ""))))
    if doc["run"].get("halt"):
        L.append("  HALT: %s" % common.clean_text(doc["run"]["halt"].get("reason"), 100))
    if doc["run"].get("level_cap"):
        L.append("  level capped to %s: %s" % (doc["run"]["level_cap"].get("level"), common.clean_text(doc["run"]["level_cap"].get("reason"), 80)))
    if doc["config"]["drift"]:
        L.append("  config drift (working tree differs from base for protected keys): %s" % ", ".join(doc["config"]["drift"][:6]))
    L.append("")
    L.append("  %-8s %-15s %-4s %s" % ("ITEM", "STATUS", "TRY", "NOTES"))
    for it in doc["items"]:
        notes = []
        if it.get("blockReason"):
            notes.append(common.clean_text(it["blockReason"], 40))
        if it.get("branch"):
            notes.append(it["branch"])
        if it.get("prUrl"):
            notes.append(it["prUrl"])
        if it.get("lastFailure") and it["status"] not in ("merged",):
            lf = it["lastFailure"]
            notes.append("last failure %s/%s" % (lf.get("class"), lf.get("reason")))
        L.append("  %-8s %-15s %-4s %s" % (common.clean_text(it["id"], 8), it["status"], it.get("attempt", 1) if it.get("tracked") else "-", "; ".join(notes)))
    c = doc["counts"]
    L.append("")
    L.append("  %d merged, %d in progress, %d waiting on a human, %d blocked, %d pending" % (
        c.get("merged", 0), sum(c.get(s, 0) for s in ("designing", "design_review", "stubbing", "building", "verifying", "reviewing", "pr_open")),
        c.get("awaiting_human", 0) + c.get("needs_human", 0), c.get("blocked", 0), c.get("pending", 0)))
    sp = doc["spend"]
    if "run" in sp:
        L.append("  spend: run $%.4f of %s; today $%.4f; %d unknown entr%s" % (
            sp["run"]["usd"], ("$%.2f" % sp["ceilings"]["run"]) if sp["ceilings"]["run"] else "no ceiling", sp["day"]["usd"], sp["run"]["unknown"],
            "y" if sp["run"]["unknown"] == 1 else "ies"))
    mt = doc["matrix"]["coverage"]
    L.append("  AC coverage: %d of %d criteria have a passing test (%.1f%%)" % (mt["covered"], mt["total"], mt["percent"]))
    if doc["pending_human"]:
        L.append("")
        L.append("  Waiting on a human (%d): `loopctl status --pending-human`" % len(doc["pending_human"]))
    if doc.get("blocked_downstream"):
        L.append("  Pending but stuck behind a blocked item: %s" % ", ".join(doc["blocked_downstream"][:8]))
    return "\n".join(L) + "\n"


def format_pending(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "Nothing is waiting on a human.\n"
    L = ["Waiting on a human (%d):" % len(items), ""]
    for n, p in enumerate(items, 1):
        L.append("%d. [%s]%s %s" % (n, p["kind"], " " + p["item"] if p.get("item") else "", p["why"]))
        if p.get("link"):
            L.append("   link: %s" % common.clean_text(p["link"], 200))
        L.append("   do:   %s" % p["command"])
    return "\n".join(L) + "\n"


def format_matrix(m: Dict[str, Any]) -> str:
    cols = m["columns"]
    L = ["%-9s %-10s %s  %s" % ("ITEM", "AC", " ".join("%-5s" % c for c in cols), "RESULT")]
    for r in m["rows"]:
        cells = " ".join("%-5s" % (r["cells"].get(c, {}).get("result", "-")) for c in cols)
        L.append("%-9s %-10s %s  %s" % (r["item"], r["ac"], cells, r["verdict"]))
    cv = m["coverage"]
    L.append("")
    L.append("coverage: %d/%d criteria with a passing executed test (%.1f%%)" % (cv["covered"], cv["total"], cv["percent"]))
    return "\n".join(L) + "\n"


def format_bypass(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return "Bypass log: no ruleset bypass has been recorded.\n"
    L = ["Bypass log (%d):" % len(entries)]
    for e in entries:
        L.append("  %s  %s  %s  %s" % (common.clean_text(e.get("at"), 24), common.clean_text(e.get("actor"), 30), common.clean_text(e.get("ref"), 40),
                                       common.clean_text(e.get("note"), 80)))
    return "\n".join(L) + "\n"
