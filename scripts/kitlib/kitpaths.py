"""Where the kit is, and how to name its files to a person or an agent.

The kit is a Claude Code plugin. Its root is this repository's root while you develop the kit, or the plugin's copy in the
plugin cache (`~/.claude/plugins/cache/...`) in a user's project. KIT_ROOT is found from this file, never from the
environment or the working directory, so a project can not point the kit somewhere else.

The project is the user's repository. Its `.claude/` holds only the project's own files: `CLAUDE.md`, `loop.config.md`, the
primers `context/<domain>.md`, `settings.json`, runtime files, and optional overrides `.claude/agents/<role>.md`.

Prose in the kit names kit files as `${CLAUDE_PLUGIN_ROOT}/...`. Claude Code fills that in for skills and agents; a script
that hands kit prose to a session it starts (the driver's `--agents` JSON) fills it in with `expand()`. A command printed for
a person or an agent names the script by its absolute path (`script_cmd`), because `${CLAUDE_PLUGIN_ROOT}` is not set in a shell.
"""
from __future__ import annotations

import os
import shlex
from typing import Iterable

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(KIT_ROOT, "scripts")
PLUGIN_JSON = os.path.join(KIT_ROOT, ".claude-plugin", "plugin.json")
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"

# kit areas: what a fingerprint covers and what no agent may write
KIT_AREAS = ("agents", "skills", "scripts", "spec", "templates", "context", "hooks", "seed", ".claude-plugin")


def kit(*parts: str) -> str:
    """Absolute path of a kit file: kit("templates", "pr-body.md")."""
    return os.path.join(KIT_ROOT, *parts)


def script(name: str) -> str:
    """Absolute path of a kit script: script("doctor.py"), script("loopctl")."""
    return os.path.join(SCRIPTS, name)


def script_cmd(name: str, *args: str) -> str:
    """The shell command that runs a kit script, for a person or an agent to copy: `python3 /abs/scripts/doctor.py --level`."""
    return " ".join(["python3", shlex.quote(script(name))] + [str(a) for a in args])


def expand(text: str, root: str = KIT_ROOT) -> str:
    """Kit prose with `${CLAUDE_PLUGIN_ROOT}` filled in, the way Claude Code fills it in for a plugin's skills and agents."""
    return text.replace(PLUGIN_ROOT_VAR, root)


def is_kit_dev_checkout(root: str) -> bool:
    """True when `root` is a checkout of the kit itself (the plugin manifest sits at its root)."""
    return os.path.isfile(os.path.join(root, ".claude-plugin", "plugin.json")) and os.path.isdir(os.path.join(root, "scripts", "kitlib"))


def kit_files(root: str = KIT_ROOT, areas: Iterable[str] = KIT_AREAS):
    """Every file under the kit areas, as sorted root-relative paths, caches skipped."""
    out = []
    for area in areas:
        base = os.path.join(root, area)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fn in filenames:
                if fn == ".DS_Store" or fn.endswith(".pyc"):
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
    return sorted(out)
