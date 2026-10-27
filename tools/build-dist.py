#!/usr/bin/env python3
"""Generate plugin/**, .claude-plugin/marketplace.json, dist/copy-install/** and .claude/KIT_MANIFEST from the tracked files (IMP-3).

    python3 tools/build-dist.py --write    regenerate every output (stale files under the output roots are removed)
    python3 tools/build-dist.py --check    exit 1 if any output differs from what the build produces (CI and the drift test run this)
    python3 tools/build-dist.py --list     print the output file count per root

`.claude/` is the source and is never written, except the generated `.claude/KIT_MANIFEST`. The build reads `git ls-files`, so untracked
files (a worktree, `settings.local.json`, runtime files) can not leak into an output: `git add` a new source file before you build.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from distlib import build as B  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--repo", default=B.REPO)
    args = ap.parse_args(argv)
    try:
        d = B.build(args.repo)
    except B.BuildError as exc:
        sys.stderr.write("build-dist: %s\n" % exc)
        return 2
    out = d.outputs()
    if args.list:
        for root in B.OWNED_ROOTS:
            print("%-22s %d files" % (root, sum(1 for p in out.files if p.startswith(root))))
        print("version %s, %d docs shipped" % (d.version, len(d.docs)))
        return 0
    if args.write:
        w, r = B.write(args.repo, d)
        print("build-dist: wrote %d file(s), removed %d stale file(s); %d outputs in total" % (w, r, len(out.files)))
        return 0
    problems = B.diff(args.repo, d)
    if problems:
        for p in problems[:50]:
            print(p)
        if len(problems) > 50:
            print("... and %d more" % (len(problems) - 50))
        sys.stderr.write("build-dist: outputs are out of date (run `python3 tools/build-dist.py --write`, then commit them)\n")
        return 1
    print("build-dist: %d generated files match the sources" % len(out.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
