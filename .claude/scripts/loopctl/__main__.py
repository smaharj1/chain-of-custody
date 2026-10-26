"""`python3 .claude/scripts/loopctl <command>` entry point."""
from __future__ import annotations

import os
import sys


def _bootstrap() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.dirname(here)
    # Running a directory puts that directory first on sys.path. Remove it so that modules inside it
    # (state.py, plan.py, ...) are only reachable as `loopctl.<module>`, then make the kit libraries importable.
    sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != here]
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


_bootstrap()

from loopctl.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.stderr.write("loopctl: interrupted\n")
        sys.exit(130)
    except BaseException as exc:  # an internal error must never read as success
        sys.stderr.write("loopctl: internal error: %s: %s\n" % (type(exc).__name__, str(exc)[:200]))
        sys.exit(2)
