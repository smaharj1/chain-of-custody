"""The serial driver: `runner: driver` (OPT-IN) (report 10.6, 11.1 item 2, 11.2, 11.6, 14.2, 15 item 5; DEC-23, IMP-1, IMP-25).

A human starts `loopctl run` once. The driver then does what the conductor's prose did, deterministically, and launches a child
`claude -p` session for every piece of judgement work. `startrun.start` has already taken the lock (with `driverPid`), written
`run.json` and called `run(ctx, loop, run_record, as_json)` here; the lock is released by the caller whatever happens.

THE LOOP. State is the only memory. Every iteration re-reads the item's record from `refs/loopctl/state/<plan>` and acts on its
status, so a crash, a restart or a human's `approve` needs no special path: the next run recovers (reconcile) and carries on.

    pending ---begin---> designing    G0 (doctor) first; the briefs are written from the approved plan item
    designing -> design_review        the plan item IS the design in driver mode (H1/H2 approved it; slicing gave it a footprint)
    design_review -> stubbing         only if gates.g2 is not off and the acceptance-author agent exists (a child writes the stubs)
                  -> building         one child per engineer role, in dependency order, each with a brief and a report path;
                                      a WIP commit after each; NEEDS_CLARIFICATION, PARTIAL, ESCALATION_NEEDED are read from the report
    building -> verifying             the gates (g1 g3 g5 g6 g9 g11) through 4a's runner; per-AC evidence is recorded for the matrix
    verifying -> reviewing            code-reviewer (and security-reviewer when sensitive) as children, then g7
    reviewing -> merged               delivery: local and nothing needing a human: `git merge --no-ff` with the Loop-Item trailer
              -> awaiting_human       a human-gated item, an infra item, or any delivery other than local: the driver stops here;
                                      the human approves (`loopctl approve item`) and runs `loopctl run` again

A step back is `Loop.fail`, which counts it against the right cap and answers retry, discard, park or hard stop. The driver obeys:
a retry goes where the answer says (with the backoff slept, interruptibly, for a platform failure), a discard starts attempt n+1, and
a park or hard stop ends the run (exit 1): "park and stop", nothing is skipped to reach the next item.

WHAT IS DECIDED HERE, NOT BY A CHILD
  * which role runs, with which settings, budget and brief (launch.py); a child can only report;
  * the cost: every child's `total_cost_usd` goes to the ledger; an unknown cost, a ceiling or 80% of the run budget parks (10.5);
  * what a child's exit means: classify_exit, with an unreadable CLI failure counted as `platform`, never as the item's fault;
  * done: a valid report AND a state transition, never "the process returned" (11.3).

WHAT IS NOT BUILT HERE (deferred, with where it goes): pushing a branch or opening a PR (Phase 7 / the human's `pr_prepare` commands),
parallel items (Phase 8), the unattended notifications and stall watchdog around this (Phase 7), the remote lock (Phase 11).

Config and plan come from the run's start base sha for the whole run (CO-11); a gate gets `base_sha` = the item's branch point so
that its diff is the item's own.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kitlib import caller as caller_mod
from kitlib import gitutil, jsonio, levels, runstate

from . import branches, common, failure, gates, heartbeat, launch, lgit, plan as plan_mod, reconcile, report as report_mod
from . import state as state_mod
from .common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, LoopctlError, Refused, UsageError

__all__ = ["run", "Driver", "SLEEP", "engineers_for", "classify_child", "parse_verdict", "reason_for", "is_sensitive", "ENGINEER_ORDER"]

SCHEMA = "loopctl.driver.v1"
ENGINEER_ORDER = ("infra-engineer", "database-engineer", "backend-engineer", "app-engineer", "admin-app-engineer")
PRE_GATES = ("g0",)
STUB_GATES = ("g2",)
VERIFY_GATES = ("g5", "g1", "g3", "g6", "g9", "g11")      # g5 first: a test-integrity hit makes the rest pointless
REVIEW_GATES = ("g7",)
MAX_ITERATIONS = 200                                      # per item: a runaway backstop, not a tuning knob

_IDENT = {"GIT_AUTHOR_NAME": "loopctl driver", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl driver", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}

_VERDICTS = {
    "code-reviewer": ("CODE_REVIEW_APPROVED", "CHANGES_REQUESTED"),
    "security-reviewer": ("SECURITY_APPROVED", "SECURITY_BLOCKED"),
    "design-critic": ("DESIGN_APPROVED", "DESIGN_NEEDS_REVISION"),
}
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
_SENSITIVE = re.compile(r"\b(auth|authn|authz|authenticat\w*|authoriz\w*|login|logout|password|passwd|credential\w*|secret\w*|token\w*|session\w*|"
                        r"oauth|jwt|payment\w*|billing|checkout|stripe|invoice\w*|upload\w*|multipart|sanitiz\w*|permission\w*|rbac|encrypt\w*|pii|"
                        r"user[- ]supplied|user input)\b", re.I)

# The backoff sleep, replaceable by tests: (seconds, tick) -> None, where tick() returns a reason to stop or None. The default sleeps in
# short slices so a HALT, a lost lock or a SIGTERM ends a 15-minute backoff within a second.
SLEEP = None  # type: Optional[Callable[[float, Callable[[], Optional[str]]], None]]


def _default_sleep(seconds: float, tick: Callable[[], Optional[str]]) -> None:
    end = time.time() + seconds
    while True:
        if tick():
            return
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


class _Stop(Exception):
    """End the run now. `outcome` names why; `code` is the exit code."""

    def __init__(self, outcome: str, message: str, code: int) -> None:
        Exception.__init__(self, message)
        self.outcome, self.message, self.code = outcome, message, code


class _StageDone(Exception):
    """The state was changed by a park or a failure; stop working on this stage and re-read the item."""


# -- pure helpers (tested directly) -----------------------------------------------------------------------------------------

def engineers_for(item: Dict[str, Any], roster: Sequence[str]) -> List[str]:
    """The engineer roles that build an item: `estimate.engineers`, else `<domain>-engineer` for each footprint domain that is on the
    roster, in dependency order (infra, database, backend, app, admin-app, then any other)."""
    roles = []  # type: List[str]
    est = item.get("estimate") if isinstance(item.get("estimate"), dict) else {}
    for r in est.get("engineers") or []:
        if isinstance(r, str) and _ROLE_RE.match(r) and r not in roles:      # a role becomes a file name and an --agent value: plain names only
            roles.append(r)
    if not roles:
        touches = item.get("touches") if isinstance(item.get("touches"), dict) else {}
        for d in touches.get("domains") or []:
            r = "%s-engineer" % d
            if isinstance(d, str) and _ROLE_RE.match(r) and r in roster and r not in roles:
                roles.append(r)
    return sorted(roles, key=lambda r: (ENGINEER_ORDER.index(r) if r in ENGINEER_ORDER else len(ENGINEER_ORDER), r))


def domain_of(role: str) -> str:
    return role[:-len("-engineer")] if role.endswith("-engineer") else role


def is_sensitive(item: Dict[str, Any]) -> bool:
    """`security_review: sensitive` (orchestrate step 7): the item touches auth, payments, uploads, user-supplied input, a dependency
    change, or is classed high risk. Conservative: the words are searched in the intent, title, criteria and footprint."""
    if item.get("riskClass") == "high":
        return True
    touches = item.get("touches") if isinstance(item.get("touches"), dict) else {}
    if touches.get("deps_change") or touches.get("schema"):
        return True
    text = " ".join([str(item.get("title", "")), str(item.get("intent", ""))] + [str(a.get("text", "")) for a in item.get("acceptance") or []
                    if isinstance(a, dict)] + [str(p) for p in touches.get("paths") or []])
    return bool(_SENSITIVE.search(text))


def parse_verdict(text: str, role: str) -> Optional[str]:
    """The reviewer's verdict token from its final message: the first line after a `## Verdict` heading that is exactly one of the
    role's tokens. None when there is none (a malformed review: the driver fails the item with `malformed_report`)."""
    tokens = _VERDICTS.get(role)
    if not tokens or not isinstance(text, str):
        return None
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^#{1,4}\s*Verdict\s*$", ln.strip(), re.I):
            for nxt in lines[i + 1:i + 6]:
                tok = nxt.strip().strip("*`_ ").strip()
                if tok in tokens:
                    return tok
                if tok and tok not in tokens and not tok.startswith(("#", "<")):
                    # the template's "A | B" line is not a verdict
                    return None
            return None
    return None


def classify_child(res: launch.ChildResult) -> Optional[Dict[str, str]]:
    """None when the child finished its work; else {failure_class, reason} (or {budget: True}). Everything the CLI itself does wrong is
    `platform` (retry with backoff, no retry budget used); the item is blamed only by what the item's own report or gates say."""
    p = res.parsed or {}
    sub = str(p.get("subtype") or "")
    if res.reason == "stall":
        return failure.classify_exit("", None, None, None, True)
    if res.reason == "timeout":
        return {"failure_class": failure.TRANSIENT, "reason": "timeout"}
    if "budget" in sub:
        return {"failure_class": "budget", "reason": "budget"}
    if sub == "error_max_turns":
        return {"failure_class": failure.ITEM, "reason": "partial"}
    clean = res.returncode == 0 and res.signal_name is None and not res.reason and res.parsed is not None \
        and not p.get("is_error") and (not sub or sub == "success")
    if clean:
        return None
    text = " ".join([str(p.get("result") or ""), res.stderr_tail, "" if p else res.stdout[-2000:]])
    fc = failure.classify_exit(text, res.returncode, res.signal_name, None, False)
    if fc["failure_class"] in (failure.ITEM,):
        return {"failure_class": failure.PLATFORM, "reason": "cli_crash"}
    return fc


def reason_for(gate: str, res: Dict[str, Any]) -> Tuple[str, str]:
    """(failure class, reason) for a blocking gate result. A gate may name its own reason in `findings[0]["reason"]` if it is a valid
    one for the class; otherwise the defaults below. A failure that is not an `item` failure keeps the gate's class."""
    fc = res.get("failure_class")
    if fc not in failure.CLASSES:
        fc = failure.ENV if res.get("status") == "error" else failure.ITEM
    hint = None
    for f in res.get("findings") or []:
        if isinstance(f, dict) and isinstance(f.get("reason"), str) and re.match(r"^[a-z][a-z0-9_]{1,39}\Z", f["reason"]):
            hint = f["reason"]
            break
    if hint and failure.KNOWN_REASONS.get(hint, fc) == fc:
        return fc, hint
    if fc == failure.ENV:
        return fc, "doctor_failed"
    if fc == failure.PLATFORM:
        return fc, "cli_crash"
    if fc == failure.TRANSIENT:
        return fc, "flaky_test"
    ids = " ".join(str(f.get("id", "")) for f in res.get("findings") or [] if isinstance(f, dict)).lower()
    if gate == "g5":
        return fc, "reward_hack"
    if gate == "g7":
        return fc, "review_blocked"
    if gate == "g1":
        return fc, "ac_unclear"
    if "lint" in ids:
        return fc, "lint_failed"
    if "build" in ids:
        return fc, "build_failed"
    return fc, "tests_failed"


# -- the driver ---------------------------------------------------------------------------------------------------------------

class Driver(object):
    compute_level = True        # the doctor's effective level, once per run, for the gates (tests turn it off)

    def __init__(self, ctx: Any, loop: Any, run_record: Dict[str, Any], as_json: bool, out: Any = None) -> None:
        self.ctx = ctx
        self.loop = loop
        self.cfg = ctx.cfg
        self.rs = ctx.rs
        self.plan = ctx.plan
        self.repo = ctx.repo
        self.env = dict(ctx.env)
        self.run_rec = run_record
        self.sid = run_record["conductor_session_id"]
        self.run_id = run_record["runId"]
        self.as_json = as_json
        self.out = out if out is not None else sys.stdout
        self.anchor = run_record.get("baseSha") or ctx.base_sha            # config, plan and agent files come from here
        self.binary = ""
        self.fake = False
        self.timing = launch.Timing()
        self.hb = heartbeat.Heartbeat(self.rs, self.sid)
        self.level = None  # type: Optional[str]
        self.events = []  # type: List[Dict[str, Any]]
        self.item_base = {}  # type: Dict[str, str]
        self.passes = {}  # type: Dict[Tuple[str, int], int]
        self.done_roles = {}  # type: Dict[Tuple[str, int, int], set]
        self.child_n = 0
        self.merged_now = []  # type: List[str]
        self._cache = None  # type: Optional[Tuple[Tuple[int, int, int], Any]]
        self.clarification = {}  # type: Dict[Tuple[str, int], str]
        self.outcome = "complete"
        self.ib = None  # type: Optional[str]
        self._old_handlers = {}  # type: Dict[int, Any]
        self.roster_kinds = dict(caller_mod.DEFAULT_ROSTER)
        extra = self.cfg.get("roster")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if v in ("engineer", "critic"):
                    self.roster_kinds[str(k)] = v

    # -- output and events -----------------------------------------------------------------------------------------------------
    def say(self, msg: str) -> None:
        if not self.as_json:
            self.out.write("loopctl driver: %s\n" % common.clean_text(msg, 300))
            try:
                self.out.flush()
            except (OSError, ValueError):
                pass

    def ev(self, name: str, **kw: Any) -> None:
        rec = dict(kw, event=name, source="driver", runId=self.run_id)
        self.events.append(rec)
        del self.events[:-300]
        try:
            self.rs.append_event("loopctl", rec)
        except (OSError, ValueError):
            pass

    def sleep(self, seconds: float) -> None:
        (SLEEP or _default_sleep)(seconds, self.hb.tick)
        r = self.hb.tick()
        if r:
            raise self._stop_for(r)

    # -- git helpers (loopctl's own git, outside the guards, RC-13) -----------------------------------------------------------
    def git(self, args: Sequence[str], check: bool = False):
        proc = lgit.run_git(list(args), cwd=self.repo, isolated=True, pin_hooks=True, env=dict(_IDENT))
        if check and proc.returncode != 0:
            raise Refused("git %s failed: %s" % (" ".join(list(args)[:2]), common.clean_text(proc.stderr, 160)))
        return proc

    def head(self) -> str:
        sha = gitutil.rev_parse("HEAD^{commit}", self.repo, isolated=True)
        if not sha:
            raise Refused("the loop worktree has no HEAD commit")
        return sha

    def current_branch(self) -> Optional[str]:
        return branches.current(self.repo)

    def wip_commit(self, message: str) -> Optional[str]:
        """Commit whatever a child left in the tree, so the work is on the item's branch and the next review sees an exact range."""
        if lgit.is_clean(self.repo):
            return None
        self.git(["add", "-A"], check=True)
        self.git(["commit", "-q", "-m", common.clean_text(message, 120)], check=True)
        return self.head()

    # -- signals -------------------------------------------------------------------------------------------------------------------
    def _install_signals(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            if self.hb.interrupted:                 # the second signal: do not wait for the next poll; unwinding kills the child's group
                raise KeyboardInterrupt()
            self.hb.interrupted = True

        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self._old_handlers[s] = signal.signal(s, handler)
            except (ValueError, OSError):        # not the main thread (a test harness): the poll loop still sees `interrupted`
                pass

    def _restore_signals(self) -> None:
        for s, h in self._old_handlers.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass
        self._old_handlers = {}

    def _stop_for(self, reason: str) -> _Stop:
        if reason == heartbeat.REASON_HALT:
            h = self.rs.read_halt() or {}
            return _Stop("halted", "HALT is set (%s): dispatch stopped" % common.clean_text(h.get("reason", "no reason recorded"), 80), EXIT_REFUSED)
        if reason == heartbeat.REASON_LOCK:
            return _Stop("lock_lost", "this driver no longer holds the run lock: it stops writing state", EXIT_REFUSED)
        return _Stop("interrupted", "interrupted by a signal", EXIT_FINDINGS)

    def check_stop(self) -> None:
        r = self.hb.tick()
        if r:
            raise self._stop_for(r)

    # -- state ---------------------------------------------------------------------------------------------------------------------
    def _ref_signature(self) -> Optional[Tuple[int, int, int]]:
        """(inode, mtime, size) of the loose ref file. `git update-ref` replaces it by a rename, so any write by anyone changes it. None when
        the ref is packed or absent (then nothing is cached)."""
        try:
            st = os.stat(os.path.join(self.rs.common_dir, *self.loop.store.ref.split("/")))
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def state(self) -> Any:
        """The current state, re-read whenever the ref has moved. The parsed document is reused while the ref file is unchanged (a stat, not
        three git calls); the signature is taken BEFORE the read, so a write that lands in between only costs one more read. Callers treat
        the result as read-only: every write goes through `Loop`, which re-validates against the fresh state inside its own
        compare-and-swap."""
        sig = self._ref_signature()
        if sig is not None and self._cache is not None and self._cache[0] == sig:
            return self._cache[1]
        st, _sha = self.loop.store.read()
        if st is None:
            raise Refused("there is no loop state: `loopctl approve plan` first")
        self._cache = (sig, st) if sig is not None else None
        return st

    def rec(self, iid: str) -> Dict[str, Any]:
        return self.state().item(iid)

    def item_def(self, iid: str) -> Dict[str, Any]:
        return self.plan.items[iid]

    def set_header(self, note: str, **kv: Any) -> None:
        def mutate(st: Any) -> None:
            st.header.update(kv)
        self.loop.store.update(mutate, note)

    # -- entry --------------------------------------------------------------------------------------------------------------------
    def main(self) -> int:
        code = EXIT_OK
        try:
            self.setup()
            code = self.run_items()
        except _Stop as s:
            self.outcome = s.outcome
            code = s.code
            self.say("stopped (%s): %s" % (s.outcome, s.message))
            self.ev("stop", outcome=s.outcome, message=common.clean_text(s.message, 200))
        except launch.LaunchRefused as exc:
            self.outcome = "refused"
            code = EXIT_REFUSED
            self.say("refused: %s" % common.clean_text(exc, 300))
            self.ev("refused", message=common.clean_text(exc, 200))
        except LoopctlError as exc:
            self.outcome = "error"
            code = exc.exit_code
            self.say("stopped: %s" % common.clean_text(exc, 300))
            self.ev("error", message=common.clean_text(exc, 200))
        finally:
            self._restore_signals()
            self.settle_worktree()
        self.finish_output(code)
        return code

    def settle_worktree(self) -> None:
        """However the run ended, leave the loop worktree clean and on the integration branch if this driver still holds the lock: the
        work in progress is committed to the item's branch first, and the branch stays (the next start discards it, L-3). A driver that
        lost its lock touches nothing: another run may own the worktree now."""
        try:
            if not self.ib or not self.hb.holds_lock():
                return
            cur = self.current_branch()
            if cur == self.ib or cur is None or branches.parse(cur) is None:
                return
            if not lgit.is_clean(self.repo):
                self.wip_commit("WIP(loopctl): work in progress when the driver stopped")
            self.git(["switch", "--quiet", self.ib])
        except Exception:  # noqa: BLE001 - best effort on the way out; the next start's recovery handles whatever is left
            pass

    def finish_output(self, code: int) -> None:
        try:
            st = self.state()
            items = {i: r["status"] for i, r in st.items.items()}
        except LoopctlError:
            items = {}
        spent = None
        try:
            spent = self.ctx.ledger().totals(run_id=self.run_id)["usd"]
        except Exception:  # noqa: BLE001
            pass
        doc = {"schema": SCHEMA, "outcome": self.outcome, "exit": code, "run": self.run_id, "items": items, "merged_this_run": self.merged_now,
               "children": self.child_n, "spent_usd": spent, "events": self.events[-40:]}
        if self.as_json:
            self.out.write(json.dumps(doc, sort_keys=True) + "\n")
        else:
            self.say("run %s: %s (exit %d); %d merged this run; %d child session(s)%s" % (
                self.run_id, self.outcome, code, len(self.merged_now), self.child_n, "; spent $%.4f" % spent if spent is not None else ""))

    # -- setup ----------------------------------------------------------------------------------------------------------------------
    def setup(self) -> None:
        self.binary = launch.find_binary(self.env)
        launch.check_nesting(self.env, self.binary)
        self.fake = launch.is_fake_binary(self.binary)
        self.timing = launch.Timing.from_config(self.cfg, self.env, self.fake)
        self.hb.every_s = self.timing.beat_s
        try:
            lvl = levels.parse_level(str(self.cfg.get("autonomy_level") or "L1"))
        except Exception:  # noqa: BLE001
            lvl = levels.L1
        for kind in ("engineer", "critic"):                    # a tampered template or config is refused before any state changes
            settings, _skipped = launch.build_settings(kind, self.rs, self.cfg, (), self.env)
            problems = launch.validate_settings(settings)
            if problems:
                raise launch.LaunchRefused("the %s child profile is unsafe: %s" % (kind, "; ".join(problems[:3])))
        v = launch.check_version(self.repo, self.anchor, self.rs, self.env, self.binary, lvl)
        if v.get("warning"):
            self.say("warning: %s" % v["warning"])
        self.ib = self.ctx.integration_branch()
        if not self.ib:
            raise Refused("there is no integration branch (main, master or integration_branch): nothing to merge into")
        self.level = self.compute_effective_level()
        self.ev("start", binary=os.path.basename(self.binary), claude_version=v.get("version"), level=self.level, fake=self.fake)
        self.say("run %s: claude %s, level %s, plan %s" % (self.run_id, v.get("version"), self.level or "not computed", self.plan.name))
        reaped = launch.reap_orphans(self.rs, os.getpid())
        for r in reaped:
            self.say("killed an orphaned child of a dead driver (session %s, item %s)" % (r["sessionId"], r["item"]))
            self.ev("reaped", **r)
        self._install_signals()
        self.check_stop()
        self.put_worktree_on_integration()
        self.recover()

    def compute_effective_level(self) -> Optional[str]:
        """The doctor's effective level, once per run, for the gates (GateContext.level). Not computed against the test double: the doctor
        would be describing a fake machine. A doctor that cannot be read is L0 (fail closed), not 'unknown'."""
        if not self.compute_level or self.fake:
            return None
        eff = self.ctx.effective_level()
        return str(eff.get("effective")) if eff.get("effective") else None

    def put_worktree_on_integration(self) -> None:
        cur = self.current_branch()
        if cur == self.ib:
            return
        if cur is not None and branches.parse(cur) is None:
            raise Refused("the loop worktree is on %s; start the driver from the integration branch %s (or a feat/* branch it left behind)" % (cur, self.ib))
        if not lgit.is_clean(self.repo):
            raise Refused("the loop worktree has uncommitted changes (L-1)")
        self.git(["switch", "--quiet", self.ib], check=True)

    def recover(self) -> None:
        """What a previous driver may have left (a crash is the normal way a run ends badly): a merge that never reached the state
        (recovered from its Loop-Item trailer), items in flight (discarded back to pending: V1 restarts an item from stage 1, L-3),
        and orphaned branches (renamed dead/*). The lock is ours, so reconcile's own 'no live lock holder' test cannot fire; this is the
        same `reconcile.apply`, fed the in-flight items explicitly."""
        st = self.state()
        rep = reconcile.scan(self.ctx, st, self.plan)
        for iid in st.by_status(*state_mod.ACTIVE):
            rep.actions.append({"op": "reset-inflight", "item": iid})
            rep.find("warn", "driver.stale-inflight", "item %s was %s when the previous driver stopped: back to pending" % (iid, st.items[iid]["status"]), iid)
        reconcile.apply(self.loop, rep, by="driver")
        for f in rep.findings:
            if f.severity in ("error", "warn"):
                self.say("recovery: %s" % common.clean_text(f.message, 200))
        for a in rep.applied:
            self.ev("recovered", action=a)
        st = self.state()
        live = [r["branch"] for r in st.items.values() if r.get("branch") and r["status"] in state_mod.IN_FLIGHT]
        done = branches.apply_sweep(self.ctx, branches.sweep(self.ctx, live))
        for d in done:
            self.ev("swept", action=d)
        left = st.by_status(*state_mod.ACTIVE)
        if left:
            raise Refused("item %s is still in flight after recovery" % left[0])

    # -- the run ----------------------------------------------------------------------------------------------------------------------
    def run_items(self) -> int:
        order = self.loop._order()
        deps = self.loop._deps()
        loops = 0
        while True:
            loops += 1
            if loops > 4 * max(1, len(order)) + 25:
                raise _Stop("runaway", "the driver went round %d times without finishing: stopping (this is a bug, not a plan problem)" % loops, EXIT_USAGE)
            self.check_stop()
            st = self.state()
            hdr = st.header
            if hdr.get("runStatus") in ("stopped", "aborted"):
                raise _Stop("stopped", "the run is %s (%s): a human clears it with `loopctl approve breaker`" % (
                    hdr.get("runStatus"), (hdr.get("parked") or {}).get("reason")), EXIT_FINDINGS)
            gate = self.pending_checkpoint(st)
            if gate == "wait":
                p = hdr.get("parked") or {}
                raise _Stop("checkpoint", "waiting for the checkpoint after %s: `loopctl approve checkpoint %s --by <you>`" % (p.get("item"), p.get("item")), EXIT_REFUSED)
            if gate == "stop":
                self.outcome = "stopped_by_human"
                self.say("a human chose to stop at the checkpoint")
                return EXIT_OK
            v = self.ctx.ledger().verdict(self.cfg, self.run_id)
            if not v.ok:
                self.budget_stop(v)
            ready = self.awaiting_with_approval(st)
            if ready:
                try:
                    self.merge_approved(ready)
                except _StageDone:
                    self.outcome = "parked"
                    return EXIT_FINDINGS
                if self.checkpoint_due(ready):
                    return EXIT_OK
                continue
            waiting = st.by_status("awaiting_human", "pr_open")
            if waiting:
                self.outcome = "awaiting_human"
                self.say("item %s is waiting for a human (`loopctl status --pending-human`); approve it and run again" % waiting[0])
                return EXIT_OK
            eligible = st.eligible(order, deps)
            if not eligible:
                return self.end_of_plan(st)
            iid = eligible[0]
            res = self.process_item(iid)
            if res == "merged":
                self.merged_now.append(iid)
                if self.checkpoint_due(iid):
                    return EXIT_OK
            elif res == "awaiting":
                self.outcome = "awaiting_human"
                return EXIT_OK
            elif res == "parked":
                self.outcome = "parked"
                return EXIT_FINDINGS
            # "retry": a discard left the item pending; the next round picks it up as attempt n+1

    def end_of_plan(self, st: Any) -> int:
        counts = st.counts()
        left = [i for i, r in st.items.items() if r["status"] not in state_mod.TERMINAL]
        if not left:
            self.outcome = "complete"
            self.set_header("run complete", runStatus="idle", parked=None)
            self.say("the plan is complete: %d item(s) merged" % counts["merged"])
            return EXIT_OK
        stuck = [i for i in left if st.items[i]["status"] in state_mod.RESTING]
        self.outcome = "deadlock" if not stuck else "parked"
        self.say("nothing is eligible: %s" % ("; ".join("%s %s (%s)" % (i, st.items[i]["status"], st.items[i].get("blockReason")) for i in sorted(stuck, key=common.natural_key)[:6])
                                              or "every remaining item waits on a dependency"))
        return EXIT_FINDINGS

    # -- checkpoints (autonomy) ------------------------------------------------------------------------------------------------------
    def checkpoint_due(self, iid: str) -> bool:
        """After a merge: pause for the human per the autonomy dial (per-feature: every item; per-milestone: at a milestone boundary) and
        always after an infra item. The pause is recorded in the state; the next start needs `approve checkpoint`."""
        item = self.item_def(iid)
        autonomy = str(self.cfg.get("autonomy") or "per-milestone")
        st = self.state()
        remaining = [i for i, r in st.items.items() if r["status"] not in state_mod.TERMINAL]
        if not remaining:
            return False
        infra = "infra" in ((item.get("touches") or {}).get("domains") or [])
        boundary = not any(self.plan.items[i].get("milestone") == item.get("milestone") for i in remaining if i in self.plan.items)
        if not (infra or autonomy == "per-feature" or (autonomy == "per-milestone" and boundary)):
            return False
        self.set_header("checkpoint after %s" % iid, runStatus="parked", parked={"item": iid, "reason": "checkpoint", "at": common.iso()})
        self.outcome = "checkpoint"
        self.say("checkpoint after %s (%s): review the result, then `loopctl approve checkpoint %s --by <you>` and run again" % (
            iid, "infra item" if infra else autonomy, iid))
        return True

    def pending_checkpoint(self, st: Any) -> Optional[str]:
        """None (no checkpoint outstanding), 'wait' (the human has not answered), 'go' (answered: consumed), 'stop' (a human said stop)."""
        p = st.header.get("parked")
        if not (isinstance(p, dict) and p.get("reason") == "checkpoint" and common.valid_item_id(p.get("item"))):
            return None
        iid = p["item"]
        found = None
        for ap in reversed(st.d["approvals"]):
            if ap.get("target") == iid and ap.get("scope") == "checkpoint" and not ap.get("used") and ap.get("choice") in ("approve", "stop"):
                found = ap["id"]
                choice = ap["choice"]
                break
        if found is None:
            return "wait"

        def mutate(s: Any) -> None:
            for ap in s.d["approvals"]:
                if ap["id"] == found:
                    ap["used"] = True
            s.header["parked"] = None
            s.header["runStatus"] = "running" if choice == "approve" else "idle"
        self.loop.store.update(mutate, "checkpoint %s answered" % iid)
        self.ev("checkpoint_answered", item=iid, choice=choice)
        return "go" if choice == "approve" else "stop"

    # -- budget -----------------------------------------------------------------------------------------------------------------------
    def budget_stop(self, v: Any, iid: Optional[str] = None) -> None:
        """The ledger says stop (a ceiling, 80% of the run budget, an unknown cost): park the item in flight, or just stop."""
        code = v.codes[0] if v.codes else "budget_run"
        why = "; ".join(v.reasons)[:200]
        st = self.state()
        target = iid or next(iter(st.by_status(*state_mod.ACTIVE)), None)
        if target and st.items[target]["status"] in state_mod.IN_FLIGHT:
            self.park(target, code, why)
        else:
            self.set_header("run parked: %s" % code, runStatus="parked", parked={"item": target, "reason": code, "at": common.iso()})
        self.ev("budget_stop", code=code, reasons=v.reasons[:2], spent=v.spent)
        raise _Stop("budget", "%s" % why, EXIT_FINDINGS)

    # -- parking -----------------------------------------------------------------------------------------------------------------
    def park(self, iid: str, code: str, detail: str, kind: str = "needs_human", breaker: bool = False) -> None:
        """A hard stop that is not an item failure: keep the work (WIP commit), park the state, and move the branch to blocked/*."""
        r = self.rec(iid)
        if r["status"] in state_mod.TERMINAL or r["status"] in state_mod.RESTING:
            return
        try:
            self.wip_commit("WIP(loopctl): %s parked (%s)" % (iid, code))
        except LoopctlError:
            pass
        self.loop.park(iid, code, detail, self.sid, "driver", kind, breaker)
        self.ev("park", item=iid, code=code, kind=kind)
        self.say("item %s parked: %s (%s)" % (iid, code, common.clean_text(detail, 140)))
        self.shelve_branch(iid)

    def shelve_branch(self, iid: str) -> None:
        br = self.rec(iid).get("branch")
        if not br or not branches.parse(br) or not branches.exists(self.repo, br):
            return
        try:
            if self.current_branch() == br:
                self.git(["switch", "--quiet", self.ib], check=True)
            new = branches.park(self.ctx, br)
        except (Refused, UsageError, LoopctlError) as exc:
            self.say("could not move %s aside: %s" % (br, common.clean_text(exc, 100)))
            return
        if new:
            def mark(st: Any) -> None:
                st.item(iid)["parkedBranch"] = new
            self.loop.store.update(mark, "parked branch %s" % iid)

    # -- failure ------------------------------------------------------------------------------------------------------------------------
    def fail(self, iid: str, fclass: str, reason: str, acs: Sequence[str] = (), stage: Optional[str] = None, detail: str = "",
             backoff: bool = True) -> Any:
        try:
            self.wip_commit("WIP(loopctl): %s before %s/%s" % (iid, fclass, reason))
        except LoopctlError:
            pass
        d = self.loop.fail(iid, fclass, reason, self.sid, "driver", list(acs), stage, common.clean_text(detail, 200))
        self.ev("fail", item=iid, failure_class=fclass, reason=reason, action=d.action, backoff_s=d.backoff_s)
        self.say("item %s: %s/%s -> %s%s" % (iid, fclass, reason, d.action, " (%ds backoff)" % d.backoff_s if d.backoff_s else ""))
        if d.action == "retry" and stage in ("verifying", "reviewing", "verify", "review"):
            self.new_pass(iid)
        if d.action == "retry" and d.backoff_s and backoff:
            self.sleep(d.backoff_s)
        if d.action in ("hard_stop", "park"):
            self.shelve_branch_if_current(iid)
        return d

    def shelve_branch_if_current(self, iid: str) -> None:
        # Loop.fail already renamed the branch (git effects); only make sure the worktree is back on the integration branch
        if self.current_branch() != self.ib and lgit.is_clean(self.repo):
            try:
                self.git(["switch", "--quiet", self.ib], check=True)
            except LoopctlError:
                pass

    def new_pass(self, iid: str) -> None:
        key = (iid, self.rec(iid)["attempt"])
        self.passes[key] = self.passes.get(key, 0) + 1

    def pass_no(self, iid: str, attempt: int) -> int:
        return self.passes.get((iid, attempt), 0)

    # -- one item ------------------------------------------------------------------------------------------------------------------------
    def process_item(self, iid: str) -> str:
        item = self.item_def(iid)
        missing = [n for n in item.get("requiresEnv") or [] if isinstance(n, str) and not self.env.get(n)]
        if missing and self.rec(iid)["status"] == "pending":
            self.loop.park(iid, "needs_provisioning", "missing environment: %s" % ", ".join(missing[:5]), self.sid, "driver", "blocked")
            self.say("item %s needs %s before it can start" % (iid, ", ".join(missing[:5])))
            return "parked"
        if self.rec(iid)["status"] == "pending":
            try:
                self.begin(iid)
            except _StageDone:
                return "parked"
        handlers = {"designing": self.stage_designing, "design_review": self.stage_design_review, "stubbing": self.stage_stubbing,
                    "building": self.stage_building, "verifying": self.stage_verifying, "reviewing": self.stage_reviewing}
        for _ in range(MAX_ITERATIONS):
            self.check_stop()
            status = self.rec(iid)["status"]
            if status in handlers:
                try:
                    handlers[status](iid)
                except _StageDone:
                    pass
            elif status == "merged":
                return "merged"
            elif status == "pending":
                return "retry"
            elif status in ("awaiting_human", "pr_open"):
                return "awaiting"
            else:
                return "parked"
        raise _Stop("runaway", "item %s went round its stages %d times: stopping" % (iid, MAX_ITERATIONS), EXIT_USAGE)

    def begin(self, iid: str) -> None:
        if self.current_branch() != self.ib or not lgit.is_clean(self.repo):
            raise _Stop("worktree", "the loop worktree is not clean and on %s: not starting %s (L-1)" % (self.ib, iid), EXIT_REFUSED)
        nb = runstate.parse_iso(str(self.rec(iid).get("notBefore") or ""))
        if nb is not None and nb > common.now():             # a platform backoff that outlived its park (a human unblocked the item early)
            self.say("item %s is backing off for %ds (platform failure)" % (iid, int(nb - common.now()) + 1))
            self.sleep(nb - common.now() + 0.5)
        rec = self.loop.begin(iid, self.sid, "driver")
        tip = gitutil.rev_parse("refs/heads/%s^{commit}" % self.ib, self.repo, isolated=True)
        self.item_base[iid] = tip or ""
        self.ev("begin", item=iid, attempt=rec["attempt"], branch=rec["branch"])
        self.say("item %s: attempt %d on %s" % (iid, rec["attempt"], rec["branch"]))
        try:
            if not tip:
                raise Refused("the integration branch %s has no commit" % self.ib)
            branches.create(self.repo, rec["branch"], tip)
            self.git(["switch", "--quiet", rec["branch"]], check=True)
        except LoopctlError as exc:
            self.loop.park(iid, "ESCALATION_NEEDED", "could not create the item branch: %s" % common.clean_text(exc, 120), self.sid, "driver")
            raise _StageDone()

    # -- gates ------------------------------------------------------------------------------------------------------------------------------
    def run_gates(self, iid: str, names: Sequence[str], stop_on: Sequence[str] = ("g5",),
                  extra: Sequence[Tuple[str, Dict[str, Any]]] = ()) -> List[Tuple[str, Dict[str, Any]]]:
        """Run the named gates that are installed, through 4a's runner (the toggle comes from the base sha's config), record every result,
        and return [(name, result)]. A blocking g5 ends the run of the rest: a test-integrity hit makes the others pointless."""
        installed = set(gates.installed())
        out = []  # type: List[Tuple[str, Dict[str, Any]]]
        rec = self.rec(iid)
        head = self.head()                                    # one head for the whole round: the gates judge the same commit
        for name in names:
            if name not in installed:
                self.ev("gate_skipped", item=iid, gate=name, why="not installed")
                continue
            gctx = self.ctx.gate_context(iid, head, self.repo, rec, self.level)
            if self.item_base.get(iid):
                gctx.base_sha = self.item_base[iid]              # the item's own diff; cfg stays the run's anchor (CO-11)
            res = gates.run_gate(name, gctx)
            out.append((name, res))
            self.ev("gate", item=iid, gate=name, status=res["status"], mode=res["mode"], blocking=res["blocking"])
            if res["blocking"] and name in stop_on:
                break
        # one compare-and-swap for the round; an `off` gate says nothing, so it writes nothing
        keep = [(n, r) for n, r in out if not (r["mode"] == "off" and r["status"] == "skipped")] + list(extra)
        if keep:
            self.loop.record_gates(iid, keep, self.sid)
        return out

    def gate_failure(self, iid: str, results: List[Tuple[str, Dict[str, Any]]], stage: str) -> bool:
        """If any gate result blocks, record ONE failure for the round (the worst class wins) and return True."""
        blocking = [(n, r) for n, r in results if r.get("blocking")]
        if not blocking:
            return False
        pri = {failure.ENV: 0, failure.PLATFORM: 1, failure.TRANSIENT: 2, failure.ITEM: 3}
        chosen = min(((reason_for(n, r), n, r) for n, r in blocking), key=lambda t: pri.get(t[0][0], 3))
        (fclass, reason), name, res = chosen
        acs = sorted({a for _n, r in results for a, cell in ((r.get("evidence") or {}).get("ac_results") or {}).items()
                      if isinstance(cell, dict) and cell.get("result") == "fail" and common.AC_RE.match(str(a))})
        msgs = [str(f.get("message", "")) for f in res.get("findings") or [] if isinstance(f, dict)][:3]
        self.fail(iid, fclass, reason, acs, stage, "gate %s: %s" % (name, "; ".join(msgs)))
        return True

    def ensure_required(self, iid: str, to: str, head: str) -> bool:
        """Run any gate the transition to `to` requires that has not passed for this head. False if one blocked (the failure is
        recorded). The gates are the ones installed and `on` in the base sha's config (`Context.required_gates_for`)."""
        rec = self.rec(iid)
        need = self.ctx.required_gates_for(rec["status"], to)
        todo = [g for g in need if not (rec["gates"].get(g, {}).get("status") == "pass" and rec["gates"][g].get("head") == head)]
        if not todo:
            return True
        return not self.gate_failure(iid, self.run_gates(iid, todo), rec["status"])

    def advance(self, iid: str, to: str, note: str = "", **kw: Any) -> bool:
        """`Loop.advance`, after the gates the transition requires. False if one of them blocked."""
        if not self.ensure_required(iid, to, kw.get("head") or self.head()):
            return False
        self.loop.advance(iid, to, self.sid, "driver", note, **kw)
        self.ev("advance", item=iid, to=to)
        return True

    # -- stages --------------------------------------------------------------------------------------------------------------------------------
    def stage_designing(self, iid: str) -> None:
        results = self.run_gates(iid, PRE_GATES)
        if self.gate_failure(iid, results, "designing"):
            raise _StageDone()
        self.write_briefs(iid)
        head = self.wip_commit("WIP(loopctl): %s briefs" % iid) or self.head()
        self.advance(iid, "design_review", "design: the approved plan item", head=head)

    def stage_design_review(self, iid: str) -> None:
        if self.stub_stage_wanted():
            self.advance(iid, "stubbing", "acceptance stubs first (g2)")
        else:
            self.advance(iid, "building", "no stubbing stage")

    def stub_stage_wanted(self) -> bool:
        if "g2" not in gates.installed():
            return False
        if gates.mode_for(self.cfg, "g2", gates.get("g2")) == "off":
            return False
        return gitutil.show_file(self.anchor or "HEAD", ".claude/agents/acceptance-author.md", self.repo, isolated=True) is not None

    def stage_stubbing(self, iid: str) -> None:
        o = self.launch_child(iid, "acceptance-author", "stubbing", self.stub_prompt(iid))
        if o is None:
            return
        self.wip_commit("WIP(loopctl): %s acceptance stubs" % iid)
        results = self.run_gates(iid, STUB_GATES)
        if self.gate_failure(iid, results, "stubbing"):
            return
        self.advance(iid, "building", "stubs committed", head=self.head())

    def stage_building(self, iid: str) -> None:
        r = self.rec(iid)
        item = self.item_def(iid)
        roles = engineers_for(item, list(self.roster_kinds))
        if not roles:
            self.park(iid, "ESCALATION_NEEDED", "the plan item names no engineer role")
            return
        attempt = r["attempt"]
        key = (iid, attempt, self.pass_no(iid, attempt))
        done = self.done_roles.setdefault(key, set())
        for role in roles:
            if role in done:
                continue
            self.check_stop()
            o = self.launch_child(iid, role, "building", self.engineer_prompt(iid, role, rework=self.is_rework(iid),
                                                                              clarification=self.clarification.get((iid, attempt), "")))
            if o is None:
                return
            if not self.engineer_report(iid, role, o):
                return
            done.add(role)
            self.wip_commit("WIP(loopctl): %s %s" % (iid, domain_of(role)))
        self.advance(iid, "verifying", "engineers approved", head=self.head())

    def stage_verifying(self, iid: str) -> None:
        self.wip_commit("WIP(loopctl): %s before verify" % iid)
        results = self.run_gates(iid, VERIFY_GATES)
        if self.gate_failure(iid, results, "verifying"):
            return
        self.advance(iid, "reviewing", "gates passed", head=self.head())

    def stage_reviewing(self, iid: str) -> None:
        item = self.item_def(iid)
        attempt = self.rec(iid)["attempt"]
        roles = self.review_roles(item)
        verdicts = {}  # type: Dict[str, str]
        rnd = self.rec(iid)["counters"].get("review", 0) + 1
        for role in roles:
            self.check_stop()
            o = self.launch_child(iid, role, "reviewing", self.review_prompt(iid, role, rnd))
            if o is None:
                return
            text = str((o.parsed or {}).get("result") or "")
            self.save_review(iid, role, rnd, text)
            v = parse_verdict(text, role)
            if v is None:
                self.fail(iid, failure.ITEM, "malformed_report", (), "reviewing", "%s returned no verdict line" % role)
                return
            verdicts[role] = v
            if v == "SECURITY_BLOCKED":
                self.loop.record_gates(iid, [self.reviews_result(verdicts)], self.sid)
                self.park(iid, "security_blocked", "the security-reviewer blocked the item; its findings are in docs/features/%s/reviews/" % item["slug"])
                raise _StageDone()
            if v == "CHANGES_REQUESTED":
                self.loop.record_gates(iid, [self.reviews_result(verdicts)], self.sid)
                self.fail(iid, failure.ITEM, "review_blocked", (), "reviewing", "the code-reviewer requested changes (round %d)" % rnd)
                return
        self.wip_commit("WIP(loopctl): %s reviews" % iid)
        head = self.head()
        results = self.run_gates(iid, REVIEW_GATES, extra=[self.reviews_result(verdicts)])
        if self.gate_failure(iid, results, "reviewing"):
            return
        self.finish_item(iid, head)

    def review_roles(self, item: Dict[str, Any]) -> List[str]:
        """code-reviewer always; security-reviewer per `security_review` (every_item, or sensitive and the item is: orchestrate step 7)."""
        roles = ["code-reviewer"]
        sr = str(self.cfg.get("security_review") or "sensitive")
        if sr == "every_item" or (sr == "sensitive" and is_sensitive(item)):
            roles.append("security-reviewer")
        return roles

    def reviews_result(self, verdicts: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
        """The reviewers' verdicts as a recorded result (`review` evidence feeds the AC x evidence view and the audit block)."""
        names = {"code-reviewer": "code", "security-reviewer": "security", "design-critic": "design"}
        ev = {"review": {names[r]: ("approved" if v.endswith("APPROVED") else "blocked") for r, v in verdicts.items() if r in names}}
        ok = all(v.endswith("APPROVED") for v in verdicts.values())
        return "reviews", {"status": "pass" if ok else "fail", "mode": "report", "blocking": False, "head": self.head(), "evidence": ev}

    def finish_item(self, iid: str, head: str) -> None:
        r = self.rec(iid)
        item = self.item_def(iid)
        gate = list(r.get("humanGate") or []) or plan_mod.item_human_gate(item, self.cfg.get_list("always_human_paths"))
        infra = "infra" in ((item.get("touches") or {}).get("domains") or [])
        delivery = str(self.cfg.get("delivery") or "local")
        if not self.ensure_required(iid, "awaiting_human" if (gate or infra or delivery != "local") else "merged", head):
            return
        if gate or infra or delivery != "local":
            why = ("needs a human before it merges: %s" % "; ".join(gate[:2])) if gate else (
                "infra changes always wait for a human" if infra else "delivery is %s: a human pushes the branch (pr_prepare prints the commands)" % delivery)
            if self.advance(iid, "awaiting_human", why, head=head):
                self.say("item %s %s" % (iid, why))
            return
        self.merge_item(iid, head)

    def merge_item(self, iid: str, head: str) -> None:
        r = self.rec(iid)
        br = r["branch"]
        if not self.ensure_required(iid, "merged", head):
            raise _StageDone()
        if gitutil.rev_parse("refs/heads/%s^{commit}" % br, self.repo, isolated=True) != head:
            self.park(iid, "head_changed", "the branch moved after its gates ran: %s is not %s" % (br, head[:12]))
            raise _StageDone()
        self.git(["switch", "--quiet", self.ib], check=True)
        msg = "Merge branch '%s'\n\nLoop-Item: %s\nLoop-Attempt: %d" % (br, iid, r["attempt"])
        proc = self.git(["merge", "--no-ff", "--no-edit", "-m", msg, br])
        if proc.returncode != 0:
            self.git(["merge", "--abort"])
            self.git(["switch", "--quiet", br])
            self.park(iid, "merge_conflict", "merging %s into %s conflicts; a human resolves it" % (br, self.ib))
            raise _StageDone()
        mc = self.head()
        self.ev("merged_in_git", item=iid, commit=mc)
        try:
            self.loop.advance(iid, "merged", self.sid, "driver", "merged by the driver", merge_commit=mc, head=head)
        except LoopctlError:
            # the merge is in git but the record failed (a lost race, a park from outside): recover it from the trailer, as a restart would
            self.recover()
            if self.rec(iid)["status"] != "merged":
                raise
        self.ev("merged", item=iid, commit=mc)
        self.say("item %s merged at %s" % (iid, mc[:12]))

    # -- an item a human approved ------------------------------------------------------------------------------------------------------
    def awaiting_with_approval(self, st: Any) -> Optional[str]:
        for iid in st.by_status("awaiting_human"):
            if state_mod._find_approval(st, iid, st.items[iid]["attempt"]) is not None:
                return iid
        return None

    def merge_approved(self, iid: str) -> None:
        r = self.rec(iid)
        br = r.get("branch")
        tip = gitutil.rev_parse("refs/heads/%s^{commit}" % br, self.repo, isolated=True) if br else None
        if not tip or tip != r.get("headSha"):
            self.loop.park(iid, "head_changed", "the branch is not at the head the human approved", self.sid, "driver")
            raise _Stop("parked", "item %s: the branch moved after it was approved; it needs a new review" % iid, EXIT_FINDINGS)
        if str(self.cfg.get("delivery") or "local") != "local":
            raise _Stop("delivery", "item %s is approved, but delivery is %s: the human pushes and merges; `loopctl reconcile` records it" % (
                iid, self.cfg.get("delivery")), EXIT_OK)
        self.put_worktree_on_integration()
        self.merge_item(iid, tip)
        self.merged_now.append(iid)

    # -- briefs and prompts ------------------------------------------------------------------------------------------------------------
    def verify_commands(self, iid: str) -> List[str]:
        out = []  # type: List[str]
        for k in ("verify_build", "verify_lint", "verify_typecheck", "verify_tests"):
            v = self.cfg.get(k)
            if isinstance(v, str) and v.strip() and "<" not in v:
                out.append(v.strip())
        t = (self.item_def(iid).get("verify") or {}).get("tests")
        if isinstance(t, str) and t.strip() and t.strip() not in out:
            out.append(t.strip())
        return out

    def brief_path(self, iid: str, role: str) -> str:
        return "docs/features/%s/briefs/%s.md" % (self.item_def(iid)["slug"], domain_of(role))

    def report_path(self, iid: str, role: str) -> str:
        return "docs/features/%s/reports/%s.md" % (self.item_def(iid)["slug"], domain_of(role))

    def write_briefs(self, iid: str) -> None:
        item = self.item_def(iid)
        roles = engineers_for(item, list(self.roster_kinds))
        if self.stub_stage_wanted():
            roles = ["acceptance-author"] + roles
        r = self.rec(iid)
        for role in roles:
            text = self.brief_text(iid, role, roles, r["attempt"])
            self.write_repo_file(self.brief_path(iid, role), text)

    def write_repo_file(self, rel: str, text: str) -> None:
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def brief_text(self, iid: str, role: str, roles: Sequence[str], attempt: int) -> str:
        item = self.item_def(iid)
        T = common.clean_text
        dom = domain_of(role)
        L = ["# Brief: %s — %s" % (T(role, 40), T(item["title"], 120)), "",
             "**Feature slug**: `%s`" % item["slug"], "**Engineer**: %s" % T(role, 40), "**Authored by**: loopctl driver, from the approved plan item %s" % iid,
             "**Iteration**: %d" % attempt, "**Retry cap**: `max_retries_per_item` from `.claude/loop.config.md`",
             "**Report path**: `%s` (write your own report here; the driver does not re-type it)" % self.report_path(iid, role), "", "---", "",
             "## Required Reading (in order)", "", "1. `.claude/context/engineer-protocol.md`",
             "2. `.claude/context/%s.md` (your primer; if it is missing, say so in your report)" % dom]
        if dom in ("app", "admin-app"):
            L.append("3. `.claude/context/shared-frontend.md`")
        L += ["", "## Intent", "", T(item.get("intent", ""), 1200), "", "## Acceptance Criteria", ""]
        for a in item.get("acceptance") or []:
            L.append("- [ ] **%s** — %s" % (T(a.get("id"), 40), T(a.get("text"), 700)))
        paths = (item.get("touches") or {}).get("paths") or []
        L += ["", "## Scope for This Engineer", "", "Work only inside the item's footprint:", ""] + ["- `%s`" % T(p, 160) for p in paths] if paths else \
            ["", "## Scope for This Engineer", "", "Work only on what the acceptance criteria require."]
        before = [x for x in roles if x != role and x != "acceptance-author" and roles.index(x) < roles.index(role)]
        L += ["", "## Dependencies", ""] + (["- [ ] `%s` (done before you; read it first)" % self.report_path(iid, x) for x in before] if before else ["- None"])
        if "acceptance-author" in roles and role != "acceptance-author":
            L += ["- The acceptance tests are already committed and pin the criteria above: make them pass; do not edit them."]
        cmds = self.verify_commands(iid)
        L += ["", "## Verification Commands (run all before reporting `APPROVED`)", ""] + (["- `%s`" % T(c, 160) for c in cmds] if cmds else ["- derive them from `.claude/context/%s.md`" % dom])
        L += ["", "## Out of Scope", "", "- Anything outside the footprint above, other items' files, CI and lint configuration, tests that already pin an acceptance criterion.",
              "- Git writes, `loopctl`, launching `claude`: the driver commits and moves the state.", "", "## Clarifications", "",
              "(Empty at dispatch. If you return `NEEDS_CLARIFICATION`, the driver writes its answer here and runs you again.)", ""]
        return "\n".join(L)

    def is_rework(self, iid: str) -> bool:
        """A round that follows a failure of the WORK (a gate, a review, a malformed report, a partial). A platform failure or a stall
        re-runs the same prompt: there is nothing for the engineer to change."""
        r = self.rec(iid)
        lf = r.get("lastFailure")
        redo = isinstance(lf, dict) and lf.get("class") == failure.ITEM
        return redo or bool(r.get("rework")) or self.pass_no(iid, r["attempt"]) > 0

    def rework_text(self, iid: str) -> str:
        r = self.rec(iid)
        L = []  # type: List[str]
        lf = r.get("lastFailure")
        if isinstance(lf, dict) and lf.get("class") == failure.ITEM:
            L.append("The previous round failed: %s/%s at %s%s." % (lf.get("class"), lf.get("reason"), lf.get("stage"),
                     "; failing criteria %s" % ", ".join(lf.get("failedAcs") or []) if lf.get("failedAcs") else ""))
            if lf.get("detail") and lf.get("reason") not in ("malformed_report",):          # gate text is the kit's; a malformed report's is the child's own
                L.append("Detail: %s" % common.clean_text(lf["detail"], 300))
        for name, g in sorted((r.get("gates") or {}).items()):
            if g.get("status") in ("fail", "error"):
                for f in (g.get("findings") or [])[:5]:
                    L.append("- gate %s: %s" % (name, common.clean_text(f.get("message", ""), 200)))
        if r.get("rework"):
            L.append("A human's feedback at the last checkpoint: %s" % common.clean_text(r["rework"][-1].get("note", ""), 400))
        slug = self.item_def(iid)["slug"]
        for role in ("code-reviewer", "security-reviewer"):
            p = os.path.join(self.repo, "docs", "features", slug, "reviews")
            if os.path.isdir(p):
                names = sorted(n for n in os.listdir(p) if n.startswith(role))
                if names:
                    L.append("The latest %s review is `docs/features/%s/reviews/%s`; address its blockers." % (role, slug, names[-1]))
        return "\n".join(L)

    def prompt_header(self, iid: str, role: str, stage: str) -> List[str]:
        item = self.item_def(iid)
        r = self.rec(iid)
        return ["You are the %s for loop item %s (\"%s\"), attempt %d, stage %s, in a session started by the loopctl driver." % (
                role, iid, common.clean_text(item["title"], 100), r["attempt"], stage),
                "No human is present: you cannot ask questions. A gap in your brief is NEEDS_CLARIFICATION in your report (engineer-protocol section 2).",
                "Do not run loopctl, do not launch claude, and do not run git commands that write; the driver commits and moves the state.", ""]

    def engineer_prompt(self, iid: str, role: str, rework: bool = False, clarification: str = "") -> str:
        L = self.prompt_header(iid, role, "building")
        L += ["Your brief: %s" % self.brief_path(iid, role), "Write your report to: %s" % self.report_path(iid, role),
              "End your final message with the JSON trailer line {\"status\": ..., \"report_path\": \"%s\", \"iteration\": N} (engineer-protocol section 7)." % self.report_path(iid, role)]
        if clarification:
            L += ["", "Answer to your clarification request: %s" % clarification]
        if rework:
            L += ["", "This is a REWORK round. Address the following, then report again with a higher iteration number.", self.rework_text(iid)]
        return "\n".join(L) + "\n"

    def stub_prompt(self, iid: str) -> str:
        L = self.prompt_header(iid, "acceptance-author", "stubbing")
        L += ["Your brief: %s" % self.brief_path(iid, "acceptance-author"),
              "Write interface stubs (signatures that raise or return a sentinel) and the acceptance tests for each criterion id, in a fresh context; do not implement the feature."]
        return "\n".join(L) + "\n"

    def review_prompt(self, iid: str, role: str, rnd: int) -> str:
        item = self.item_def(iid)
        base = self.item_base.get(iid) or self.ctx.base_sha or ""
        roles = engineers_for(item, list(self.roster_kinds))
        L = self.prompt_header(iid, role, "reviewing")
        L += ["Review the item's diff: `git diff %s..HEAD` (commits `WIP(loopctl)` are the engineers' work, one per engineer)." % base[:40],
              "Briefs: " + ", ".join(self.brief_path(iid, r) for r in roles), "Reports: " + ", ".join(self.report_path(iid, r) for r in roles),
              "Run the verify commands yourself: " + "; ".join(self.verify_commands(iid)), "Review round %d." % rnd,
              "Reply in your strict output format. The line after `## Verdict` must be exactly one of: %s." % " or ".join(_VERDICTS.get(role, ("APPROVED",)))]
        return "\n".join(L) + "\n"

    def save_review(self, iid: str, role: str, rnd: int, text: str) -> None:
        slug = self.item_def(iid)["slug"]
        self.write_repo_file("docs/features/%s/reviews/%s-r%d.md" % (slug, role, rnd), common.clean_text(text, 60000, keep_newlines=True) + "\n")

    # -- an engineer's report -----------------------------------------------------------------------------------------------------------
    def engineer_report(self, iid: str, role: str, o: "Outcome") -> bool:
        """True when the engineer's report is valid and APPROVED (or a deviation that lets the item go on). Otherwise the matching
        failure, park or clarification has been recorded and the stage returns."""
        item = self.item_def(iid)
        rel = self.report_path(iid, role)
        final = str((o.parsed or {}).get("result") or "")
        rep = report_mod.read_report(self.repo, rel, final_message=final, slug=item["slug"], require_trailer=True)
        if rep.malformed:
            self.fail(iid, failure.ITEM, "malformed_report", (), "building", "; ".join(rep.problems[:3]))
            return False
        st = rep.status
        self.ev("engineer_report", item=iid, role=role, status=st)
        if st in ("APPROVED", "CONTRACT_DEVIATION", "DESIGN_DEVIATION"):
            return True
        if st == "PARTIAL":
            self.fail(iid, failure.ITEM, "partial", (), "partial", "%s returned PARTIAL" % role)
            return False
        if st == "NEEDS_CLARIFICATION":
            n = self.loop.clarify(iid, self.sid, "driver")
            if n > 2:
                self.say("item %s: more than two clarification rounds: blocked for a human" % iid)
                try:
                    self.wip_commit("WIP(loopctl): %s blocked (NEEDS_CLARIFICATION)" % iid)
                except LoopctlError:
                    pass
                self.shelve_branch(iid)
                return False
            self.answer_clarification(iid, role, n)
            return False
        if st == "CONTRACT_INCONSISTENCY":
            self.park(iid, "contract_blocked", "%s reports the API contract is inconsistent" % role)
            return False
        self.park(iid, "ESCALATION_NEEDED", "%s returned %s" % (role, st))
        return False

    def answer_clarification(self, iid: str, role: str, n: int) -> None:
        """The driver can answer only from the plan: it records that in the brief's Clarifications section (custody) and runs the
        engineer again with the most conservative reading. A real gap comes back as a second and third round, then a human."""
        rel = self.brief_path(iid, role)
        full = os.path.join(self.repo, rel)
        answer = ("Round %d: the plan item is the whole specification and no one else can add to it. Choose the most conservative reading of the "
                  "acceptance criteria, do the smallest change that satisfies them, and write the assumption you made under 'Open Questions / Brief Gaps' "
                  "in your report." % n)
        try:
            with open(full, "a", encoding="utf-8") as fh:
                fh.write("\n- %s\n" % answer)
        except OSError:
            pass
        self.ev("clarified", item=iid, role=role, round=n)
        key = (iid, self.rec(iid)["attempt"])
        self.clarification[key] = answer

    # -- launching a child ----------------------------------------------------------------------------------------------------------------
    def launch_child(self, iid: str, role: str, stage: str, prompt: str) -> Optional["Outcome"]:
        """Run one child and settle everything around it: budget in, spend out, canary, classification. Returns an Outcome when the
        child did its work (the caller reads its report), or None when the failure or park has been recorded."""
        self.check_stop()                                     # HALT, a lost lock or a signal that arrived since the last look: no new child
        r = self.rec(iid)
        attempt = r["attempt"]
        if self.live_child():
            raise _Stop("double_launch", "a child of this driver is still alive: refusing to start another", EXIT_USAGE)
        led = self.ctx.ledger()
        v0 = led.verdict(self.cfg, self.run_id, iid)
        if not v0.ok:
            self.budget_stop(v0, iid)
        try:
            allowance = led.launch_budget(self.cfg, self.run_id, iid, 1)
        except Refused as exc:
            v1 = led.verdict(self.cfg, self.run_id, iid)
            if not v1.ok:
                self.budget_stop(v1, iid)
            raise _Stop("budget", str(exc), EXIT_REFUSED)
        if allowance < 0.0001:
            raise _Stop("budget", "the remaining budget is too small to launch a child", EXIT_FINDINGS)
        sid = str(uuid.uuid4())
        kind = launch.role_kind(role, self.roster_kinds)
        item = self.item_def(iid)
        extra_cmds = [(item.get("verify") or {}).get("tests")]
        settings, skipped = launch.build_settings(kind, self.rs, self.cfg, extra_cmds, self.env)
        problems = launch.validate_settings(settings)
        if problems:
            raise launch.LaunchRefused("the child profile is unsafe: %s" % "; ".join(problems[:3]))
        if skipped:
            self.ev("verify_command_skipped", commands=skipped[:5])
        model, effort = launch.model_for(str(self.cfg.get("model_profile") or "quality"), role, self.pass_no(iid, attempt) + 1, self.env)
        agent = launch.agent_definition(self.repo, self.anchor, role, model, effort)
        d = os.path.join(self.rs.root, "children")
        jsonio.ensure_dir(d)
        settings_path = os.path.join(d, sid + ".settings.json")
        prompt_path = os.path.join(d, sid + ".prompt.md")
        jsonio.atomic_write_json(settings_path, settings, 0o600)
        jsonio.atomic_write_text(prompt_path, prompt, 0o600)
        extra = {"brief": self.brief_path(iid, role) if role != "code-reviewer" and role != "security-reviewer" else None,
                 "report": self.report_path(iid, role) if kind == "engineer" else None, "attempt": attempt}
        spec = launch.ChildSpec(role, sid, prompt_path, self.repo, settings_path, agent, settings["permissions"]["allow"], allowance,
                                launch.child_env(self.env, list(self.cfg.get_list("verify_env_allow")) + list(item.get("requiresEnv") or []), self.fake), iid, stage, extra)
        self.child_n += 1
        self.ev("launch", item=iid, role=role, stage=stage, session=sid, budget_usd=allowance)
        self.say("item %s: %s (%s), budget $%.4f" % (iid, role, stage, allowance))
        activity = heartbeat.child_activity(self.rs, sid)
        try:
            self.rs.register_session(sid, role, registered_by="driver", runId=self.run_id, item=iid)
            res = launch.run_child(self.binary, spec, self.rs, self.timing, self.run_id, os.getpid(), tick=self.hb.tick, activity=activity,
                                   canary=lambda: heartbeat.canary_present(self.rs, sid), sleep=time.sleep)
        finally:
            self.rs.unregister_session(sid)
            launch.remove_child_record(self.rs, sid)
        also = [str((res.parsed or {}).get("session_id"))] if res.parsed and isinstance((res.parsed or {}).get("session_id"), str) else []
        if res.canary is False and also:
            res.canary = heartbeat.canary_present(self.rs, sid, also)
        self.ev("child_done", item=iid, role=role, stderr=common.clean_text(res.stderr_tail.strip().splitlines()[-1] if res.stderr_tail.strip() else "", 160),
                **res.to_dict())
        # the money first: whatever happened, the ledger hears about it (an unreadable cost parks, never guessed)
        led.record(self.run_id, iid, attempt, sid, result=res.parsed, role=role)
        verdict = led.verdict(self.cfg, self.run_id, iid)
        if res.reason in (heartbeat.REASON_HALT, heartbeat.REASON_LOCK, heartbeat.REASON_INTERRUPT):
            raise self._stop_for(res.reason)
        outcome = Outcome(res)
        bad = None  # type: Optional[Dict[str, str]]
        if res.canary is False or res.reason == "canary":
            bad = {"failure_class": failure.ENV, "reason": "canary_missing"}
        else:
            bad = classify_child(res)
        if bad is not None and bad.get("failure_class") == "budget":
            if not verdict.ok:
                self.budget_stop(verdict, iid)
            bad = {"failure_class": failure.ITEM, "reason": "partial"}
        if bad is not None:
            stage_arg = "partial" if bad["reason"] == "partial" else None
            self.fail(iid, bad["failure_class"], bad["reason"], (), stage_arg, "%s: %s" % (role, self.child_detail(res)), backoff=verdict.ok)
            if not verdict.ok and self.rec(iid)["status"] in state_mod.IN_FLIGHT:
                self.budget_stop(verdict, iid)
            if not verdict.ok:
                raise _Stop("budget", "; ".join(verdict.reasons)[:200], EXIT_FINDINGS)
            return None
        if not verdict.ok:
            self.budget_stop(verdict, iid)
        return outcome

    def live_child(self) -> bool:
        d = os.path.join(self.rs.root, "children")
        try:
            names = [n for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".settings.json")]
        except OSError:
            return False
        for n in names:
            try:
                rec = jsonio.read_json(os.path.join(d, n))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(rec, dict) and rec.get("driverPid") == os.getpid() and runstate.pid_alive(rec.get("pid")):
                return True
        return False

    def child_detail(self, res: launch.ChildResult) -> str:
        """What the state records about a child that failed: the exit, the signal and why the driver ended it. Nothing the child wrote:
        that text would sit in the state and could reach a later prompt. The stderr tail goes to the event log for the human."""
        bits = ["exit %s" % res.returncode]
        if res.signal_name:
            bits.append(res.signal_name)
        if res.reason:
            bits.append("ended by the driver: %s" % res.reason)
        sub = (res.parsed or {}).get("subtype")
        if isinstance(sub, str) and re.match(r"^[a-z_]{1,40}\Z", sub):
            bits.append(sub)
        return ", ".join(bits)


class Outcome(object):
    def __init__(self, res: launch.ChildResult) -> None:
        self.res = res
        self.parsed = res.parsed


def run(ctx: Any, loop: Any, run_record: Dict[str, Any], as_json: bool) -> int:
    """The seam `startrun.start` calls (spec: the driver contract). The lock is held; the caller releases it."""
    return Driver(ctx, loop, run_record, as_json).main()
