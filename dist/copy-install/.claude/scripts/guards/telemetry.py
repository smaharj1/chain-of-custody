"""Telemetry (report §7.3, P-6, A7, D-31, L-31): diagnostic events, one file per agent_id, redacted, no hash chain.

Replaces the hand-written metrics.jsonl and loop.jsonl. Events are DIAGNOSTIC ONLY: promotion rests on server-side
facts (§6.3, CS-21), so there is no tamper-evidence. Files live in `<git-common-dir>/loopctl/events/` and loopctl
merges them. `kitlib.runstate.append_event` redacts secrets at write time (§5.4). No command text, tool input or
file content is recorded: only names, ids, decisions, rule ids and hashes.

    SubagentStart     agent_id, agent_type, model if exposed (P33), parent session classified per §5.7
    SubagentStop      agent_id, last-message hash, report path from the JSON trailer
    PostToolUse       tool, ok, duration (async); also re-stamps the lock heartbeat for the owning session (§19.7)
    PermissionDenied  the stall signal (§6.3)
    guard             one event per deny, with the rule id (the corpus rows and the telemetry use the same ids, IMP-22)
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

from kitlib import caller as caller_mod
from kitlib import runstate

from .lib import Ctx


def stream_name(hook) -> Optional[str]:
    aid = hook.get("agent_id")
    if isinstance(aid, str) and runstate.valid_id(aid):
        return "a-" + aid
    sid = hook.get("session_id")
    if runstate.valid_id(sid):
        return "s-" + sid
    return None


def _append(ctx: Ctx, event: Dict[str, Any], name: Optional[str] = None) -> bool:
    st = ctx.state
    nm = name or stream_name(ctx.hook)
    if st is None or nm is None:
        return False
    ev = dict(event)
    ev.setdefault("session_id", ctx.session_id if isinstance(ctx.session_id, str) else None)
    if ctx.agent_id:
        ev.setdefault("agent_id", ctx.agent_id)
    st.append_event(nm, ev)
    return True


def record_deny(ctx: Ctx, rule: str, kind: str = "deny") -> None:
    try:
        _append(ctx, {"event": "guard", "decision": kind, "rule": rule, "tool": ctx.tool_name, "kind": ctx.caller.kind})
    except Exception:
        pass


def _trailer_report_path(text: str) -> Optional[str]:
    for line in reversed(text.strip().splitlines()[-3:]):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("report_path"), str):
                return d["report_path"][:300]
    return None


def handle(ctx: Ctx) -> None:
    ev = ctx.event
    h = ctx.hook
    if ev == "SubagentStart":
        parent = caller_mod.classify({"session_id": ctx.session_id}, {ctx.session_id: ctx.state.get_session(ctx.session_id)}
                                     if ctx.state is not None and ctx.state.get_session(ctx.session_id) else {}, ctx.roster)
        _append(ctx, {"event": "subagent_start", "agent_type": h.get("agent_type"), "model": h.get("model"),
                      "parent_session": ctx.session_id, "parent_kind": parent.kind})
    elif ev == "SubagentStop":
        msg = h.get("last_assistant_message")
        msg = msg if isinstance(msg, str) else ""
        _append(ctx, {"event": "subagent_stop", "agent_type": h.get("agent_type"),
                      "last_message_sha256": hashlib.sha256(msg.encode("utf-8", "replace")).hexdigest() if msg else None,
                      "report_path": _trailer_report_path(msg)})
    elif ev == "PostToolUse":
        resp = h.get("tool_response")
        err = bool(isinstance(resp, dict) and (resp.get("is_error") or resp.get("error")))
        _append(ctx, {"event": "tool", "tool": ctx.tool_name, "tool_use_id": h.get("tool_use_id"), "ok": not err,
                      "duration_ms": h.get("duration_ms")})
        sid = ctx.session_id
        if ctx.state is not None and isinstance(sid, str) and not ctx.agent_id:
            try:
                ctx.state.heartbeat(sid)
            except Exception:
                pass
    elif ev == "PermissionDenied":
        _append(ctx, {"event": "permission_denied", "tool": ctx.tool_name, "reason": str(h.get("reason") or h.get("message") or "")[:120]})
