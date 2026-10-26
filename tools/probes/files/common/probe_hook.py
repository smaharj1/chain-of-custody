#!/usr/bin/env python3
"""Probe hook helper (IMP-7). Installed in a scratch repo as .claude/probe/probe_hook.py.

    python3 probe_hook.py MODE --tag TAG --log LOG [--seconds N] [--nonce TEXT]

It reads the hook event JSON from stdin, appends one marker line to LOG, then behaves as MODE:

    mark         exit 0, print nothing (a witness that this exact handler ran)
    crash        exit 1 after raising an exception (a crashing script)
    sleep        sleep for --seconds (default 60), meant to outlive the hook `timeout`
    deny         print a reason on stderr and exit 2 (a blocking hook)
    inject       print a SessionStart `additionalContext` JSON object that carries --nonce
    statusline   print one short status line (for a statusLine command); the stdin JSON is kept

A marker line is {"observed_at": ..., "marker": {"name": TAG, "mode": MODE, ...}} and goes in the
same file as tools/baseline/observe.py writes, so one log tells the whole story. Stdlib only,
Python 3.9 compatible. The marker is written FIRST: a crash or a timeout later does not lose it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

KEYS = ("hook_event_name", "session_id", "agent_id", "agent_type", "tool_name", "tool_use_id",
        "source", "cwd")


def arg(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def write_marker(path, record):
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def main(argv):
    mode = argv[0] if argv and not argv[0].startswith("--") else "mark"
    tag = arg(argv, "--tag", mode)
    log = arg(argv, "--log", os.environ.get("BASELINE_OBSERVE_LOG"))
    raw = ""
    event = {}
    try:
        raw = sys.stdin.read(8 * 1024 * 1024)
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except (OSError, ValueError):
        event = {}
    marker = {"name": tag, "mode": mode, "pid": os.getpid()}
    for k in KEYS:
        if k in event:
            marker[k] = event[k]
    output = None
    if mode == "inject":
        nonce = arg(argv, "--nonce", "PROBE-NONCE")
        output = {"hookSpecificOutput": {"hookEventName": event.get("hook_event_name", "SessionStart"),
                                         "additionalContext": "PROBE-V20-NONCE %s: this line was injected by a "
                                                              "SessionStart hook." % nonce}}
        marker["output"] = output
    if mode == "statusline":
        marker["stdin_keys"] = sorted(event.keys())
        marker["stdin"] = event
    write_marker(log, {"observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                       "marker": marker})
    if mode == "crash":
        raise RuntimeError("probe_hook crash mode: this is the crashing script")
    if mode == "sleep":
        time.sleep(float(arg(argv, "--seconds", "60")))
        return 0
    if mode == "deny":
        sys.stderr.write("probe-deny: blocked by the probe hook (%s)\n" % tag)
        return 2
    if mode == "inject":
        sys.stdout.write(json.dumps(output))
        return 0
    if mode == "statusline":
        sys.stdout.write("probe-status\n")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
