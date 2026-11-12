#!/usr/bin/env python3
"""init_project: set a project up for the kit's plugin (the /init skill; IMP-3, report P19, §7.2, CO-33).

The kit is a Claude Code plugin: its agents, skills, scripts and hooks live in the plugin. A plugin cannot ship a project's own
files or its deny list and sandbox, so this script writes them into the PROJECT. `.claude/settings.json` is a guardrail file (H6),
so a person applies the change:

    init_project.py [--repo DIR] [--option KEY=VALUE ...]            dry run: print the diff of every file it would write, change nothing
    init_project.py [--repo DIR] [--option KEY=VALUE ...] --apply    HUMAN-RUN, from a plain terminal; refuses inside a Claude Code session

What it plans (each is shown as a diff):
  1. The project's starter files, copied from the plugin's `seed/.claude/` into `.claude/` when absent (CLAUDE.md, loop.config.md, the
     domain primers). A file you already have is never overwritten; the summary lists what was copied and what was kept.
  2. `.claude/settings.json`: the deny list from `templates/settings/interactive.settings.json` (typo guards, guardrail paths, credentials) is
     ADDED to yours, the sandbox profile and `enableAllProjectMcpServers: false` are set where you have no value. Nothing of yours is removed
     or changed; a value of yours that differs from the kit's is listed and kept. The allow and ask lists are not touched:
     `/fewer-permission-prompts` and your own judgement own them. No hooks are written: the plugin's `hooks/hooks.json` supplies them.
  3. The plugin's `userConfig` choices (protected_branches, integration_branch, main_edit_allow; set with `--option` or the
     CLAUDE_PLUGIN_OPTION_<KEY> variables Claude Code sets) written into `.claude/loop.config.md` through the key registry. They are H6 keys.
  4. `.gitignore`: the secrets policy and the kit's runtime files, appended where missing.

It stops, changing nothing, when the project still holds an old copy install of the kit (`.claude/scripts/`, `.claude/KIT_MANIFEST`,
`.claude/KIT_VERSION`, kit hooks in settings.json): /migrate takes that out first. It also refuses to run on the kit's own checkout.

`--apply` backs `.claude/settings.json` up to `.claude/settings.json.pre-init` first. Standard library only, Python 3.9.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import shlex
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap_config  # noqa: E402
import humanonly  # noqa: E402
from kitlib import gitutil, install, jsonio, kitpaths, registry  # noqa: E402

KIT_ROOT = kitpaths.KIT_ROOT
OPTIONS = ("protected_branches", "integration_branch", "main_edit_allow")
LIST_OPTIONS = ("protected_branches", "main_edit_allow")
RUNTIME_IGNORES = [".claude/loop.jsonl", ".claude/metrics.jsonl", ".claude/orchestrate.lock", ".claude/settings.local.json", ".claude/launch.json",
                   ".claude/pr/", ".claude/bootstrap/", ".claude/.migrate/", ".claude/worktrees/", ".claude/settings.json.pre-init",
                   ".claude/settings.local.json.pre-migrate"]
FULL_PATH_HINT = ("every command the kit prints for you to run (doctor, loopctl, the human-only steps) already names the script by its full "
                  "path under this folder; copy it as printed")


class InitError(Exception):
    pass


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def union(current: List[Any], wanted: List[Any]) -> List[Any]:
    out = list(current)
    for item in wanted:
        if item not in out:
            out.append(item)
    return out


def merge_value(current: Any, wanted: Any, where: str, kept: List[str]) -> Any:
    """Add what `wanted` has and `current` lacks. Lists are unioned, mappings merged, a differing scalar of yours is kept and reported."""
    if isinstance(wanted, dict) and isinstance(current, dict):
        out = dict(current)
        for k, v in wanted.items():
            out[k] = merge_value(current[k], v, where + "." + k, kept) if k in current else copy.deepcopy(v)
        return out
    if isinstance(wanted, list) and isinstance(current, list):
        return union(current, wanted)
    if current != wanted:
        kept.append("%s is %s in your settings (the kit suggests %s); yours kept" % (where, json.dumps(current), json.dumps(wanted)))
    return current


def merge_settings(current: Dict[str, Any], kit_root: str) -> Tuple[Dict[str, Any], List[str]]:
    """(current plus the kit's deny list, sandbox and MCP lockdown, notes about values of yours that differ)."""
    tpl = read_text(os.path.join(kit_root, "templates", "settings", "interactive.settings.json"))
    if tpl is None:
        raise InitError("the kit's templates/settings/interactive.settings.json is missing; run init_project from a complete kit")
    template = json.loads(tpl)
    wanted = {"permissions": {"deny": template["permissions"]["deny"]}, "enableAllProjectMcpServers": template["enableAllProjectMcpServers"],
              "sandbox": template["sandbox"], "extraKnownMarketplaces": {install.MARKETPLACE: {"source": install.MARKETPLACE_SOURCE}},
              "enabledPlugins": {install.PLUGIN_ID: True}}
    kept = []   # type: List[str]
    merged = merge_value(current, wanted, "settings", kept)
    if not install.plugin_disabled(merged) and not install.plugin_disabled(current):
        kept.append("settings.json names the plugin's marketplace and turns the plugin on for this project, so a teammate who trusts the "
                    "folder is asked to install it")
    return merged, kept


def read_settings(root: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """(text, parsed) of the project's `.claude/settings.json`; ({} when absent). Raises InitError when it is not a plain JSON object."""
    old = read_text(os.path.join(root, ".claude", "settings.json"))
    if old is None:
        return None, {}
    try:
        current = json.loads(old)
    except ValueError as exc:
        raise InitError(".claude/settings.json is not plain JSON (%s). init_project will not rewrite a file it cannot read exactly; fix it or merge "
                        "templates/settings/interactive.settings.json by hand" % exc)
    if not isinstance(current, dict):
        raise InitError(".claude/settings.json is not a JSON object")
    return old, current


def plan_settings(root: str, kit_root: str, base: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str], List[str]]:
    """(old text, new text, notes about values of yours that differ). new is None when nothing changes. `base` replaces the parsed file
    (migrate passes the settings with the kit's hook entries already taken out)."""
    old, current = read_settings(root)
    merged, kept = merge_settings(current if base is None else base, kit_root)
    same = (old is not None and json.loads(old) == merged)
    return old, (None if same else json.dumps(merged, indent=2, ensure_ascii=False) + "\n"), kept


def plan_seeds(root: str, kit_root: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """([(project-relative path, text)] to copy, [paths the project already has, kept]) for the plugin's `seed/` files."""
    seed = os.path.join(kit_root, "seed")
    copy_, kept = [], []   # type: List[Tuple[str, str]], List[str]
    for dirpath, dirnames, filenames in os.walk(seed):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn == ".DS_Store":
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, seed).replace(os.sep, "/")
            if os.path.lexists(os.path.join(root, rel)):
                kept.append(rel)
                continue
            text = read_text(full)
            if text is not None:
                copy_.append((rel, text))
    return copy_, kept


def option_values(options: Sequence[str], env: Dict[str, str]) -> Dict[str, str]:
    vals = {}   # type: Dict[str, str]
    for k in OPTIONS:
        v = env.get("CLAUDE_PLUGIN_OPTION_" + k.upper())
        if v:
            vals[k] = v
    for item in options:
        if "=" not in item:
            raise InitError("--option wants KEY=VALUE, got %r" % item[:40])
        k, v = item.split("=", 1)
        if k not in OPTIONS:
            raise InitError("unknown option %s (the plugin's userConfig keys are %s)" % (k, ", ".join(OPTIONS)))
        vals[k] = v
    return vals


def plan_config(config_text: Optional[str], vals: Dict[str, str]) -> Tuple[Optional[str], List[Tuple[str, str, str]]]:
    """(new loop.config.md text, changes) for the userConfig choices that differ from the registry defaults."""
    if not vals:
        return config_text, []
    if config_text is None:
        raise InitError("the userConfig choices need .claude/loop.config.md, and the project has none (the plugin's seed/ copy was not found)")
    reg = registry.load_registry()
    defaults = reg.defaults()
    try:
        current = bootstrap_config.config_mod.load_text(config_text, reg).effective()
    except bootstrap_config.config_mod.ConfigError:
        current = {}
    planned = {}   # type: Dict[str, Any]
    for k, v in vals.items():
        value = [x.strip() for x in v.split(",") if x.strip()] if k in LIST_OPTIONS else v.strip()
        if value == current.get(k, defaults.get(k)) or value in ("", []):
            continue
        bootstrap_config.check_entry(reg, "", k, value)
        planned[k] = value
    if not planned:
        return config_text, []
    try:
        return bootstrap_config.apply_plan_text(config_text, planned, reg)
    except bootstrap_config.ConfigPlanError as exc:
        raise InitError(str(exc))


def plan_gitignore(root: str, kit_root: str) -> Optional[Tuple[str, str]]:
    old = read_text(os.path.join(root, ".gitignore")) or ""
    have = {ln.strip() for ln in old.split("\n")}
    snippet = read_text(os.path.join(kit_root, "templates", "security", "gitignore.snippet")) or ""
    add = []   # type: List[str]
    for ln in snippet.split("\n"):
        if ln.strip() and not ln.lstrip().startswith("#") and ln.strip() not in have and ln.strip() not in add:
            add.append(ln.strip())
    for ln in RUNTIME_IGNORES:
        if ln not in have and ln not in add:
            add.append(ln)
    if not add:
        return None
    block = "\n# Chain of Custody kit (added by init_project.py)\n" + "\n".join(add) + "\n"
    return old, old + ("" if old.endswith("\n") or not old else "\n") + block


def diff_text(path: str, old: Optional[str], new: str) -> str:
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), "a/" + path if old is not None else "/dev/null", "b/" + path))


def plan(root: str, kit_root: str, options: Sequence[str], env: Dict[str, str],
         settings_base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything /init would write: {"writes": [(path, old text, new text)], "notes": [...], "copied": [...], "kept": [...]}.
    migrate.py calls this too, after it has planned taking the copy install out."""
    writes = []   # type: List[Tuple[str, Optional[str], str]]
    notes = []    # type: List[str]
    seeds, kept_seeds = plan_seeds(root, kit_root)
    config_text = read_text(os.path.join(root, ".claude", "loop.config.md"))
    for rel, text in seeds:
        writes.append((rel, None, text))
        if rel == ".claude/loop.config.md":
            config_text = text
    old_s, new_s, kept = plan_settings(root, kit_root, settings_base)
    if new_s is not None:
        writes.append((".claude/settings.json", old_s, new_s))
    notes += kept
    new_c, changes = plan_config(config_text, option_values(options, env))
    if changes:
        writes = [w for w in writes if w[0] != ".claude/loop.config.md"]
        writes.append((".claude/loop.config.md", read_text(os.path.join(root, ".claude", "loop.config.md")), new_c))
        notes.append("loop.config.md keys set from your choices (H6 keys, review the diff): " + ", ".join(sorted(c[0] for c in changes)))
    gi = plan_gitignore(root, kit_root)
    if gi:
        writes.append((".gitignore", gi[0] if os.path.exists(os.path.join(root, ".gitignore")) else None, gi[1]))
    return {"writes": writes, "notes": notes, "copied": [rel for rel, _ in seeds], "kept": kept_seeds}


def print_plan(p: Dict[str, Any]) -> None:
    for path, old, new in p["writes"]:
        print("")
        print("--- %s%s" % (path, "" if old is not None else "   (new file)"))
        sys.stdout.write(diff_text(path, old, new) if old != new else "")
    print("")
    if p["copied"]:
        print("starter files copied from the plugin: " + ", ".join(p["copied"]))
    if p["kept"]:
        print("starter files you already have, kept as they are: " + ", ".join(p["kept"]))
    for n in p["notes"]:
        print("note: " + n)
    print("note: the guard hooks come from the plugin's hooks/hooks.json; nothing was written for them.")


def write_all(root: str, writes: Sequence[Tuple[str, Optional[str], str]]) -> None:
    for path, old, new in writes:
        full = os.path.join(root, path)
        if path == ".claude/settings.json" and old is not None and not os.path.exists(full + ".pre-init"):
            jsonio.atomic_write_text(full + ".pre-init", old)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        jsonio.atomic_write_text(full, new)


def check_project(root: str) -> None:
    """Raise InitError when /init must not run here: not a directory, the kit's own checkout, or an old copy install."""
    if not os.path.isdir(root):
        raise InitError("%s is not a directory" % root)
    if kitpaths.is_kit_dev_checkout(root):
        raise InitError("%s is the kit's own checkout (the plugin itself), not a project. Run /init in the project that uses the kit." % root)
    markers = install.copy_install_markers(root)
    if markers:
        raise InitError("this project still has an old copy install of the kit (%s). Nothing was changed. Run /chain-of-custody:migrate "
                        "first: it takes the copy out, keeps your edits, and then does what /init does." % ", ".join(markers))


def apply_command(root: str, vals: Dict[str, str]) -> str:
    """The command that applies this plan, the choices written out (CLAUDE_PLUGIN_OPTION_* is not set in a plain terminal)."""
    args = ["--repo", shlex.quote(root)]
    for k, v in sorted(vals.items()):
        args += ["--option", shlex.quote("%s=%s" % (k, v))]
    return kitpaths.script_cmd("init_project.py", *(args + ["--apply"]))


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None, kit_root: str = KIT_ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--option", action="append", default=[], metavar="KEY=VALUE", help="a plugin userConfig choice: " + ", ".join(OPTIONS))
    ap.add_argument("--apply", action="store_true", help="HUMAN-RUN: write the files")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.apply:
            refusal = humanonly.refusal("init_project", env)
            if refusal:
                sys.stderr.write(refusal + "\n")
                return 3
        root = os.path.realpath(args.repo)
        top = gitutil.toplevel(root) if os.path.isdir(root) else None
        where = []   # type: List[str]
        if top is not None and top != root:
            where.append("note: %s is inside the git repository %s; /init works on the repository root" % (root, top))
            root = top
        check_project(root)
        if top is None:
            where.append("note: %s is not a git repository; the guards, delivery and doctor expect one (git init)" % root)
        p = plan(root, kit_root, args.option, env)
        print("init_project for %s%s" % (root, "" if args.apply else "   (dry run: nothing was changed)"))
        print("kit: %s" % kit_root)
        print("     (%s)" % FULL_PATH_HINT)
        for line in where:
            print(line)
        if not p["writes"]:
            print("nothing to do: the starter files are there and the settings already hold the kit's deny block and sandbox profile")
        print_plan(p)
        if not args.apply:
            if p["writes"]:
                print("\nto apply, from a plain terminal (not inside Claude Code):")
                print("    " + apply_command(root, option_values(args.option, env)))
            return 0
        write_all(root, p["writes"])
        print("\nwritten: %d file(s). Review `git status` and `git diff` (new files are untracked) and commit them yourself "
              "(settings are a guardrail, H6)." % len(p["writes"]))
        return 0
    except InitError as exc:
        sys.stderr.write("init_project: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
