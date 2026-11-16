"""The plugin install: is the kit this project runs the plugin, and is the project set up for it.

The kit is the Claude Code plugin `chain-of-custody@chain-of-custody`; the doctor runs from the plugin folder (`ctx.kit_root`). These
checks look at how that folder and the project fit together:

    plugin.version       the plugin's `.claude-plugin/plugin.json` version is the version in its `KIT_VERSION`
    plugin.enabled       no settings file turns the plugin off for this project (then its hooks, the guards, do not load)
    plugin.copy-install  nothing is left of an old copy install (`.claude/scripts/`, `.claude/KIT_MANIFEST`, kit hooks in settings)
    plugin.seed          the project has the files /init copies (`.claude/CLAUDE.md`, `.claude/loop.config.md`)
    plugin.overrides     the project's `.claude/agents/<role>.md` files that replace one of the kit's agents (information only)

In the kit's own checkout all but plugin.version are skipped: it is the plugin, not a project.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Any, Dict, Optional

from kitlib import install, kitpaths, semver

from . import safeio
from .model import Context, Result, check, failed, gatherer, passed, skipped, ERROR, WARN

INIT = "/chain-of-custody:init"
MIGRATE = "/chain-of-custody:migrate"


def _kit_checkout(ctx: Context) -> bool:
    return kitpaths.is_kit_dev_checkout(ctx.repo)


@gatherer("plugin_json")
def gather_plugin_json(ctx: Context) -> Dict[str, Any]:
    p = ctx.kit_path(".claude-plugin", "plugin.json")
    text, problem = safeio.read_text_status(p, 1 << 16)
    if problem:
        return {"ok": False, "path": p, "error": "not found" if problem == safeio.MISSING else problem, "version": None}
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return {"ok": False, "path": p, "error": "not JSON (%s)" % exc, "version": None}
    version = doc.get("version") if isinstance(doc, dict) else None
    return {"ok": isinstance(version, str), "path": p, "error": "" if isinstance(version, str) else "has no version", "version": version}


@check("plugin.version", "plugin", doc="the plugin's plugin.json version matches its KIT_VERSION")
def check_plugin_version(ctx: Context) -> Result:
    pj = ctx.fact("plugin_json")
    kv = ctx.fact("kit_version")
    if not pj["ok"]:
        return failed("plugin.version", "%s %s" % (pj["path"], pj["error"]),
                      "reinstall the plugin (/plugin install %s)" % install.PLUGIN_ID, severity=WARN)
    if not kv.get("version"):
        return skipped("plugin.version", "KIT_VERSION is unreadable (see version.kit)")
    a, b = semver.try_parse(pj["version"]), semver.try_parse(kv["version"])
    if a is None or a != b:
        return failed("plugin.version", "plugin.json says %s but KIT_VERSION says %s: the plugin folder is a mix of two releases" % (pj["version"], kv["version"]),
                      "reinstall the plugin (/plugin install %s); in the kit's checkout, bump both together" % install.PLUGIN_ID, severity=WARN)
    return passed("plugin.version", "plugin %s (plugin.json and KIT_VERSION agree)" % pj["version"])


def _enabled_value(path: str) -> Optional[bool]:
    """True/False when this settings file names the plugin in enabledPlugins, None when it does not say."""
    s = install.load_settings(path)
    ep = s.get("enabledPlugins") if isinstance(s, dict) else None
    v = ep.get(install.PLUGIN_ID) if isinstance(ep, dict) else None
    return v if isinstance(v, bool) else None


@check("plugin.enabled", "plugin", doc="no settings file disables the plugin for this project (its hooks are the guards)")
def check_plugin_enabled(ctx: Context) -> Result:
    if _kit_checkout(ctx):      # the kit's checkout turns the installed copy off on purpose and loads itself with --plugin-dir
        return skipped("plugin.enabled", "this is the kit's own checkout")
    # the most specific scope that names the plugin wins: local, then project, then user
    scopes = [(".claude/settings.local.json", ctx.repo_path(".claude", "settings.local.json")),
              (".claude/settings.json", ctx.repo_path(".claude", "settings.json")),
              ("~/.claude/settings.json", ctx.home_path(".claude", "settings.json"))]
    for label, path in scopes:
        v = _enabled_value(path)
        if v is False:
            return failed("plugin.enabled", "%s sets enabledPlugins[%s] to false: the plugin's hooks (the guards) and agents do not load here"
                          % (label, install.PLUGIN_ID), "remove that entry, or set it to true", severity=ERROR)
        if v is True:
            return passed("plugin.enabled", "%s enables the plugin" % label)
    return passed("plugin.enabled", "no settings file disables the plugin")


@check("plugin.copy-install", "plugin", doc="nothing is left of an old copy install of the kit")
def check_copy_install(ctx: Context) -> Result:
    if _kit_checkout(ctx):
        return skipped("plugin.copy-install", "this is the kit's own checkout")
    found = install.copy_install_markers(ctx.repo)
    if found:
        return failed("plugin.copy-install", "an old copy install of the kit is still here: %s; its hooks would run a second, older set of guards"
                      % ", ".join(found),
                      "run %s (or see the plan first: %s)" % (MIGRATE, kitpaths.script_cmd("migrate.py", "--repo", shlex.quote(ctx.repo))),
                      severity=ERROR, detail={"found": found})
    return passed("plugin.copy-install", "no copy install left in .claude/")


@check("plugin.seed", "plugin", doc="the project has the starter files /init copies")
def check_seed(ctx: Context) -> Result:
    if _kit_checkout(ctx):
        return skipped("plugin.seed", "this is the kit's own checkout")
    missing = [rel for rel in install.SEED_FILES if not os.path.isfile(ctx.repo_path(*rel.split("/")))]
    if missing:
        return failed("plugin.seed", "not set up for the kit yet: %s missing" % ", ".join(missing),
                      "run %s (it copies the starter files and changes nothing you already have)" % INIT, severity=WARN, detail={"missing": missing})
    return passed("plugin.seed", "%s present" % ", ".join(install.SEED_FILES))


@check("plugin.overrides", "plugin", doc="project agent files that replace one of the kit's agents (information)")
def check_overrides(ctx: Context) -> Result:
    if _kit_checkout(ctx):
        return skipped("plugin.overrides", "this is the kit's own checkout")
    adir = ctx.repo_path(".claude", "agents")
    try:
        names = sorted(n for n in os.listdir(adir) if n.endswith(".md"))
    except OSError:
        names = []
    kit = set()
    try:
        kit = {n for n in os.listdir(ctx.kit_path("agents")) if n.endswith(".md")}
    except OSError:
        pass
    mine = [n[:-3] for n in names if n in kit]
    if mine:
        return passed("plugin.overrides", "the project's .claude/agents/ replaces the kit's %s agent(s): %s; delete a file to use the kit's again"
                      % (len(mine), ", ".join(mine)), detail={"overrides": mine})
    return passed("plugin.overrides", "no project file replaces a kit agent")
