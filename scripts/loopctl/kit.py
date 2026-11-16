"""The kit the driver runs with: its version, a fingerprint of its files, and where an agent definition comes from.

The kit is the plugin the driver was started from (`kitpaths.KIT_ROOT`), not anything in the project. A run records the kit's
identity when it starts (`run.json` `kit`: version and fingerprint) and every launch checks it again: a kit that changed under a
running loop (a plugin update, an edited checkout) would give the second half of a run different rules from the first, so the
launch is refused and the run is finished or abandoned and started again.

An agent definition is the project's override `.claude/agents/<role>.md` AT THE BASE SHA when the project has one (an item
cannot edit its own reviewer's tools), else the kit's `agents/<role>.md`. Either way `${CLAUDE_PLUGIN_ROOT}` is filled in before
the text goes into the `--agents` JSON, because Claude Code does not substitute it there.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from kitlib import gitutil, kitpaths

from .common import Refused

__all__ = ["KitChanged", "version", "fingerprint", "identity", "check", "agent_names", "resolve_agent", "describe_source",
           "OVERRIDE_DIR"]

OVERRIDE_DIR = ".claude/agents"
_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")


class KitChanged(Refused):
    """The kit's files are not the ones the run started with."""


def version(root: str = kitpaths.KIT_ROOT) -> str:
    """The plugin's version from `.claude-plugin/plugin.json`, or `unknown` when it cannot be read."""
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return "unknown"
    return v if isinstance(v, str) and v else "unknown"


def fingerprint(root: str = kitpaths.KIT_ROOT) -> str:
    """sha256 over every kit file: its root-relative path, a NUL, the sha256 of its bytes, a newline. Names and contents both count."""
    h = hashlib.sha256()
    for rel in kitpaths.kit_files(root):
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digest = "unreadable"
        h.update(rel.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
    return h.hexdigest()


def identity(root: str = kitpaths.KIT_ROOT) -> Dict[str, str]:
    """{version, fingerprint}: what run.json records and every launch compares against."""
    return {"version": version(root), "fingerprint": fingerprint(root)}


def _short(ident: Mapping[str, Any]) -> str:
    return "%s/%s" % (ident.get("version") or "unknown", str(ident.get("fingerprint") or "none")[:12])


def check(recorded: Any, root: str = kitpaths.KIT_ROOT) -> Dict[str, str]:
    """The kit now, compared with the run's record. Raises KitChanged on a mismatch, and when the run has no usable record (a run
    started before the kit was fingerprinted cannot prove its kit did not change). Returns the identity now."""
    now = identity(root)
    if not isinstance(recorded, dict) or not recorded.get("fingerprint"):
        raise KitChanged("the run did not record its kit (version and fingerprint); finish or abandon the run, then start a new one")
    if recorded.get("fingerprint") != now["fingerprint"] or recorded.get("version") != now["version"]:
        raise KitChanged("the kit changed mid-run: started with %s, now %s; finish or abandon the run, then start a new one"
                         % (_short(recorded), _short(now)))
    return now


def agent_names(root: str = kitpaths.KIT_ROOT) -> List[str]:
    """The kit's agents: `agents/<name>.md` under the kit root."""
    try:
        names = os.listdir(os.path.join(root, "agents"))
    except OSError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".md") and _ROLE.match(n[:-3]))


def resolve_agent(repo: str, base_sha: Optional[str], role: str, root: str = kitpaths.KIT_ROOT) -> Tuple[Optional[str], Dict[str, Any]]:
    """(text with ${CLAUDE_PLUGIN_ROOT} filled in, source). The project's override at the base sha wins; else the kit's file.
    The text is None when neither exists. A role that is not a plain name is never looked up."""
    if not _ROLE.match(role or ""):
        return None, {"kind": "none"}
    if base_sha:
        rel = "%s/%s.md" % (OVERRIDE_DIR, role)
        text = gitutil.show_file(base_sha, rel, repo, isolated=True)
        if text is not None:
            return kitpaths.expand(text, root), {"kind": "override", "path": rel, "sha": base_sha}
    path = os.path.join(root, "agents", role + ".md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, {"kind": "none"}
    return kitpaths.expand(text, root), {"kind": "kit", "path": "agents/%s.md" % role, "version": version(root)}


def describe_source(src: Mapping[str, Any]) -> str:
    """One line for a person: `override .claude/agents/x.md at abc123def456` or `kit 2.0.0 agents/x.md`."""
    if src.get("kind") == "override":
        return "override %s at %s" % (src.get("path"), str(src.get("sha") or "")[:12])
    if src.get("kind") == "kit":
        return "kit %s %s" % (src.get("version"), src.get("path"))
    return "none"
