#!/usr/bin/env python3
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

A run:

    {
      "behavior": "succeed | crash | stall | malformed | overloaded | empty",
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
      # overloaded: prints a 529 overloaded_error result and exits 1 (synthetic shape)
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
    entry = {"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "run_index": idx,
             "behavior": behavior, "stdin": stdin_text,
             "env": {k: os.environ.get(k) for k in (scenario.get("log_env") or [])}}
    _log(scenario, entry)

    if run.get("delay"):
        time.sleep(float(run["delay"]))
    _write_files(run)
    if run.get("stderr"):
        sys.stderr.write(run["stderr"] + "\n")
        sys.stderr.flush()

    sid = run.get("session_id") or "fake-session-%d" % idx
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
        _emit(argv, payload, msg)
        sys.stderr.write(msg + "\n")
        return int(run.get("exit_code", 1))
    if behavior == "empty":
        return int(run.get("exit_code", 0))
    sys.stderr.write("fake claude: unknown behavior %r\n" % behavior)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
