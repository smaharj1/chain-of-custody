#!/bin/sh
# Apply the GitHub-side boundary: rulesets (no bot bypass), tag protection, squash setting,
# Actions permissions. HUMAN-RUN ONLY, as a repo admin, from a plain terminal.
# Dry run by default: prints every gh api call and sends nothing. Add --apply to send them.
#
#   apply-boundary.sh --repo OWNER/NAME --variant solo|team [--apply]
#
# Refuses to run when CLAUDECODE is set (inside a Claude Code session). Thin wrapper: the logic is in boundary.py.
if [ -n "${CLAUDECODE:-}" ]; then
  echo "apply-boundary: refusing to run inside a Claude Code session (CLAUDECODE is set). Run it from a plain terminal." >&2
  exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$here/boundary.py" apply "$@"
