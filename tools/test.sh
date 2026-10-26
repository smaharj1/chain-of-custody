#!/bin/sh
# Run the whole kit test suite: stdlib unittest, offline, Python 3.9+ (IMP-4).
# Extra arguments go to unittest, e.g.: tools/test.sh -v tests.kitlib.test_miniyaml
set -eu
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -t . "$@"
