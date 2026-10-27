"""The loopctl command line (report section 10.2). Run: `python3 .claude/scripts/loopctl <command> [options]`.

Command classification. The guards (`guards/g_exec.py`, `guards/g_halt.py`) read the same table: a READ command is open to any
caller; every other command is a write and is denied to everyone but the registered conductor session (RC-13). Keep the three
in step (a test compares them).

    READ (no state, lock or file is written)
        validate  status  render  doctor  audit  report  help  version
        lock status   evidence --check   cost --projection   reconcile --dry-run   migrate-plan --check|--dry-run
        run --dry-run   abort --dry-run   unattended check|status|promotion|intake (without --write)

    TIER 1 WRITES (work with `loopctl_tier2: false`)
        lock acquire|release|steal   migrate-plan --write   halt   resume   unattended watch|notify-test|intake --write

    TIER 2 WRITES (refuse unless `loopctl_tier2: true` in the BASE sha's loop.config; OPT-IN, IMP-1)
        approve  begin  advance  fail  clarify  park  gate  spend  run  abort  reconcile --apply

The first argument is the command, exactly: no abbreviations, and no option before it that takes a separate value, so the guards
and this parser always agree on which command is being run. Options are not abbreviated either.

Exit codes: 0 done, 1 the check found a problem, 2 usage or internal error, 3 refused by policy (see common.py).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from kitlib import runstate

from . import VERSION, common
from .common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, LoopctlError, Refused, UsageError

__all__ = ["main", "READ_COMMANDS", "FLAG_READ", "TIER1_WRITES", "TIER2_WRITES", "classification"]

READ_COMMANDS = ("validate", "status", "render", "doctor", "audit", "report", "help", "version")
# command -> flags that make an otherwise-writing command read-only (and the flags that always make it a write)
FLAG_READ = {"reconcile": ("--dry-run",), "migrate-plan": ("--check", "--dry-run"), "cost": ("--projection",), "evidence": ("--check",),
             "lock": ("status",), "run": ("--dry-run",), "abort": ("--dry-run",), "unattended": ("check", "status", "promotion", "intake")}
WRITE_FLAGS = ("--apply", "--write", "--force")
TIER1_WRITES = ("lock", "migrate-plan", "halt", "resume", "unattended")
TIER2_WRITES = ("approve", "begin", "advance", "fail", "clarify", "park", "gate", "spend", "run", "abort", "reconcile")


def classification() -> Dict[str, Any]:
    """The table the guards must agree with (tests/loopctl/test_guard_alignment.py)."""
    return {"read": list(READ_COMMANDS), "flag_read": {k: list(v) for k, v in FLAG_READ.items()}, "write_flags": list(WRITE_FLAGS),
            "tier1": list(TIER1_WRITES), "tier2": list(TIER2_WRITES)}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:       # argparse would sys.exit(2) with its own text
        raise UsageError("%s: %s" % (self.prog, message))

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        raise _Exit(status, message)


class _Exit(Exception):
    def __init__(self, status: int, message: Optional[str]) -> None:
        Exception.__init__(self, message or "")
        self.status, self.message = status, message


def _parser(prog: str, desc: str = "") -> _Parser:
    ap = _Parser(prog="loopctl " + prog, description=desc, allow_abbrev=False)
    ap.add_argument("--repo", default=".", help="the repository (default: the current directory)")
    ap.add_argument("--base", default=None, help="the base revision config and plan are read from (default: merge-base with the default branch)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


class Out(object):
    def __init__(self, stdout: TextIO, stderr: TextIO) -> None:
        self.stdout, self.stderr = stdout, stderr

    def text(self, s: str) -> None:
        self.stdout.write(s if s.endswith("\n") else s + "\n")

    def json(self, obj: Any) -> None:
        self.stdout.write(json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n")

    def err(self, s: str) -> None:
        self.stderr.write(s if s.endswith("\n") else s + "\n")


def _ctx(a: argparse.Namespace, env: Mapping[str, str], plan: str = "base", allow_head_config: bool = True) -> Any:
    from .context import Context
    return Context.build(os.path.abspath(a.repo), env, base=a.base, plan=plan, allow_head_config=allow_head_config)


def _need_tier2(ctx: Any) -> None:
    ctx.require_tier2()


def _session(a: argparse.Namespace, env: Mapping[str, str], required: bool = True) -> Optional[str]:
    sid = getattr(a, "session", None) or env.get("LOOPCTL_SESSION")
    if sid is None:
        if required:
            raise UsageError("--session <id> is required (the session that holds the run lock)")
        return None
    if not runstate.valid_id(sid):
        raise UsageError("--session is not a valid session id")
    return sid


# ===== read commands ==============================================================================================

def cmd_validate(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("validate", "Validate plan/: schema, AC ids, the dependency DAG, critical path, width, slicing, data-loss classes.")
    ap.add_argument("--committed", action="store_true", help="read the plan committed at the base sha (default: the working tree, for drafts)")
    ap.add_argument("--strict", action="store_true", help="warnings are errors")
    a = ap.parse_args(args)
    ctx = _ctx(a, env, plan="base" if a.committed else "worktree")
    from . import gates, plan
    rep = plan.validate(ctx.plan, max_item_loc=ctx.cfg.get_int("max_item_loc"), roster=ctx.roster(),
                        always_human_paths=ctx.cfg.get_list("always_human_paths"))
    for n in sorted(ctx.cfg.gates()):
        if n not in gates.KNOWN_GATES and n != "g8":
            rep.findings.append(common.Finding("warn", "config.unknown-gate", "gates.%s names no known gate (%s)" % (n, ", ".join(gates.KNOWN_GATES))))
    for n in ctx.opt_in_violations():
        rep.findings.append(common.Finding("error", "config.evidence-gated", "%s needs the evidence gate, which has not passed" % n))
    for n, _b, _h in ctx.config_drift:
        rep.findings.append(common.Finding("info", "config.drift", "protected key %s differs in the working tree; loopctl uses the base sha's value" % n))
    d = rep.to_dict()
    bad = bool(rep.errors) or any(f.id == "config.evidence-gated" for f in rep.findings) or (a.strict and any(f.severity == "warn" for f in rep.findings))
    d["ok"] = not bad
    if a.json:
        out.json(d)
    else:
        out.text("plan %s (%s): %d item(s)" % (common.clean_text(rep.name, 40), common.clean_text(rep.source, 40), len(rep.order)))
        for f in rep.findings:
            if f.severity != "info" or not a.strict:
                out.text("  %-5s %-26s %s" % (f.severity, f.id, common.clean_text(f.message, 220)))
        if not rep.errors and rep.order:
            out.text("order: %s" % " -> ".join(rep.order))
            out.text("critical path: %d item(s): %s" % (len(rep.critical), " -> ".join(rep.critical)))
            out.text("width: %d item(s) could run at once (levels: %s)" % (rep.width, "; ".join(",".join(l) for l in rep.levels)))
        out.text("OK" if not bad else "INVALID")
    return EXIT_OK if not bad else EXIT_FINDINGS


def cmd_status(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("status", "Where the run is. --pending-human lists what waits on a person.")
    ap.add_argument("--pending-human", action="store_true")
    ap.add_argument("--matrix", nargs="?", const="*", default=None, metavar="ITEM", help="the AC x evidence matrix (G8's view)")
    ap.add_argument("--bypass", action="store_true", help="the ruleset bypass log (RC-9)")
    ap.add_argument("--level", action="store_true", help="also ask the doctor for the effective level")
    a = ap.parse_args(args)
    from . import status
    ctx = _ctx(a, env, plan="auto")
    if ctx.plan.fatal:
        raise Refused("no plan: %s" % (ctx.plan.findings[0].message if ctx.plan.findings else "plan/plan.json is missing"))
    doc = status.collect(ctx, include_level=a.level)
    if a.pending_human:
        if a.json:
            out.json({"schema": "loopctl.pending-human.v1", "pending": doc["pending_human"]})
        else:
            out.text(status.format_pending(doc["pending_human"]))
    elif a.matrix is not None:
        m = status.ac_matrix(ctx.plan, ctx.store().read()[0] if doc["state"]["present"] else None, None if a.matrix == "*" else a.matrix)
        out.json(m) if a.json else out.text(status.format_matrix(m))
    elif a.bypass:
        out.json(doc["bypass"]) if a.json else out.text(status.format_bypass(doc["bypass"]))
    elif a.json:
        out.json(doc)
    else:
        out.text(status.format_text(doc))
        if a.level and "level" in doc:
            lv = doc["level"]
            out.text("  effective level %s (configured %s)" % (lv.get("effective"), lv.get("configured")))
    return EXIT_OK


def cmd_render(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("render", "Print BUILD_PLAN.md (md), a Mermaid diagram (mermaid) or JSON. Writes nothing.")
    ap.add_argument("--format", choices=("md", "mermaid", "json"), default="md")
    ap.add_argument("--no-state", action="store_true")
    a = ap.parse_args(args)
    from . import plan, render
    ctx = _ctx(a, env, plan="auto")
    if ctx.plan.fatal:
        raise Refused("no plan to render")
    rep = plan.validate(ctx.plan, max_item_loc=ctx.cfg.get_int("max_item_loc"))
    st, warn = None, None
    if not a.no_state and ctx.plan.name:
        try:
            st, _ = ctx.store().read()
        except LoopctlError as exc:
            warn = "STATE UNREADABLE: %s" % exc
    if a.format == "mermaid":
        out.text(render.render_mermaid(ctx.plan, st, rep))
    elif a.format == "json":
        out.json(render.render_json(ctx.plan, st, rep))
    else:
        out.text(render.render_markdown(ctx.plan, st, rep, warn))
    return EXIT_OK


def cmd_doctor(args: List[str], env: Mapping[str, str], out: Out) -> int:
    """Wraps scripts/doctor.py (Phase 1): the same program, with the repo and the base sha filled in."""
    from .context import SCRIPTS_DIR
    argv = [sys.executable, os.path.join(SCRIPTS_DIR, "doctor.py")] + list(args)
    if "--repo" not in args:
        argv += ["--repo", os.getcwd()]
    if "--config-ref" not in args:
        try:
            from .context import _resolve_base
            from kitlib import gitutil
            repo = args[args.index("--repo") + 1] if "--repo" in args else os.getcwd()
            top = gitutil.toplevel(repo)
            base = _resolve_base(top, None) if top else None
            if base:
                argv += ["--config-ref", base]
        except Exception:  # noqa: BLE001 - the doctor then reads the working tree, as it always did
            pass
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, env=dict(env))
    out.stdout.write(proc.stdout.decode("utf-8", "replace"))
    out.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return proc.returncode


def cmd_audit(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("audit", "audit render ITEM | audit check ITEM --body FILE: the PR audit block, built from state and validated with pr_prepare.")
    ap.add_argument("action", choices=("render", "check"))
    ap.add_argument("item")
    ap.add_argument("--lane", choices=("full", "quick"), default="full")
    ap.add_argument("--body", default=None)
    a = ap.parse_args(args)
    from . import audit
    ctx = _ctx(a, env)
    st, _ = ctx.store().read()
    if st is None or a.item not in st.items:
        raise Refused("item %s has no state yet" % common.clean_text(a.item, 30))
    if a.action == "render":
        out.text(audit.render_body(ctx, st, a.item, a.lane))
        return EXIT_OK
    if not a.body:
        raise UsageError("audit check needs --body FILE")
    path = os.path.realpath(os.path.join(os.path.abspath(a.repo), a.body))
    if os.path.getsize(path) > 200000:
        raise UsageError("the body file is too large")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    fs = audit.check_body(text, ctx, st, a.item, a.lane)
    errs = [f for f in fs if f.severity == "error"]
    if a.json:
        out.json({"schema": "loopctl.audit.v1", "ok": not errs, "findings": [f.to_dict() for f in fs]})
    else:
        for f in fs:
            out.text("  %-5s %-28s %s" % (f.severity, f.id, common.clean_text(f.message, 220)))
        out.text("audit block OK" if not errs else "audit block has %d error(s)" % len(errs))
    return EXIT_OK if not errs else EXIT_FINDINGS


def cmd_report(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("report", "report check PATH [--message FILE]: parse an engineer report and its JSON trailer.")
    ap.add_argument("action", choices=("check",))
    ap.add_argument("path")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--message", default=None, help="a file holding the engineer's final message (for the trailer)")
    a = ap.parse_args(args)
    from . import report
    msg = None
    if a.message:
        with open(_rel(a, a.message), "r", encoding="utf-8", errors="replace") as fh:
            msg = fh.read(1 << 16)
    res = report.read_report(os.path.abspath(a.repo), a.path, msg, a.slug)
    out.json(res.to_dict()) if a.json else out.text("report %s: %s%s" % (common.clean_text(a.path, 80), "OK" if res.ok else "MALFORMED",
                                                                     "".join("\n  - " + p for p in res.problems)))
    return EXIT_OK if res.ok else EXIT_FINDINGS


def cmd_help(args: List[str], env: Mapping[str, str], out: Out) -> int:
    out.text(__doc__.split("Exit codes")[0].strip().split("\n", 1)[1].strip())
    return EXIT_OK


def cmd_version(args: List[str], env: Mapping[str, str], out: Out) -> int:
    out.text("loopctl %s" % VERSION)
    return EXIT_OK


def cmd_evidence(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("evidence", "evidence --check: has the evidence gate passed? (docs/baseline/evidence.json, a human-committed file)")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(args)
    if not a.check:
        raise UsageError("evidence supports only --check (the evidence file is committed by a human)")
    from kitlib import evidence, gitutil
    top = gitutil.toplevel(os.path.abspath(a.repo), isolated=True)
    if not top:
        raise UsageError("not inside a git repository")
    st = evidence.check(top)
    if a.json:
        out.json({"schema": "loopctl.evidence.v1", "passed": bool(st), "reason": st.reason, "path": evidence.EVIDENCE_PATH,
                  "effective_level_cap": "none" if st else "L3"})
    else:
        out.text("evidence gate: %s" % ("PASSED" if st else "NOT PASSED -- " + st.reason))
        if not st:
            out.text("effective level is capped at L3; OPT-IN features (unattended L4, L5, parallel worktrees, G4, the G5 AST lint, remote lock, G10, the G8 verifier) refuse to enable")
    return EXIT_OK if st else EXIT_FINDINGS


def cmd_cost(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("cost", "cost --projection: estimate the run's spend from the baseline, and show the ledger and ceilings.")
    ap.add_argument("--projection", action="store_true")
    a = ap.parse_args(args)
    if not a.projection:
        raise UsageError("cost supports --projection (spend is recorded with `loopctl spend record`)")
    from . import ledger
    ctx = _ctx(a, env, plan="auto")
    n = len([i for i in ctx.plan.items]) if not ctx.plan.fatal else 0
    proj = ledger.projection(ctx.repo, n)
    run = ctx.rs.read_run() if ctx.rs.exists() else None
    doc = {"schema": "loopctl.cost.v1", "projection": proj,
           "ceilings": {"run": ctx.cfg.get("run_budget_usd"), "item": ctx.cfg.get("item_budget_usd"), "day": ctx.cfg.get("day_budget_usd")},
           "ledger_today": ctx.ledger().totals(day=common.iso()[:10]),
           "ledger_run": ctx.ledger().totals(run_id=run["runId"]) if run and isinstance(run.get("runId"), str) else None}
    if a.json:
        out.json(doc)
    else:
        out.text("projection: " + proj["message"])
        out.text("ceilings: run %s, item %s, day %s (0 = not set)" % (doc["ceilings"]["run"], doc["ceilings"]["item"], doc["ceilings"]["day"]))
        out.text("spent today: $%.4f (%d unknown entries)" % (doc["ledger_today"]["usd"], doc["ledger_today"]["unknown"]))
    return EXIT_OK


# ===== Tier 1 writes ==================================================================================================

def cmd_migrate_plan(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("migrate-plan", "Convert a v1 build-plan.json into plan/plan.json and plan/items/*.json.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--write", action="store_true")
    ap.add_argument("--from", dest="src", default="build-plan.json")
    ap.add_argument("--name", default=None, help="the plan name (default: the repository directory name)")
    ap.add_argument("--force", action="store_true", help="with --write: replace an existing plan/")
    ap.add_argument("--default-size", choices=("S", "M"), default="M")
    a = ap.parse_args(args)
    from . import migrate
    from kitlib import gitutil
    repo = os.path.abspath(a.repo)
    top = gitutil.toplevel(repo, isolated=True) or repo
    src = os.path.realpath(os.path.join(top, a.src))
    if not (src == top or src.startswith(top + os.sep)) or os.path.islink(os.path.join(top, a.src)):
        raise UsageError("--from must be a regular file inside the repository")
    if a.force and not a.write:
        raise UsageError("--force only goes with --write")
    if not os.path.isfile(src):
        raise UsageError("%s does not exist (use --from <file>)" % common.clean_text(a.src, 80))
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read(2 * 1024 * 1024 + 1)
    try:
        doc = migrate.read_v1(raw)
    except ValueError as exc:
        raise UsageError("%s is not usable JSON: %s" % (a.src, common.clean_text(exc, 100)))
    import re
    name = a.name or re.sub(r"[^a-z0-9-]+", "-", os.path.basename(top).lower()).strip("-")[:40] or "plan"
    res = migrate.migrate_v1(doc, name, default_size=a.default_size)
    written = []  # type: List[str]
    if a.write and res.ok:
        written = migrate.write_plan(top, res, force=a.force)
    if a.json:
        d = res.to_dict()
        d["written"] = written
        out.json(d)
    else:
        for f in res.findings:
            out.text("  %-5s %-26s %s" % (f.severity, f.id, common.clean_text(f.message, 200)))
        if written:
            out.text("wrote %d file(s): %s" % (len(written), ", ".join(written)))
            out.text("next: review them, add touches/estimate/verify/migrations where they matter, commit, then `loopctl approve plan --by <you> --seed-v1 %s`" % a.src)
        else:
            out.text("%s: %d item(s) %s" % ("OK" if res.ok else "REFUSED", len(res.item_docs), "would be written" if res.ok else "nothing would be written"))
    return EXIT_OK if res.ok else EXIT_FINDINGS


def cmd_lock(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("lock", "lock status | acquire | release | steal")
    ap.add_argument("action", choices=("status", "acquire", "release", "steal"))
    ap.add_argument("--session", default=None)
    ap.add_argument("--nonce", default=None, help="the canary nonce SessionStart printed for the session")
    ap.add_argument("--after", type=float, default=None, help="steal: minutes without a heartbeat (default lock_stale_min)")
    ap.add_argument("--host-dead", action="store_true")
    ap.add_argument("--corrupt", action="store_true")
    a = ap.parse_args(args)
    ctx = _ctx(a, env, plan="auto")
    lk = ctx.lock()
    if a.action == "status":
        s = lk.status()
        out.json(s) if a.json else out.text("lock: " + ("none" if not s["held"] else ("UNREADABLE (counts as held)" if s["corrupt"] else
                                                            "held by session %s, run %s, heartbeat %s min ago%s" % (
            common.clean_text(s["record"].get("sessionId"), 40), common.clean_text(s["record"].get("runId"), 40), s["age_min"], " (STALE)" if s["stale"] else ""))))
        return EXIT_OK
    sid = _session(a, env)
    if ctx.rs.is_halted() and a.action != "release":
        raise Refused("HALT is set; the lock is not handed out")
    if a.action == "release":
        ok = lk.release(sid)
        out.text("released" if ok else "that session does not hold the lock")
        return EXIT_OK if ok else EXIT_REFUSED
    _check_canary(ctx, sid, a.nonce)
    run_id = "run-%d-%s" % (int(common.now()), secrets.token_hex(2))
    plan_rev = _plan_rev(ctx)
    if a.action == "acquire":
        rec = lk.acquire(run_id, sid, ctx.repo, None, plan_rev)
    else:
        rec = lk.steal(run_id, sid, ctx.repo, a.after, a.host_dead, a.corrupt, None, plan_rev)
    out.json(rec) if a.json else out.text("lock %s: run %s, session %s" % ("acquired" if a.action == "acquire" else "stolen", rec["runId"], sid))
    return EXIT_OK


def _plan_rev(ctx: Any) -> str:
    from . import plan
    try:
        return plan.plan_digest(ctx.plan)[:12] if ctx.plan and not ctx.plan.fatal else ""
    except Exception:  # noqa: BLE001
        return ""


def _check_canary(ctx: Any, sid: str, nonce: Optional[str]) -> None:
    """The liveness canary (report section 5.8, RC-7): the nonce the SessionStart hook wrote for this session. Without it the hooks may not be
    firing, and a session without a nonce does not dispatch."""
    have = ctx.rs.read_nonce(sid)
    if not have:
        raise Refused("no liveness canary for session %s: the SessionStart hook did not run (hooks may be off), so the guards cannot be trusted; "
                      "do not dispatch. Start a new session, or run `loopctl doctor`" % sid)
    if not nonce or not hmac.compare_digest(have, nonce):
        raise Refused("the canary nonce does not match the one SessionStart wrote for session %s (pass --nonce <value from the session's start-up line>)" % sid)


def cmd_halt(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("halt", "Set HALT: every mutating tool is denied until a human runs `loopctl resume`.")
    ap.add_argument("--reason", default="halted with loopctl halt")
    ap.add_argument("--by", default="loopctl")
    a = ap.parse_args(args)
    ctx = _ctx(a, env, plan="none")
    ctx.rs.set_halt(common.clean_text(a.reason, 200), common.clean_text(a.by, 60))
    out.text("HALT set")
    return EXIT_OK


def cmd_resume(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("resume", "Clear HALT. Human only: refuses inside a Claude Code session.")
    a = ap.parse_args(args)
    if env.get("CLAUDECODE"):
        raise Refused("resume is a human command and refuses to run inside a Claude Code session (the kill switch is not the agent's to release); "
                      "use a plain terminal, or `!` in the prompt")
    ctx = _ctx(a, env, plan="none")
    out.text("HALT cleared" if ctx.rs.clear_halt() else "no HALT was set")
    return EXIT_OK


# ===== Tier 2 writes ===================================================================================================

def _loop(a: argparse.Namespace, env: Mapping[str, str], plan: str = "base") -> Any:
    from .state import Loop
    ctx = _ctx(a, env, plan=plan)
    _need_tier2(ctx)
    return Loop(ctx)


def cmd_approve(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("approve", "approve plan|item|checkpoint|unblock|env|breaker|cost|demotion [TARGET] --by NAME")
    ap.add_argument("scope", choices=("plan", "item", "checkpoint", "unblock", "env", "breaker", "cost", "demotion"))
    ap.add_argument("target", nargs="?", default=None)
    ap.add_argument("--by", required=True, help="the human who approves (recorded)")
    ap.add_argument("--choice", default="approve")
    ap.add_argument("--note", default="")
    ap.add_argument("--seed-v1", default=None, metavar="build-plan.json", help="plan: take item statuses from a v1 plan")
    a = ap.parse_args(args)
    loop = _loop(a, env)
    if a.scope == "plan":
        seed = None
        if a.seed_v1:
            from . import migrate
            src = os.path.realpath(os.path.join(loop.ctx.repo, a.seed_v1))
            if not src.startswith(loop.ctx.repo + os.sep) or os.path.islink(os.path.join(loop.ctx.repo, a.seed_v1)):
                raise UsageError("--seed-v1 must be a regular file inside the repository")
            with open(src, "r", encoding="utf-8", errors="replace") as fh:
                seed = migrate.state_seed(migrate.read_v1(fh.read(2 * 1024 * 1024)))
        st = loop.approve_plan(a.by, a.note, seed=seed)
        out.json({"approved": True, "plan": loop.plan.name, "seq": st.seq}) if a.json else out.text(
            "plan %s approved by %s (state seq %d)" % (loop.plan.name, common.clean_text(a.by, 40), st.seq))
        return EXIT_OK
    if a.scope in ("item", "checkpoint", "unblock") and not a.target:
        raise UsageError("approve %s needs an item id" % a.scope)
    ap_rec = loop.approve(a.scope, a.target or a.scope, a.by, a.choice, a.note)
    out.json(ap_rec) if a.json else out.text("recorded %s %s (%s) by %s" % (a.scope, a.target or "", a.choice, common.clean_text(a.by, 40)))
    return EXIT_OK


def cmd_begin(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("begin", "begin ITEM --session SID: start an item (pending -> designing).")
    ap.add_argument("item")
    ap.add_argument("--session", default=None)
    ap.add_argument("--by", default="conductor")
    ap.add_argument("--create-branch", action="store_true", help="also create feat/<slug>-a<n> at the base sha (not checked out)")
    a = ap.parse_args(args)
    loop = _loop(a, env)
    rec = loop.begin(a.item, _session(a, env), a.by)
    if a.create_branch:
        from . import branches
        branches.create(loop.ctx.repo, rec["branch"], loop.ctx.base_sha)
    out.json(rec) if a.json else out.text("item %s: designing (attempt %d, branch %s)" % (a.item, rec["attempt"], rec["branch"]))
    return EXIT_OK


def cmd_advance(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("advance", "advance ITEM STAGE --session SID [--merge-commit SHA] [--head SHA] [--pr-url URL]")
    ap.add_argument("item")
    ap.add_argument("stage")
    ap.add_argument("--session", default=None)
    ap.add_argument("--by", default="conductor")
    ap.add_argument("--note", default="")
    ap.add_argument("--merge-commit", default=None)
    ap.add_argument("--head", default=None)
    ap.add_argument("--pr-url", default=None)
    a = ap.parse_args(args)
    loop = _loop(a, env)
    rec = loop.advance(a.item, a.stage, _session(a, env), a.by, a.note, a.merge_commit, a.head, a.pr_url)
    out.json(rec) if a.json else out.text("item %s: %s" % (a.item, rec["status"]))
    return EXIT_OK


def cmd_fail(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("fail", "fail ITEM --class platform|transient|env|item --reason CODE --session SID")
    ap.add_argument("item")
    ap.add_argument("--class", dest="fclass", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--session", default=None)
    ap.add_argument("--by", default="conductor")
    ap.add_argument("--stage", default=None)
    ap.add_argument("--ac", action="append", default=[], help="a failing acceptance criterion id (repeatable)")
    ap.add_argument("--detail", default="")
    a = ap.parse_args(args)
    loop = _loop(a, env)
    d = loop.fail(a.item, a.fclass, a.reason, _session(a, env), a.by, a.ac, a.stage, a.detail)
    out.json(d.to_dict()) if a.json else out.text("item %s: %s (%s). %s" % (a.item, d.action, d.to_status or "stays", d.note))
    return EXIT_OK


def cmd_clarify(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("clarify", "clarify ITEM --session SID: an engineer asked for clarification; two rounds per attempt, then a human.")
    ap.add_argument("item")
    ap.add_argument("--session", default=None)
    a = ap.parse_args(args)
    loop = _loop(a, env)
    n = loop.clarify(a.item, _session(a, env))
    out.text("clarification round %d%s" % (n, " -- over the limit: blocked, a human must answer" if n > 2 else ""))
    return EXIT_OK


def cmd_park(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("park", "park ITEM --code CODE: a hard stop that is not a failure of the item (ESCALATION_NEEDED, security_blocked, ...).")
    ap.add_argument("item")
    ap.add_argument("--code", required=True)
    ap.add_argument("--detail", default="")
    ap.add_argument("--kind", choices=("needs_human", "blocked"), default="needs_human")
    ap.add_argument("--session", default=None)
    ap.add_argument("--by", default="conductor")
    a = ap.parse_args(args)
    loop = _loop(a, env)
    loop.park(a.item, a.code, a.detail, _session(a, env, required=False), a.by, a.kind)
    out.text("item %s: %s (%s)" % (a.item, a.kind, common.clean_text(a.code, 40)))
    return EXIT_OK


def cmd_gate(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("gate", "gate NAME ITEM: run one gate (toggle read from the base sha) and record its result.")
    ap.add_argument("name")
    ap.add_argument("item")
    ap.add_argument("--head", default=None)
    ap.add_argument("--worktree", default=None)
    ap.add_argument("--session", default=None)
    a = ap.parse_args(args)
    from . import gates
    loop = _loop(a, env)
    st, _ = loop.store.read()
    if st is None or a.item not in st.items:
        raise Refused("item %s has no state" % common.clean_text(a.item, 30))
    wt = os.path.realpath(a.worktree) if a.worktree else None
    gctx = loop.ctx.gate_context(a.item, a.head, wt, st.items[a.item])
    res = gates.run_gate(a.name, gctx)
    loop.record_gate(a.item, a.name, res, _session(a, env, required=False))
    out.json(res) if a.json else out.text("gate %s on %s: %s (mode %s%s)" % (a.name, a.item, res["status"], res["mode"], ", BLOCKING" if res["blocking"] else ""))
    return EXIT_FINDINGS if res["blocking"] else EXIT_OK


def cmd_spend(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("spend", "spend record --item ID --attempt N --result FILE|-: add a child session's cost to the ledger. Unknown cost parks.")
    ap.add_argument("action", choices=("record",))
    ap.add_argument("--item", required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--result", required=True, help="the child's -p JSON result (a file, or - for stdin)")
    ap.add_argument("--child-session", default=None)
    ap.add_argument("--role", default=None)
    ap.add_argument("--session", default=None)
    a = ap.parse_args(args)
    if not common.valid_item_id(a.item):
        raise UsageError("--item is not a valid item id")
    loop = _loop(a, env)
    if a.result == "-":
        raw = sys.stdin.read(1 << 20)
    else:
        with open(_rel(a, a.result), "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(1 << 20)
    try:
        result = common.strict_loads(raw, max_depth=8, max_bytes=1 << 20)
    except ValueError:
        result = None
    run = loop.ctx.rs.read_run() or {}
    if not isinstance(run.get("runId"), str):
        raise Refused("no run is active (run.json): `loopctl run` first")
    led = loop.ctx.ledger()
    rec = led.record(run["runId"], a.item, a.attempt, a.child_session, result=result if result is not None else {}, role=a.role)
    v = led.verdict(loop.ctx.cfg, run["runId"], a.item)
    if not v.ok:
        try:
            loop.park(a.item, v.codes[0], "; ".join(v.reasons)[:200], _session(a, env, required=False), "ledger")
        except Refused:
            pass
    out.json({"entry": rec, "verdict": v.to_dict()}) if a.json else out.text(
        "recorded %s for %s%s" % ("$%.4f" % rec["total_cost_usd"] if not rec["unknown"] else "UNKNOWN cost", a.item, "" if v.ok else " -- PARKED: " + v.reasons[0]))
    return EXIT_OK if v.ok else EXIT_REFUSED


def cmd_reconcile(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("reconcile", "reconcile --dry-run | --apply [--prs FILE] [--rule-suites FILE]")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--prs", default=None, help="recorded `gh pr list --json ...` output")
    ap.add_argument("--rule-suites", default=None, help="recorded ruleset rule-suite JSON (the bypass log)")
    ap.add_argument("--live", action="store_true", help="human only: run `gh pr list` for the PR facts")
    ap.add_argument("--by", default="reconcile")
    a = ap.parse_args(args)
    from . import reconcile
    prs = None
    suites = None
    if a.live:
        if env.get("CLAUDECODE"):
            raise Refused("--live is human-only and refuses to run inside a Claude Code session")
        proc = subprocess.run(reconcile.live_prs_argv(), cwd=os.path.abspath(a.repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=60)
        if proc.returncode != 0:
            raise Refused("gh failed: %s" % common.clean_text(proc.stderr.decode("utf-8", "replace"), 160))
        prs = reconcile.normalize_prs(json.loads(proc.stdout.decode("utf-8", "replace")))
    elif a.prs:
        prs = reconcile.normalize_prs(common.strict_loads(_read_small(_rel(a, a.prs)), max_depth=10))
    if a.rule_suites:
        suites = common.strict_loads(_read_small(_rel(a, a.rule_suites)), max_depth=10)
    if a.apply:
        loop = _loop(a, env)
    else:
        loop = None
    ctx = loop.ctx if loop else _ctx(a, env)
    if ctx.plan is None or ctx.plan.fatal:
        raise Refused("no plan at the base sha")
    st, _ = ctx.store().read()
    if st is None:
        raise Refused("there is no state to reconcile yet")
    rep = reconcile.scan(ctx, st, ctx.plan, prs, suites)
    if a.apply and rep.actions:
        reconcile.apply(loop, rep, a.by)
    d = rep.to_dict()
    d["mode"] = "apply" if a.apply else "dry-run"
    if a.json:
        out.json(d)
    else:
        for f in rep.findings:
            out.text("  %-5s %-28s %s" % (f.severity, f.id, common.clean_text(f.message, 200)))
        out.text("%d change(s) %s" % (len(rep.actions), "applied" if a.apply else "would be made (--apply does them)"))
    return EXIT_OK if not (rep.drift and not a.apply) else EXIT_FINDINGS


def _rel(a: argparse.Namespace, path: str) -> str:
    """A file named on the command line, relative to --repo (not to wherever the shell happens to be)."""
    return path if os.path.isabs(path) else os.path.join(os.path.abspath(a.repo), path)


def _read_small(path: str) -> str:
    if os.path.getsize(path) > 4 * 1024 * 1024:
        raise UsageError("%s is larger than 4 MiB" % common.clean_text(path, 60))
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def cmd_abort(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("abort", "abort [--rollback-to SHA] --by NAME | abort --dry-run [--rollback-to SHA]")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback-to", default=None)
    ap.add_argument("--by", default=None)
    ap.add_argument("--reason", default="")
    ap.add_argument("--session", default=None)
    a = ap.parse_args(args)
    from . import abort
    if a.dry_run:
        ctx = _ctx(a, env)
        st, _ = ctx.store().read()
        if st is None:
            raise Refused("there is no state yet")
        ap_ = abort.plan_abort(ctx, st, ctx.plan, a.rollback_to)
        out.json(ap_.to_dict()) if a.json else out.text(_format_abort(ap_))
        return EXIT_OK
    if not a.by:
        raise UsageError("--by <name> is required: an abort is recorded")
    loop = _loop(a, env)
    st, _ = loop.store.read()
    if st is None:
        raise Refused("there is no state yet")
    ap_ = abort.plan_abort(loop.ctx, st, loop.plan, a.rollback_to)
    rec = abort.execute(loop, ap_, a.by, a.reason, _session(a, env, required=False))
    out.json(rec) if a.json else out.text("aborted: %d reverted, %d left for a human%s" % (
        len(rec["reverted"]), len(rec["revertPlan"]), "; plan in " + rec["planFile"] if rec.get("planFile") else ""))
    return EXIT_OK


def _format_abort(p: Any) -> str:
    L = ["abort plan (nothing has been changed)", "  HALT will be set; integration branch: %s" % p.integration]
    for it in p.items:
        L.append("  %s %s: %s%s" % ("REVERT " if it["executable"] else "PLAN   ", it["item"], it["commit"][:12],
                                     "" if it["executable"] else " -- %s" % it["plan_only_reason"]))
    if p.rollback_to is None:
        L.append("  no --rollback-to: only the run stops; in-flight items are parked")
    L.append("  in flight: %s" % (", ".join(p.in_flight) or "none"))
    L.append("  open PRs to close by hand: %d" % len(p.open_prs))
    L.append("  in-flight items depending on a rolled-back item (abandoned): %s" % (", ".join(p.cascade) or "none"))
    return "\n".join(L)


def cmd_run(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("run", "run --session SID --nonce N | run --dry-run: start the run. sequential: print the hand-off; driver: dispatch to the driver.")
    ap.add_argument("--dry-run", action="store_true", help="render the plan, gates, runner and budget projection; execute nothing")
    ap.add_argument("--session", default=None)
    ap.add_argument("--nonce", default=None)
    a = ap.parse_args(args)
    from . import startrun as runner_mod
    ctx = _ctx(a, env)
    if a.dry_run:
        return runner_mod.dry_run(ctx, out, a.json)
    return runner_mod.start(ctx, out, _session(a, env, required=False), a.nonce, a.json, env)


# ===== unattended (Phase 7) ===============================================================================================

def _inside_repo(ctx: Any, path: str, flag: str) -> None:
    """A file named on the command line must be inside the repository: these commands are open to every caller, and an error message about an
    arbitrary file would otherwise say something about it."""
    real = os.path.realpath(path)
    root = os.path.realpath(ctx.repo)
    if not (real == root or real.startswith(root.rstrip(os.sep) + os.sep)) or os.path.islink(path):
        raise UsageError("%s must be a regular file inside the repository" % flag)


def cmd_unattended(args: List[str], env: Mapping[str, str], out: Out) -> int:
    ap = _parser("unattended", "unattended check|status|promotion|intake|watch|notify-test: the L4 operations (docs/unattended.md). "
                               "check, status, promotion and intake (without --write) change nothing.")
    ap.add_argument("action", choices=("check", "status", "promotion", "intake", "watch", "notify-test"))
    ap.add_argument("--quiet", action="store_true", help="check: print nothing, exit 0 when a run could start and make progress")
    ap.add_argument("--facts", default=None, help="promotion: a promotion.facts.v1 file made from GitHub or CI")
    ap.add_argument("--in", dest="infile", default=None, help="intake: the stage-1 triage answer (a file)")
    ap.add_argument("--source", default=None, help="intake: where the text came from, e.g. issue#12")
    ap.add_argument("--write", action="store_true", help="intake: quarantine the record and print the draft plan item")
    a = ap.parse_args(args)
    from . import unattended, promotion, intake
    from doctorlib import safeio
    if a.action in ("watch", "notify-test"):
        import notify as notify_mod
        ctx = _ctx(a, env, plan="none", allow_head_config=False)
        if not ctx.cfg_trusted:
            raise Refused("notify_command is read from the loop.config committed at the base sha; there is none (%s)" % ctx.cfg_note)
        if a.action == "watch":
            res = unattended.watch_once(ctx)
            out.json(res) if a.json else out.text("watch: %s (%s)%s" % (res["status"], common.clean_text(res["message"], 160), "; notified" if res.get("notified") else ""))
            return EXIT_OK if res["status"] in ("idle", "ok", "halted") else EXIT_FINDINGS
        st = notify_mod.settings_from_config(ctx.cfg)
        msg = notify_mod.compose("test", "info", detail="this is a test of notify_command", next_step="nothing; it worked")
        res = notify_mod.send(st["command"], msg, ctx.env, notify_mod.forbidden_roots(ctx.repo, ctx.rs.common_dir), st["timeout_s"], st["allow"])
        out.json(res) if a.json else out.text("notify-test: %s" % ("sent" if res["ok"] else "FAILED: " + common.clean_text(res["why"], 200)))
        return EXIT_OK if res["ok"] else (EXIT_REFUSED if res.get("refused") else EXIT_FINDINGS)
    if a.action == "promotion":
        if not a.facts:
            raise UsageError("promotion needs --facts <file> (a promotion.facts.v1 document made from GitHub or CI)")
        ctx = _ctx(a, env, plan="none")
        _inside_repo(ctx, _rel(a, a.facts), "--facts")
        raw, problem = safeio.read_text_status(_rel(a, a.facts), 2 * 1024 * 1024)
        if raw is None or os.path.islink(_rel(a, a.facts)):
            raise UsageError("--facts: %s" % (problem or "a symbolic link"))
        try:
            facts = promotion.parse_facts(raw)
        except ValueError as exc:
            raise UsageError("--facts: %s" % common.clean_text(exc, 160))
        res = promotion.evaluate(facts, promotion.bar_from_config(ctx.cfg), ctx.evidence_passed())
        res["schema"] = "loopctl.promotion.v1"
        res["note"] = "advice only: promotion is a reviewed commit of `autonomy: unattended` and `autonomy_level: L4` (H6); nothing was changed"
        if a.json:
            out.json(res)
        else:
            out.text("promotion to L4: %s" % ("ELIGIBLE" if res["eligible"] else "not yet"))
            for r in res["reasons"]:
                out.text("  - %s" % common.clean_text(r, 200))
            out.text("  window: %s" % ", ".join("%s=%s" % kv for kv in sorted(res["stats"].items())))
            out.text("  " + res["note"])
        return EXIT_OK if res["eligible"] else EXIT_FINDINGS
    if a.action == "intake":
        if not a.infile or not a.source:
            raise UsageError("intake needs --in <triage answer> and --source <ref, e.g. issue#12>")
        ctx = _ctx(a, env, plan="none")
        p = _rel(a, a.infile)
        _inside_repo(ctx, p, "--in")
        raw, problem = safeio.read_text_status(p, intake.iv.DEFAULT_MAX_BYTES + 1)
        if raw is None or os.path.islink(p):
            raise UsageError("--in: %s" % (problem or "a symbolic link"))
        clean, why = intake.check(raw)
        if clean is None:
            out.json({"schema": "loopctl.intake.check.v1", "ok": False, "reason": why}) if a.json else out.err("intake: invalid: %s" % why)
            return EXIT_FINDINGS
        rec = intake.make_record(clean, a.source)
        path = intake.quarantine(ctx.rs, rec) if a.write else None
        doc = {"schema": "loopctl.intake.check.v1", "ok": True, "id": rec["id"], "record": path, "draft": intake.draft_item(rec) if a.write else None,
               "note": "UNTRUSTED. Not in the plan. A human writes the plan item from the draft and approves the plan (H1, H7)."}
        if a.json:
            out.json(doc)
        else:
            out.text("intake %s: valid%s" % (rec["id"], "; quarantined at %s" % path if path else " (nothing written: add --write)"))
            if a.write:
                out.text(json.dumps(doc["draft"], indent=2))
            out.text(doc["note"])
        return EXIT_OK
    ctx = _ctx(a, env, plan="base")
    state = None
    if ctx.plan and not ctx.plan.fatal:
        try:
            state, _ = ctx.store().read()
        except LoopctlError:
            state = None
    rs = unattended.rows(ctx, state)
    ok = unattended.ready(rs)
    if a.action == "check":
        if a.quiet:
            return EXIT_OK if ok else EXIT_FINDINGS
        out.json({"schema": "loopctl.unattended.check.v1", "ready": ok, "rows": rs}) if a.json else out.text(
            "\n".join(["unattended: %s" % ("a run could start now" if ok else "NOT ready")] + ["  [%s] %-22s %s" % ("ok" if r["ok"] else "NO", r["id"], common.clean_text(r["message"], 200)) for r in rs]))
        return EXIT_OK if ok else EXIT_FINDINGS
    import socket
    import time
    prog = unattended.read_progress(ctx.rs)
    verdict = unattended.assess(prog, time.time(), socket.gethostname(), runstate.pid_alive, float(ctx.cfg.get_int("stall_run_min") or 30) * 60.0, ctx.rs.is_halted())
    notes = [e for e in ctx.rs.read_events("notify")][-5:]
    doc = {"schema": "loopctl.unattended.status.v1", "wanted": unattended.wanted(ctx.cfg), "ready": ok, "rows": rs, "demotion": unattended.demotion_of(state),
           "run": prog, "watchdog": verdict, "watch": unattended.read_watch(ctx.rs), "last_notifications": notes}
    if a.json:
        out.json(doc)
    else:
        out.text("unattended: %s; %s" % ("wanted by the config" if doc["wanted"] else "not enabled (autonomy is not unattended)", "ready" if ok else "not ready"))
        for r in rs:
            out.text("  [%s] %-22s %s" % ("ok" if r["ok"] else "NO", r["id"], common.clean_text(r["message"], 200)))
        out.text("  run: %s" % common.clean_text(verdict["message"], 200))
        for e in notes:
            out.text("  notified: %s %s %s" % (e.get("kind"), "ok" if e.get("ok") else "FAILED (%s)" % common.clean_text(e.get("why"), 80), e.get("ts", "")))
    return EXIT_OK


COMMANDS = {
    "validate": cmd_validate, "status": cmd_status, "render": cmd_render, "doctor": cmd_doctor, "audit": cmd_audit, "report": cmd_report,
    "help": cmd_help, "version": cmd_version, "evidence": cmd_evidence, "cost": cmd_cost, "migrate-plan": cmd_migrate_plan, "lock": cmd_lock,
    "halt": cmd_halt, "resume": cmd_resume, "approve": cmd_approve, "begin": cmd_begin, "advance": cmd_advance, "fail": cmd_fail,
    "clarify": cmd_clarify, "park": cmd_park, "gate": cmd_gate, "spend": cmd_spend, "reconcile": cmd_reconcile, "abort": cmd_abort, "run": cmd_run,
    "unattended": cmd_unattended,
}


def main(argv: Sequence[str], env: Optional[Mapping[str, str]] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None) -> int:
    out = Out(stdout or sys.stdout, stderr or sys.stderr)
    env = os.environ if env is None else env
    args = list(argv)
    # The command is the first argument. Before it, only value-less flags are accepted, so a guard that takes the first
    # non-dash word as the command and this parser always agree.
    while args and args[0].startswith("-"):
        if args[0] in ("-h", "--help"):
            return cmd_help([], env, out)
        if args[0] == "--version":
            return cmd_version([], env, out)
        out.err("loopctl: unknown option %r before the command; options come after it. Try `loopctl help`." % common.clean_text(args[0], 30))
        return EXIT_USAGE
    if not args:
        return cmd_help([], env, out)
    cmd, rest = args[0], args[1:]
    fn = COMMANDS.get(cmd)
    if fn is None:
        out.err("loopctl: unknown command %r. Try `loopctl help`." % common.clean_text(cmd, 30))
        return EXIT_USAGE
    try:
        return fn(rest, env, out)
    except _Exit as e:
        if e.message:
            (out.stdout if e.status == 0 else out.stderr).write(e.message)
        return e.status
    except LoopctlError as e:
        out.err("loopctl %s: %s" % (cmd, common.clean_text(e, 600)))
        return e.exit_code
    except BrokenPipeError:
        return EXIT_OK
    except (OSError, ValueError) as e:
        out.err("loopctl %s: %s: %s" % (cmd, type(e).__name__, common.clean_text(e, 300)))
        return EXIT_USAGE
