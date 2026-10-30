#!/bin/sh
# HUMAN-RUN. Runs Claude Code's own validator on the plugin (the repository root) and its marketplace file.
# tools/validate-plugin.py is the offline stand-in; this is the real thing. It starts the real `claude` binary, so an agent session
# must not run it. From a plain terminal, in the repository root:
#
#     sh tools/validate-plugin-real.sh
#
# Expected: every command exits 0 with no errors or warnings.
set -eu
cd "$(dirname "$0")/.."
python3 tools/validate-plugin.py --strict
echo "--- claude plugin validate . --strict"
claude plugin validate . --strict
echo "--- To try the plugin locally: claude --plugin-dir .   (start a session, read the canary line, then run /chain-of-custody:pm)"
