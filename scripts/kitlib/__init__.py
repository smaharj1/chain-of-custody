"""kitlib: shared substrate for the Chain of Custody kit scripts (IMP-10).

Stdlib only, Python 3.9+. The kit is a Claude Code plugin; its scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/`.
A script imports kitlib from its own directory like this:

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # ${CLAUDE_PLUGIN_ROOT}/scripts
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
    kitpaths   where the kit is, and how to name its files to a person or an agent
    kitmanifest  recognising the unedited files of an old copy install (migrate.py)
"""
from __future__ import annotations

KITLIB_VERSION = "1"
