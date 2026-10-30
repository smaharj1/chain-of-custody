#!/usr/bin/env python3
"""init_project: write the project settings a plugin cannot ship (Phase 9; IMP-3, report P19, §7.2, CO-33).

A plugin's own settings.json applies only `agent` and `subagentStatusLine`, so the deny block, the sandbox profile and the MCP lockdown have to
be written into the PROJECT's `.claude/settings.json`. That is a guardrail file (H6), so a person applies the change:

    init_project.py [--repo DIR] [--option KEY=VALUE ...]            dry run: print the diff of every file it would write, change nothing
    init_project.py [--repo DIR] [--option KEY=VALUE ...] --apply    HUMAN-RUN, from a plain terminal; refuses inside a Claude Code session

What it plans (each is shown as a diff):
  1. `.claude/settings.json`: the deny list from `templates/settings/interactive.settings.json` (typo guards, guardrail paths, credentials) is ADDED to
     yours, the sandbox profile and `enableAllProjectMcpServers: false` are set where you have no value. Nothing of yours is removed or changed; a
     value of yours that differs from the kit's is listed and kept. The allow and ask lists are not touched: `/fewer-permission-prompts` and your
     own judgement own them. The hooks are not written either: a plugin's `hooks/hooks.json` supplies them; a copy install already has them.
  2. Plugin install only: the files a project owns, copied from the plugin's `seed/` when absent (CLAUDE.md, loop.config.md, the domain primers).
  3. Plugin install only: the plugin's `userConfig` choices (protected_branches, integration_branch, main_edit_allow; set with `--option` or the
     CLAUDE_PLUGIN_OPTION_<KEY> variables Claude Code sets) written into `.claude/loop.config.md` through the key registry. They are H6 keys.
  4. `.gitignore`: the secrets policy and the kit's runtime files, appended where missing.

`--apply` backs `.claude/settings.json` up to `.claude/settings.json.pre-init` first. Standard library only, Python 3.9.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap_config  # noqa: E402
import humanonly  # noqa: E402
from kitlib import jsonio, registry  # noqa: E402

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # <repo>/.claude (copy install) or <plugin root>
OPTIONS = ("protected_branches", "integration_branch", "main_edit_allow")
LIST_OPTIONS = ("protected_branches", "main_edit_allow")
RUNTIME_IGNORES = [".claude/loop.jsonl", ".claude/metrics.jsonl", ".claude/orchestrate.lock", ".claude/settings.local.json", ".claude/launch.json",
                   ".claude/pr/", ".claude/bootstrap/", ".claude/.migrate/", ".claude/worktrees/", ".claude/settings.json.pre-init"]


class InitError(Exception):
    pass


def layout(kit_root: str = KIT_ROOT) -> str:
    return "plugin" if os.path.isfile(os.path.join(kit_root, ".claude-plugin", "plugin.json")) else "copy"


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


def plan_settings(root: str, kit_root: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """(old text, new text, notes about values of yours that differ). new is None when nothing changes."""
    tpl = read_text(os.path.join(kit_root, "templates", "settings", "interactive.settings.json"))
    if tpl is None:
        raise InitError("the kit's templates/settings/interactive.settings.json is missing; run init_project from a complete kit")
    template = json.loads(tpl)
    wanted = {"permissions": {"deny": template["permissions"]["deny"]}, "enableAllProjectMcpServers": template["enableAllProjectMcpServers"],
              "sandbox": template["sandbox"]}
    path = os.path.join(root, ".claude", "settings.json")
    old = read_text(path)
    if old is None:
        current = {}   # type: Dict[str, Any]
    else:
        try:
            current = json.loads(old)
        except ValueError as exc:
            raise InitError(".claude/settings.json is not plain JSON (%s). init_project will not rewrite a file it cannot read exactly; fix it or merge "
                            "templates/settings/interactive.settings.json by hand" % exc)
        if not isinstance(current, dict):
            raise InitError(".claude/settings.json is not a JSON object")
    kept = []   # type: List[str]
    merged = merge_value(current, wanted, "settings", kept)
    new = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    same = (old is not None and json.loads(old) == merged)
    return old, (None if same else new), kept


def plan_seeds(root: str, kit_root: str) -> List[Tuple[str, str]]:
    """[(project-relative path, text)] for seed files the plugin has and the project lacks. Empty on a copy install (they came with `.claude/`)."""
    seed = os.path.join(kit_root, "seed")
    out = []   # type: List[Tuple[str, str]]
    for dirpath, dirnames, filenames in os.walk(seed):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, seed).replace(os.sep, "/")
            if not os.path.exists(os.path.join(root, rel)):
                text = read_text(full)
                if text is not None:
                    out.append((rel, text))
    return out


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
        raise InitError("the userConfig choices need .claude/loop.config.md, and the project has none (the plugin's seed copy was not found)")
    reg = registry.load_registry()
    defaults = reg.defaults()
    planned = {}   # type: Dict[str, Any]
    for k, v in vals.items():
        value = [x.strip() for x in v.split(",") if x.strip()] if k in LIST_OPTIONS else v.strip()
        if value == defaults.get(k) or value in ("", []):
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
        if not os.path.isdir(root):
            raise InitError("%s is not a directory" % root)
        lay = layout(kit_root)
        writes = []   # type: List[Tuple[str, Optional[str], str]]     # (path, old text, new text)
        notes = []    # type: List[str]
        old_s, new_s, kept = plan_settings(root, kit_root)
        if new_s is not None:
            writes.append((".claude/settings.json", old_s, new_s))
        notes += kept
        config_text = read_text(os.path.join(root, ".claude", "loop.config.md"))
        if lay == "plugin":
            for rel, text in plan_seeds(root, kit_root):
                writes.append((rel, None, text))
                if rel == ".claude/loop.config.md":
                    config_text = text
        vals = option_values(args.option, env) if lay == "plugin" or args.option else {}
        new_c, changes = plan_config(config_text, vals)
        if changes:
            writes = [w for w in writes if w[0] != ".claude/loop.config.md"]
            old_c = read_text(os.path.join(root, ".claude", "loop.config.md"))
            writes.append((".claude/loop.config.md", old_c, new_c))
            notes.append("loop.config.md keys set from your choices (H6 keys, review the diff): " + ", ".join(sorted(c[0] for c in changes)))
        gi = plan_gitignore(root, kit_root)
        if gi:
            writes.append((".gitignore", gi[0] if os.path.exists(os.path.join(root, ".gitignore")) else None, gi[1]))
        print("init_project (%s install) for %s%s" % (lay, root, "" if args.apply else "   (dry run: nothing was changed)"))
        if not writes:
            print("nothing to do: the settings already hold the kit's deny block and sandbox profile")
        for path, old, new in writes:
            print("")
            print("--- %s%s" % (path, "" if old is not None else "   (new file)"))
            sys.stdout.write(diff_text(path, old, new) if old != new else "")
        for n in notes:
            print("note: " + n)
        if lay == "plugin":
            print("note: the guard hooks come from the plugin's hooks/hooks.json; nothing was written for them.")
        else:
            print("note: the hooks are the ones in your .claude/settings.json; activating the v2 guards is a separate human step (docs/packaging.md).")
        if not args.apply:
            if writes:
                print("\nto apply, from a plain terminal (not inside Claude Code): add --apply")
            return 0
        for path, old, new in writes:
            full = os.path.join(root, path)
            if path == ".claude/settings.json" and old is not None and not os.path.exists(full + ".pre-init"):
                jsonio.atomic_write_text(full + ".pre-init", old)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            jsonio.atomic_write_text(full, new)
        print("\nwritten: %d file(s). Review `git diff` and commit them yourself (settings are a guardrail, H6)." % len(writes))
        return 0
    except InitError as exc:
        sys.stderr.write("init_project: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
