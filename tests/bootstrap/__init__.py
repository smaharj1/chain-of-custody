"""Phase 11a tests: bootstrap, the receipt writer, the human-run GitHub scripts and the ops workflow templates.

Everything runs offline in temp directories. `gh` is a recorder or the fake from tests/helpers, never the real one.
"""
from __future__ import annotations

import os

from tests.helpers import clean_env


def plain_env(home=None, extra=None):
    """A clean environment that looks like a plain terminal: no Claude Code session marker of any kind."""
    env = clean_env(home, extra)
    for k in list(env):
        if k.startswith("CLAUDE"):
            del env[k]
    if extra:
        env.update(extra)
    return env


def session_env(extra=None):
    env = plain_env()
    env["CLAUDECODE"] = "1"
    if extra:
        env.update(extra)
    return env
