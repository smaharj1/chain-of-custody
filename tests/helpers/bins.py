"""Install the fake `claude` and fake `gh` as executables a test can put on PATH.

    claude = install_fake_claude(dir, {"runs": [{"behavior": "overloaded"}, {"behavior": "succeed"}]})
    env["KIT_CLAUDE_BIN"] = claude          # doctor, driver and probes read this (IMP-14)
    calls = read_jsonl(claude + ".log.jsonl")

    gh = install_fake_gh(dir, [{"argv_prefix": ["api", "repos/x/y"], "stdout_json": {...}}])
    env["PATH"] = os.path.dirname(gh) + os.pathsep + env["PATH"]

The installed file gets a `#!<sys.executable>` shebang so it runs under the same Python as
the tests. The scenario / replay file sits next to the executable, so it survives `env -i`.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any, Dict, List, Optional

HELPERS_DIR = os.path.dirname(os.path.abspath(__file__))

__all__ = ["install_fake_claude", "install_fake_gh", "read_jsonl", "set_scenario"]


def _install(src_name: str, dest_dir: str, dest_name: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(HELPERS_DIR, src_name), "r") as fh:
        body = fh.read()
    lines = body.split("\n")
    assert lines[0].startswith("#!")
    lines[0] = "#!" + sys.executable
    dest = os.path.join(dest_dir, dest_name)
    with open(dest, "w") as fh:
        fh.write("\n".join(lines))
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for suffix in (".state", ".log.jsonl", ".replay.json.used"):
        try:
            os.unlink(dest + suffix)
        except OSError:
            pass
    return dest


def set_scenario(claude_path: str, scenario: Dict[str, Any]) -> None:
    """Replace the scenario of an installed fake claude and reset its invocation counter."""
    with open(claude_path + ".scenario.json", "w") as fh:
        json.dump(scenario, fh)
    try:
        os.unlink(claude_path + ".state")
    except OSError:
        pass


def install_fake_claude(dest_dir: str, scenario: Optional[Dict[str, Any]] = None, name: str = "claude") -> str:
    path = _install("fake_claude.py", dest_dir, name)
    set_scenario(path, scenario or {})
    return path


def install_fake_gh(dest_dir: str, rules: Optional[List[Dict[str, Any]]] = None, name: str = "gh") -> str:
    path = _install("fake_gh.py", dest_dir, name)
    with open(path + ".replay.json", "w") as fh:
        json.dump(rules or [], fh)
    return path


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    try:
        with open(path, "r") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except FileNotFoundError:
        pass
    return out
