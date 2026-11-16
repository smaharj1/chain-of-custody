#!/bin/sh
# Run the whole kit test suite: stdlib unittest, offline, Python 3.9+ (IMP-4).
# With no arguments, modules run in parallel (tools/run_tests.py). Extra arguments go to
# unittest, serially, e.g.: tools/test.sh -v tests.kitlib.test_miniyaml
# The serial whole-suite form is: python3 -m unittest discover -s tests -t .
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -eq 0 ]; then
    exec python3 tools/run_tests.py
fi
exec python3 -m unittest discover -s tests -t . "$@"
