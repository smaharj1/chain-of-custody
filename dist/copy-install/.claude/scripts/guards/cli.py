"""Command line of the guard suite: hook mode, selftest, explain, and the human state commands."""
from __future__ import annotations

import json
import os
import sys
import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

from kitlib import jsonio, runstate

from . import engine, g_taint, selftest, telemetry
from .lib import Ctx, Decision

BLOCKING = engine.BLOCKING_EVENTS


def _out(stream: TextIO, obj: Any) -> None:
    stream.write(json.dumps(obj) + "\n")


def main(argv: Sequence[str], stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
         env: Optional[Mapping[str, str]] = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = list(argv)
    if not args:
        stderr.write("usage: run.py <Event> | --selftest | explain | register-session | ...\n")
        return 2
    head = args[0]
    if head in ("--selftest", "selftest"):
        return _selftest(args[1:], stdout, stderr, env)
    if head == "explain":
        return _explain(args[1:], stdout, stderr, env)
    if head in ("register-session", "unregister-session", "set-halt", "clear-halt"):
        return _state_cmd(head, args[1:], stdout, stderr)
    event = head
    entry_id = None
    if "--id" in args:
        k = args.index("--id")
        entry_id = args[k + 1] if k + 1 < len(args) else None
    blocking = event in BLOCKING
    try:
        raw = stdin.read()
        hook = json.loads(raw) if raw.strip() else None
        if not isinstance(hook, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as exc:
        stderr.write("kit guards: malformed hook input (%s); failing closed (rule guards:bad-input)\n" % str(exc)[:80])
        return 2 if blocking else 1
    hook.setdefault("hook_event_name", event)
    if hook.get("hook_event_name") != event:
        hook["hook_event_name"] = event
    try:
        return _dispatch(event, hook, entry_id, stdout, stderr, env)
    except SystemExit:
        raise
    except Exception as exc:
        stderr.write("kit guards: internal error in %s (%s: %s); %s (rule guards:internal-error)\n" %
                     (event, type(exc).__name__, str(exc)[:100], "failing closed" if blocking else "diagnostic hook skipped"))
        return 2 if blocking else 1


def _dispatch(event: str, hook: Dict[str, Any], entry_id: Optional[str], stdout: TextIO, stderr: TextIO, env: Optional[Mapping[str, str]]) -> int:
    man = engine.load_manifest()
    tool = str(hook.get("tool_name") or "")
    entries = engine.entries_for(man, event, tool, entry_id)
    if event in BLOCKING:
        ctx = Ctx(hook, env=env)
        dec = engine.evaluate(hook, env=env, manifest=man, entry_id=entry_id, ctx=ctx)
        if dec is None:
            return 0
        try:
            telemetry.record_deny(ctx, dec.rule, dec.kind)
        except Exception:
            pass
        if dec.denied:
            stderr.write(dec.line() + "\n")
            return 2
        _out(stdout, {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "ask", "permissionDecisionReason": dec.line()}})
        return 0
    ctx = Ctx(hook, env=env)
    names = [g for e in entries for g in e.get("guards", [])]
    notes = []   # type: List[str]
    if "injection" in names:
        res = g_taint.check(ctx)
        if res["hits"]:
            try:
                telemetry._append(ctx, {"event": "injection_suspected", "tool": ctx.tool_name, "patterns": res["hits"], "source": res["source"]})
            except Exception:
                pass
        if res["context"]:
            notes.append(res["context"])
    if "telemetry" in names:
        telemetry.handle(ctx)
    if "selftest" in names:
        text = _session_selftest(ctx)
        if text:
            notes.append(text)
    if notes:
        _out(stdout, {"hookSpecificOutput": {"hookEventName": event, "additionalContext": " ".join(notes)[:1000]}})
    return 0


def manifest_sha() -> str:
    with open(engine.MANIFEST_PATH, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _session_selftest(ctx: Ctx) -> Optional[str]:
    ok, failures, n = selftest.run_selftest()
    st = ctx.state
    if st is not None:
        try:
            jsonio.ensure_dir(st.root)
            jsonio.atomic_write_json(os.path.join(st.root, "selftest.json"), {
                "ok": ok, "at": runstate.now_iso(), "version": selftest.SELFTEST_VERSION, "cases": n, "failures": failures[:10],
                "manifest_sha256": manifest_sha()}, 0o600)
        except OSError:
            pass
    if ok:
        return None
    return ("kit guards SELFTEST FAILED (%d of %d): %s. The guards are not trustworthy; the effective level is L0 until this passes (report §7.1)."
            % (len(failures), n, "; ".join(failures[:3])))[:700]


def _selftest(args: List[str], stdout: TextIO, stderr: TextIO, env: Optional[Mapping[str, str]]) -> int:
    ok, failures, n = selftest.run_selftest()
    if "--record" in args:
        st = runstate.RunState.for_path(os.getcwd())
        if st is not None:
            jsonio.ensure_dir(st.root)
            jsonio.atomic_write_json(os.path.join(st.root, "selftest.json"), {
                "ok": ok, "at": runstate.now_iso(), "version": selftest.SELFTEST_VERSION, "cases": n, "failures": failures[:10],
                "manifest_sha256": manifest_sha()}, 0o600)
    if ok:
        stdout.write("guards selftest: %d/%d cases behaved as expected\n" % (n, n))
        return 0
    for f in failures:
        stderr.write("guards selftest FAILED: %s\n" % f)
    return 1


class _StubState(object):
    """In-memory registry for `explain`: no files, no repo needed."""

    def __init__(self, records: Mapping[str, Mapping[str, Any]]) -> None:
        self.records = dict(records)
        self.root = os.path.join(os.sep, "nonexistent", "loopctl")
        self.run_path = os.path.join(self.root, "run.json")

    def get_session(self, sid: Any) -> Optional[Mapping[str, Any]]:
        return self.records.get(sid) if isinstance(sid, str) else None

    def is_halted(self) -> bool:
        return False

    def lock_held(self) -> bool:
        return False


def evaluate_for_caller(tool: str, tool_input: Mapping[str, Any], caller: str = "unknown", cwd: Optional[str] = None,
                        env: Optional[Mapping[str, str]] = None, config: Any = None) -> Tuple[Ctx, Optional[Decision]]:
    """Run the guards for a synthetic call, with no repository and an in-memory session registry. `caller` is one of
    engineer, critic, unknown, conductor (the registered conductor session). Used by `explain` and by tools/evals/run_local.py."""
    hook = {"session_id": "explain-session", "cwd": cwd or os.getcwd(), "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": dict(tool_input)}
    records = {}   # type: Dict[str, Any]
    if caller in ("engineer", "critic", "infra-engineer", "security-reviewer", "design-critic"):
        hook["agent_id"] = "explain-agent"
        hook["agent_type"] = {"engineer": "backend-engineer", "critic": "code-reviewer"}.get(caller, caller)
    elif caller == "conductor":
        records["explain-session"] = {"session_id": "explain-session", "role": "conductor"}
    ctx = Ctx(hook, env=env, state=_StubState(records), config=config)
    return ctx, engine.evaluate(hook, env=env, ctx=ctx)


def _explain(args: List[str], stdout: TextIO, stderr: TextIO, env: Optional[Mapping[str, str]]) -> int:
    caller = "unknown"
    tool = "Bash"
    rest = []   # type: List[str]
    i = 0
    while i < len(args):
        if args[i] == "--caller" and i + 1 < len(args):
            caller = args[i + 1]
            i += 2
        elif args[i] == "--tool" and i + 1 < len(args):
            tool = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    text = " ".join(rest)
    ctx, dec = evaluate_for_caller(tool, {"command": text} if tool == "Bash" else {"file_path": text, "content": ""}, caller, env=env)
    stdout.write("caller: %s (%s)\n" % (ctx.caller.kind, ctx.caller.reason))
    if dec is None:
        stdout.write("decision: allow\n")
        return 0
    stdout.write("decision: %s\nrule: %s\nmessage: %s\n" % (dec.kind, dec.rule, dec.message))
    return 0


def _state_cmd(cmd: str, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if os.environ.get("CLAUDECODE"):
        stderr.write("run.py %s is a human command and refuses to run inside a Claude Code session; use `!` in the prompt or a plain terminal\n" % cmd)
        return 2
    opts = {}   # type: Dict[str, str]
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            opts[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    st = runstate.RunState.for_path(os.getcwd())
    if st is None:
        stderr.write("not inside a git repository\n")
        return 2
    if cmd == "register-session":
        sid, role = opts.get("session"), opts.get("role")
        if not sid or not role or not runstate.valid_id(sid):
            stderr.write("register-session needs --session <id> --role conductor|<roster role>\n")
            return 2
        rec = st.register_session(sid, role, registered_by="human")
        stdout.write("registered session %s as %s in %s\n" % (sid, role, st.root))
        return 0
    if cmd == "unregister-session":
        ok = st.unregister_session(opts.get("session", ""))
        stdout.write("unregistered\n" if ok else "no such session\n")
        return 0
    if cmd == "set-halt":
        st.set_halt(opts.get("reason", "set by a human"), "human")
        stdout.write("HALT set\n")
        return 0
    if cmd == "clear-halt":
        stdout.write("HALT cleared\n" if st.clear_halt() else "no HALT was set\n")
        return 0
    return 2
