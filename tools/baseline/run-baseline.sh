#!/bin/sh
# Prepare one baseline run of V1 (tag v1.2.0) on the hello-loop fixture. HUMAN-RUN ONLY (IMP-2).
#
#   tools/baseline/run-baseline.sh run-1 [--dest-root DIR] [--dry-run]
#
# It clones the tag into a directory outside this repo, overlays the fixture without its answer
# key, installs the observation hook and the sandbox setting, and prints the exact `claude`
# command. It never runs claude. It refuses when CLAUDECODE is set (a Claude Code session) or
# when `claude` is not on PATH (KIT_CLAUDE_BIN overrides the name). Exit codes: 3 refused
# (CLAUDECODE or a precondition), 4 claude absent, 2 usage.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if [ -n "${CLAUDECODE:-}" ]; then
    echo "run-baseline: refusing to run inside a Claude Code session (CLAUDECODE is set)." >&2
    echo "run-baseline: open a normal terminal and run this there." >&2
    exit 3
fi

claude_bin="${KIT_CLAUDE_BIN:-claude}"
if ! command -v "$claude_bin" >/dev/null 2>&1; then
    echo "run-baseline: '$claude_bin' not found on PATH; install Claude Code first (this script only checks, it never runs it)." >&2
    exit 4
fi

exec python3 "$here/prepare.py" --claude-bin "$claude_bin" "$@"
