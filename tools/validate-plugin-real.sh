#!/bin/sh
# HUMAN-RUN. Runs Claude Code's own validator on the generated plugin and the marketplace file (Phase 9, pending-human).
# tools/validate-plugin.py is the offline stand-in; this is the real thing. It starts the real `claude` binary, so an agent session
# must not run it (the guards deny it). From a plain terminal, in the repository root:
#
#     sh tools/validate-plugin-real.sh
#
# Expected: both commands exit 0 with no errors or warnings. Paste anything else into the Phase 9 review.
set -eu
cd "$(dirname "$0")/.."
python3 tools/build-dist.py --check
python3 tools/validate-plugin.py --strict
echo "--- claude plugin validate ./plugin --strict"
claude plugin validate ./plugin --strict
echo "--- claude plugin validate . --strict   (the marketplace file)"
claude plugin validate . --strict
echo "--- To try the plugin locally: claude --plugin-dir ./plugin   (then run /chain-of-custody:pm and check that hooks fire: start a session and read the canary line)"
