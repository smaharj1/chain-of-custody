---
name: chain-of-custody
description: Apply the Chain of Custody role-based engineering workflow when a project contains the repository's .claude definitions and written handoff artifacts.
metadata:
  short-description: Use the Chain of Custody engineering workflow
---

# Chain of Custody

Use this skill when the current project contains the Chain of Custody workflow
under `.claude/`. The `.claude/` files are canonical; this skill is only the
Codex discovery and routing layer.

## Route the work

- Requirements: read `.claude/commands/pm.md`.
- Architecture and API design: read `.claude/commands/architect.md`.
- Dispatch and review: read `.claude/commands/tech-lead.md`.
- Whole-project planning: read `.claude/commands/planner.md`.
- Autonomous execution: read `.claude/commands/orchestrate.md` and
  `.claude/spec/loop-engineering.md`.

When delegating implementation, use the matching definition in
`.claude/agents/` and read `.claude/context/engineer-protocol.md` plus the
relevant domain primer first.

## Handoff contract

Preserve the written artifact chain:

`requirements.md` → `technical-design.md` + `api-contract.md` →
`briefs/<domain>.md` → `reports/<domain>.md`

Do not bypass design critique, code review, security review when required, or
primer-defined verification. Keep changes scoped to the active brief and
surface missing or conflicting requirements rather than guessing.

If `.claude/` is absent, do not pretend this workflow is active; use the
project's available instructions or explain that the kit must be installed
first.
