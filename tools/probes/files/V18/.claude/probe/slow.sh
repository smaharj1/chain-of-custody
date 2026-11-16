#!/bin/sh
# V18: a deliberately slow command. The first argument is the label that shows up in the hook log.
sleep "${V18_SLEEP:-20}"
echo "$1"
