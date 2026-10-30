"""install: how the kit is installed in a project, for /init, /migrate, doctor and the session-start hook.

The kit is a Claude Code plugin (`chain-of-custody@chain-of-custody`). A project's `.claude/` holds only the project's own files
(CLAUDE.md, loop.config.md, the primers, settings.json without kit hooks, runtime files, optional `.claude/agents/<role>.md`).
Before 2.0 the kit was copied into `.claude/`; this module recognises what such a copy left behind:

  * `copy_install_markers(root)`: the signs of an old copy install (`.claude/scripts/`, `.claude/KIT_MANIFEST`, `.claude/KIT_VERSION`,
    `.claude/hooks/guard-*.sh`, or settings hooks that run the copied kit). Any of them means: run /migrate.
  * `is_kit_hook(command)` and `strip_kit_hooks(settings)`: the kit's hook entries in a settings file. The plugin's hooks.json supplies
    the hooks now, so a copy in settings.json would run the guards twice (or run scripts that are gone). The person's own hooks stay.
  * `plugin_disabled(settings)` and `plugin_installed(home)`: whether a settings file turns the plugin off, and whether Claude Code
    lists it as installed (a best-effort read of `~/.claude/plugins/installed_plugins.json`).

Standard library only, Python 3.9.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["PLUGIN_ID", "MARKETPLACE", "MARKETPLACE_SOURCE", "INSTALL_COMMANDS", "SEED_FILES", "copy_install_markers", "is_kit_hook", "strip_kit_hooks", "plugin_disabled",
           "plugin_installed", "load_settings"]

PLUGIN_ID = "chain-of-custody@chain-of-custody"
MARKETPLACE = "chain-of-custody"
MARKETPLACE_SOURCE = {"source": "github", "repo": "smaharj1/chain-of-custody"}
INSTALL_COMMANDS = ("/plugin marketplace add %s" % MARKETPLACE_SOURCE["repo"], "/plugin install %s" % PLUGIN_ID)

# what /init copies from the plugin's seed/ (project-relative); doctor checks the first two
SEED_FILES = (".claude/CLAUDE.md", ".claude/loop.config.md")

# A hook command is the kit's when it runs a copied kit hook script (v2 copy install: .claude/scripts/guards/run.py, session_start.py,
# loopctl/compact_hook.py, worktree_hook.py), a v1 guard (.claude/hooks/guard-git.sh, guard-main-edit.sh), or the plugin's own
# scripts through ${CLAUDE_PLUGIN_ROOT} (a hand copy of hooks.json; that variable is not set for project hooks). A hook of the
# team's own that happens to live under .claude/scripts/ is not the kit's: migrate reports it instead of taking it out.
KIT_HOOK_SCRIPTS = ("guards/run.py", "session_start.py", "loopctl/compact_hook.py", "worktree_hook.py")
_KIT_HOOK = re.compile(r"\.claude/scripts/(?:%s)(?![\w./-])|\.claude/hooks/guard-(?:git|main-edit)\.sh|\$\{?CLAUDE_PLUGIN_ROOT\}?/scripts/"
                       % "|".join(re.escape(s) for s in KIT_HOOK_SCRIPTS))


MAX_SETTINGS = 1 << 20


def load_settings(path: str) -> Optional[Dict[str, Any]]:
    """A settings file as a dict; None when it is missing, unreadable, not a regular file (a FIFO must not hang the reader), larger than
    1 MiB, or not a JSON object."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SETTINGS:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(MAX_SETTINGS + 1)
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return doc if isinstance(doc, dict) else None


def is_kit_hook(command: Any) -> bool:
    return isinstance(command, str) and bool(_KIT_HOOK.search(command))


def strip_kit_hooks(settings: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """(settings without the kit's hook entries, the removed commands). A matcher group left empty is dropped, an event left empty
    too, and `hooks` itself when nothing is left. Everything else, the person's own hooks included, is kept as it was."""
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    removed = []   # type: List[str]
    if not isinstance(hooks, dict):
        return out, removed
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            mine = [h for h in entries if not (isinstance(h, dict) and is_kit_hook(h.get("command")))]
            removed += [h["command"] for h in entries if isinstance(h, dict) and is_kit_hook(h.get("command"))]
            if mine:
                g = dict(group)
                g["hooks"] = mine
                kept_groups.append(g)
            elif not entries:
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out, removed


def copy_install_markers(root: str) -> List[str]:
    """What an old copy install left in the project at `root` (project-relative, for a message). Empty for a plugin project."""
    c = os.path.join(root, ".claude")
    found = []   # type: List[str]
    if os.path.isdir(os.path.join(c, "scripts")):
        found.append(".claude/scripts/")
    for name in ("KIT_MANIFEST", "KIT_VERSION"):
        if os.path.isfile(os.path.join(c, name)):
            found.append(".claude/" + name)
    for name in ("guard-git.sh", "guard-main-edit.sh"):
        if os.path.isfile(os.path.join(c, "hooks", name)):
            found.append(".claude/hooks/" + name)
    for name in ("settings.json", "settings.local.json"):
        s = load_settings(os.path.join(c, name))
        if s is not None and strip_kit_hooks(s)[1]:
            found.append(".claude/%s (hooks that run the copied kit)" % name)
    return found


def plugin_disabled(settings: Optional[Dict[str, Any]]) -> bool:
    """True when this settings dict sets `enabledPlugins[PLUGIN_ID]` to false."""
    ep = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    return isinstance(ep, dict) and ep.get(PLUGIN_ID) is False


def plugin_installed(home: str) -> Optional[bool]:
    """Whether Claude Code lists the plugin in `~/.claude/plugins/installed_plugins.json`. None when that file is missing or unreadable
    (an older Claude Code, another layout, or `--plugin-dir`): the answer is not known, not no."""
    try:
        with open(os.path.join(home, ".claude", "plugins", "installed_plugins.json"), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if isinstance(plugins, dict):
        return PLUGIN_ID in plugins
    if isinstance(plugins, list):
        return any(isinstance(p, dict) and (p.get("id") == PLUGIN_ID or p.get("name") == PLUGIN_ID) for p in plugins)
    return None
