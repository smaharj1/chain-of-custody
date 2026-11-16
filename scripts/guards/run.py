#!/usr/bin/env python3
"""Single entry point of the v2 guard suite (report §7.1). Wired from hooks.manifest.json by wiring.py.

    run.py <Event> [--id <entry>]       a hook: reads the hook input from stdin, decides by event and tool
    run.py --selftest [--record]        feed known-bad and known-good payloads through every guard
    run.py explain [--caller K] CMD     what the guards say about a Bash command (K: engineer critic unknown conductor)
    run.py register-session --session ID --role conductor|<roster role>     human only: give a session its rights
    run.py unregister-session --session ID | set-halt --reason TEXT | clear-halt     human only

Exit 0 allow, 2 block with a one-line reason on stderr (rule ids look like guard-git:no-verify). Any exception on a
blocking event exits 2: the guards fail closed. This file must parse on old Pythons so that a too-old python3 also
fails closed instead of crashing with exit 1.
"""
import os
import sys

if sys.version_info < (3, 9):
    sys.stderr.write("kit guards: Python 3.9 or newer is required (found %d.%d); failing closed (rule guards:python-version)\n" % sys.version_info[:2])
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    try:
        from guards import cli
        code = cli.main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last line of defence: fail closed
        sys.stderr.write("kit guards: internal error (%s: %s); failing closed (rule guards:internal-error)\n" % (type(exc).__name__, str(exc)[:120]))
        code = 2
    sys.exit(code)
