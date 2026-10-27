#!/bin/sh
# Read the GitHub-side boundary settings (GET calls only), record them as a fixture, and run
# the doctor on it. HUMAN-RUN ONLY, from a plain terminal. Dry run by default; add --run to read.
#
#   verify-boundary.sh --repo OWNER/NAME [--run] [--out FILE]
#
# Refuses to run when CLAUDECODE is set (inside a Claude Code session). Thin wrapper: the logic is in boundary.py.
if [ -n "${CLAUDECODE:-}" ]; then
  echo "verify-boundary: refusing to run inside a Claude Code session (CLAUDECODE is set). Run it from a plain terminal." >&2
  exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$here/boundary.py" verify "$@"
