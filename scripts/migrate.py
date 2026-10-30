#!/usr/bin/env python3
"""migrate: move a project from an old copy install of the kit (v1.x or a v2 copy) to the plugin, keeping every change you made (the /migrate skill; IMP-3, CO-33).

    python3 <kit>/scripts/migrate.py --repo PROJECT            dry run: print the plan, change nothing (the default)
    python3 <kit>/scripts/migrate.py --repo PROJECT --apply    HUMAN-RUN, from a plain terminal: apply the plan

`<kit>` is the plugin's folder (doctor.py prints it as `kit:`). Before 2.0 the kit was copied into the project's `.claude/` (and, in v1,
`AGENTS.md`, `.cursor/rules/`, `skills/`). The kit is a plugin now: its agents, skills, scripts and hooks come from the plugin, and the
project's `.claude/` keeps only the project's own files. To tell the kit's files from your edits, migrate compares each file with the
SHA-256 the old version shipped: the v2 copy's `.claude/KIT_MANIFEST`, and for v1.x the v1.2.0 tree the kit ships (`migrate/v1.2.0.tar`).
Everything runs offline.

Per file of the old copy:

    still the kit's file (the hash matches)          removed: the plugin has it now
    .claude/agents/<role>.md that you edited         kept as a project override: from now on it replaces the kit's <role> agent
    any other .claude/ kit file that you edited      moved to .claude/kit-backup/<old version>/ and listed, so you can carry the edit over
    a file in .claude/scripts/ the kit never shipped moved to the backup too (the folder has to go: /init and doctor look for it),
                                                     unless your settings run it or name it: then it NEEDS YOU (move it, fix the reference)
    AGENTS.md, .cursor/rules/, skills/ that you edited   left where they are and listed (they are outside .claude/); adapt.py makes new ones
    .claude/KIT_VERSION, .claude/KIT_MANIFEST        removed last (the plugin's own KIT_VERSION is the version now)
    CLAUDE.md and the primers, unchanged since the   replaced with the plugin's current starter file (UPDATE)
    old version
    CLAUDE.md, loop.config.md, the primers, rules/   otherwise kept as they are: they are the project's. loop.config.md is never replaced
    .claude/settings.json                            the kit's hook entries are taken out (the plugin's hooks.json supplies them); your
                                                     own hooks and everything else stay. It is backed up to settings.json.pre-init first.
    .claude/settings.local.json                      the same, backed up to settings.local.json.pre-migrate first

Then it does what /init does (init_project.py): the starter files that are missing, the deny list and sandbox, the userConfig choices and
the `.gitignore` lines. The plan prints both parts. `--apply` writes `.claude/.migrate/report.json` as the receipt.

`--apply` refuses inside a Claude Code session (humanonly.py) and the guards deny an agent running it: taking out the kit's instructions,
hooks and scripts is a human decision (H6). It also refuses while a path NEEDS YOU (a symlink, an unreadable file), and a dirty git work
tree (commit first, so `git checkout` is your undo) unless you pass `--allow-dirty`. Standard library only, Python 3.9.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import humanonly  # noqa: E402
import init_project  # noqa: E402
from kitlib import install, kitmanifest as km, kitpaths  # noqa: E402

SCHEMA = "kit.migrate.v2"
WORK_DIR = ".claude/.migrate"
BACKUP_DIR = ".claude/kit-backup"
MAX_FILE = 8 * 1024 * 1024
BASE_ARCHIVE = "v1.2.0.tar"

REMOVE, OVERRIDE, BACKUP, LEFT, PROBLEM, UPDATE = "remove", "override", "backup", "left-in-place", "problem", "update"
ORDER = (PROBLEM, OVERRIDE, BACKUP, LEFT, UPDATE, REMOVE)
TITLES = {REMOVE: "REMOVE: still the kit's file; the plugin has it now", OVERRIDE: "KEEP AS PROJECT OVERRIDE: you edited it",
          BACKUP: "MOVE TO %s/<version>/: you edited it, or the kit never shipped it" % BACKUP_DIR,
          LEFT: "LEFT IN PLACE: outside .claude/, migrate does not touch it",
          UPDATE: "UPDATE: a starter file you never changed; the plugin's current one replaces it",
          PROBLEM: "NEEDS YOU: migrate will not touch it"}
_AGENT = re.compile(r"^\.claude/agents/([A-Za-z0-9_-]+)\.md$")
# the v1 Codex and Cursor adapters, outside .claude/; adapt.py writes their replacement
ADAPTERS = ("AGENTS.md", ".cursor/", "skills/")
LOCAL_SETTINGS = ".claude/settings.local.json"
LOCAL_BACKUP = LOCAL_SETTINGS + ".pre-migrate"
# seed files an unchanged copy may be replaced in; loop.config.md is not one (the new seed would raise git_autonomy to the push tier)
UPDATABLE_SEED = re.compile(r"^\.claude/(?:CLAUDE\.md|context/[A-Za-z0-9_-]+\.md)$")


class MigrateError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- inputs

def read_local(root: str, rel: str) -> Tuple[Optional[bytes], Optional[str]]:
    """(content, problem). A missing file is (None, None); a symlink, a directory, or a file under a symlinked folder is a problem,
    never followed."""
    full = os.path.join(root, rel)
    try:
        st = os.lstat(full)
    except OSError:
        return None, None
    if stat.S_ISLNK(st.st_mode):
        return None, "is a symlink; migrate never follows or removes links"
    if os.path.realpath(os.path.dirname(full)) != os.path.join(os.path.realpath(root), *rel.split("/")[:-1]):
        return None, "sits under a symlinked folder; migrate never follows links"
    if not stat.S_ISREG(st.st_mode):
        return None, "is not a regular file"
    if st.st_size > MAX_FILE:
        return None, "is larger than %d bytes" % MAX_FILE
    with open(full, "rb") as fh:
        return fh.read(), None


def installed_version(root: str) -> Optional[str]:
    data, _ = read_local(root, km.VERSION_REL)
    return km.parse_version(data.decode("utf-8", "replace")) if data else None


def known_hashes(root: str, kit_root: str, from_version: Optional[str],
                 archive: Optional[str]) -> Tuple[Optional[str], Dict[str, Set[str]], Dict[str, Set[str]], List[str]]:
    """(old version, {path: hashes the old kit shipped for it}, {seed path: hashes of the old starter file}, notes). Only kit-class
    files are the kit's: seed and settings files are the project's, and a seed file is only replaced when it is still the old starter
    text. A v2 copy's KIT_MANIFEST and the shipped v1.2.0 tree are both used, so a v2 copy upgraded from v1 is recognised too."""
    notes = []   # type: List[str]
    known = {}   # type: Dict[str, Set[str]]
    seeds = {}   # type: Dict[str, Set[str]]
    version = from_version or installed_version(root)
    data, problem = read_local(root, km.MANIFEST_REL)
    if problem:
        raise MigrateError("%s %s" % (km.MANIFEST_REL, problem))
    if data is not None:
        try:
            manifest = km.loads(data.decode("utf-8", "replace"))
        except ValueError as exc:
            raise MigrateError("%s; migrate will not guess which files are the kit's. Restore it from git, or delete it to fall back "
                               "on the v1.2.0 hashes (files that differ are then backed up, not removed)" % exc)
        version = version or manifest["version"]
        for rel, ent in manifest["files"].items():
            if ent["class"] == km.CLASS_KIT:
                known.setdefault(rel, set()).add(ent["sha256"])
            elif ent["class"] == km.CLASS_SEED and UPDATABLE_SEED.match(rel):
                seeds.setdefault(rel, set()).add(ent["sha256"])
    tar = archive or os.path.join(kit_root, "migrate", BASE_ARCHIVE)
    try:
        tree = km.read_tar_tree(tar)
    except Exception as exc:   # OSError, ValueError, tarfile.TarError
        raise MigrateError("cannot read the base archive %s: %s" % (tar, exc))
    for rel, content in tree.items():
        if km.classify(rel) == km.CLASS_KIT:
            known.setdefault(rel, set()).add(km.sha256_bytes(content))
        elif km.classify(rel) == km.CLASS_SEED and UPDATABLE_SEED.match(rel):
            seeds.setdefault(rel, set()).add(km.sha256_bytes(content))
    if data is None and version and not version.startswith("1.2."):
        notes.append("your copy is %s and the kit ships the hashes of v1.2.0 only: a file of yours that differs from v1.2.0 is backed up, "
                     "not removed, so nothing is lost; delete the backup when you have looked at it" % version)
    return version, known, seeds, notes


def candidates(root: str, known: Dict[str, Set[str]]) -> List[str]:
    """The paths to decide on: every file the old kit shipped, the bookkeeping, and everything under `.claude/scripts/`."""
    out = set(p for p in known if os.path.lexists(os.path.join(root, p)))
    out.update(p for p in km.BOOKKEEPING if os.path.lexists(os.path.join(root, p)))
    base = os.path.join(root, ".claude", "scripts")
    if os.path.islink(base):
        out.add(".claude/scripts")      # a problem entry: never walked through
        return sorted(out)
    for dirpath, dirnames, filenames in os.walk(base):
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            if name in filenames or os.path.islink(full):
                out.add(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


# ---------------------------------------------------------------------------------------------------------------- planning

class Entry(object):
    def __init__(self, path: str, action: str, detail: str = "") -> None:
        self.path = path
        self.action = action
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "action": self.action, "detail": self.detail}


def is_adapter(rel: str) -> bool:
    return any(rel == a or (a.endswith("/") and rel.startswith(a)) for a in ADAPTERS)


def plan_files(root: str, known: Dict[str, Set[str]], referenced: str = "") -> List[Entry]:
    """`referenced` is the text of the settings files with the kit's hooks already taken out: a file it names is still in use."""
    out = []   # type: List[Entry]
    for p in candidates(root, known):
        data, problem = read_local(root, p)
        if problem and not p.startswith(".claude/"):
            out.append(Entry(p, LEFT, "your path " + problem + "; it is outside .claude/, so migrate leaves it"))
            continue
        if problem:
            out.append(Entry(p, PROBLEM, "your path " + problem + "; look at it and remove it by hand"))
            continue
        if data is None:
            continue
        m = _AGENT.match(p)
        if p in km.BOOKKEEPING:
            out.append(Entry(p, REMOVE, "the old copy's bookkeeping"))
        elif "/__pycache__/" in p or p.endswith(".pyc"):
            out.append(Entry(p, REMOVE, "Python cache"))
        elif km.sha256_bytes(data) in known.get(p, ()):
            out.append(Entry(p, REMOVE))
        elif m and p in known:
            out.append(Entry(p, OVERRIDE, "from now on this file replaces the kit's %s agent; delete it to use the kit's" % m.group(1)))
        elif p.startswith(".claude/scripts/") and p not in known and p in referenced:
            out.append(Entry(p, PROBLEM, "your settings still run or name it (a hook or a permission rule), and .claude/scripts/ has to go: "
                                         "move it to a project path such as scripts/ and update that reference"))
        elif p.startswith(".claude/"):
            out.append(Entry(p, BACKUP, "you edited it" if p in known else "not a file the kit shipped"))
        elif is_adapter(p):
            out.append(Entry(p, LEFT, "the kit's old Codex/Cursor adapter, which you edited; adapt.py writes the current one"))
        else:
            out.append(Entry(p, LEFT, "you edited it; delete it when you no longer need it"))
    return out


def plan_seed_updates(root: str, kit_root: str, seeds: Dict[str, Set[str]]) -> List[Tuple[Entry, str]]:
    """[(UPDATE entry, the plugin's starter text)] for CLAUDE.md and the primers that are still the old starter file."""
    out = []   # type: List[Tuple[Entry, str]]
    for rel in sorted(seeds):
        data, problem = read_local(root, rel)
        if problem or data is None or km.sha256_bytes(data) not in seeds[rel]:
            continue
        new = init_project.read_text(os.path.join(kit_root, "seed", *rel.split("/")))
        if new is not None and new.encode("utf-8") != data:
            out.append((Entry(rel, UPDATE, "unchanged since the old version"), new))
    return out


def plan_settings(root: str) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[Tuple[str, str]], List[str], str]:
    """(settings.json without the kit's hooks, or None when it has none; the removed commands; (old, new) text of settings.local.json
    when it has kit hooks too; notes; the text of both files without the kit's hooks, to find the files they still name)."""
    notes = []   # type: List[str]
    try:
        _, current = init_project.read_settings(root)
    except init_project.InitError as exc:
        raise MigrateError(str(exc))
    stripped, removed = install.strip_kit_hooks(current)
    referenced = json.dumps(stripped)
    local_write = None   # type: Optional[Tuple[str, str]]
    local_text = init_project.read_text(os.path.join(root, LOCAL_SETTINGS))
    local = install.load_settings(os.path.join(root, LOCAL_SETTINGS))
    if local_text is not None and local is None:
        notes.append("%s is not a JSON object migrate can read; if it has hook entries that run the old copy of the kit, take them "
                     "out by hand" % LOCAL_SETTINGS)
    if local is not None:
        local_stripped, local_removed = install.strip_kit_hooks(local)
        referenced += "\n" + json.dumps(local_stripped)
        if local_removed:
            removed = removed + local_removed
            local_write = (local_text or "", json.dumps(local_stripped, indent=2, ensure_ascii=False) + "\n")
            notes.append("%s had hook entries that run the old copy of the kit; they are taken out, and the file is backed up to %s "
                         "first (it is git-ignored: delete the backup when you are happy)" % (LOCAL_SETTINGS, LOCAL_BACKUP))
    if install.plugin_disabled(current) or install.plugin_disabled(local):
        notes.append("your project settings set enabledPlugins[\"%s\"] to false: the kit does not load here until you remove that" % install.PLUGIN_ID)
    return (stripped if removed and stripped != current else None), removed, local_write, notes, referenced


def plugin_installed(env: Dict[str, str], kit_root: str) -> Optional[bool]:
    if os.sep + os.path.join(".claude", "plugins") + os.sep in kit_root + os.sep:
        return True              # this script runs from the plugin cache
    return install.plugin_installed(env.get("HOME") or os.path.expanduser("~"))


# ---------------------------------------------------------------------------------------------------------------- applying

def safe_dest(root: str, rel: str) -> str:
    full = os.path.join(root, rel)
    real_root = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(full))
    if parent != real_root and not parent.startswith(real_root + os.sep):
        raise MigrateError("%s would be written outside the project" % rel)
    return full


def write_atomic(full: str, data: bytes, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".migrate-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, full)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def prune(root: str, full: str) -> None:
    """Remove the directories left empty above `full`, up to (not including) the project root and `.claude/`."""
    stop = {os.path.realpath(root), os.path.realpath(os.path.join(root, ".claude"))}
    d = os.path.dirname(full)
    while os.path.realpath(d) not in stop and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)


def backup_rel(version: Optional[str], rel: str) -> str:
    return "%s/%s/%s" % (BACKUP_DIR, version or "unknown", rel[len(".claude/"):])


def dirty_tree(root: str) -> Optional[str]:
    """A description of uncommitted changes when `root` is a git work tree, else None."""
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        if top.returncode != 0:
            return None
        r = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    text = r.stdout.decode("utf-8", "replace").strip()
    return text.split("\n")[0] if text else None


def apply(root: str, version: Optional[str], entries: Sequence[Entry], init_writes: Sequence[Tuple[str, Optional[str], str]],
          local_write: Optional[Tuple[str, str]] = None) -> List[str]:
    """Write the settings and the /init files, then move and remove the copy's files, the bookkeeping last (so a run that stops half way
    is still recognised as a copy install and can be run again). Every destination is checked before the first write. Returns the
    paths touched."""
    for rel in (".claude", BACKUP_DIR, WORK_DIR):
        if os.path.islink(os.path.join(root, rel)):
            raise MigrateError("%s is a symlink; migrate will not write through it" % rel)
    moves = [e for e in entries if e.action in (REMOVE, BACKUP)]
    moves.sort(key=lambda e: e.path in km.BOOKKEEPING)
    for path, _, _ in init_writes:
        safe_dest(root, path)
    if local_write is not None:
        safe_dest(root, LOCAL_SETTINGS)
        safe_dest(root, LOCAL_BACKUP)
    for e in moves:
        safe_dest(root, e.path)
        if e.action == BACKUP:
            safe_dest(root, backup_rel(version, e.path) + ".2")
    writes = sorted(init_writes, key=lambda w: w[0] != ".claude/settings.json")
    init_project.write_all(root, writes)
    done = [w[0] for w in writes]
    if local_write is not None:
        if not os.path.exists(os.path.join(root, LOCAL_BACKUP)):
            write_atomic(os.path.join(root, LOCAL_BACKUP), local_write[0].encode("utf-8"), 0o600)
        write_atomic(os.path.join(root, LOCAL_SETTINGS), local_write[1].encode("utf-8"), 0o600)
        done.append(LOCAL_SETTINGS)
    for e in moves:
        full = os.path.join(root, e.path)
        if e.action == BACKUP:
            with open(full, "rb") as fh:
                data = fh.read()
            mode = stat.S_IMODE(os.lstat(full).st_mode)
            dest = os.path.join(root, backup_rel(version, e.path))
            if os.path.lexists(dest):
                with open(dest, "rb") as fh:
                    same = fh.read() == data
                if not same:
                    dest += ".2"
            write_atomic(dest, data, mode)
        os.remove(full)
        prune(root, full)
        done.append(e.path)
    return done


# ---------------------------------------------------------------------------------------------------------------- output

def counts(entries: Sequence[Entry]) -> Dict[str, int]:
    c = {}   # type: Dict[str, int]
    for e in entries:
        c[e.action] = c.get(e.action, 0) + 1
    return c


def render(doc: Dict[str, Any], entries: Sequence[Entry], init_plan: Dict[str, Any]) -> None:
    print("migrate: copy install %s -> plugin %s%s" % (doc["from_version"] or "(version unknown)", doc["kit_version"],
                                                         "" if doc["applied"] else "   (dry run: nothing was changed)"))
    print("  project: %s" % doc["repo"])
    print("  kit:     %s" % doc["kit"])
    for act in ORDER:
        rows = [e for e in entries if e.action == act]
        if not rows:
            continue
        print("")
        print("%s (%d)" % (TITLES[act].replace("<version>", doc["from_version"] or "unknown"), len(rows)))
        for e in rows:
            print("  %s%s" % (e.path, "   -- " + e.detail if e.detail else ""))
    if doc["removed_hooks"]:
        print("")
        print("SETTINGS: the kit's hook entries taken out of .claude/settings.json (%d); your own hooks stay:" % len(doc["removed_hooks"]))
        for c in doc["removed_hooks"]:
            print("  " + (c if len(c) <= 110 else c[:107] + "..."))
    c = counts(entries)
    print("")
    print("summary: " + (", ".join("%d %s" % (c[a], a) for a in ORDER if a in c) if c else "no copied kit files left"))
    print("")
    print("then, what /init does:")
    if not init_plan["writes"]:
        print("  nothing to do: the starter files are there and the settings already hold the kit's deny block and sandbox profile")
    init_project.print_plan(init_plan)
    for n in doc["notes"]:
        print("note: " + n)
    if doc["plugin_installed"] is not True:
        print("")
        print("the plugin does not look installed%s. In Claude Code, run:" % ("" if doc["plugin_installed"] is False else " (could not tell)"))
        for cmd in install.INSTALL_COMMANDS:
            print("    " + cmd)
    if doc["applied"]:
        print("")
        print("applied: %d path(s) moved, removed or written. Receipt: %s/report.json" % (len(doc["done"]), WORK_DIR))
        print("when you are happy: review `git diff`, commit, and delete %s/ (and %s/ once you have carried your edits over)" % (WORK_DIR, BACKUP_DIR))
    elif doc["has_changes"]:
        print("")
        print("to apply, from a plain terminal (not inside Claude Code):")
        print("    " + apply_command(doc))


def apply_command(doc: Dict[str, Any]) -> str:
    """The command that applies this plan: the same choices as the dry run, written out (the plugin's CLAUDE_PLUGIN_OPTION_*
    variables are not set in a plain terminal)."""
    args = ["--repo", shlex.quote(doc["repo"])]
    for flag, key in (("--from-version", "from_version_arg"), ("--base-archive", "base_archive")):
        if doc.get(key):
            args += [flag, shlex.quote(doc[key])]
    for k, v in sorted(doc.get("options", {}).items()):
        args += ["--option", shlex.quote("%s=%s" % (k, v))]
    return kitpaths.script_cmd("migrate.py", *(args + ["--apply"]))


# ---------------------------------------------------------------------------------------------------------------- main

def resolve_root(repo: str) -> str:
    repo = os.path.abspath(repo)
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.decode("utf-8").strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(repo)


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None, kit_root: str = kitpaths.KIT_ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--repo", default=".", help="the project to migrate (default: the current directory)")
    ap.add_argument("--from-version", default=None, help="the old copy's version, when .claude/KIT_VERSION is missing")
    ap.add_argument("--base-archive", default=None, help="a tar of the old kit tree (`git archive`) to take the hashes from, instead of the shipped v1.2.0")
    ap.add_argument("--option", action="append", default=[], metavar="KEY=VALUE", help="a plugin userConfig choice, as for init_project.py")
    ap.add_argument("--apply", action="store_true", help="HUMAN-RUN: apply the plan")
    ap.add_argument("--allow-dirty", action="store_true", help="apply even though the git work tree has uncommitted changes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.apply:
            refusal = humanonly.refusal("migrate", env)
            if refusal:
                sys.stderr.write(refusal + "\n")
                return 3
        root = resolve_root(args.repo)
        if kitpaths.is_kit_dev_checkout(root):
            raise MigrateError("%s is the kit's own checkout, not a project" % root)
        if not os.path.isdir(os.path.join(root, ".claude")):
            raise MigrateError("%s has no .claude/ directory: there is no copy install to migrate. Install the plugin and run "
                               "/chain-of-custody:init instead" % root)
        version, known, seeds, notes = known_hashes(root, kit_root, args.from_version, args.base_archive)
        stripped, removed, local_write, snotes, referenced = plan_settings(root)
        entries = plan_files(root, known, referenced)
        updates = plan_seed_updates(root, kit_root, seeds)
        entries += [e for e, _ in updates]
        notes += snotes
        try:
            options = init_project.option_values(args.option, env)
            init_plan = init_project.plan(root, kit_root, args.option, env, settings_base=stripped)
        except init_project.InitError as exc:
            raise MigrateError(str(exc))
        for e, text in updates:
            old = init_project.read_text(os.path.join(root, e.path))
            init_plan["writes"].append((e.path, old, text))
            init_plan["kept"] = [k for k in init_plan["kept"] if k != e.path]
        if not any(e.path == ".claude/CLAUDE.md" for e, _ in updates):
            claude_md = init_project.read_text(os.path.join(root, ".claude", "CLAUDE.md")) or ""
            if ".claude/commands/" in claude_md or ".claude/scripts/" in claude_md:
                notes.append(".claude/CLAUDE.md is yours (you edited it) and still names .claude/commands/ or .claude/scripts/: the modes are "
                             "/chain-of-custody:<mode> skills now and the scripts live in the plugin; compare it with the plugin's "
                             "seed/.claude/CLAUDE.md")
        if any(is_adapter(e.path) for e in entries):
            notes.append("the kit's old Codex/Cursor adapter files are part of this plan. If you use Codex or Cursor, write the current "
                         "ones after the apply: " + kitpaths.script_cmd("adapt.py", "--target", shlex.quote(root), "--tool", "codex")
                         + " (or --tool cursor)")
        if not entries and not removed:
            notes.append("no copy install of the kit was found here; /chain-of-custody:init sets a project up for the plugin")
        if any(e.action == OVERRIDE for e in entries):
            notes.append("a kept .claude/agents/<role>.md is a project override: doctor lists it, and it does not get the kit's updates")
        if any(e.path.startswith(".claude/commands/") for e in entries):
            notes.append("the kit's modes are plugin skills now: /chain-of-custody:<mode> (an edited command file is backed up, not kept)")
        kit_version = km.parse_version(init_project.read_text(os.path.join(kit_root, "KIT_VERSION")) or "") or "(unknown)"
        doc = {"schema": SCHEMA, "repo": root, "kit": kit_root, "from_version": version, "kit_version": kit_version,
               "counts": counts(entries), "actions": [e.to_dict() for e in entries], "removed_hooks": removed,
               "init": {"writes": [w[0] for w in init_plan["writes"]], "copied": init_plan["copied"], "kept": init_plan["kept"],
                        "notes": init_plan["notes"]},
               "notes": notes, "plugin_installed": plugin_installed(env, kit_root), "applied": False, "done": [],
               "options": options, "from_version_arg": args.from_version, "base_archive": args.base_archive,
               "has_changes": bool([e for e in entries if e.action in (REMOVE, BACKUP)] or init_plan["writes"] or local_write)}
        if args.apply:
            stuck = [e.path for e in entries if e.action == PROBLEM]
            if stuck:
                raise MigrateError("nothing was changed: %d path(s) need you first (%s). Run the dry run to see why" % (len(stuck), ", ".join(stuck[:5])))
            dirty = None if args.allow_dirty else dirty_tree(root)
            if dirty:
                raise MigrateError("the git work tree has uncommitted changes (%s). Commit or stash them first, so `git checkout` is your undo; "
                                   "or pass --allow-dirty" % dirty)
            doc["done"] = apply(root, version, entries, init_plan["writes"], local_write)
            doc["applied"] = True
            doc["applied_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            write_atomic(safe_dest(root, WORK_DIR + "/report.json"), (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"))
        if args.json:
            print(json.dumps(doc, indent=1, sort_keys=True))
        else:
            render(doc, entries, init_plan)
        return 1 if any(e.action == PROBLEM for e in entries) else 0
    except MigrateError as exc:
        sys.stderr.write("migrate: %s\n" % exc)
        return 2
    except OSError as exc:
        sys.stderr.write("migrate: %s. The apply may have stopped part way: the kit's bookkeeping goes last, so run the same command "
                         "again once the cause is fixed (`git status` shows what changed)\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
