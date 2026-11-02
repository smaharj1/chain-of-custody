"""Build the plugin, the marketplace file, the copy install and KIT_MANIFEST in memory from the tracked files (IMP-3).

`build(repo)` is pure given the tracked files: it reads `git ls-files`, never walks the disk, so a stray file, a worktree under
`.claude/worktrees/`, a `settings.local.json` or a runtime file can not reach `plugin/` or `dist/`. `write`/`check` compare or
update the four output roots.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, ".claude", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from kitlib import kitmanifest  # noqa: E402
from guards import wiring  # noqa: E402

from . import rewrite  # noqa: E402

PLUGIN_NAME = "chain-of-custody"
OWNER_NAME = "Sujil Maharjan"
HOMEPAGE = "https://github.com/smaharj1/chain-of-custody"
LICENSE_ID = "MIT"
DESCRIPTION = ("A role-based engineering workflow for Claude Code: a product manager, architect and tech lead mode, engineer and "
               "reviewer subagents, hook-enforced guards, and an optional plan-driven build loop.")
KEYWORDS = ["workflow", "agents", "code-review", "guards", "planning"]

# Output roots. Anything under these that the build does not produce is stale (and a leak risk), so --check fails and --write removes it.
PLUGIN_DIR = "plugin"
DIST_DIR = "dist/copy-install"
MARKETPLACE = ".claude-plugin/marketplace.json"
OWNED_ROOTS = (PLUGIN_DIR + "/", DIST_DIR + "/", ".claude-plugin/")
OWNED_FILES = (kitmanifest.MANIFEST_REL,)

ADAPTERS = ("AGENTS.md", ".cursor/rules/chain-of-custody.mdc", "skills/chain-of-custody/SKILL.md")

# What never ships, even if somebody tracked it by mistake (defence in depth: the build reads tracked files only).
NEVER_PREFIXES = (".claude/commands/", ".claude/worktrees/", ".claude/bootstrap/", ".claude/pr/")
NEVER_FILES = {".claude/settings.local.json", ".claude/loop.jsonl", ".claude/metrics.jsonl", ".claude/orchestrate.lock",
               ".claude/bootstrap-receipt.json", ".claude/bootstrap-receipt.json.lock", ".claude/launch.json"}

# where each .claude/ area goes in the plugin; None = not part of the plugin
PLUGIN_DIRS = {"agents": "agents", "skills": "skills", "scripts": "scripts", "spec": "spec", "templates": "templates", "context": "context"}
NOT_IN_PLUGIN = {"hooks"}                                  # v1 shell hooks (the plugin has hooks.json)
SOURCE_ONLY_PREFIXES = (".claude/migrate/",)               # base archives for migrate.py: they ship with the kit checkout, never into a project
NOT_IN_PLUGIN_FILES = {".claude/settings.json", ".claude/settings.v2.json", kitmanifest.MANIFEST_REL}   # the project owns settings
SEED_FILES = {".claude/CLAUDE.md", ".claude/loop.config.md"}   # copied into the project by /init; not rewritten (they are the project's)

USER_CONFIG = {
    "protected_branches": {"type": "string", "title": "Protected branches",
                           "description": "Comma-separated branches that are shared: no edits to app source while checked out, no push. "
                                          "main and master are always protected. /init writes this into .claude/loop.config.md.",
                           "default": "main,master"},
    "integration_branch": {"type": "string", "title": "Integration branch",
                           "description": "The loop's integration branch, if you use one. Protected like main. Empty means none.",
                           "default": ""},
    "main_edit_allow": {"type": "string", "title": "Extra editable paths on main",
                        "description": "Comma-separated path globs that may be edited while on a protected branch. Empty means only the built-in set.",
                        "default": ""},
}


class BuildError(Exception):
    pass


class Tree(object):
    """{path: bytes} plus the set of paths that carry the executable bit."""

    def __init__(self) -> None:
        self.files = {}    # type: Dict[str, bytes]
        self.exe = set()   # type: Set[str]

    def put(self, rel: str, data: bytes, exe: bool = False) -> None:
        if rel in self.files and self.files[rel] != data:
            raise BuildError("two sources for %s with different content" % rel)
        self.files[rel] = data
        if exe:
            self.exe.add(rel)


class Dist(object):
    def __init__(self) -> None:
        self.plugin = Tree()
        self.copy = Tree()
        self.extra = Tree()          # .claude-plugin/marketplace.json and .claude/KIT_MANIFEST
        self.version = ""
        self.docs = set()            # type: Set[str]

    def outputs(self) -> Tree:
        """Every output file keyed by its repo-relative path."""
        t = Tree()
        for rel, data in self.plugin.files.items():
            t.put(PLUGIN_DIR + "/" + rel, data, rel in self.plugin.exe)
        for rel, data in self.copy.files.items():
            t.put(DIST_DIR + "/" + rel, data, rel in self.copy.exe)
        for rel, data in self.extra.files.items():
            t.put(rel, data, rel in self.extra.exe)
        return t


# ------------------------------------------------------------------------------------------------------------ reading

def tracked_files(repo: str) -> List[Tuple[str, str]]:
    """[(path, mode)] from `git ls-files -s`, in path order."""
    out = subprocess.run(["git", "-C", repo, "ls-files", "-s", "-z"], stdout=subprocess.PIPE, check=True).stdout.decode("utf-8")
    rows = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        rows.append((path, meta.split()[0]))
    return sorted(rows)


def junk(rel: str) -> bool:
    parts = rel.split("/")
    base = parts[-1]
    return ("__pycache__" in parts or base == ".DS_Store" or base.endswith((".pyc", ".swp", ".lock", "~")) or rel in NEVER_FILES
            or rel.startswith(NEVER_PREFIXES))


def read_sources(repo: str, rows: Iterable[Tuple[str, str]]) -> Tuple[Dict[str, bytes], Set[str]]:
    """Contents of the tracked files that exist on disk. Refuses a tracked symlink in a shipped area."""
    data = {}   # type: Dict[str, bytes]
    exe = set()   # type: Set[str]
    for rel, mode in rows:
        full = os.path.join(repo, rel)
        if mode == "120000":
            if rel.startswith((".claude/", "docs/", "evals/")) or rel in ADAPTERS:
                raise BuildError("%s is a tracked symlink in a shipped area; the build refuses links" % rel)
            continue
        if not os.path.isfile(full):
            continue   # tracked but deleted in the working tree
        with open(full, "rb") as fh:
            data[rel] = fh.read()
        if mode == "100755":
            exe.add(rel)
    return data, exe


def kit_version(src: Dict[str, bytes]) -> str:
    raw = src.get(kitmanifest.VERSION_REL)
    if raw is None:
        raise BuildError(".claude/KIT_VERSION is missing")
    v = kitmanifest.parse_version(raw.decode("utf-8", "replace"))
    if not v:
        raise BuildError(".claude/KIT_VERSION does not start with a version")
    return v


_DOC_REF = re.compile(r"(?<![A-Za-z0-9_./-])(docs/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md)")
_REL_LINK = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
# Docs for maintainers of the kit, not for users of an install: never shipped.
MAINTAINER_DOC_PREFIXES = ("docs/design/", "docs/baseline/")


def docs_closure(shipped: Dict[str, bytes], docs: Dict[str, bytes]) -> Set[str]:
    """The docs the shipped files point at (a `docs/x.md` token, or a relative link from another doc), and the docs those point at.
    `docs` maps the shippable docs to their content."""
    seen = set()   # type: Set[str]
    queue = [("", data) for rel, data in shipped.items() if not rel.endswith((".tar", ".png", ".svg"))]
    while queue:
        origin, raw = queue.pop()
        text = raw.decode("utf-8", "replace")
        found = [m.group(1) for m in _DOC_REF.finditer(text)]
        if origin:
            for m in _REL_LINK.finditer(text):
                target = posixpath.normpath(posixpath.join(posixpath.dirname(origin), m.group(1)))
                found.append(target)
        for d in found:
            if d in docs and d not in seen:
                seen.add(d)
                queue.append((d, docs[d]))
    return seen


# ------------------------------------------------------------------------------------------------------------ building

def plugin_target(rel: str) -> Optional[str]:
    """Plugin path for a shipped `.claude/` file, or None when it is not part of the plugin."""
    if rel in NOT_IN_PLUGIN_FILES:
        return None
    if rel == kitmanifest.VERSION_REL:
        return "KIT_VERSION"
    if rel in SEED_FILES:
        return "seed/" + rel
    parts = rel.split("/")
    if len(parts) < 3:
        raise BuildError("%s: unmapped top-level .claude file; add it to the build's plugin map or to NOT_IN_PLUGIN_FILES" % rel)
    area = parts[1]
    if area in NOT_IN_PLUGIN:
        return None
    if area == "context":
        name = parts[2]
        if len(parts) == 3 and name.endswith(".md") and name[:-3] in kitmanifest.PRIMERS:
            return "seed/" + rel
        return rel[len(".claude/"):]
    if area in PLUGIN_DIRS:
        return PLUGIN_DIRS[area] + "/" + "/".join(parts[2:])
    raise BuildError("%s: unmapped .claude area %r; add it to PLUGIN_DIRS or NOT_IN_PLUGIN" % (rel, area))


def build(repo: str = REPO, rows: Optional[List[Tuple[str, str]]] = None, sources: Optional[Tuple[Dict[str, bytes], Set[str]]] = None) -> Dist:
    rows = rows if rows is not None else tracked_files(repo)
    src, src_exe = sources if sources is not None else read_sources(repo, rows)
    d = Dist()
    d.version = kit_version(src)

    # --- the copy install: shipped .claude/, adapters, docs closure
    shipped = {}   # type: Dict[str, bytes]
    for rel in sorted(src):
        if rel.startswith(".claude/") and not junk(rel) and rel != kitmanifest.MANIFEST_REL and not rel.startswith(SOURCE_ONLY_PREFIXES):
            shipped[rel] = src[rel]
    for rel in ADAPTERS:
        if rel not in src:
            raise BuildError("adapter %s is not tracked" % rel)
        shipped[rel] = src[rel]
    all_docs = {p: src[p] for p in src if p.startswith("docs/") and p.endswith(".md") and not p.startswith(MAINTAINER_DOC_PREFIXES)}
    d.docs = docs_closure(shipped, all_docs)
    for p in d.docs:
        shipped[p] = src[p]
    manifest = kitmanifest.build_manifest(d.version, shipped)
    manifest_bytes = kitmanifest.dumps(manifest).encode("utf-8")
    for rel, data in shipped.items():
        d.copy.put(rel, data, rel in src_exe)
    d.copy.put(kitmanifest.MANIFEST_REL, manifest_bytes)
    d.extra.put(kitmanifest.MANIFEST_REL, manifest_bytes)

    # --- the plugin
    for rel, data in shipped.items():
        if not rel.startswith(".claude/"):
            continue
        target = plugin_target(rel)
        if target is None:
            continue
        if target.startswith("seed/"):
            d.plugin.put(target, data, rel in src_exe)
            continue
        if rel.endswith((".md", ".mdc")):
            data = rewrite.rewrite_text(data.decode("utf-8"), kitmanifest.PRIMERS, d.docs).encode("utf-8")
        d.plugin.put(target, data, rel in src_exe)
    for p in sorted(d.docs):
        text = rewrite.rewrite_text(src[p].decode("utf-8"), kitmanifest.PRIMERS, d.docs)
        d.plugin.put(p, text.encode("utf-8"), False)
    for rel in sorted(src):
        if rel.startswith("evals/") and not junk(rel):
            d.plugin.put(rel, src[rel], rel in src_exe)
    hooks = wiring.build_plugin_hooks(wiring.load_manifest(os.path.join(repo, ".claude", "scripts", "guards", "hooks.manifest.json")))
    d.plugin.put("hooks/hooks.json", wiring.dumps(hooks).encode("utf-8"))
    d.plugin.put(".claude-plugin/plugin.json", dumps(plugin_json(d.version)).encode("utf-8"))

    # --- the marketplace
    d.extra.put(MARKETPLACE, dumps(marketplace_json(d.version)).encode("utf-8"))
    return d


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def plugin_json(version: str) -> Dict:
    return {"name": PLUGIN_NAME, "version": version, "description": DESCRIPTION, "author": {"name": OWNER_NAME},
            "homepage": HOMEPAGE, "repository": HOMEPAGE, "license": LICENSE_ID, "keywords": list(KEYWORDS),
            "userConfig": json.loads(json.dumps(USER_CONFIG))}


def marketplace_json(version: str) -> Dict:
    return {"name": PLUGIN_NAME, "owner": {"name": OWNER_NAME},
            "metadata": {"description": "The Chain of Custody kit, as a Claude Code plugin."},
            "plugins": [{"name": PLUGIN_NAME, "source": "./" + PLUGIN_DIR, "description": DESCRIPTION, "version": version,
                         "license": LICENSE_ID, "keywords": list(KEYWORDS)}]}


# ------------------------------------------------------------------------------------------------------------ disk

def disk_files(repo: str) -> Dict[str, bool]:
    """{path: executable} for every file under the output roots (plus the manifest file), junk ignored."""
    found = {}   # type: Dict[str, bool]
    for root in OWNED_ROOTS:
        base = os.path.join(repo, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in filenames:
                if fn == ".DS_Store" or fn.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fn)
                found[os.path.relpath(full, repo).replace(os.sep, "/")] = bool(os.stat(full).st_mode & 0o111)
    for rel in OWNED_FILES:
        full = os.path.join(repo, rel)
        if os.path.isfile(full):
            found[rel] = bool(os.stat(full).st_mode & 0o111)
    return found


def diff(repo: str, d: Dist) -> List[str]:
    """Human-readable differences between the build and the disk. Empty means no drift."""
    out = d.outputs()
    have = disk_files(repo)
    problems = []   # type: List[str]
    for rel in sorted(out.files):
        if rel not in have:
            problems.append("missing: %s" % rel)
            continue
        with open(os.path.join(repo, rel), "rb") as fh:
            cur = fh.read()
        if cur != out.files[rel]:
            problems.append("differs: %s" % rel)
        elif (rel in out.exe) != have[rel]:
            problems.append("mode differs: %s" % rel)
    for rel in sorted(have):
        if rel not in out.files:
            problems.append("stale (not generated): %s" % rel)
    return problems


def write(repo: str, d: Dist) -> Tuple[int, int]:
    """Write every output, remove stale files under the output roots. Returns (written, removed)."""
    out = d.outputs()
    have = disk_files(repo)
    written = removed = 0
    for rel, data in sorted(out.files.items()):
        full = os.path.join(repo, rel)
        cur = None
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                cur = fh.read()
        if cur != data:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp-build"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, full)
            written += 1
        want = 0o755 if rel in out.exe else 0o644
        if (os.stat(full).st_mode & 0o777) != want:
            os.chmod(full, want)
    for rel in sorted(have):
        if rel not in out.files and rel.startswith(OWNED_ROOTS):    # never delete outside the roots (the manifest file is always generated)
            os.remove(os.path.join(repo, rel))
            removed += 1
    for root in OWNED_ROOTS:
        base = os.path.join(repo, root)
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            if dirpath != base.rstrip("/") and not os.listdir(dirpath):
                os.rmdir(dirpath)
    return written, removed
