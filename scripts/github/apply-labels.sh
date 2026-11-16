#!/bin/sh
# Create the factory labels (factory:feature, factory:bug, factory:go, ...). HUMAN-RUN ONLY, from a plain terminal.
# Dry run by default: prints every gh call and sends nothing. Add --apply to send them.
#
#   apply-labels.sh --repo OWNER/NAME [--labels-file FILE] [--apply]
#
# Refuses to run when CLAUDECODE is set (inside a Claude Code session). Thin wrapper: the logic is in labels.py.
if [ -n "${CLAUDECODE+x}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT+x}" ]; then
  echo "apply-labels: refusing to run inside a Claude Code session (CLAUDECODE is set). Run it from a plain terminal." >&2
  exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$here/labels.py" apply "$@"
