#!/usr/bin/env python3
"""migrate: upgrade a copy install of the kit to the version in a kit checkout, without hand-merging and without overwriting your changes (Phase 9; IMP-3, CO-33).

    python3 <kit>/.claude/scripts/migrate.py --repo PROJECT                 dry run: print the plan, change nothing (the default)
    python3 <kit>/.claude/scripts/migrate.py --repo PROJECT --apply         HUMAN-RUN, from a plain terminal: apply the plan
    python3 .../migrate.py --repo PROJECT --resolved PATH [PATH ...]        HUMAN-RUN: you merged PATH by hand; stop reporting it as a conflict
    python3 .../migrate.py --repo PROJECT --take-kit PATH [PATH ...]        HUMAN-RUN: replace PATH by the kit's version (your copy is backed up)

`<kit>` is a checkout of the kit (or its `dist/copy-install/`): a tree that holds `.claude/KIT_MANIFEST`. Your install has no kit history, so the
BASE of a three-way merge comes from an archive shipped in the kit (`.claude/migrate/v1.2.0.tar`, made with `git archive v1.2.0`). Everything
runs offline.

Per file, with B = the base version's file, N = the new kit's file, L = yours:

    L absent                       kit file you never had -> add; one you deleted -> stays deleted
    L == N                         up to date
    L == B (you never touched it)  replace by N
    N == B (the kit did not change it)   keep yours
    all three differ               `git merge-file` three-way merge: clean -> written; conflicts -> NOT written, listed for you
    kit no longer ships it         removed if you never touched it; kept and listed if you did
    seed files (CLAUDE.md, loop.config.md, primers, rules)   added if absent, replaced only if still byte-identical to the base, else yours
    frozen files (.claude/settings.json)                     added if absent, never changed (settings are a guardrail, H6)

Nothing you changed is overwritten silently: a file you modified is merged cleanly or left alone with the merge result in
`.claude/.migrate/conflicts/`. Before `--apply` writes anything it backs up every file it replaces or removes to `.claude/.migrate/backup/<time>/`.
`KIT_VERSION` and `KIT_MANIFEST` are written last, and only when there is nothing left to resolve, so a half-done migration can simply be run again.

`--apply` refuses inside a Claude Code session (humanonly.py) and the guards deny an agent running it: applying an upgrade to the kit's own
instructions, hooks and scripts is a human decision (H6). It also refuses a dirty git work tree (commit first so `git checkout` is your undo)
unless you pass `--allow-dirty`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import humanonly  # noqa: E402
from kitlib import kitmanifest as km  # noqa: E402

SCHEMA = "kit.migrate.v1"
WORK_DIR = ".claude/.migrate"
STATE_REL = WORK_DIR + "/state.json"
MAX_FILE = 8 * 1024 * 1024

ADD, UPDATE, MERGE, CONFLICT, KEEP, UPTODATE = "add", "update", "merge", "conflict", "keep", "up-to-date"
REMOVE, KEEP_RETIRED, SKIP_DELETED, TAKE_KIT, RESOLVED = "remove", "keep-retired", "skip-deleted", "take-kit", "resolved"
CHANGES = (ADD, UPDATE, MERGE, REMOVE, TAKE_KIT)


class MigrateError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- inputs

def sha(data: bytes) -> str:
    return km.sha256_bytes(data)


def read_local(root: str, rel: str) -> Tuple[Optional[bytes], Optional[str]]:
    """(content, problem). A missing file is (None, None); a symlink or a directory is a problem, never followed."""
    full = os.path.join(root, rel)
    try:
        st = os.lstat(full)
    except OSError:
        return None, None
    if stat.S_ISLNK(st.st_mode):
        return None, "is a symlink; migrate never writes through links"
    if not stat.S_ISREG(st.st_mode):
        return None, "is not a regular file"
    if st.st_size > MAX_FILE:
        return None, "is larger than %d bytes" % MAX_FILE
    with open(full, "rb") as fh:
        return fh.read(), None


def load_source(source: str) -> Tuple[Dict[str, Any], Dict[str, bytes], Dict[str, bool]]:
    """(manifest, {path: content} of every manifest file, {path: executable}). Verifies each file against the manifest."""
    mpath = os.path.join(source, km.MANIFEST_REL)
    if not os.path.isfile(mpath):
        raise MigrateError("%s has no %s. --source must be a checkout of the kit (or its dist/copy-install/); "
                           "a plugin directory is not a source for a copy install" % (source, km.MANIFEST_REL))
    with open(mpath, "r", encoding="utf-8") as fh:
        try:
            manifest = km.loads(fh.read())
        except ValueError as exc:
            raise MigrateError(str(exc))
    files = {}   # type: Dict[str, bytes]
    exe = {}     # type: Dict[str, bool]
    for rel, ent in manifest["files"].items():
        full = os.path.join(source, rel)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
            exe[rel] = bool(os.stat(full).st_mode & 0o111)
        except OSError:
            raise MigrateError("the kit source is incomplete: %s is listed in KIT_MANIFEST but missing" % rel)
        if sha(data) != ent["sha256"]:
            raise MigrateError("the kit source is not the version its KIT_MANIFEST describes: %s differs. "
                               "Re-download the kit; do not migrate from a modified checkout" % rel)
        files[rel] = data
    return manifest, files, exe


def installed_version(root: str) -> Optional[str]:
    data, _ = read_local(root, km.VERSION_REL)
    return km.parse_version(data.decode("utf-8", "replace")) if data else None


def choose_base(root: str, source: str, new_manifest: Dict[str, Any], from_version: Optional[str], archive: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """(version label, base manifest) for the installed version. The base is the tree inside a shipped archive."""
    version = from_version or installed_version(root)
    candidates = []   # type: List[str]
    if archive:
        candidates = [archive]
    elif version:
        # the archives ship with the kit checkout (.claude/migrate/), next to this script, and are not installed into projects
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrate")
        for d in (os.path.join(source, ".claude", "migrate"), here):
            p = os.path.join(d, "v%s.tar" % version)
            if os.path.isfile(p):
                candidates = [p]   # a shipped archive wins even for the kit's own version: before a release bump they carry the same label
                break
    if not candidates and version is not None and version == new_manifest["version"]:
        return version, new_manifest        # same version, no archive: the base is the kit itself, so only your own changes differ
    if not candidates:
        have = []   # type: List[str]
        for d in (os.path.join(source, ".claude", "migrate"), os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrate")):
            have += sorted(f for f in os.listdir(d) if f.endswith(".tar")) if os.path.isdir(d) else []
        raise MigrateError("no base archive for the installed version %s. The kit ships: %s. Pass --from-version if %s is a renamed copy of one of them"
                           % (version or "(unknown: .claude/KIT_VERSION is missing)", ", ".join(have) or "none", version or "it"))
    try:
        tree = km.read_tar_tree(candidates[0])
    except Exception as exc:   # OSError, ValueError, tarfile.TarError
        raise MigrateError("cannot read the base archive %s: %s" % (candidates[0], exc))
    label = os.path.basename(candidates[0])[1:-4] if not archive else (version or "base")
    base = km.tree_manifest(label, tree)
    base["_tree"] = tree
    return label, base


# ---------------------------------------------------------------------------------------------------------------- merging

def three_way(local: bytes, base: bytes, kit: bytes, labels: Tuple[str, str, str]) -> Tuple[bool, bytes, str]:
    """(clean, merged-or-conflict-marked content, reason). Uses `git merge-file`; binary content never merges."""
    if b"\0" in local or b"\0" in base or b"\0" in kit:
        return False, b"", "binary file"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"})
    tmp = tempfile.mkdtemp(prefix="kit-merge-")
    try:
        paths = []
        for name, data in (("local", local), ("base", base), ("kit", kit)):
            p = os.path.join(tmp, name)
            with open(p, "wb") as fh:
                fh.write(data)
            paths.append(p)
        try:
            r = subprocess.run(["git", "merge-file", "-p", "-L", labels[0], "-L", labels[1], "-L", labels[2]] + paths,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, b"", "git merge-file could not run: %s" % exc
        if r.returncode < 0 or r.returncode >= 127:
            return False, b"", "git merge-file failed: %s" % r.stderr.decode("utf-8", "replace")[:80]
        return r.returncode == 0, r.stdout, ("%d conflicting hunk(s)" % r.returncode) if r.returncode else ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------- planning

class Entry(object):
    def __init__(self, path: str, action: str, detail: str = "", content: Optional[bytes] = None, marked: Optional[bytes] = None) -> None:
        self.path = path
        self.action = action
        self.detail = detail
        self.content = content      # what --apply writes for ADD, UPDATE, MERGE, TAKE_KIT
        self.marked = marked        # the conflict-marked text for CONFLICT

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "action": self.action, "detail": self.detail}


def load_state(root: str) -> Dict[str, Any]:
    data, _ = read_local(root, STATE_REL)
    if not data:
        return {"resolved": {}}
    try:
        doc = json.loads(data.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("resolved"), dict):
            return doc
    except ValueError:
        pass
    return {"resolved": {}}


def plan(root: str, new_manifest: Dict[str, Any], new_files: Dict[str, bytes], base_label: str, base: Dict[str, Any],
         state: Dict[str, Any], take_kit: Sequence[str] = ()) -> List[Entry]:
    nf = new_manifest["files"]
    bf = base["files"]
    out = []   # type: List[Entry]
    for p in sorted(set(nf) | set(bf)):
        in_new, in_base = p in nf, p in bf
        local, problem = read_local(root, p)
        if problem:
            out.append(Entry(p, CONFLICT, "your path " + problem))
            continue
        L = sha(local) if local is not None else None
        N = nf[p]["sha256"] if in_new else None
        B = bf[p]["sha256"] if in_base else None
        cls = nf[p]["class"] if in_new else km.classify(p)
        if not in_new:                                         # the kit stopped shipping it
            if local is None:
                continue
            if cls != km.CLASS_KIT:
                out.append(Entry(p, KEEP, "yours"))
            elif L == B:
                out.append(Entry(p, REMOVE, "no longer shipped, and you never changed it"))
            else:
                out.append(Entry(p, KEEP_RETIRED, "no longer shipped, but you changed it: review it and delete it by hand if you do not need it"))
            continue
        kit = new_files[p]
        if cls == km.CLASS_FROZEN:
            out.append(Entry(p, ADD, "a guardrail file; added because it is absent", kit) if local is None else Entry(p, KEEP, "a guardrail file: yours, never changed"))
            continue
        if cls == km.CLASS_SEED:
            if local is None:
                out.append(Entry(p, SKIP_DELETED, "you deleted it") if in_base else Entry(p, ADD, "seed file, absent", kit))
            elif L == N:
                out.append(Entry(p, UPTODATE))
            elif in_base and L == B:
                out.append(Entry(p, UPDATE, "still the unedited template", kit))
            else:
                out.append(Entry(p, KEEP, "yours (the project owns it once installed)"))
            continue
        # kit-owned
        if local is None:
            out.append(Entry(p, SKIP_DELETED, "you deleted it%s" % ("; the kit changed it since" if N != B else "")) if in_base else Entry(p, ADD, "new in the kit", kit))
        elif L == N:
            out.append(Entry(p, UPTODATE))
        elif p in take_kit:
            out.append(Entry(p, TAKE_KIT, "you asked for the kit's version", kit))
        elif state["resolved"].get(p) == L:
            out.append(Entry(p, RESOLVED, "you resolved it by hand"))
        elif not in_base:
            out.append(Entry(p, CONFLICT, "exists in your project and is not the kit's file; the base has nothing to merge against", marked=local))
        elif L == B:
            out.append(Entry(p, UPDATE, "you never changed it", kit))
        elif N == B:
            out.append(Entry(p, KEEP, "you changed it; the kit did not"))
        else:
            btree = base.get("_tree", {})
            bdata = btree.get(p)
            if bdata is None:       # base is the new kit itself (same version): nothing to merge
                out.append(Entry(p, KEEP, "you changed it"))
                continue
            clean, merged, reason = three_way(local, bdata, kit, ("yours", "v" + base_label, "kit " + new_manifest["version"]))
            if clean:
                out.append(Entry(p, MERGE, "three-way merge, no conflicts", merged))
            else:
                out.append(Entry(p, CONFLICT, "you and the kit both changed it (%s)" % reason, marked=merged or None))
    return out


def notes_for(root: str, entries: Sequence[Entry], new_manifest: Dict[str, Any]) -> List[str]:
    notes = []   # type: List[str]
    by = {e.path: e for e in entries}
    if os.path.isdir(os.path.join(root, ".claude", "commands")) or any(e.path.startswith(".claude/commands/") for e in entries):
        notes.append("Modes are skills now (.claude/skills/<mode>/SKILL.md). Unmodified .claude/commands/ files are removed. A command you changed stays: "
                     "move your edits into the skill of the same name and delete the command file (with both present, which one runs is Claude Code's choice).")
    if ".claude/settings.v2.json" in by or os.path.isfile(os.path.join(root, ".claude", "settings.v2.json")):
        notes.append(".claude/settings.json is yours and was not touched. The v2 guards are staged in .claude/settings.v2.json; switching to them is a "
                     "human step: read docs/packaging.md (Activating the v2 guards), then merge the hooks block.")
    notes.append(".claude/loop.config.md is yours and was not touched. Keys added since your version use their defaults. "
                 "Run `python3 .claude/scripts/doctor.py` to see what the new version expects.")
    return notes


# ---------------------------------------------------------------------------------------------------------------- applying

def safe_dest(root: str, rel: str) -> str:
    full = os.path.join(root, rel)
    real_root = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(full))
    if parent != real_root and not parent.startswith(real_root + os.sep):
        raise MigrateError("%s would be written outside the project" % rel)
    return full


def write_atomic(full: str, data: bytes, exe: bool) -> None:
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".migrate-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o755 if exe else 0o644)
        os.replace(tmp, full)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


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


def apply(root: str, source: str, entries: Sequence[Entry], new_manifest: Dict[str, Any], exe: Dict[str, bool], now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup = os.path.join(root, WORK_DIR, "backup", stamp)
    wrote = []   # type: List[str]
    work = os.path.join(root, WORK_DIR)
    if os.path.islink(work) or os.path.islink(os.path.join(root, ".claude")):
        raise MigrateError("%s or .claude is a symlink; migrate will not write through it" % WORK_DIR)
    for e in entries:
        if e.action not in CHANGES and not (e.action == CONFLICT and e.marked is not None):
            continue
        full = safe_dest(root, e.path)
        if e.action in (UPDATE, MERGE, TAKE_KIT, REMOVE) and os.path.isfile(full):
            dest = os.path.join(backup, e.path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
        if e.action in (ADD, UPDATE, MERGE, TAKE_KIT):
            assert e.content is not None
            mode_exe = exe.get(e.path, False) if e.action != MERGE else (os.access(full, os.X_OK) if os.path.isfile(full) else exe.get(e.path, False))
            write_atomic(full, e.content, mode_exe)
            wrote.append(e.path)
        elif e.action == REMOVE:
            os.remove(full)
            wrote.append(e.path)
            d = os.path.dirname(full)
            while d != os.path.realpath(root) and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        elif e.action == CONFLICT and e.marked is not None:
            write_atomic(safe_dest(root, WORK_DIR + "/conflicts/" + e.path), e.marked, False)
    pending = [e.path for e in entries if e.action in (CONFLICT, KEEP_RETIRED)]
    finished = not [e for e in entries if e.action == CONFLICT]
    if finished:
        for rel in (km.VERSION_REL, km.MANIFEST_REL):
            with open(os.path.join(source, rel), "rb") as fh:
                write_atomic(safe_dest(root, rel), fh.read(), False)
    report = {"schema": SCHEMA, "applied": stamp, "to_version": new_manifest["version"], "finished": finished, "pending": pending,
              "actions": [e.to_dict() for e in entries if e.action not in (UPTODATE,)]}
    write_atomic(safe_dest(root, WORK_DIR + "/report.json"), (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"), False)
    return {"wrote": wrote, "finished": finished, "backup": os.path.relpath(backup, root) if os.path.isdir(backup) else None}


# ---------------------------------------------------------------------------------------------------------------- output

def counts(entries: Sequence[Entry]) -> Dict[str, int]:
    c = {}   # type: Dict[str, int]
    for e in entries:
        c[e.action] = c.get(e.action, 0) + 1
    return c


def render(doc: Dict[str, Any], entries: Sequence[Entry], notes: Sequence[str], applied: Optional[Dict[str, Any]]) -> str:
    lines = ["migrate: %s -> kit %s%s" % (doc["from_version"], doc["to_version"], "" if applied else "   (dry run: nothing was changed)")]
    lines.append("  project: %s" % doc["repo"])
    lines.append("  kit:     %s" % doc["source"])
    quiet = (UPTODATE, KEEP, SKIP_DELETED, RESOLVED)
    for act in (CONFLICT, MERGE, UPDATE, TAKE_KIT, ADD, REMOVE, KEEP_RETIRED):
        rows = [e for e in entries if e.action == act]
        if rows:
            lines.append("")
            lines.append("%s (%d)" % (act.upper(), len(rows)))
            for e in rows:
                lines.append("  %s%s" % (e.path, "   -- " + e.detail if e.detail else ""))
    c = counts(entries)
    lines.append("")
    lines.append("summary: " + ", ".join("%d %s" % (c[a], a) for a in sorted(c)) if c else "summary: nothing to do")
    lines.append("  (%s are left as they are)" % ", ".join(quiet))
    for n in notes:
        lines.append("note: " + n)
    conflicts = [e for e in entries if e.action == CONFLICT]
    if conflicts:
        lines.append("")
        lines.append("%d conflict(s). With --apply, nothing is written to those files. The merge result, with conflict markers, goes to "
                     ".claude/.migrate/conflicts/<path>. Resolve each one by hand in your file, then run:" % len(conflicts))
        lines.append("    python3 <kit>/.claude/scripts/migrate.py --repo %s --resolved <path>" % doc["repo"])
        lines.append("or take the kit's version with --take-kit <path> (your copy is backed up). KIT_VERSION moves only after the last one.")
    if applied:
        lines.append("")
        lines.append("applied: %d file(s) written or removed%s" % (len(applied["wrote"]), "; backups in " + applied["backup"] if applied["backup"] else ""))
        lines.append("finished: KIT_VERSION and KIT_MANIFEST updated" if applied["finished"] else "not finished: resolve the conflicts above, then run it again")
        lines.append("when you are happy: review `git diff`, commit, and delete .claude/.migrate/")
    elif any(e.action in CHANGES for e in entries):
        lines.append("")
        lines.append("to apply, from a plain terminal (not inside Claude Code): add --apply")
    return "\n".join(lines)


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


def check_paths(root: str, paths: Sequence[str]) -> List[str]:
    bad = [p for p in paths if p.startswith("/") or ".." in p.split("/") or p.endswith("/")]
    if bad:
        raise MigrateError("not a project-relative file path: %s" % bad[0])
    return [p.replace(os.sep, "/") for p in paths]


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--repo", default=".", help="the project to upgrade (default: the current directory)")
    ap.add_argument("--source", default=None, help="a kit checkout or dist/copy-install/ (default: the tree this script is in)")
    ap.add_argument("--from-version", default=None, help="treat the install as this version when .claude/KIT_VERSION is missing or renamed")
    ap.add_argument("--base-archive", default=None, help="use this tar as the base instead of the one the kit ships for your version")
    ap.add_argument("--apply", action="store_true", help="HUMAN-RUN: write the plan")
    ap.add_argument("--resolved", nargs="+", default=None, metavar="PATH", help="HUMAN-RUN: mark files you merged by hand")
    ap.add_argument("--take-kit", nargs="+", default=None, metavar="PATH", help="HUMAN-RUN: apply the plan, and replace these files by the kit's version (your copy is backed up)")
    ap.add_argument("--allow-dirty", action="store_true", help="apply even though the git work tree has uncommitted changes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    mutating = bool(args.apply or args.resolved or args.take_kit)
    try:
        if mutating:
            refusal = humanonly.refusal("migrate", env)
            if refusal:
                sys.stderr.write(refusal + "\n")
                return 3
        root = resolve_root(args.repo)
        if not os.path.isdir(os.path.join(root, ".claude")):
            raise MigrateError("%s has no .claude/ directory: there is nothing to migrate. To install the kit, copy dist/copy-install/ into the project, "
                               "or install the plugin (docs/packaging.md)" % root)
        default_source = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        source = os.path.realpath(args.source or default_source)
        new_manifest, new_files, exe = load_source(source)
        label, base = choose_base(root, source, new_manifest, args.from_version, args.base_archive)
        state = load_state(root)
        take = check_paths(root, args.take_kit or [])
        entries = plan(root, new_manifest, new_files, label, base, state, take)
        by_path = {e.path: e for e in entries}
        for p in take:
            if p not in by_path or by_path[p].action != TAKE_KIT:
                raise MigrateError("--take-kit %s: not a kit file that differs from yours" % p)
        notes = notes_for(root, entries, new_manifest)
        doc = {"schema": SCHEMA, "repo": root, "source": source, "from_version": label, "to_version": new_manifest["version"],
               "counts": counts(entries), "actions": [e.to_dict() for e in entries if e.action != UPTODATE], "notes": notes, "applied": False}
        applied = None
        if args.resolved:
            for p in check_paths(root, args.resolved):
                data, problem = read_local(root, p)
                if data is None or problem:
                    raise MigrateError("--resolved %s: no such file in the project" % p)
                if p not in new_manifest["files"] or new_manifest["files"][p]["class"] != km.CLASS_KIT:
                    raise MigrateError("--resolved %s: not a kit-owned file the kit ships" % p)
                state["resolved"][p] = sha(data)
            write_atomic(safe_dest(root, STATE_REL), (json.dumps(state, indent=1, sort_keys=True) + "\n").encode("utf-8"), False)
            print("recorded %d resolved file(s) in %s; run the dry run again" % (len(args.resolved), STATE_REL))
            return 0
        if args.apply or take:
            continuing = False
            rep, _ = read_local(root, WORK_DIR + "/report.json")
            if rep:
                try:
                    continuing = json.loads(rep.decode("utf-8")).get("finished") is False
                except ValueError:
                    pass
            dirty = None if (args.allow_dirty or continuing) else dirty_tree(root)
            if dirty:
                raise MigrateError("the git work tree has uncommitted changes (%s). Commit or stash them first, so `git checkout` is your undo; "
                                   "or pass --allow-dirty" % dirty)
            applied = apply(root, source, entries, new_manifest, exe)
            doc["applied"] = True
            doc["finished"] = applied["finished"]
        if args.json:
            print(json.dumps(doc, indent=1, sort_keys=True))
        else:
            print(render(doc, entries, notes, applied))
        return 1 if any(e.action == CONFLICT for e in entries) else 0
    except MigrateError as exc:
        sys.stderr.write("migrate: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
