"""kitlib: shared substrate for the Chain of Custody kit scripts (IMP-10).

Stdlib only, Python 3.9+. The kit is installed by copying `.claude/` into a project,
so there is no install step. A script imports kitlib like this:

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # .claude/scripts
    from kitlib import config, gitutil

Modules (one job each):

    miniyaml   restricted YAML subset parser (IMP-11)
    registry   loop.config key registry loader, merges schema/*.json (IMP-11)
    config     load loop.config.md, typed accessors, base-sha loading (report §9)
    gitutil    git resolution and compare-and-swap helpers (report §10.3)
    jsonio     atomic write and exclusive create
    redact     secret redaction (report §5.4)
    semver     semantic versions and the Claude Code version range (report §14)
    evidence   the human-committed evidence gate (IMP-1)
    runstate   the <git-common-dir>/loopctl/ layout (report §10.3, §19.7)
    caller     who is calling a hook: conductor, engineer, critic, ... (report §5.7)
    levels     autonomy levels L0-L5 and the effective-level computation (IMP-21)
"""
from __future__ import annotations

KITLIB_VERSION = "1"
