#!/usr/bin/env python3
"""A fake `gh` that records its argv and replays recorded JSON (IMP-14). No network, ever.

Rules come from the env var FAKE_GH_REPLAY (a path to a JSON file) or `<this file>.replay.json`
next to the installed copy:

    [ {"argv_prefix": ["api", "repos/acme/widgets/rulesets"],   # or "argv_contains": [...] or "regex": "..."
       "stdout_json": [...],                                   # or "stdout": "text" or "stdout_file": "path"
       "stderr": "", "exit": 0, "times": 1} ]                   # "times" omitted = unlimited

The first rule that matches an invocation (and still has uses left) answers it. Anything
unmatched exits 1 with "fake gh: no recorded response". Every call is appended to
`<this file>.log.jsonl` (or FAKE_GH_LOG) as {"argv": [...], "matched": rule index or null}.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.abspath(__file__)


def _load_rules():
    path = os.environ.get("FAKE_GH_REPLAY") or (HERE + ".replay.json")
    if not os.path.exists(path):
        return [], path
    with open(path, "r") as fh:
        return json.load(fh), path


def _matches(rule, argv):
    if "argv_prefix" in rule:
        p = rule["argv_prefix"]
        return argv[:len(p)] == p
    if "argv_contains" in rule:
        return all(x in argv for x in rule["argv_contains"])
    if "regex" in rule:
        return re.search(rule["regex"], " ".join(argv)) is not None
    return False


def main(argv):
    rules, rules_path = _load_rules()
    used_path = rules_path + ".used"
    try:
        used = json.load(open(used_path)) if os.path.exists(used_path) else {}
    except ValueError:
        used = {}
    chosen = None
    for i, rule in enumerate(rules):
        if not _matches(rule, argv):
            continue
        times = rule.get("times")
        if times is not None and used.get(str(i), 0) >= times:
            continue
        chosen = i
        break
    log = os.environ.get("FAKE_GH_LOG") or (HERE + ".log.jsonl")
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": argv, "matched": chosen, "cwd": os.getcwd()}, sort_keys=True) + "\n")
    if chosen is None:
        sys.stderr.write("fake gh: no recorded response for: gh %s\n" % " ".join(argv))
        return 1
    used[str(chosen)] = used.get(str(chosen), 0) + 1
    with open(used_path, "w") as fh:
        json.dump(used, fh)
    rule = rules[chosen]
    if "stdout_json" in rule:
        sys.stdout.write(json.dumps(rule["stdout_json"]) + "\n")
    elif "stdout_file" in rule:
        with open(rule["stdout_file"], "r") as fh:
            sys.stdout.write(fh.read())
    elif "stdout" in rule:
        sys.stdout.write(rule["stdout"])
    if rule.get("stderr"):
        sys.stderr.write(rule["stderr"])
    return int(rule.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
