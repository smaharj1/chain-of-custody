"""Tests for tools/probes/* (IMP-7): the probe harness, its settings snippets and the analyzer."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import REPO_ROOT

PROBES_DIR = os.path.join(REPO_ROOT, "tools", "probes")
SETTINGS_DIR = os.path.join(PROBES_DIR, "settings")
FILES_DIR = os.path.join(PROBES_DIR, "files")
README = os.path.join(PROBES_DIR, "README.md")


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_tool(name: str):
    return load_module(os.path.join(PROBES_DIR, name + ".py"), "probes_" + name)


class LogBuilder(object):
    """Builds a synthetic observe.py / probe_hook.py log, one line at a time, with a clock."""

    def __init__(self) -> None:
        self.rows = []  # type: List[Dict[str, Any]]
        self.now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.n = 0

    def tick(self, seconds: float = 1.0) -> "LogBuilder":
        self.now += timedelta(seconds=seconds)
        return self

    def stamp(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def event(self, name: str, session: str = "sess-1", dt: float = 0.5, **fields: Any) -> "LogBuilder":
        self.tick(dt)
        ev = {"hook_event_name": name, "session_id": session}
        ev.update({k: v for k, v in fields.items() if v is not None})
        self.rows.append({"observed_at": self.stamp(), "event": ev})
        return self

    def marker(self, name: str, session: str = "sess-1", dt: float = 0.1, **fields: Any) -> "LogBuilder":
        self.tick(dt)
        m = {"name": name, "mode": "mark", "session_id": session}
        m.update({k: v for k, v in fields.items() if v is not None})
        self.rows.append({"observed_at": self.stamp(), "marker": m})
        return self

    def bash(self, command: str, session: str = "sess-1", agent_id: Optional[str] = None,
             agent_type: Optional[str] = None, outcome: str = "ok", output: str = "",
             run_seconds: float = 0.5, dt: float = 0.5) -> "LogBuilder":
        """PreToolUse for a Bash call, then (outcome ok|failed) its Post; outcome 'no-post' = blocked."""
        return self.tool("Bash", {"command": command}, session, agent_id, agent_type, outcome, output,
                         run_seconds, dt)

    def tool(self, tool: str, tool_input: Dict[str, Any], session: str = "sess-1",
             agent_id: Optional[str] = None, agent_type: Optional[str] = None, outcome: str = "ok",
             output: str = "", run_seconds: float = 0.5, dt: float = 0.5) -> "LogBuilder":
        self.n += 1
        tid = "toolu_%03d" % self.n
        self.event("PreToolUse", session, dt, tool_name=tool, tool_input=tool_input, tool_use_id=tid,
                   agent_id=agent_id, agent_type=agent_type)
        if outcome == "ok":
            self.event("PostToolUse", session, run_seconds, tool_name=tool, tool_input=tool_input,
                       tool_use_id=tid, tool_response={"stdout": output}, agent_id=agent_id, agent_type=agent_type)
        elif outcome == "failed":
            self.event("PostToolUseFailure", session, run_seconds, tool_name=tool, tool_input=tool_input,
                       tool_use_id=tid, error=output, agent_id=agent_id, agent_type=agent_type)
        return self

    def write(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            for row in self.rows:
                fh.write(json.dumps(row) + "\n")
        return path
