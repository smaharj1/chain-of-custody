"""loopctl's git: `kitlib.gitutil.run_git` with the pins every loopctl call needs (IMP-25).

Fixed argument lists, `shell=False`, the user's global and system git config ignored, hooks off, and the config keys that make git run a
program just because it looks at the working tree switched off: `core.fsmonitor` (run by `status`, `reset`, `switch`, `revert`),
`core.pager`, `core.editor`, and external diff and textconv drivers. A repository's own `.git/config` cannot make a loopctl call execute
anything. (Only a person who can already write `.git/config` could try, and the guards deny that to agents; this is the second wall.)
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from kitlib import gitutil

__all__ = ["run_git", "is_clean", "PINS"]

PINS = ["-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "core.editor=true", "-c", "diff.external=", "-c", "protocol.ext.allow=never",
        "-c", "core.askPass="]


def run_git(args: Sequence[str], cwd: Optional[str] = None, **kw: Any):
    kw.setdefault("isolated", True)
    kw.setdefault("pin_hooks", True)
    return gitutil.run_git(PINS + list(args), cwd=cwd, **kw)


def is_clean(cwd: str) -> bool:
    proc = run_git(["status", "--porcelain=v1", "--untracked-files=normal"], cwd=cwd)
    return proc.returncode == 0 and not proc.stdout.strip()
