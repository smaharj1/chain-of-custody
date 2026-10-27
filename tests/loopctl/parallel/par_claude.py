#!/usr/bin/env python3
# kit-fake-claude: v1  (a per-item front end to tests/helpers/fake_claude.py; the driver's fake-binary marker, see launch.is_fake_binary)
"""The fake `claude` for parallel tests: tests/helpers/fake_claude.py, but the run it plays is chosen by the ITEM and the ROLE named in the prompt.

A parallel run launches children in an order nobody controls, so `by_role` (one list per role, consumed in call order) cannot say which item's report a
child writes. This wrapper reads the prompt (the driver puts "... for loop item A (...)" in its first line), picks the next run for (item, role) from
`<this file>.par.json`, and hands it to the shared fake as its only run. Everything the shared fake does (the canary nonce, the telemetry stream, the
cost, the files written, the stall and crash behaviours) is unchanged. Two extra log files record when each child started and ended, so a test can
prove two children ran at the same moment.

    {"base": {...the shared fake's usual keys: nonce_dir, events_dir, log...},
     "items": {"A": {"backend-engineer": [run, run], "code-reviewer": [run]}},
     "roles": {"code-reviewer": [run]},         # for an item with nothing of its own
     "default": {...}}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.abspath(__file__)


def main(argv):
    sys.path.insert(0, os.path.dirname(HERE))
    import fake_claude
    with open(HERE + ".par.json") as fh:
        scen = json.load(fh)
    prompt = sys.stdin.read(200000)
    m = re.search(r"for loop item ([A-Za-z0-9]+)", prompt)
    item = m.group(1) if m else "?"
    role = fake_claude._arg_value(argv, "--agent") or "?"
    runs = (scen.get("items", {}).get(item, {}).get(role)) or scen.get("roles", {}).get(role) or [scen.get("default") or {"behavior": "succeed"}]
    idx = fake_claude._next_index({"state": "%s.state.%s.%s" % (HERE, item, role)})
    run = runs[idx] if idx < len(runs) else runs[-1]
    base = dict(scen.get("base") or {})
    base.update({"runs": [run], "default": run, "read_stdin": False, "state": HERE + ".fakestate"})
    os.environ["FAKE_CLAUDE_SCENARIO"] = json.dumps(base)
    rec = {"item": item, "role": role, "idx": idx, "pid": os.getpid(), "cwd": os.getcwd(), "start": time.time(), "stdin": prompt[:4000],
           "env": {k: os.environ.get(k) for k in ("KIT_PORT_OFFSET", "KIT_PORT_SLOT", "KIT_PORTS", "KIT_ITEM")}, "argv": argv}
    try:
        code = fake_claude.main(argv)
    finally:
        rec["end"] = time.time()
        with open(HERE + ".par.log", "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
