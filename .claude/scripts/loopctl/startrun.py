"""`run` and `run --dry-run` (report section 11.2, 15 item 6, 5.8; DEC-23; IMP-1).

`runner: sequential` (the default): the prose loop is the runner. `run` checks everything it can check, takes the run lock,
registers the calling session as the conductor, writes run.json and PRINTS the hand-off, then exits. The conductor keeps the
judgement work (design, briefs, integration conflicts, summaries) and calls the other loopctl commands for every deterministic
decision.

`runner: driver` (OPT-IN): `run` imports `loopctl.driver` lazily and calls `driver.run(ctx, loop, run_record, as_json)`. If the
module is not installed it says "driver not installed" and exits 2 before it touches the lock.

CANARY (RC-7, section 5.8): a sequential run needs `--session` and the `--nonce` the SessionStart hook printed for that session.
A session without a nonce does not dispatch: the hooks may not be firing, and then none of the guards can be trusted.

`--dry-run` renders the plan, the gates with their toggles, the runner, the budget projection and every preflight result, and
executes nothing (§15 item 6).
"""
from __future__ import annotations

import hmac
import importlib
import os
import secrets
from typing import Any, Dict, List, Mapping, Optional, Tuple

from kitlib import gitutil, runstate

from . import lgit, common, gates, ledger, lock as lock_mod, plan as plan_mod
from .common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Refused

__all__ = ["preflight", "dry_run", "start", "handoff_text", "load_driver"]


def load_driver() -> Any:
    """The driver module (Phase 4c), or None if it is not installed."""
    try:
        return importlib.import_module("loopctl.driver")
    except ImportError as exc:
        if getattr(exc, "name", "") in ("loopctl.driver", "driver"):
            return None
        raise


def _tree_clean(ctx: Any) -> bool:
    return lgit.is_clean(ctx.repo)


def preflight(ctx: Any, session: Optional[str], nonce: Optional[str]) -> List[Dict[str, Any]]:
    """Every check `run` makes, as [{id, ok, message}]. Never raises; never writes."""
    res = []  # type: List[Dict[str, Any]]

    def chk(cid: str, ok: bool, msg: str) -> None:
        res.append({"id": cid, "ok": bool(ok), "message": msg})

    chk("config", ctx.cfg_trusted, ctx.cfg_note)
    chk("tier2", ctx.tier2, "loopctl_tier2 is %s (OPT-IN; set in the base sha's loop.config)" % ("on" if ctx.tier2 else "off"))
    bad = ctx.opt_in_violations()
    chk("evidence-gated", not bad, ("loop.config sets %s but the evidence gate has not passed" % ", ".join(bad)) if bad else "no evidence-gated value is set")
    chk("plan", bool(ctx.plan and not ctx.plan.fatal and ctx.plan_trusted), "plan read at %s" % ctx.plan_source if ctx.plan else "no plan")
    state, err = None, None
    if ctx.plan and not ctx.plan.fatal:
        rep = plan_mod.validate(ctx.plan, max_item_loc=ctx.cfg.get_int("max_item_loc"), roster=ctx.roster(),
                                always_human_paths=ctx.cfg.get_list("always_human_paths"))
        chk("plan-valid", rep.ok, "the plan validates" if rep.ok else "%d plan error(s): %s" % (len(rep.errors), common.clean_text(rep.errors[0].message, 100)))
        try:
            state, _ = ctx.store().read()
        except common.LoopctlError as exc:
            err = str(exc)
        chk("state", err is None and state is not None, err or ("state seq %d" % state.seq if state else "no state: `loopctl approve plan --by <you>` first"))
        if state is not None:
            ap = state.header.get("approvedPlan")
            same = bool(ap) and ap["digest"] == rep.digest
            chk("plan-approved", same, "the plan is approved (H1)" if same else "the plan is not approved, or changed since (H1/H2): `loopctl approve plan`")
            chk("run-status", state.header.get("runStatus") not in ("stopped", "aborted"),
                "run status %s%s" % (state.header.get("runStatus"), " (a human runs `loopctl approve breaker` to run again)"
                                 if state.header.get("runStatus") in ("stopped", "aborted") else ""))
            chk("level-cap", not state.header.get("levelCap"), "no environment demotion" if not state.header.get("levelCap")
                else "capped: %s" % state.header["levelCap"].get("reason"))
    halted = ctx.rs.is_halted()
    chk("halt", not halted, "HALT is not set" if not halted else "HALT is set: a human runs `loopctl resume`")
    chk("clean-tree", _tree_clean(ctx), "the working tree is clean" if _tree_clean(ctx) else
        "the working tree has uncommitted changes; the loop runs in a dedicated clean clone or worktree (L-1)")
    if ctx.runner == "sequential":
        if session is None:
            chk("canary", False, "--session <id> --nonce <value> from the SessionStart line is required")
        else:
            have = ctx.rs.read_nonce(session)
            ok = bool(have and nonce and hmac.compare_digest(have, nonce))
            chk("canary", ok, "liveness canary matches" if ok else
                ("no canary for this session: the SessionStart hook did not run" if not have else "the canary nonce does not match"))
    else:
        budget = ctx.cfg.get("run_budget_usd")
        chk("budget", isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0,
            "run_budget_usd is %s" % budget if budget else "run_budget_usd is not set: the driver refuses to start without a ceiling")
        chk("driver", load_driver() is not None, "the driver module is installed" if load_driver() else "driver not installed")
    if ctx.runner == "driver" and ctx.cfg.get("autonomy") == "unattended":
        from . import unattended                   # Phase 7: an unattended start is decided here, from committed facts, before the lock (no-acknowledge start)
        res += unattended.rows(ctx, state)
    lk = ctx.lock().status()
    mine = bool(lk["held"] and session and (lk.get("record") or {}).get("sessionId") == session)
    dead = bool(ctx.runner == "driver" and lk["held"] and not lk["corrupt"] and lock_mod.owner_dead(lk.get("record") or {}))
    chk("lock", (not lk["held"]) or mine or dead, "the run lock is free" if not lk["held"] else
        ("the lock is yours" if mine else ("the run lock belongs to a driver that is gone (pid %s): this run takes it over" % (lk.get("record") or {}).get("driverPid")
                                          if dead else "the run lock is held by another session (loopctl lock status)")))
    return res


def dry_run(ctx: Any, out: Any, as_json: bool) -> int:
    rep = plan_mod.validate(ctx.plan, max_item_loc=ctx.cfg.get_int("max_item_loc"), roster=ctx.roster(),
                            always_human_paths=ctx.cfg.get_list("always_human_paths")) if ctx.plan and not ctx.plan.fatal else None
    pre = preflight(ctx, None, None)
    gate_rows = []
    for g in gates.KNOWN_GATES:
        mod = None
        try:
            mod = gates.get(g)
        except gates.GateNotInstalled:
            pass
        gate_rows.append({"gate": g, "installed": mod is not None, "mode": gates.mode_for(ctx.cfg, g, mod)})
    n = len(rep.order) if rep else 0
    proj = ledger.projection(ctx.repo, n)
    doc = {"schema": "loopctl.run-dry.v1", "runner": ctx.runner, "tier2": ctx.tier2, "plan": ctx.plan.name if ctx.plan else None,
           "base_sha": ctx.base_sha, "order": rep.order if rep else [], "critical_path": rep.critical if rep else [], "width": rep.width if rep else 0,
           "gates": gate_rows, "budget": {"run": ctx.cfg.get("run_budget_usd"), "item": ctx.cfg.get("item_budget_usd"), "day": ctx.cfg.get("day_budget_usd"),
                                          "projection": proj},
           "evidence_gate": ctx.evidence_passed(), "level_cap_from_evidence": "none" if ctx.evidence_passed() else "L3",
           "preflight": pre, "ready": all(p["ok"] for p in pre if p["id"] not in ("canary", "lock", "tier2"))}
    if as_json:
        out.json(doc)
    else:
        out.text("run --dry-run (nothing is executed)")
        out.text("  plan %s: %d item(s), critical path %d, width %d" % (common.clean_text(doc["plan"], 40), n, len(doc["critical_path"]), doc["width"]))
        out.text("  order: %s" % (" -> ".join(doc["order"]) or "-"))
        out.text("  runner: %s; Tier 2 writes: %s; evidence gate: %s (level capped at %s)" % (
            ctx.runner, "on" if ctx.tier2 else "off", "passed" if doc["evidence_gate"] else "not passed", doc["level_cap_from_evidence"] if not doc["evidence_gate"] else "none"))
        out.text("  gates: " + ", ".join("%s=%s%s" % (g["gate"], g["mode"], "" if g["installed"] else "(not installed)") for g in gate_rows))
        out.text("  budget: run %s item %s day %s; projection: %s" % (doc["budget"]["run"], doc["budget"]["item"], doc["budget"]["day"], proj["message"]))
        for p in pre:
            out.text("  [%s] %-14s %s" % ("ok" if p["ok"] else "NO", p["id"], common.clean_text(p["message"], 160)))
    return EXIT_OK if doc["ready"] else EXIT_FINDINGS


def handoff_text(ctx: Any, run_id: str, session: str, order: List[str], eligible: List[str], state: Any) -> str:
    base = "python3 .claude/scripts/loopctl"
    L = ["loopctl run: runner is sequential, so the prose loop (.claude/commands/orchestrate.md) runs this run.",
         "  run id     %s" % run_id, "  session    %s (registered as the conductor; holds the run lock)" % session,
         "  plan       %s, %d item(s); approved by %s" % (ctx.plan.name, len(order), (state.header.get("approvedPlan") or {}).get("by")),
         "  config and plan read at base sha %s" % (ctx.base_sha or "")[:12],
         "  next item  %s" % (eligible[0] if eligible else "none eligible"), "",
         "Every deterministic decision goes through loopctl; you keep design, briefs, conflicts and summaries:",
         "  %s status --json                       re-read the state at every stage" % base,
         "  %s begin <item> --session %s            pending -> designing" % (base, session),
         "  %s advance <item> <stage> --session %s  designing > design_review > stubbing > building > verifying > reviewing > ..." % (base, session),
         "  %s fail <item> --class <c> --reason <r> --session %s   the class decides retry, park or discard" % (base, session),
         "  %s gate <name> <item> --session %s      run a gate; the base sha's toggle decides whether it blocks" % (base, session),
         "  %s approve item <item> --by <human>     only after the human said so; never on your own" % base,
         "  %s status --pending-human               what is waiting on the human" % base,
         "  %s lock release --session %s           when the run ends" % (base, session),
         "", "A step back (verify failed, review blockers, a CI fix) is `fail`, never `advance`: it counts against the right cap.",
         "HALT or a lock owned by someone else stops every write. Do not work around either."]
    return "\n".join(L) + "\n"


def start(ctx: Any, out: Any, session: Optional[str], nonce: Optional[str], as_json: bool, env: Mapping[str, str]) -> int:
    ctx.require_tier2()
    pre = preflight(ctx, session, nonce)
    failed = [p for p in pre if not p["ok"]]
    if failed:
        for p in failed:
            out.err("loopctl run: refused: %s: %s" % (p["id"], common.clean_text(p["message"], 200)))
        if any(p["id"] in ("driver",) for p in failed) and ctx.runner == "driver":
            return EXIT_USAGE if any("driver not installed" in p["message"] for p in failed) else EXIT_REFUSED
        return EXIT_REFUSED
    from .state import Loop
    loop = Loop(ctx)
    st, _ = loop.store.read()
    order = plan_mod.topo_order(ctx.plan.items, [m for m in ctx.plan.plan.get("milestones", []) if isinstance(m, str)])
    deps = {i: [d for d in ctx.plan.items[i].get("dependsOn", []) if isinstance(d, str)] for i in ctx.plan.items}
    eligible = st.eligible(order, deps)
    lk = ctx.lock()
    run_id = None
    prior = None
    try:
        prior = ctx.rs.read_run()
    except common.READ_ERRORS:
        pass
    plan_rev = plan_mod.plan_digest(ctx.plan)[:12]
    if ctx.runner == "driver":
        driver = load_driver()
        sid = "driver-%d" % os.getpid()
        run_id = "run-%d-%s" % (int(common.now()), secrets.token_hex(2))
        held = lk.status()
        if held["held"] and not held["corrupt"] and lock_mod.owner_dead(held.get("record") or {}):
            lk.takeover(run_id, sid, ctx.repo, os.getpid(), plan_rev)          # a driver that crashed: its lock is nobody's (Phase 4c)
        else:
            lk.acquire(run_id, sid, ctx.repo, os.getpid(), plan_rev)
        rec = _write_run(ctx, run_id, sid, order, plan_rev, "driver")
        try:
            rc = driver.run(ctx, loop, rec, as_json)
        finally:
            lk.release(sid)
        return int(rc or 0)
    assert session is not None
    cur = ctx.rs.read_lock() if ctx.rs.lock_held() else None
    if cur and cur.get("sessionId") == session and prior and prior.get("runId") == cur.get("runId"):
        run_id = cur["runId"]
    else:
        run_id = "run-%d-%s" % (int(common.now()), secrets.token_hex(2))
        lk.acquire(run_id, session, ctx.repo, None, plan_rev)
    ctx.rs.register_session(session, "conductor", registered_by="loopctl", runId=run_id)
    rec = _write_run(ctx, run_id, session, order, plan_rev, "sequential")

    def mark(s: Any) -> None:
        s.header["runId"] = run_id
        if s.header.get("runStatus") in ("idle", "parked", None):
            s.header["runStatus"] = "running"

    loop.store.update(mark, "run %s started" % run_id)
    text = handoff_text(ctx, run_id, session, order, eligible, st)
    if as_json:
        out.json({"schema": "loopctl.run.v1", "runner": "sequential", "run": rec, "next": eligible[:1], "eligible": eligible, "handoff": text})
    else:
        out.stdout.write(text)
    return EXIT_OK


def _write_run(ctx: Any, run_id: str, sid: str, order: List[str], plan_rev: str, runner: str) -> Dict[str, Any]:
    rec = {"runId": run_id, "plan": ctx.plan.name, "planRev": plan_rev, "baseSha": ctx.base_sha, "runner": runner,
           "worktree": ctx.repo, "conductor_session_id": sid, "startedAt": common.iso(), "items": len(order)}
    ctx.rs.write_run(rec)
    return rec
