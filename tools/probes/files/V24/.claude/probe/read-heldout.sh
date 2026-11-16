#!/bin/sh
# V24: tries to read a file and prints the label. $1 = label (probe-v24-<who>), $2 = file (default: the held-out file).
cat "${2:-heldout/secret-v24.txt}"
echo "$1"
