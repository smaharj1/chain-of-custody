# Chain of Custody agent instructions

> **Advisory runtime.** Outside Claude Code this workflow is guidance, not enforcement. The kit's guards, subagents and unattended loop run only in Claude Code. Read the parity table below before relying on it.

This repository contains a role-based engineering workflow. The canonical
definitions live in `.claude/`; use them as the source of truth even when the
current agent runtime is Codex, Cursor, or another coding agent.

## Workflow routing

- Product requirements: read `.claude/skills/pm/SKILL.md`.
- Architecture and API design: read `.claude/skills/architect/SKILL.md`.
- Dispatch, implementation, and review: read `.claude/skills/tech-lead/SKILL.md`.
- Whole-project planning: read `.claude/skills/planner/SKILL.md`.
- Autonomous item execution: read `.claude/skills/orchestrate/SKILL.md` and the
  authoritative `.claude/spec/loop-engineering.md`.
- Maintenance modes, for a person at the keyboard (never started by the loop):
  `.claude/skills/bootstrap/SKILL.md` (set up an install),
  `.claude/skills/retro/SKILL.md` (retrospective over a milestone's run data),
  `.claude/skills/init/SKILL.md` (project settings a plugin cannot ship),
  `.claude/skills/migrate/SKILL.md` (upgrade a copy install).

Use the role definitions in `.claude/agents/` when delegating work. The
specialists are `security-reviewer`, `design-critic` and `code-reviewer` (all
read-only), `acceptance-author` (writes the black-box tests for an item, never
the implementation) and `intake-triage` (reads one piece of untrusted text; the
intake workflow or `loopctl` starts it, never a mode). Before
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
- Preserve the guard suite (`.claude/scripts/guards/`, wired in
  `.claude/settings.json`). Agents never push, merge a pull request or grant an
  approval that `loopctl` marks human-only. Print the command as
  `HUMAN STEP: <command>` and wait for the person to run it. A merge approval
  is bound to one item, attempt and head commit.
- Guardrail files (`.claude/settings.json`, the guard keys in
  `.claude/loop.config.md`, workflows, rulesets) are changed by a person in a
  reviewed commit, not by an agent.

For Cursor-specific loading, see `.cursor/rules/chain-of-custody.mdc`.

## What each runtime gets (parity)

| Capability | Claude Code | Codex / Cursor (this adapter) |
|---|---|---|
| Role prompts (`.claude/skills/*/SKILL.md`) | Slash commands (skills): `/pm`, `/architect`, `/tech-lead`, `/planner`, `/orchestrate`; maintenance modes `/bootstrap`, `/retro`, `/init`, `/migrate` | Read as instructions; you name the role in your prompt. No slash commands |
| Subagents with a clean context (engineers, critics, reviewers) | `.claude/agents/*.md`, dispatched by the Agent tool | Not loaded by the kit. Delegation, if the tool has any, is the tool's own |
| Independent code review | The `code-reviewer` agent, in a clean context | Only if you run the review in a separate session yourself. A single context reviewing its own work is not independent |
| Guard suite (`.claude/scripts/guards/run.py`; the v1 shell hooks were removed in 2.0.0) | On by default, wired in `.claude/settings.json` | **Not installed**: these runtimes ignore Claude Code settings. Nothing blocks `git push` or edits on `main` |
| Permissions and sandbox | Claude Code settings | The tool's own settings; the kit does not configure them |
| Unattended `/orchestrate` loop and auto-demo | Supported (with the limits in `README.md`) | **Not supported.** Do not run the loop unattended here |

Enforcement for these runtimes comes from CI (required status checks and code review on every PR), not from the adapter.

