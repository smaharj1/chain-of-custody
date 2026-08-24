# Chain of Custody agent instructions

This repository contains a role-based engineering workflow. The canonical
definitions live in `.claude/`; use them as the source of truth even when the
current agent runtime is Codex, Cursor, or another coding agent.

## Workflow routing

- Product requirements: read `.claude/commands/pm.md`.
- Architecture and API design: read `.claude/commands/architect.md`.
- Dispatch, implementation, and review: read `.claude/commands/tech-lead.md`.
- Whole-project planning: read `.claude/commands/planner.md`.
- Autonomous item execution: read `.claude/commands/orchestrate.md` and the
  authoritative `.claude/spec/loop-engineering.md`.

Use the role definitions in `.claude/agents/` when delegating work. Before
implementation, read `.claude/context/engineer-protocol.md` and the relevant
domain primer. Treat the written artifacts in `docs/features/<slug>/` as the
handoff contract between roles:

`requirements.md` → `technical-design.md` + `api-contract.md` →
`briefs/<domain>.md` → `reports/<domain>.md`

## Repository rules

- Do not skip the design or review gates for feature work.
- Match the canonical exemplars and conventions documented in the primers.
- Keep changes scoped to the active brief; report missing or conflicting
  requirements instead of silently inventing them.
- Run the verification commands documented by the relevant primer before
  reporting work as approved.
- Preserve the existing git safety rules in `.claude/hooks/` and
  `.claude/settings.json`.

For Cursor-specific loading, see `.cursor/rules/chain-of-custody.mdc`.
