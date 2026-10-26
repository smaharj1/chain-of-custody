#!/bin/sh
# Prepare a scratch repo for one platform probe (IMP-7). HUMAN-RUN ONLY.
#
#   tools/probes/make-scratch.sh <scratch-dir> --probe V1 [--variant NAME] [--control] [--dry-run]
#
# It creates a git repo outside this one, wires tools/baseline/observe.py as a hook-input logger on
# every hook event, merges the probe's settings snippet and prints what to do next. It never runs
# claude. It refuses when CLAUDECODE is set (a Claude Code session). Exit codes: 3 refused, 2 usage.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if [ -n "${CLAUDECODE:-}" ]; then
    echo "make-scratch: refusing to run inside a Claude Code session (CLAUDECODE is set)." >&2
    echo "make-scratch: open a normal terminal and run this there." >&2
    exit 3
fi

exec python3 "$here/make_scratch.py" "$@"
