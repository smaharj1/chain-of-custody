#!/usr/bin/env python3
"""Offline structural validation of the plugin (the repository root) and .claude-plugin/marketplace.json (IMP-3): the stdlib stand-in for `claude plugin validate --strict`.

    python3 tools/validate-plugin.py [--repo PATH] [--strict]

Checks: plugin.json (name, version, userConfig), hooks/hooks.json (known events, timeouts, every script exists, no worktree hooks),
agents (frontmatter, no `Bash(...)` in disallowedTools, no ignored keys), skills (name = directory, no skill named `loop` or `code-review`,
description, links), every ${CLAUDE_PLUGIN_ROOT}/ path in kit prose exists, no kit path left as a project `.claude/` path, no junk or
runtime files, the marketplace entry points at ./. It reads the files git tracks or would track (untracked files that are not ignored),
so a local cache or scratch file does not count. `--strict` is accepted for symmetry with the real command; every check here is strict.

Passing is necessary, not sufficient: running `claude plugin validate . --strict` is a human step (tools/validate-plugin-real.sh).
Exit 0 when sound, 1 with the problems listed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pluginlib import validate as V  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tree(root: str):
    """{path: bytes} of what the plugin ships: the files git tracks or would track. Not a git checkout: every file under root."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        rels = [r for r in out.decode("utf-8", "replace").split("\0") if r]
    except (OSError, subprocess.CalledProcessError):
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            rels.extend(os.path.relpath(os.path.join(dirpath, fn), root) for fn in filenames)
    files = {}
    for rel in rels:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                files[rel.replace(os.sep, "/")] = fh.read()
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    files = load_tree(args.repo)
    mk = os.path.join(args.repo, ".claude-plugin", "marketplace.json")
    market = None
    if os.path.isfile(mk):
        with open(mk, "rb") as fh:
            market = fh.read()
    problems = V.validate(files, market)
    for p in problems:
        print(p)
    if problems:
        sys.stderr.write("validate-plugin: %d problem(s)\n" % len(problems))
        return 1
    print("validate-plugin: ok (%d plugin files)" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
