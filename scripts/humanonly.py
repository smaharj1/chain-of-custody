"""humanonly: the "refuse inside a Claude Code session" check for human-run scripts (Phase 11a; IMP-15, DEC-13).

Scripts that change GitHub settings, repository labels or an H6-protected `loop.config.md` key are run by a
human, from a plain terminal. They refuse when an environment marker of a Claude Code session is present.

This is a tripwire, not a boundary. A process can remove `CLAUDECODE` from its own environment (`env -u CLAUDECODE`),
and then this check passes. What actually stops an agent is the rest of the boundary: it holds no `gh` credential
(B2), the guards deny `gh` mutations (Phase 2), and the human reads the dry run before adding `--apply`.
Two markers are checked so that removing one is not enough:

    CLAUDECODE               set to 1 by Claude Code in the environment of every tool process it starts
    CLAUDE_CODE_ENTRYPOINT   set by the CLI for the processes it starts (best effort; not documented as stable)

Presence counts, not value: `CLAUDECODE=` (empty) refuses too.
"""
from __future__ import annotations

from typing import Mapping, Optional

__all__ = ["MARKERS", "session_marker", "refusal"]

MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def session_marker(env: Mapping[str, str]) -> Optional[str]:
    """The name of the first Claude Code session marker present in `env`, or None."""
    for name in MARKERS:
        if name in env:
            return name
    return None


def refusal(tool: str, env: Mapping[str, str]) -> Optional[str]:
    """The refusal message for `tool`, or None when it may run."""
    marker = session_marker(env)
    if marker is None:
        return None
    return ("%s: refusing to run inside a Claude Code session (%s is set). This is a human-run script: "
            "run it from a plain terminal." % (tool, marker))
