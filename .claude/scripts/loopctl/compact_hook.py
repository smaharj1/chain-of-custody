#!/usr/bin/env python3
"""SessionStart `compact` hook: give a session back the thread of the run after `/compact` (report 10.6; CS-24, CS-23, L-17, L-18; CO-35).

    "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command",
        "command": "python3 \\"$CLAUDE_PROJECT_DIR/.claude/scripts/loopctl/compact_hook.py\\"", "timeout": 10}]}]

What survives a compaction is what loopctl can re-read: the item, its stage and attempt, the last failure, the counters, what is
waiting on a human, the brief and report paths, and the next action. The hook returns that as `additionalContext` (at most 10,000
characters; PostCompact cannot inject, P30) and nothing else. It also repeats the liveness canary line, so a conductor that checks the
nonce after a compaction still finds it. It reads state only: it writes nothing, and it never blocks (every path exits 0; a failure is
reported in the context it returns).

Two kinds of session get a context:

* a driver child (`children/<session>.json`, written by launch.py): its role, item, stage, brief, report path and the rules it runs
  under. The driver is the continuation mechanism (10.6): a child that loses its thread finishes its stage or writes a PARTIAL report;
  it does not try to run the loop.
* the registered conductor (`run.json`): the item in flight, its stage, and the loopctl command that moves it on.

Any other session gets nothing: the hook is wired for every compaction, and most sessions are not part of a run.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != _HERE]      # the package's own modules are loopctl.<name>, never top level
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from kitlib import gitutil, jsonio, runstate  # noqa: E402

MAX_CONTEXT = 10000
PROTOCOL = ".claude/context/engineer-protocol.md"
SPEC = ".claude/spec/loopctl.md"

_NEXT = {   # conductor: status -> what to do now (the commands are the ones in orchestrate.md's loopctl hand-off table)
    "pending": "run `python3 .claude/scripts/loopctl begin {item} --session {sid}` if the item is eligible",
    "designing": "finish the design and briefs, then `loopctl advance {item} design_review --session {sid}`",
    "design_review": "run the design-critic; then `loopctl advance {item} building` (or `stubbing`), or `loopctl fail {item} --class item --reason design_blocked`",
    "stubbing": "the acceptance stubs and tests are written; then `loopctl advance {item} building --session {sid}`",
    "building": "dispatch or resume the engineers by agent_id; when their reports are APPROVED, `loopctl advance {item} verifying --session {sid}`",
    "verifying": "run `loopctl gate <name> {item}` for each gate; on a pass `loopctl advance {item} reviewing`, on a failure `loopctl fail {item} ...`",
    "reviewing": "dispatch code-reviewer (and security-reviewer when sensitive); then `loopctl advance {item} pr_open|awaiting_human|merged`",
    "pr_open": "wait for CI; `loopctl reconcile` reads the PR state; a red check is `loopctl fail {item} --class item --reason ci_failed`",
    "awaiting_human": "stop and tell the human the item is waiting for `loopctl approve item {item} --by <name>`; do not approve it yourself",
    "needs_human": "stop: a human decides (`loopctl status --pending-human`); do not work around it",
    "blocked": "stop: the item is parked (`loopctl status --pending-human`); do not work around it",
    "merged": "pick the next eligible item: `loopctl status --json`",
    "abandoned": "nothing to do for this item",
}

_CHILD = {   # driver child: stage -> one line of what the stage is
    "stubbing": "write the interface stubs and the acceptance tests named in the brief; do not implement them",
    "building": "implement your brief; run its verify commands; write your report",
    "reviewing": "review the diff in the brief against its acceptance criteria and finish with the verdict line the brief names",
}


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        d = jsonio.read_json_opt(path)
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _t(value: Any, limit: int = 160) -> str:
    s = value if isinstance(value, str) else str(value)
    out = "".join(ch if ch >= " " and ch != "\x7f" and not ("\u200b" <= ch <= "\u200f" or "\u202a" <= ch <= "\u202e" or "\u2066" <= ch <= "\u2069")
                  else " " for ch in s.replace("\r", " ").replace("\n", " "))
    return out if len(out) <= limit else out[:limit - 3] + "..."


def _state(repo: str, plan: str) -> Optional[Dict[str, Any]]:
    from loopctl.state import StateCorrupt, StateStore
    try:
        st, _ = StateStore(repo, plan).read()
    except (StateCorrupt, Exception):  # noqa: BLE001 - the context says the state could not be read
        return None
    return st.d if st is not None else None


def _waiting(items: Mapping[str, Any]) -> List[str]:
    out = []   # type: List[str]
    for iid in sorted(items):
        r = items[iid]
        if r.get("status") in ("awaiting_human", "needs_human", "blocked", "pr_open"):
            out.append("%s %s%s" % (iid, r["status"], " (%s)" % _t(r.get("blockReason"), 40) if r.get("blockReason") else ""))
    return out


def build_context(hook: Any, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Pure core: {context, kind, item, status}. `kind` is child | conductor | none."""
    hi = hook if isinstance(hook, dict) else {}
    sid = hi.get("session_id")
    cwd = hi.get("cwd") if isinstance(hi.get("cwd"), str) and hi.get("cwd") else os.getcwd()
    out = {"context": "", "kind": "none", "item": None, "status": None}  # type: Dict[str, Any]
    rs = runstate.RunState.for_path(cwd)
    if rs is None or not runstate.valid_id(sid):
        return out                                  # not a session of a run: say nothing (the hook is wired for every session)
    child = _read(os.path.join(rs.root, "children", sid + ".json"))
    run = _read(rs.run_path) or {}
    conductor = run.get("conductor_session_id") == sid
    if child is None and not conductor:
        return out
    repo = _t(run.get("worktree") or (child or {}).get("worktree") or cwd, 300)
    plan = run.get("plan") if isinstance(run.get("plan"), str) else None
    st = _state(repo, plan) if plan else None
    L = ["kit loopctl: context restored after /compact (report 10.6). Re-read the state instead of trusting memory: "
         "`python3 .claude/scripts/loopctl status --json`."]
    nonce = rs.read_nonce(sid)
    if nonce:
        L.append("canary session=%s nonce=%s." % (sid, nonce))
    if rs.is_halted():
        L.append("HALT is set (%s): do nothing that writes." % _t((rs.read_halt() or {}).get("reason", "no reason recorded"), 80))
    iid = (child or {}).get("item") or run.get("currentItem")
    out["kind"] = "child" if child is not None else "conductor"
    out["item"] = iid
    rec = (st or {}).get("items", {}).get(iid) if st and iid else None
    L.append("run %s, plan %s, runner %s." % (_t(run.get("runId"), 40), _t(plan, 40), _t(run.get("runner"), 20)))
    if st is None:
        L.append("The loop state could not be read from git (refs/loopctl/state/%s): run `loopctl status` and stop if it fails." % _t(plan, 40))
    if iid and rec:
        out["status"] = rec.get("status")
        L.append("item %s: status %s, attempt %s, branch %s%s." % (_t(iid, 30), rec.get("status"), rec.get("attempt"), _t(rec.get("branch"), 80),
                 ", blockReason %s" % _t(rec.get("blockReason"), 60) if rec.get("blockReason") else ""))
        lf = rec.get("lastFailure")
        if isinstance(lf, dict):
            L.append("last failure: %s/%s at %s, action %s%s." % (_t(lf.get("class"), 20), _t(lf.get("reason"), 40), _t(lf.get("stage"), 20),
                     _t(lf.get("action"), 20), "; failing criteria %s" % ", ".join(_t(a, 24) for a in (lf.get("failedAcs") or [])[:10]) if lf.get("failedAcs") else ""))
        c = rec.get("counters") or {}
        L.append("counters: " + ", ".join("%s=%s" % (k, c[k]) for k in sorted(c) if c[k]) if any(c.values()) else "counters: all zero.")
    elif iid:
        L.append("item %s is not in the state." % _t(iid, 30))
    if child is not None:
        stage = _t(child.get("stage"), 20)
        L.append("You are the %s of a driver-launched session (stage %s, session %s). No human is present: ask no questions; a gap is "
                 "NEEDS_CLARIFICATION in your report." % (_t(child.get("role"), 30), stage, _t(sid, 40)))
        if child.get("brief"):
            L.append("Your brief: %s. Read it again now." % _t(child["brief"], 200))
        if child.get("report"):
            L.append("Write your report to %s and end your final message with the JSON trailer {\"status\", \"report_path\", \"iteration\"}." % _t(child["report"], 200))
        L.append("Next action: %s. Do not run loopctl, git writes, or launch claude. Protocol: %s." % (_CHILD.get(stage, "finish the stage in your brief"), PROTOCOL))
    elif rec is not None:
        nxt = _NEXT.get(str(rec.get("status")), "re-read the status")
        L.append("Next action: " + nxt.format(item=iid, sid=sid) + ".")
        L.append("Where things are: plan/items/%s.json (the item), docs/features/<slug>/ (briefs, reports), %s (commands)." % (_t(iid, 30), SPEC))
    waiting = _waiting((st or {}).get("items", {}))
    if waiting:
        L.append("Waiting on a human or on CI: " + "; ".join(waiting[:20]) + ".")
    ctx = " ".join(L)
    out["context"] = ctx if len(ctx) <= MAX_CONTEXT else ctx[:MAX_CONTEXT - 3] + "..."
    return out


def main(stdin: Any = None, stdout: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
        context = build_context(hook, env if env is not None else os.environ)["context"]
        if not context:
            return 0
    except Exception as exc:  # noqa: BLE001 - never block: say what failed
        context = "kit loopctl compact hook FAILED (%s: %s). Run `python3 .claude/scripts/loopctl status --json` before you continue." % (
            type(exc).__name__, _t(exc, 120))
    stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # even a broken stdout must not turn into a block
        sys.exit(0)
