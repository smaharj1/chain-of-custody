"""Unattended L4: a driver run that nobody watches still reaches a person (report 6.2, 6.3, 10.4, 5.3, 16.1 row 7; DEC-31, R2, IMP-1, IMP-25).

OPT-IN and evidence-gated. It is on only when the BASE sha's loop.config says `autonomy: unattended` (which already refuses until
`docs/baseline/evidence.json` is complete) and the driver runs (`runner: driver`). Everything here is deterministic; the kit never sends a
message itself, it runs the human's `notify_command` (`.claude/scripts/notify.py`).

WHAT IT ADDS TO THE SERIAL DRIVER (the driver is otherwise unchanged; it calls the hooks below)

  NO-ACKNOWLEDGE START   `rows()` checks every precondition from committed facts before the lock is taken: the evidence file (and that it
                         is the one at the base sha), L4 configured and reached (the doctor's effective level), a driver runner, spend
                         ceilings (run and day), a usable notify_command, no demotion, nothing parked that only a human can clear, and
                         work to do. A run never starts and then waits for a person: if a row fails it does not start (exit 3), says which,
                         and writes nothing. At start it also sends a "started" notification; a channel that is down refuses the start.
  PARK AND NOTIFY        every way the run can end sends exactly one notification (`finish`): done, waiting for a human, or a stop (park,
                         hard stop, budget, unknown cost, HALT, a lost lock, a signal, an internal error). An exception that escapes the
                         driver is a stop too. The message names the item, the outcome, the reason code and the next command; never repository text.
  STALL WATCHDOG         (1) a thread inside the driver notices when the driver's own thread has made no sign of life for `stall_run_min`
                         (a hung gate, a stuck git call), which the child's stall rule cannot see, and notifies once per stall; (2) `loopctl unattended watch`, run from
                         cron or launchd every few minutes, notices what no thread can: a driver that is gone (SIGKILL, OOM, the machine
                         slept and the process died) while its run is recorded as running, and delivers a closing message the driver could
                         not send because the channel was down. A start is refused unless that watchdog has checked in recently. It
                         notifies once, then again only after an hour.
  DEMOTION (6.3)         a revert of a merged item, a security block, a test-integrity flag (a reward hack) or a stall storm writes
                         `header.demotion` into the state and ends the run. While it is set, unattended runs refuse to start; a human clears it
                         with `loopctl approve demotion --by <name>`. Promotion is the human's: `loopctl unattended promotion` only reads facts.
  KILLED CHILD COST      a child the driver killed (stall, timeout) or that crashed prints no result, so its cost is unknown, and an unknown
                         cost parks the run. When this run has already measured children, the cost is ESTIMATED from the highest measured
                         dollars per second, times 1.25, capped by the child's own allowance (the `--max-budget-usd` it was started with),
                         and recorded as `source: estimated`. With no measured child yet it stays unknown and parks (never a guess from nothing).
  EVENT RESUME           `loopctl unattended check` exits 0 only when a run could start and make progress now; a scheduler runs
                         `loopctl unattended check --quiet && loopctl run` after a human approves, merges or unblocks.

NOT BUILT HERE (deferred, with why): the driver does not push branches or open PRs (the L2 bot push needs a live credential and `gh`; `pr_prepare`
prints the commands and an item that needs a PR stops at `awaiting_human`, which this module reports as "waiting for you"); parallel items (Phase 8).
"""
from __future__ import annotations

import math
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import notify as notify_mod
from kitlib import evidence, gitutil, jsonio, levels, runstate

from . import common, ledger, plan as plan_mod, state as state_mod
from .common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED

__all__ = ["SCHEMA", "wanted", "rows", "ready", "Supervisor", "attach", "assess", "watch_once", "estimate_killed_cost", "demotion_of", "DEMOTION_REASONS",
           "classify_outcome", "next_step"]

SCHEMA = "loopctl.unattended.v1"
PROGRESS_FILE = "unattended.json"
WATCH_FILE = "unattended.watch.json"
DEMOTION_REASONS = ("revert", "security_block", "integrity_flag", "stall_storm")
DEMOTED_TO = "L3"
ESTIMATE_MARGIN = 1.25
RENOTIFY_S = 3600.0
WATCH_FRESH_S = 900.0
TIMING_ENV = "KIT_DRIVER_TEST_TIMING"

# outcome (Driver.outcome) -> (notification kind, level)
_WAITING = ("awaiting_human", "checkpoint", "stopped_by_human", "delivery")
_NEXT = {
    "cost_unknown": "check the cost field, then run loopctl approve cost --by NAME",
    "budget_run_80": "run loopctl approve cost --by NAME to accept the rest",
    "budget_run": "raise run_budget_usd in a reviewed config change",
    "budget_day": "wait for tomorrow, or raise day_budget_usd in a reviewed config change",
    "budget_item": "raise item_budget_usd in a reviewed config change",
    "security_blocked": "read the security review, then run loopctl approve unblock ITEM --by NAME",
    "platform_outage": "check the platform status, then run loopctl approve unblock ITEM --by NAME",
    "merge_conflict": "resolve the merge by hand, then run loopctl reconcile --apply",
    "head_changed": "review the branch, then run loopctl approve item ITEM --by NAME",
}


def wanted(cfg: Any) -> bool:
    """True when the base sha's config asks for an unattended run."""
    return cfg.get("autonomy") == "unattended"


def demotion_of(st: Any) -> Optional[Dict[str, Any]]:
    d = st.header.get("demotion") if st is not None else None
    return d if isinstance(d, dict) else None


def next_step(kind: str, code: Optional[str], item: Optional[str]) -> str:
    if kind == "demoted":
        return "read why, then run loopctl approve demotion --by NAME" + (", and loopctl approve unblock %s --by NAME if it is parked" % item if item else "")
    if code in _NEXT:
        return _NEXT[code].replace("ITEM", item or "ITEM")
    if kind == "done":
        return "nothing, loopctl status shows the result"
    if kind == "stall":
        return "check the machine and run loopctl status, a new loopctl run takes over a dead driver"
    return "run loopctl status --pending-human"


def classify_outcome(outcome: str, code: int) -> Tuple[str, str]:
    """(kind, level) of the closing notification."""
    if outcome == "complete" and code == EXIT_OK:
        return "done", "info"
    if outcome in _WAITING:
        return "waiting", "action"
    if outcome == "demoted":
        return "demoted", "alert"
    return "stop", "alert"


# -- the start: every precondition, from committed facts ---------------------------------------------------------------------------

def _row(rid: str, ok: bool, msg: str) -> Dict[str, Any]:
    return {"id": "unattended." + rid, "ok": bool(ok), "message": msg}


def evidence_row(ctx: Any) -> Dict[str, Any]:
    """The evidence gate, and that the file counted is the one a human merged: the working tree's copy must equal the blob at the base sha
    (an item's branch cannot supply its own evidence)."""
    st = evidence.check(ctx.repo)
    if not st:
        return _row("evidence", False, "the evidence gate has not passed: %s" % common.clean_text(st.reason, 140))
    if ctx.base_sha:
        committed = gitutil.show_file(ctx.base_sha, evidence.EVIDENCE_PATH, ctx.repo, isolated=True)
        try:
            with open(os.path.join(ctx.repo, evidence.EVIDENCE_PATH), "rb") as fh:             # bytes: a CRLF file must equal its blob
                disk = fh.read().decode("utf-8", "replace")
        except OSError:
            disk = None
        if committed is None or disk != committed:
            return _row("evidence", False, "%s is not the committed copy at the base sha (%s): only a merged evidence file counts (H6)" % (
                evidence.EVIDENCE_PATH, ctx.base_sha[:12]))
    return _row("evidence", True, "the evidence gate passed (attested by %s)" % common.clean_text((st.data or {}).get("attested_by"), 40))


def default_level(ctx: Any) -> Optional[str]:
    """The doctor's effective level, or None against the test double (the doctor would describe a fake machine; the driver does the same)."""
    from . import launch
    binary = ctx.env.get("KIT_CLAUDE_BIN")
    if binary and launch.is_fake_binary(binary):
        return None
    eff = ctx.effective_level()
    return str(eff.get("effective")) if eff.get("effective") else None


def rows(ctx: Any, state: Any = None, level_fn: Optional[Callable[[Any], Optional[str]]] = None) -> List[Dict[str, Any]]:
    """Every check an unattended start makes, as [{id, ok, message}]. Reads only (the driver sends the start notification itself)."""
    out = [evidence_row(ctx)]
    cfg = ctx.cfg
    try:
        configured = levels.parse_level(str(cfg.get("autonomy_level") or "L1"))
    except ValueError:
        configured = levels.L0
    out.append(_row("autonomy", cfg.get("autonomy") == "unattended" and configured >= levels.L4,
                    "autonomy is %s and autonomy_level is L%d (unattended needs `autonomy: unattended` and L4 or L5)" % (cfg.get("autonomy"), configured)))
    out.append(_row("runner", ctx.runner == "driver", "runner is %s (unattended runs need runner: driver)" % ctx.runner))
    ceilings = {k: cfg.get(k) for k in ("run_budget_usd", "day_budget_usd")}
    missing = [k for k, v in ceilings.items() if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)]
    spent = None  # type: Optional[Any]
    if not missing:
        spent = ctx.ledger().verdict(cfg, "run-preflight")     # a run id that has no spend: what is left is the day's ceiling and any unreadable or unknown entry
    out.append(_row("budget", not missing and spent.ok, ("run_budget_usd and day_budget_usd are set" if not missing else "%s not set: nobody is watching the spend, so a ceiling is required" % " and ".join(missing))
                    if not missing and spent.ok else (("the spend ledger says stop: %s" % common.clean_text("; ".join(spent.reasons), 200)) if spent is not None else
                    "%s not set: nobody is watching the spend, so a ceiling is required" % " and ".join(missing))))
    ok, why = notify_mod.configured(notify_mod.settings_from_config(cfg), ctx.env, notify_mod.forbidden_roots(ctx.repo, ctx.rs.common_dir))
    out.append(_row("notify", ok, "notify_command is usable" if ok else "%s: a run that cannot reach you does not start" % why))
    fn = level_fn or default_level
    try:
        eff = fn(ctx)
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot be read is a refusal, not a crash
        eff, err = "L0", common.clean_text(exc, 80)
    else:
        err = ""
    if eff is None:
        out.append(_row("level", True, "the effective level is not computed against the test double"))
    else:
        try:
            reached = levels.parse_level(eff) >= levels.L4
        except ValueError:
            reached = False
        out.append(_row("level", reached, "the doctor's effective level is %s%s" % (eff, "" if reached else " (L4 needs the prerequisites and caps clear; run loopctl doctor --level)") + (" " + err if err else "")))
    if state is None:
        out.append(_row("state", False, "there is no loop state: `loopctl approve plan --by <you>` first"))
        return out
    dem = demotion_of(state)
    out.append(_row("demotion", dem is None, "not demoted" if dem is None else "demoted to %s (%s) at %s: review it, then `loopctl approve demotion --by <name>`" % (
        dem.get("to"), common.clean_text(dem.get("reason"), 40), common.clean_text(dem.get("at"), 30))))
    out.append(_parked_row(state))
    if ctx.plan and not ctx.plan.fatal:
        order = plan_mod.topo_order(ctx.plan.items, [m for m in ctx.plan.plan.get("milestones", []) if isinstance(m, str)])
        deps = {i: [d for d in ctx.plan.items[i].get("dependsOn", []) if isinstance(d, str)] for i in ctx.plan.items}
        eligible = state.eligible(order, deps)
        approved = [i for i in state.by_status("awaiting_human") if state_mod._find_approval(state, i, state.items[i]["attempt"]) is not None]
        waiting = [i for i in state.by_status("awaiting_human", "pr_open") if i not in approved]
        inflight = state.by_status(*state_mod.ACTIVE)             # a driver that died mid-item: the next start's recovery puts it back to pending
        if eligible or approved or inflight:
            out.append(_row("work", True, "next: %s" % (approved[0] if approved else (inflight[0] + " (left in flight; recovery resets it)" if inflight else eligible[0]))))
        else:
            out.append(_row("work", False, "nothing can run: %s" % (("%s waits for a human" % waiting[0]) if waiting else "no item is eligible (done, parked or waiting on a dependency)")))
    wt = read_watch(ctx.rs)
    age = time.time() - float(wt.get("ts", 0)) if wt and isinstance(wt.get("ts"), (int, float)) else None
    fresh = age is not None and 0 <= age < WATCH_FRESH_S
    out.append(_row("watch", fresh, "the external watchdog checked in %ds ago" % int(age) if fresh else
                    "no external watchdog has checked in within %d minutes: schedule `loopctl unattended watch` every 5 minutes (cron or launchd); a driver that is "
                    "killed cannot notify anyone, so a run nobody watches does not start" % (WATCH_FRESH_S // 60)))
    return out


def _parked_row(state: Any) -> Dict[str, Any]:
    """Only a park that a human must answer first stops the start: a breaker stop or an abort (`approve breaker`), and a checkpoint nobody has
    answered. Every other park is judged by the rows beside it (the ledger for money, `work` for an item that needs a human)."""
    hdr = state.header
    rs = hdr.get("runStatus")
    if rs in ("stopped", "aborted"):
        return _row("parked", False, "the run is %s (%s): a human clears it with `loopctl approve breaker --by <name>`" % (rs, common.clean_text((hdr.get("parked") or {}).get("reason"), 40)))
    p = hdr.get("parked")
    if rs == "parked" and isinstance(p, dict) and p.get("reason") == "checkpoint":
        answered = any(a.get("target") == p.get("item") and a.get("scope") == "checkpoint" and not a.get("used") and a.get("choice") in ("approve", "stop")
                       for a in state.d["approvals"])
        if not answered:
            return _row("parked", False, "the run waits at a checkpoint after %s: `loopctl approve checkpoint %s --by <name>`" % (common.clean_text(p.get("item"), 24), common.clean_text(p.get("item"), 24)))
        return _row("parked", True, "the checkpoint after %s was answered" % common.clean_text(p.get("item"), 24))
    return _row("parked", True, "nothing waits for an acknowledgement")


def ready(rs: Sequence[Dict[str, Any]]) -> bool:
    return all(r["ok"] for r in rs)


# -- the progress record and the external watchdog ---------------------------------------------------------------------------------

def progress_path(rs: runstate.RunState) -> str:
    return os.path.join(rs.root, PROGRESS_FILE)


def watch_path(rs: runstate.RunState) -> str:
    return os.path.join(rs.root, WATCH_FILE)


def read_progress(rs: runstate.RunState) -> Optional[Dict[str, Any]]:
    try:
        doc = jsonio.read_json(progress_path(rs))
    except common.READ_ERRORS:
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def read_watch(rs: runstate.RunState) -> Optional[Dict[str, Any]]:
    try:
        doc = jsonio.read_json(watch_path(rs))
    except common.READ_ERRORS:
        return None
    return doc if isinstance(doc, dict) else None


def assess(rec: Optional[Dict[str, Any]], now: float, host: str, alive: Callable[[Any], bool], stall_s: float, halted: bool = False) -> Dict[str, Any]:
    """The verdict on a recorded run. Pure. status: idle (nothing is running) | ok | dead (the driver process is gone) | stalled (no sign of life for
    stall_s) | halted (HALT is set; a human did it) | unknown_host (a record from another machine: only its age can be judged)."""
    if not rec or rec.get("state") != "running":
        return {"status": "idle", "message": "no unattended run is recorded as running"}
    beat = rec.get("lastBeat")
    known = isinstance(beat, (int, float)) and not isinstance(beat, bool) and math.isfinite(beat)
    age = now - float(beat) if known else float("inf")                                          # a record without a usable beat is silent
    ago = "%ds ago" % int(age) if known else "never"
    shown = age if known else None
    same = rec.get("host") == host
    if same and not alive(rec.get("pid")):
        return {"status": "dead", "message": "the driver (pid %s) is gone but the run is still recorded as running; last sign of life %s" % (common.clean_text(rec.get("pid"), 12), ago),
                "age_s": shown}
    if age > stall_s:
        return {"status": "stalled" if same else "unknown_host", "message": "no sign of life from the driver for %s" % (
            "%d minutes" % int(age // 60) if known else "as long as the record shows"), "age_s": shown}
    if halted:
        return {"status": "halted", "message": "HALT is set; the run is stopping", "age_s": shown}
    return {"status": "ok", "message": "the driver is alive (last sign of life %s)" % ago, "age_s": shown}


def watch_once(ctx: Any, now: Optional[float] = None, sender: Callable[..., Dict[str, Any]] = notify_mod.send) -> Dict[str, Any]:
    """One check by the external watchdog: assess the recorded run and notify on `dead` or `stalled` (once, then again after an hour). Writes only
    its own record. Returns {status, message, notified}."""
    t = time.time() if now is None else now
    rec = read_progress(ctx.rs)
    stall_s = max(60.0, float(ctx.cfg.get_int("stall_run_min") or 30) * 60.0)
    res = assess(rec, t, socket.gethostname(), runstate.pid_alive, stall_s, ctx.rs.is_halted())
    wt = read_watch(ctx.rs) or {}
    seen = wt.get("notified") if isinstance(wt.get("notified"), dict) else {}
    notified = False
    key = "%s:%s" % ((rec or {}).get("runId"), res["status"])
    if res["status"] in ("dead", "stalled", "unknown_host"):
        last = seen.get(key)
        if not isinstance(last, (int, float)) or t - float(last) >= RENOTIFY_S:
            settings = notify_mod.settings_from_config(ctx.cfg)
            msg = notify_mod.compose("watchdog", "alert", run=(rec or {}).get("runId"), item=(rec or {}).get("item"), outcome=res["status"],
                                     detail=res["message"], next_step=next_step("stall", None, None), now=t)
            r = sender(settings["command"], msg, ctx.env, notify_mod.forbidden_roots(ctx.repo, ctx.rs.common_dir), settings["timeout_s"], settings["allow"])
            try:
                ctx.rs.append_event("notify", {"source": "watch", "kind": "watchdog", "status": res["status"], "ok": r["ok"], "why": r["why"], "run": (rec or {}).get("runId")})
            except (OSError, ValueError):
                pass
            if r["ok"]:
                seen[key] = t                      # only a notification that went out counts as told: a broken channel is tried again next time
                notified = True
            res["notify_error"] = r["why"] if not r["ok"] else ""
    uns = (rec or {}).get("unsent") if rec and rec.get("state") == "ended" else None
    if isinstance(uns, dict):
        ukey = "%s:unsent" % rec.get("runId")
        if ukey not in seen:                                      # the driver could not tell anyone how its run ended: tell them now
            settings = notify_mod.settings_from_config(ctx.cfg)
            try:
                msg = notify_mod.compose(str(uns.get("kind")), str(uns.get("level")), run=rec.get("runId"), item=uns.get("item"), code=uns.get("code"),
                                         outcome=uns.get("outcome"), detail=str(uns.get("detail") or ""), next_step=str(uns.get("next") or ""), now=t)
                r = sender(settings["command"], msg, ctx.env, notify_mod.forbidden_roots(ctx.repo, ctx.rs.common_dir), settings["timeout_s"], settings["allow"])
            except ValueError:
                r = {"ok": False, "why": "the stored message is not valid"}
            try:
                ctx.rs.append_event("notify", {"source": "watch", "kind": uns.get("kind"), "resent": True, "ok": r["ok"], "why": r["why"], "run": rec.get("runId")})
            except (OSError, ValueError):
                pass
            if r["ok"]:
                seen[ukey] = t
                notified = True
                res["resent"] = True
                res["message"] = "delivered the closing message of run %s that the driver could not send" % common.clean_text(rec.get("runId"), 40)
            else:
                res.update({"status": "unsent", "message": "the closing message of run %s could not be delivered: %s" % (common.clean_text(rec.get("runId"), 40), r["why"]),
                            "notify_error": r["why"]})
    keep = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and t - float(v) < 7 * 86400.0}
    jsonio.ensure_dir(ctx.rs.root)
    jsonio.atomic_write_json(watch_path(ctx.rs), {"schema": SCHEMA, "ts": t, "lastCheck": common.iso(t), "lastStatus": res["status"], "notified": keep}, 0o600)
    res["notified"] = notified
    return res


# -- the cost of a child that printed nothing ------------------------------------------------------------------------------------------

def estimate_killed_cost(measured: Sequence[Tuple[float, float]], duration_s: float, allowance: float) -> Optional[float]:
    """Dollars for a child that was killed or crashed without a result: the highest measured dollars per second of this run's children, times
    ESTIMATE_MARGIN, times how long it ran, never more than its own allowance. None when nothing was measured (then the cost stays unknown)."""
    rates = [usd / dur for dur, usd in measured if dur > 0.01 and usd >= 0]
    if not rates or duration_s <= 0 or allowance <= 0:
        return None
    return round(min(max(rates) * duration_s * ESTIMATE_MARGIN, float(allowance)), 4)


# -- the supervisor the driver carries ------------------------------------------------------------------------------------------------------

class Supervisor(object):
    """Created by `attach` when the config asks for an unattended run. The driver calls: `on_event` (every event), `stop_reason` (every check_stop),
    `tick` (every poll), `child_cost` (before a child's cost is recorded) and `finish` (once, however the run ended)."""

    def __init__(self, drv: Any, sender: Callable[..., Dict[str, Any]] = notify_mod.send) -> None:
        self.drv = drv
        self.ctx = drv.ctx
        self.cfg = drv.cfg
        self.rs = drv.rs
        self.run_id = drv.run_id
        self.sender = sender
        self.settings = notify_mod.settings_from_config(self.cfg)
        self.forbid = notify_mod.forbidden_roots(self.ctx.repo, self.rs.common_dir)
        self.lock = threading.Lock()
        self.sent = set()  # type: set
        self.failures = 0
        self.last_beat = time.time()
        self.last_write = 0.0
        self.beat_s = 30.0
        self.stall_s = max(60.0, float(self.cfg.get_int("stall_run_min") or 30) * 60.0)
        self.stalls = []  # type: List[float]
        self.stall_count = 0
        self.measured = []  # type: List[Tuple[float, float]]
        self.estimated_usd = 0.0
        self.pending = None  # type: Optional[Tuple[str, Optional[str], str]]
        self.demoted = False
        self.demoted_reason = ""
        self.start_failed = ""
        self.finished = False
        self.thread = None  # type: Optional[threading.Thread]
        self.stop_flag = threading.Event()
        self.watch_key = None  # type: Any
        self.item = None  # type: Optional[str]
        self.last_event = ""
        self.started = False

    # -- wiring -----------------------------------------------------------------------------------------------------------------------
    @classmethod
    def attach(cls, drv: Any, sender: Callable[..., Dict[str, Any]] = notify_mod.send) -> "Supervisor":
        sup = cls(drv, sender)
        drv.observers.append(sup.on_event)
        drv.stop_hooks.append(sup.stop_reason)
        drv.finishers.append(sup.finish)
        drv.cost_hook = sup.child_cost
        drv.tick = sup.tick
        drv.supervisor = sup
        return sup

    # -- liveness ----------------------------------------------------------------------------------------------------------------------
    def tick(self) -> Optional[str]:
        r = self.drv.hb.tick()
        self.touch()
        return r

    def touch(self, force: bool = False) -> None:
        now = time.time()
        self.last_beat = now
        if force or now - self.last_write >= self.beat_s:
            self.write_progress("running")

    def write_progress(self, state: str, outcome: Optional[str] = None, code: Optional[int] = None, unsent: Optional[Dict[str, Any]] = None) -> None:
        self.last_write = time.time()
        doc = {"schema": SCHEMA, "runId": self.run_id, "pid": os.getpid(), "host": socket.gethostname(), "state": state, "outcome": outcome, "exit": code,
               "startedAt": self.drv.run_rec.get("startedAt"), "lastBeat": self.last_beat, "lastBeatAt": common.iso(self.last_beat), "item": self.item,
               "event": self.last_event, "stallRunS": self.stall_s, "unsent": unsent}
        try:
            jsonio.ensure_dir(self.rs.root)
            jsonio.atomic_write_json(progress_path(self.rs), doc, 0o600)
        except OSError:
            pass

    def _watch_loop(self) -> None:
        interval = max(0.05, min(5.0, self.stall_s / 4.0))
        while not self.stop_flag.wait(interval):
            idle = time.time() - self.last_beat
            if idle > self.stall_s and self.watch_key != self.last_beat:
                self.watch_key = self.last_beat
                self.notify("stall", "alert", item=self.item, outcome="driver_silent", detail="the driver made no progress for %d minutes" % max(1, int(idle // 60)),
                            next_step=next_step("stall", None, None), key=("watchdog", self.last_beat))

    # -- notifications -------------------------------------------------------------------------------------------------------------------
    def notify(self, kind: str, level: str, item: Optional[str] = None, code: Optional[str] = None, outcome: Optional[str] = None,
               detail: Optional[str] = None, next_step: Optional[str] = None, key: Any = None) -> bool:
        k = key if key is not None else (kind, item, code, outcome)
        with self.lock:
            if k in self.sent:
                return True
            self.sent.add(k)
        msg = notify_mod.compose(kind, level, run=self.run_id, item=item, code=code, outcome=outcome, detail=detail, next_step=next_step)
        res = self.sender(self.settings["command"], msg, self.ctx.env, self.forbid, self.settings["timeout_s"], self.settings["allow"])
        try:
            self.rs.append_event("notify", {"source": "driver", "kind": kind, "level": level, "item": msg.get("item"), "code": msg.get("code"),
                                            "outcome": msg.get("outcome"), "ok": res["ok"], "why": res["why"], "run": self.run_id})
        except (OSError, ValueError):
            pass
        if not res["ok"]:
            self.failures += 1
            try:
                self.drv.say("notification failed (%s): %s" % (kind, common.clean_text(res["why"], 120)))
            except Exception:  # noqa: BLE001
                pass
        return bool(res["ok"])

    # -- events ----------------------------------------------------------------------------------------------------------------------------------
    def on_event(self, rec: Dict[str, Any]) -> None:
        name = rec.get("event")
        self.last_event = str(name)
        if isinstance(rec.get("item"), str) and common.valid_item_id(rec["item"]):
            self.item = rec["item"]
        self.touch(force=name in ("start", "begin", "merged", "park", "stop"))
        if name == "start":
            self._start()
        elif name == "fail":
            if rec.get("reason") in ("stall", "timeout"):
                self.stall_count += 1
                self._stall(rec)
            if rec.get("reason") == "reward_hack":
                self._pend("integrity_flag", rec.get("item"), "a test-integrity flag (reward hack)")
        elif name == "park" and rec.get("code") == "security_blocked":
            self._pend("security_block", rec.get("item"), "the security reviewer blocked the item")
        elif name == "recovered" and isinstance(rec.get("action"), str) and rec["action"].startswith("human-revert "):
            self._pend("revert", rec["action"].split(" ", 1)[1], "a merged item was reverted")        # recovery applied "human-revert <item>"

    def _start(self) -> None:
        d = self.drv
        raw = d.env.get(TIMING_ENV)
        if raw and d.fake:
            try:
                over = common.strict_loads(raw, max_depth=3)
                v = over.get("stall_run_s")
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    self.stall_s = float(v)
            except (ValueError, AttributeError):
                pass
        self.beat_s = float(getattr(d.timing, "beat_s", 30.0))
        self.write_progress("running")
        ok = self.notify("start", "info", detail="plan %s, level %s" % (common.clean_text(self.ctx.plan.name, 40), d.level or "n/a"), next_step="nothing, you are told when it stops",
                         key=("start", self.run_id))
        if not ok:
            self.start_failed = "the start notification could not be sent: a run that cannot reach you does not start"
        self.started = True
        self.thread = threading.Thread(target=self._watch_loop, name="unattended-watchdog", daemon=True)
        self.thread.start()

    def _stall(self, rec: Dict[str, Any]) -> None:
        n = self.cfg.get_int("stall_storm_n")
        win = float(self.cfg.get_int("stall_storm_window_min") or 0) * 60.0
        now = time.time()
        self.stalls.append(now)
        self.stalls = [t for t in self.stalls if now - t <= win] if win > 0 else self.stalls[-1:]
        if n > 0 and len(self.stalls) >= n:
            self._pend("stall_storm", rec.get("item"), "%d stalls inside %d minutes" % (len(self.stalls), int(win // 60)))

    def _pend(self, reason: str, item: Any, detail: str) -> None:
        if self.pending is None and not self.demoted:
            self.pending = (reason, item if isinstance(item, str) and common.valid_item_id(item) else None, detail)

    # -- the driver asks ---------------------------------------------------------------------------------------------------------------
    def stop_reason(self) -> Optional[Tuple[str, str, int]]:
        if self.start_failed:
            msg, self.start_failed = self.start_failed, ""
            return ("notify_failed", msg, EXIT_REFUSED)
        if self.pending is not None and not self.demoted:
            self.write_demotion()
            return ("demoted", "demoted to %s (%s): unattended runs stop until `loopctl approve demotion --by <name>`" % (DEMOTED_TO, self.demoted_reason), EXIT_FINDINGS)
        return None

    def write_demotion(self) -> None:
        if self.pending is None or self.demoted:
            return
        reason, item, detail = self.pending
        run_id = self.run_id

        def mutate(st: Any) -> None:
            if not st.header.get("demotion"):
                st.header["demotion"] = {"to": DEMOTED_TO, "reason": reason, "item": item, "at": common.iso(), "run": run_id, "detail": common.clean_text(detail, 120)}
        try:
            self.drv.loop.store.update(mutate, "demotion to %s: %s" % (DEMOTED_TO, reason))
        except common.LoopctlError:
            return                                    # the state would not take it (a competing write): the next start sees the same facts
        self.demoted = True
        self.demoted_reason = reason
        self.drv.ev("demoted", to=DEMOTED_TO, reason=reason, item=item)

    def child_cost(self, res: Any, allowance: float) -> Optional[Tuple[Dict[str, Any], str]]:
        """What to record for a child. None = the driver's default (the parsed result as it is). A child that was killed or crashed without a
        result gets an estimate when this run has measured children; any other case is left alone, so an odd or hostile result still parks."""
        if res.parsed is not None:
            cost, _why = ledger.parse_cost(res.parsed)
            if cost is not None:
                self.measured.append((float(res.duration_s), cost))
            return None
        if res.reason in ("halt", "lock_lost", "interrupted"):
            return None
        est = estimate_killed_cost(self.measured, float(res.duration_s), float(allowance))
        if est is None:
            return None
        self.estimated_usd += est
        self.drv.ev("cost_estimated", usd=est, basis="highest measured $/s of this run x%.2f, capped by the allowance" % ESTIMATE_MARGIN, reason=res.reason or "crash")
        return {"total_cost_usd": est}, "estimated"

    # -- the end ----------------------------------------------------------------------------------------------------------------------------
    def finish(self, code: int) -> None:
        """Once, however the run ended. Every step is guarded on its own: a failure in one (the state would not take the demotion, the facts could
        not be read) must never cost the closing message."""
        if self.finished:
            return
        self.finished = True
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        try:
            self.write_demotion()
        except Exception:  # noqa: BLE001
            pass
        d = self.drv
        outcome = str(d.outcome)
        if code not in (EXIT_OK,) and outcome == "complete":
            outcome = "error"                       # the driver never set an outcome: an exception escaped it
        if self.demoted and outcome in ("parked", "error"):
            outcome = "demoted"
        kind, level = classify_outcome(outcome, code)
        try:
            item, why = self._facts(outcome)
        except Exception:  # noqa: BLE001
            item, why = self.item, None
        if self.demoted and kind == "stop":
            kind = "demoted"
        bits = ["merged %d" % len(d.merged_now), "children %d" % d.child_n]
        if self.stall_count:
            bits.append("stalls %d" % self.stall_count)
        if self.estimated_usd:
            bits.append("estimated cost %.2f USD" % self.estimated_usd)
        if self.demoted:
            bits.append("demoted to %s (%s)" % (DEMOTED_TO, self.demoted_reason))
        detail, nxt = ", ".join(bits), next_step(kind, why, item)
        try:
            ok = self.notify(kind, level, item=item, code=why, outcome=outcome, detail=detail, next_step=nxt, key=("final", self.run_id))
        except Exception:  # noqa: BLE001
            ok = False
        # a closing message the channel would not take is kept in the record, and the external watchdog delivers it when the channel is back
        self.write_progress("ended", outcome, code, None if ok else {"kind": kind, "level": level, "item": item, "code": why, "outcome": outcome, "detail": detail, "next": nxt})

    def _facts(self, outcome: str) -> Tuple[Optional[str], Optional[str]]:
        """(item, reason code) of the stop, from the events the driver wrote; only ids and codes, never text."""
        if outcome == "complete":
            return None, None
        for e in reversed(self.drv.events):
            n = e.get("event")
            if n == "park":
                return e.get("item"), e.get("code")
            if n == "budget_stop":
                return self.item, e.get("code")
            if n == "fail" and e.get("action") in ("hard_stop", "park"):
                return e.get("item"), e.get("reason")
            if n in ("refused", "error"):
                return None, n
        if outcome in _WAITING:
            try:
                waiting = self.drv.state().by_status("awaiting_human", "pr_open")
                return (waiting[0] if waiting else self.item), None
            except common.LoopctlError:
                pass
        return self.item, None


def attach(drv: Any) -> Optional[Supervisor]:
    """Called by the driver's constructor: a Supervisor when the base sha's config asks for an unattended run, else None."""
    return Supervisor.attach(drv) if wanted(drv.cfg) else None
