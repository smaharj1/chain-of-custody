"""doctorlib: the checks behind `.claude/scripts/doctor.py` (Phase 1, report §5, §8.4, §14).

Gathering is separate from judging. A *gatherer* reads the filesystem, the environment,
recorded `gh api` JSON or an injected function and returns plain data. A *check* is a small
registered function with a stable id that judges that data and returns `Result` objects.
Tests drive a check either with a fixture directory or by injecting the gathered facts.

    model      Result, Context (all I/O seams), the check registry
    paths      repo/home/fs-root path helpers
    creds      B2: every credential an agent's OS user could reach (RC-5)
    deploy     B5: the deployment surface and its attestation (RC-1)
    github     B1/B4: rulesets, bypass actors, branch protection, plan features (RC-9, RC-11)
    settings   Workflow allow, sandbox, MCP lockdown, managed settings, devcontainer (RC-17)
    secrets    .env tracking, .gitignore coverage, worktree include (report §5.4)
    codeowners CODEOWNERS parsing and coverage (H3, H6)
    versions   Claude Code range and KIT_VERSION (report §14)
    levels_    the effective-level computation with its caps (IMP-21)
    workflows  small structural reader for GitHub workflow YAML, shared with ci_lint

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

DOCTORLIB_VERSION = "1"
