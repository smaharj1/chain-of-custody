"""The guard pipeline: manifest entry -> guards -> one decision (report §7.1). Used by run.py, the selftest and the tests.

`evaluate(hook)` runs the PreToolUse (or ConfigChange) guards of the matching manifest entry and returns the first
deny, else the first ask, else None. Guards run in manifest order; `halt` is first. A guard that raises makes the
whole evaluation raise: the entry point turns that into exit 2 (fail closed), it is never swallowed here.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import g_config, g_critic, g_exec, g_file, g_git, g_halt, g_infra
from .lib import UNSET, BASH_TOOLS, EDIT_TOOLS, Ctx, Decision

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(HERE, "hooks.manifest.json")
BLOCKING_EVENTS = ("PreToolUse", "ConfigChange", "PermissionRequest", "UserPromptSubmit")


def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MANIFEST_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("hooks.manifest.json: no entries")
    return data


def matches(matcher: Optional[str], tool: str) -> bool:
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def entries_for(manifest: Mapping[str, Any], event: str, tool: str, entry_id: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for e in manifest["entries"]:
        if entry_id is not None:
            if e.get("id") == entry_id:
                out.append(e)
            continue
        if e.get("event") == event and matches(e.get("matcher"), tool):
            out.append(e)
    return out


def _file_guard(ctx: Ctx) -> Optional[Decision]:
    if ctx.tool_name in EDIT_TOOLS:
        return g_file.check_edit(ctx)
    if ctx.tool_name in BASH_TOOLS:
        return g_file.check_bash(ctx)
    return None


def _bash_only(fn: Callable[[Ctx], Optional[Decision]]) -> Callable[[Ctx], Optional[Decision]]:
    def wrapped(ctx: Ctx) -> Optional[Decision]:
        return fn(ctx) if ctx.tool_name in BASH_TOOLS else None
    return wrapped


def _infra_guard(ctx: Ctx) -> Optional[Decision]:
    if ctx.tool_name in BASH_TOOLS:
        return g_infra.check_bash(ctx)
    if ctx.tool_name.startswith("mcp__"):
        return g_infra.check_mcp(ctx)
    return None


def _main_edit_guard(ctx: Ctx) -> Optional[Decision]:
    return g_file.check_main_edit(ctx) if ctx.tool_name in EDIT_TOOLS else None


GUARDS = {
    "halt": g_halt.check,
    "file": _file_guard,
    "main-edit": _main_edit_guard,
    "git": _bash_only(g_git.check),
    "infra": _infra_guard,
    "exec": _bash_only(g_exec.check_bash),
    "critic": g_critic.check,
    "config-change": g_config.check,
}


def evaluate(hook: Mapping[str, Any], env: Optional[Mapping[str, str]] = None, state: Any = UNSET, config: Any = UNSET,
             manifest: Optional[Mapping[str, Any]] = None, entry_id: Optional[str] = None, ctx: Optional[Ctx] = None) -> Optional[Decision]:
    man = manifest or load_manifest()
    c = ctx or Ctx(hook, env=env, state=state, config=config)
    ask = None   # type: Optional[Decision]
    entries = entries_for(man, c.event or "PreToolUse", c.tool_name, entry_id)
    for e in entries:
        for name in e.get("guards", []):
            fn = GUARDS.get(name)
            if fn is None:
                continue
            d = fn(c)
            if d is None:
                continue
            if d.denied:
                return d
            if ask is None:
                ask = d
    return ask
