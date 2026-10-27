#!/usr/bin/env python3
"""Offline structural validation of plugin/ and .claude-plugin/marketplace.json (IMP-3): the stdlib stand-in for `claude plugin validate --strict`.

    python3 tools/validate-plugin.py [--repo PATH] [--strict]

Checks: plugin.json (name, version, userConfig), hooks/hooks.json (known events, timeouts, every script exists, no worktree hooks),
agents (frontmatter, no `Bash(...)` in disallowedTools, no ignored keys), skills (name = directory, no skill named `loop` or `code-review`,
description, links), every ${CLAUDE_PLUGIN_ROOT}/ path in prose exists, no kit path left unrewritten, no junk or runtime files, the
marketplace entry points at ./plugin. `--strict` is accepted for symmetry with the real command; every check here is strict.

Passing is necessary, not sufficient: running `claude plugin validate ./plugin --strict` is a human step (tools/validate-plugin-real.sh).
Exit 0 when sound, 1 with the problems listed.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from distlib import validate as V  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tree(root: str):
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            with open(full, "rb") as fh:
                files[os.path.relpath(full, root).replace(os.sep, "/")] = fh.read()
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    plugin_root = os.path.join(args.repo, "plugin")
    files = load_tree(plugin_root) if os.path.isdir(plugin_root) else {}
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
