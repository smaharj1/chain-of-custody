#!/usr/bin/env python3
"""V13: records what CLAUDE_PLUGIN_ROOT was when a plugin hook command ran."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def main(argv):
    log = argv[argv.index("--log") + 1] if "--log" in argv else None
    if not log:
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    marker = {"name": "v13-plugin-root-expanded", "mode": "root-check", "script": os.path.abspath(__file__),
              "env_CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),
              "agent_id": event.get("agent_id"), "agent_type": event.get("agent_type"),
              "session_id": event.get("session_id"), "hook_event_name": event.get("hook_event_name")}
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                             "marker": marker}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
