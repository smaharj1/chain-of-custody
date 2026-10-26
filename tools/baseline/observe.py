#!/usr/bin/env python3
"""Observation-only hook for baseline runs (IMP-2, report §15 item 1, §16.1 Phase 0a).

Claude Code runs this for hook events. It reads the event JSON from stdin, redacts secrets
(kitlib.redact), and appends one JSON line to a log file. It changes nothing: it writes
nothing to stdout or stderr, never blocks a tool call, and ALWAYS exits 0, whatever goes
wrong (a broken logger must not change the run it is observing).

    python3 tools/baseline/observe.py --log /path/to/observe.jsonl

The log path comes from `--log`, else the BASELINE_OBSERVE_LOG environment variable. With
neither, the hook does nothing. Each line is:

    {"observed_at": "<UTC ISO-8601>", "event": {<hook stdin, redacted>}}

Strings longer than MAX_STRING characters are cut (marked "...[truncated N chars]") so one
huge tool result cannot make the log unreadable; the home directory is shown as "~".
The probe harness (IMP-7) reuses this file.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), ".claude", "scripts"))

MAX_STDIN = 8 * 1024 * 1024
MAX_STRING = 50000


def _clip(obj: Any, home: Optional[str]) -> Any:
    if isinstance(obj, str):
        if home and len(home) > 1:
            obj = obj.replace(home, "~")
        if len(obj) > MAX_STRING:
            return obj[:MAX_STRING] + "...[truncated %d chars]" % (len(obj) - MAX_STRING)
        return obj
    if isinstance(obj, list):
        return [_clip(v, home) for v in obj]
    if isinstance(obj, dict):
        return {k: _clip(v, home) for k, v in obj.items()}
    return obj


def build_record(raw: str, home: Optional[str] = None) -> dict:
    from kitlib import redact
    try:
        event = json.loads(raw)
    except ValueError:
        event = {"unparsed": raw[:MAX_STRING]}
    if not isinstance(event, dict):
        event = {"value": event}
    # Redact before clipping so a secret that straddles the cut point cannot survive.
    event = _clip(redact.redact_obj(event), home)
    return {"observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "event": event}


def log_path(argv: list) -> Optional[str]:
    if "--log" in argv:
        i = argv.index("--log")
        if i + 1 < len(argv):
            return argv[i + 1]
        return None
    return os.environ.get("BASELINE_OBSERVE_LOG") or None


def append_line(path: str, line: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def main(argv: list) -> int:
    try:
        path = log_path(argv)
        if not path:
            return 0
        raw = sys.stdin.read(MAX_STDIN)
        record = build_record(raw, os.path.expanduser("~"))
        append_line(path, json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    except BaseException:  # noqa: B902  (observation must never break the run)
        pass
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.exit(code)
