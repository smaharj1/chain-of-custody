# Chain of Custody agent instructions

> **Advisory runtime.** Outside Claude Code this workflow is guidance, not enforcement. The kit's guards, subagents and unattended loop run only in Claude Code. Read the parity table below before relying on it.

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

## What each runtime gets (parity)

| Capability | Claude Code | Codex / Cursor (this adapter) |
|---|---|---|
| Role prompts (`.claude/commands/*.md`) | Slash commands: `/pm`, `/architect`, `/tech-lead`, `/planner`, `/orchestrate` | Read as instructions; you name the role in your prompt. No slash commands |
| Subagents with a clean context (engineers, critics, reviewers) | `.claude/agents/*.md`, dispatched by the Agent tool | Not loaded by the kit. Delegation, if the tool has any, is the tool's own |
| Independent code review | The `code-reviewer` agent, in a clean context | Only if you run the review in a separate session yourself. A single context reviewing its own work is not independent |
| Safety hooks (`guard-git.sh`, `guard-main-edit.sh`) | Wired in `.claude/settings.json` | **Not installed**: these runtimes ignore Claude Code settings. Nothing blocks `git push` or edits on `main` |
| Permissions and sandbox | Claude Code settings | The tool's own settings; the kit does not configure them |
| Unattended `/orchestrate` loop and auto-demo | Supported (with the limits in `README.md`) | **Not supported.** Do not run the loop unattended here |

Enforcement for these runtimes comes from CI (required status checks and code review on every PR), not from the adapter.

