"""distlib: the generator behind `tools/build-dist.py` and the checks behind `tools/validate-plugin.py` (Phase 9, IMP-3).

`.claude/` stays the one hand-edited source. This package turns the TRACKED files of the repository into:

    plugin/**                         the Claude Code plugin (agents, skills, hooks.json, scripts, context, spec, templates, docs, evals)
    .claude-plugin/marketplace.json   the repo marketplace, pointing at ./plugin
    dist/copy-install/**              what a copy install copies into a project (.claude/, the Codex and Cursor adapters, the docs the prose points at)
    .claude/KIT_MANIFEST              SHA-256 and class of every file of the copy install, for migrate.py

Everything is built in memory as {path: bytes} first, so a drift check and a test never need a temp directory. Standard library only,
Python 3.9.
"""
from __future__ import annotations
