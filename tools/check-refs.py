#!/usr/bin/env python3
"""Check that repo paths mentioned in tracked markdown exist (IMP-10).

For every tracked `.md` / `.mdc` file this checks:
  * inline-code spans (`like this`): each whitespace-separated token that starts with a
    repo path prefix (`.claude/`, `docs/`, `tools/`, `examples/`, `tests/`, `skills/`,
    `.github/`, `.cursor/`, `changelog.d/`) must exist relative to the repo root
  * markdown links `[text](target)`: a relative target must exist relative to the file

Skipped: fenced code blocks, URLs and `#anchors`, and tokens that are patterns, not paths
(`<slug>`, `*`, `{a,b}`, `$VAR`, `...`).

Excluded files (they cite paths on purpose): `.claude/worktrees/`, `docs/design/review-*.md`
(the report cites v1 line numbers and future paths), `docs/design/v2-implementation-decisions.md`,
and `changelog.d/*.md` (fragments name files of their own branch).

`tools/check-refs.allow` lists paths that are intentionally absent: future files, placeholders,
or files that live in a user's project. One fnmatch pattern per line; `#` starts a comment.

Usage: python3 tools/check-refs.py [--repo PATH] [--list-unused-allow]
Exit 0 if every reference resolves, 1 otherwise.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from typing import Iterable, List, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PREFIXES = (".claude/", "docs/", "tools/", "examples/", "tests/", "skills/", ".github/", ".cursor/", "changelog.d/")
EXCLUDE_GLOBS = (
    ".claude/worktrees/*",
    "docs/design/review-*.md",
    "docs/design/v2-implementation-decisions.md",
    "changelog.d/*.md",
)
MD_SUFFIXES = (".md", ".mdc")

_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_PATTERN_CHARS = re.compile(r"[<>*{}$|\[\]…]|\.\.\.")
_TRAIL_RE = re.compile(r"[.,;:!?)\]'\"]+$")
_LINENO_RE = re.compile(r"(?::\d+(?:-\d+)?)+$")


def tracked_files(repo: str) -> List[str]:
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z"], stdout=subprocess.PIPE, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in EXCLUDE_GLOBS)


def load_allow(path: str) -> List[str]:
    pats = []  # type: List[str]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    pats.append(line)
    return pats


def iter_refs(text: str) -> Iterable[Tuple[int, str, str]]:
    """Yield (line number, kind, reference) for code-span paths and links, skipping fences."""
    in_fence = None  # type: str
    for n, line in enumerate(text.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if in_fence is None:
                in_fence = marker
            elif marker == in_fence:
                in_fence = None
            continue
        if in_fence is not None:
            continue
        for m in _LINK_RE.finditer(line):
            yield n, "link", m.group(1)
        for m in _CODE_SPAN_RE.finditer(line):
            for tok in re.split(r"[\s,;]+", m.group(2)):
                yield n, "code", tok


def normalize_code_token(tok: str) -> str:
    tok = tok.strip("\"'(")
    if tok.startswith("./"):
        tok = tok[2:]
    tok = _TRAIL_RE.sub("", tok)
    tok = _LINENO_RE.sub("", tok)
    tok = tok.split("#", 1)[0]
    return tok


def check(repo: str, allow: List[str]) -> Tuple[List[str], Set[str]]:
    problems = []  # type: List[str]
    used = set()  # type: Set[str]
    for rel in tracked_files(repo):
        if not rel.endswith(MD_SUFFIXES) or excluded(rel):
            continue
        with open(os.path.join(repo, rel), "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for lineno, kind, ref in iter_refs(text):
            if kind == "code":
                path = normalize_code_token(ref)
                if not path.startswith(PREFIXES) or _PATTERN_CHARS.search(path):
                    continue
                target = os.path.join(repo, path)
                shown = path
            else:
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref) or ref.startswith("#") or ref.startswith("//"):
                    continue
                path = ref.split("#", 1)[0].split("?", 1)[0]
                if not path or _PATTERN_CHARS.search(path):
                    continue
                target = os.path.normpath(os.path.join(repo, os.path.dirname(rel), path))
                shown = os.path.relpath(target, repo)
            if os.path.exists(target):
                continue
            hit = [p for p in allow if fnmatch.fnmatch(shown.rstrip("/"), p) or fnmatch.fnmatch(shown, p)]
            if hit:
                used.update(hit)
                continue
            problems.append("%s:%d: %s reference to missing path %s" % (rel, lineno, kind, shown))
    return problems, used


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check repo paths mentioned in tracked markdown.")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--allow", default=None, help="allowlist file (default: tools/check-refs.allow in --repo)")
    ap.add_argument("--list-unused-allow", action="store_true", help="also report allowlist entries nothing needed")
    args = ap.parse_args(argv)
    allow_path = args.allow or os.path.join(args.repo, "tools", "check-refs.allow")
    allow = load_allow(allow_path)
    problems, used = check(args.repo, allow)
    for p in problems:
        print(p)
    if args.list_unused_allow:
        for pat in allow:
            if pat not in used:
                print("unused allowlist entry: %s" % pat)
    if problems:
        print("\n%d missing reference(s). Fix the path, or add it to tools/check-refs.allow with a comment." % len(problems),
              file=sys.stderr)
        return 1
    print("check-refs: all references resolve (%d allowlisted pattern(s))" % len(allow))
    return 0


if __name__ == "__main__":
    sys.exit(main())
