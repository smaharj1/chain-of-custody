#!/usr/bin/env python3
"""Generate `.claude/loop.config.md` and `examples/loop.config.md` from the key registry (IMP-11).

    python3 tools/gen-config.py --check    exit 1 if a committed file differs from the generated one
    python3 tools/gen-config.py --write    regenerate both files in place
    python3 tools/gen-config.py --stdout [--example]   print one generated file

Source of truth: `.claude/scripts/kitlib/schema/*.json` plus the hand-written templates in
`.claude/scripts/kitlib/schema/templates/`. Never edit the generated files by hand.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, ".claude", "scripts"))

from kitlib import configgen, jsonio, registry  # noqa: E402

TARGETS = (
    (os.path.join(".claude", "loop.config.md"), False),
    (os.path.join("examples", "loop.config.md"), True),
)


def generate(repo: str = REPO):
    reg = registry.load_registry()
    return [(rel, configgen.render(reg, example=ex)) for rel, ex in TARGETS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--stdout", action="store_true")
    ap.add_argument("--example", action="store_true", help="with --stdout: print examples/loop.config.md")
    ap.add_argument("--repo", default=REPO, help="repository root to read and write (default: this repo)")
    args = ap.parse_args(argv)
    try:
        outputs = generate(args.repo)
    except registry.RegistryError as exc:
        print("gen-config: registry error: %s" % exc, file=sys.stderr)
        return 2
    if args.stdout:
        sys.stdout.write(outputs[1][1] if args.example else outputs[0][1])
        return 0
    bad = 0
    for rel, text in outputs:
        path = os.path.join(args.repo, rel)
        current = open(path, "r", encoding="utf-8").read() if os.path.exists(path) else ""
        if args.write:
            if current != text:
                jsonio.atomic_write_text(path, text)
                print("wrote %s" % rel)
            else:
                print("unchanged %s" % rel)
        elif current != text:
            bad += 1
            print("OUT OF SYNC: %s (run: python3 tools/gen-config.py --write)" % rel, file=sys.stderr)
            diff = difflib.unified_diff(current.splitlines(), text.splitlines(), "committed/" + rel, "generated/" + rel, lineterm="", n=1)
            for i, line in enumerate(diff):
                if i > 40:
                    print("  ...", file=sys.stderr)
                    break
                print(line, file=sys.stderr)
    if args.check and bad == 0:
        print("loop.config templates are in sync with the registry")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
