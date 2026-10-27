#!/usr/bin/env python3
# kit-fake-claude: v1  (the driver looks for this marker before it lets a harness bypass the CLAUDECODE refusal)
"""A scriptable fake `claude` executable (IMP-14, IMP-25). Never calls the network.

Tests point `KIT_CLAUDE_BIN` at an installed copy (see `bins.install_fake_claude`). The
real `claude` is never run by any test or agent.

Scenario: JSON, from the env var FAKE_CLAUDE_SCENARIO (a JSON string, or a path to a JSON
file), else from `<this file>.scenario.json` next to the installed copy.

    {
      "version": "2.1.302 (Claude Code)",       # printed for --version / -v
      "log": "/path/calls.jsonl",              # optional; default <this file>.log.jsonl
      "log_env": ["HOME", "KIT_RUN_ID"],       # env vars to record in each log line
      "read_stdin": false,                     # true: read and log stdin (only if the test closes it)
      "runs": [ {run}, {run}, ... ],           # consumed one per invocation, in order
      "default": {run}                         # used when `runs` is exhausted (default: succeed)
    }

Phase 4c additions (the driver tests; none changes an existing scenario):

    "by_role": {"backend-engineer": [ {run}, ... ]}   # runs chosen by the `--agent <role>` argument, one list per role, each
                                                     # consumed in order (the last one repeats). Takes precedence over `runs`.
    "nonce_dir": "/path/loopctl/nonces"              # emulate the SessionStart canary: write <nonce_dir>/<session id> at start
    "events_dir": "/path/loopctl/events"             # emulate the telemetry hook: append a tool event to <events_dir>/s-<sid>.jsonl

A run may also have: "canary": false (hooks did not load: no nonce), "tool_events": N and "event_interval": seconds (N tool events
written while the child "works", which keeps a long child from looking stalled), "pid_log": "/path" (append the pid),
"check_cwd_file": "path" (exit 3 unless that path exists, relative to the working directory), "spawn_sleeper": "pidfile" (start a
grandchild in the same process group, a leaked dev server, and write its pid), "hook": {"argv": [...], "stdin_json": {...}, "out": "path"}
(run a hook command at start with that JSON on stdin, "$SID" replaced by the session id; its stdout is written to "out").

A run:

    {
      "behavior": "succeed | crash | stall | malformed | overloaded | empty | budget_exceeded | max_turns | rate_limited | error",
      "delay": 0.0,                            # seconds to sleep first
      "write_files": [ {"path": "...", "content": "...", "mode": 420, "append": false} ],
      "report": {"path": "...", "text": "..."},# shorthand for one write_files entry
      "stderr": "text", "exit_code": 0,

      # succeed
      "result": "ok", "total_cost_usd": 0.01,  # null omits the cost field entirely
      "cost_field": "total_cost_usd",          # rename it, to test a driver that cannot find it
      "session_id": "...", "num_turns": 3,
      # crash
      "signal": "SIGKILL",                     # die by signal instead of exit_code
      "partial_stdout": "...",                 # printed before crashing
      # stall
      "seconds": 3600, "ignore_sigterm": false, "pidfile": "/path", "after_output": "..."
      # overloaded: prints a 529 overloaded_error result and exits 1 (synthetic shape); "total_cost_usd" adds the cost field
    }

Output follows `--output-format`: `json` (one object), `stream-json` (one object per line),
anything else prints the result text. The synthetic JSON shape mirrors what the report
assumes (`total_cost_usd`, `session_id`, `is_error`, `result`); it is not a claim about
the real CLI's exact output.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.abspath(__file__)


def _load_scenario():
    raw = os.environ.get("FAKE_CLAUDE_SCENARIO")
    if raw:
        raw = raw.strip()
        if raw.startswith("{"):
            return json.loads(raw)
        with open(raw, "r") as fh:
            return json.load(fh)
    side = HERE + ".scenario.json"
    if os.path.exists(side):
        with open(side, "r") as fh:
            return json.load(fh)
    return {}


def _next_index(scenario):
    """Per-scenario invocation counter, kept in a file so it survives across processes."""
    state = (scenario.get("state") or (HERE + ".state"))
    try:
        fd = os.open(state, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return 0
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        raw = os.read(fd, 32).decode() or "0"
        n = int(raw) if raw.strip().isdigit() else 0
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n + 1).encode())
        return n
    finally:
        os.close(fd)


def _arg_value(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _log(scenario, entry):
    path = scenario.get("log") or (HERE + ".log.jsonl")
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def _write_files(run):
    files = list(run.get("write_files") or [])
    if run.get("report"):
        files.append({"path": run["report"]["path"], "content": run["report"].get("text", "")})
    for f in files:
        p = f["path"]
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "a" if f.get("append") else "w") as fh:
            fh.write(f.get("content", ""))
        if "mode" in f:
            os.chmod(p, f["mode"])


def _emulate_session_start(scenario, run, sid):
    """The SessionStart canary hook: a nonce file named after the session (kitlib.runstate.write_nonce)."""
    d = scenario.get("nonce_dir")
    if not d or run.get("canary") is False or not sid:
        return
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid), "w") as fh:
        fh.write(os.urandom(16).hex() + "\n")


def _run_hook(run, sid):
    """Run a hook command as the CLI would on a SessionStart (for example the compact hook after a forced /compact). The JSON on stdin is
    the run's "hook.stdin_json" with the string "$SID" replaced by the session id; stdout goes to "hook.out"."""
    h = run.get("hook")
    if not h:
        return
    data = json.loads(json.dumps(h.get("stdin_json") or {}).replace("$SID", sid or ""))
    proc = subprocess.run(h["argv"], input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=os.getcwd(), env=os.environ.copy())
    if h.get("out"):
        with open(h["out"], "w") as fh:
            fh.write(proc.stdout.decode("utf-8", "replace"))


def _emulate_tool_events(scenario, run, sid):
    """The PostToolUse telemetry hook: one event line per tool call, in events/s-<session>.jsonl."""
    d = scenario.get("events_dir")
    n = int(run.get("tool_events") or 0)
    if not d or not n or not sid:
        return
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        with open(os.path.join(d, "s-%s.jsonl" % sid), "a") as fh:
            fh.write(json.dumps({"event": "tool", "tool": "Bash", "ts": time.time(), "i": i}) + "\n")
        if i + 1 < n:
            time.sleep(float(run.get("event_interval", 0.05)))


def _emit(argv, payload, text):
    fmt = _arg_value(argv, "--output-format") or "text"
    if fmt == "json":
        sys.stdout.write(json.dumps(payload) + "\n")
    elif fmt == "stream-json":
        sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": payload.get("session_id")}) + "\n")
        sys.stdout.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n")
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main(argv):
    scenario = _load_scenario()
    if "--version" in argv or "-v" in argv:
        print(scenario.get("version", "2.1.302 (Claude Code)"))
        return 0
    role = _arg_value(argv, "--agent")
    by_role = scenario.get("by_role") or {}
    if role in by_role and by_role[role]:
        ridx = _next_index(dict(scenario, state=(scenario.get("state") or (HERE + ".state")) + ".role." + role.replace("/", "_")))
        lst = by_role[role]
        run = lst[ridx] if ridx < len(lst) else lst[-1]
        idx = ridx
    else:
        idx = _next_index(scenario)
        runs = scenario.get("runs") or []
        run = runs[idx] if idx < len(runs) else (scenario.get("default") or {"behavior": "succeed"})
    behavior = run.get("behavior", "succeed")
    stdin_text = ""
    if scenario.get("read_stdin"):
        try:
            stdin_text = sys.stdin.read(20000)
        except OSError:
            stdin_text = ""
    sid_arg = _arg_value(argv, "--session-id")
    entry = {"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "run_index": idx, "role": role,
             "time": time.time(),
             "behavior": behavior, "stdin": stdin_text,
             "env": {k: os.environ.get(k) for k in (scenario.get("log_env") or [])}}
    _log(scenario, entry)

    if run.get("pid_log"):
        with open(run["pid_log"], "a") as fh:
            fh.write("%d\n" % os.getpid())
    if run.get("check_cwd_file") and not os.path.exists(run["check_cwd_file"]):
        sys.stderr.write("fake claude: %s not found in %s\n" % (run["check_cwd_file"], os.getcwd()))
        return 3
    if run.get("spawn_sleeper"):                 # a grandchild in the same process group (a leaked dev server): its pid goes in the file
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(run["spawn_sleeper"], "w") as fh:
            fh.write(str(child.pid))
    _emulate_session_start(scenario, run, sid_arg)
    _run_hook(run, sid_arg)
    _emulate_tool_events(scenario, run, sid_arg)
    if run.get("delay"):
        time.sleep(float(run["delay"]))
    _write_files(run)
    if run.get("stderr"):
        sys.stderr.write(run["stderr"] + "\n")
        sys.stderr.flush()

    sid = run.get("session_id") or sid_arg or "fake-session-%d" % idx
    if behavior == "succeed":
        text = run.get("result", "ok")
        payload = {"type": "result", "subtype": "success", "is_error": False, "result": text,
                   "session_id": sid, "num_turns": run.get("num_turns", 1),
                   "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}
        cost = run.get("total_cost_usd", 0.01)
        if cost is not None:
            payload[run.get("cost_field", "total_cost_usd")] = cost
        _emit(argv, payload, text)
        return int(run.get("exit_code", 0))
    if behavior == "crash":
        if run.get("partial_stdout"):
            sys.stdout.write(run["partial_stdout"])
            sys.stdout.flush()
        if run.get("signal"):
            os.kill(os.getpid(), getattr(signal, run["signal"]))
            time.sleep(5)
        return int(run.get("exit_code", 1))
    if behavior == "stall":
        if run.get("pidfile"):
            with open(run["pidfile"], "w") as fh:
                fh.write(str(os.getpid()))
        if run.get("after_output"):
            sys.stdout.write(run["after_output"])
            sys.stdout.flush()
        if run.get("ignore_sigterm"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        end = time.time() + float(run.get("seconds", 3600))
        while time.time() < end:
            time.sleep(0.1)
        return int(run.get("exit_code", 0))
    if behavior == "malformed":
        sys.stdout.write(run.get("text", '{"type":"result","subtype":"success","result":"trunc'))
        sys.stdout.flush()
        return int(run.get("exit_code", 0))
    if behavior == "overloaded":
        msg = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
        payload = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": msg,
                   "session_id": sid, "num_turns": 0}
        if run.get("total_cost_usd") is not None:                # optional (Phase 4c): the real CLI reports a cost even on an API error
            payload[run.get("cost_field", "total_cost_usd")] = run["total_cost_usd"]
        _emit(argv, payload, msg)
        sys.stderr.write(msg + "\n")
        return int(run.get("exit_code", 1))
    if behavior == "empty":
        return int(run.get("exit_code", 0))
    if behavior == "error":                      # any error result: "subtype", "result", "total_cost_usd", "exit_code"
        msg = run.get("result", "something broke")
        payload = {"type": "result", "subtype": run.get("subtype", "error_during_execution"), "is_error": True, "result": msg,
                   "session_id": sid, "num_turns": run.get("num_turns", 0)}
        cost = run.get("total_cost_usd")
        if cost is not None:
            payload[run.get("cost_field", "total_cost_usd")] = cost
        _emit(argv, payload, msg)
        return int(run.get("exit_code", 1))
    if behavior in ("budget_exceeded", "max_turns", "rate_limited"):
        sub = {"budget_exceeded": "error_max_budget_usd", "max_turns": "error_max_turns", "rate_limited": "error_during_execution"}[behavior]
        msg = run.get("result") or ("API Error: 429 rate limit reached" if behavior == "rate_limited" else behavior)
        payload = {"type": "result", "subtype": sub, "is_error": True, "result": msg, "session_id": sid, "num_turns": run.get("num_turns", 1)}
        cost = run.get("total_cost_usd", 0.01)
        if cost is not None:
            payload[run.get("cost_field", "total_cost_usd")] = cost
        _emit(argv, payload, msg)
        return int(run.get("exit_code", 1))
    sys.stderr.write("fake claude: unknown behavior %r\n" % behavior)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
