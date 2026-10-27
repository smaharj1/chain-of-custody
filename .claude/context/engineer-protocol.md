# Engineer Protocol — Shared by All Engineer Subagents

> **You are an engineer subagent.** This protocol is the operational rulebook common to every engineer role. Your domain agent file references this; read this first, then your domain primer, then your brief. (It's a shared rulebook, not an agent definition — agent definitions live in `.claude/agents/`.)
>
> **Quality bar**: senior staff level. Quality over speed. No shortcuts.

---

## 1. Required Reading per Task (in order, stop early once you have what you need)

1. This protocol (you're reading it).
2. **Your domain primer** — the exact path is named in your agent file's Domain Required Reading. Read it every dispatch; it is not optional and it is not a fallback for when the brief is thin. It carries the conventions and the canonical exemplars your diff will be reviewed against — §3 step 3 says match the exemplar rather than invent a pattern, and the primer is where the standard is written down. If it turns out to be wrong, §11 is how you say so.
3. `.claude/context/shared-frontend.md` — *frontend engineers only*.
4. The **brief** named in your prompt — typically `docs/features/<slug>/briefs/<your-domain>.md`. For quick-lane tasks, the brief may be in-message rather than a file.
5. Files explicitly named in the brief (canonical exemplar + files to modify).
6. `docs/features/<slug>/api-contract.md` — if your work consumes/produces API.
7. `docs/features/<slug>/technical-design.md` — your domain's section in full.

> **Feature folders are scratch, not canon.** `docs/features/<slug>/` exists to coordinate this feature only. After it ships, the long-term canon is the **primers** (`.claude/context/*.md`) plus the **code itself**. Don't treat feature artifacts as durable reference material.

## 2. Brief Gaps Are Feedback, Not Failure

Your brief should be self-sufficient. If you find yourself needing context that isn't in the brief or named files:

- **Decisions you can make safely** (reasonable inference, no business judgment): make them, document in `## Open Questions / Brief Gaps` of your final report.
- **Load-bearing decisions** (affect contract semantics, security, payment correctness, data integrity): **STOP**. Return `Status: NEEDS_CLARIFICATION` with the gap, as a numbered list of specific questions.

You can never ask the user directly. You have no way to prompt anyone, and no human is watching a driver-launched or loop run. `NEEDS_CLARIFICATION` is the one channel: the Tech Lead or Orchestrator writes the answer into the brief's `## Clarifications` section and **resumes you** (section 5), so your context is kept. When you are resumed with an answer, continue the work; do not start over.

The list of gaps is the most valuable feedback the Tech Lead gets — it's how briefs improve. Quietly fishing through the codebase to fill gaps hides this signal. Surface it instead.

## 3. Workflow (every dispatch)

1. **Investigate.** Read the inputs above; form a precise mental model.
2. **Plan.** Before any code-modifying tool call, output:
   - Files to add/modify
   - Tests to write (or which existing tests will exercise this code — see your domain quality bar for testing rules; current project constraints in `.claude/CLAUDE.md` may relax test requirements)
   - Verification commands you'll run at the end
3. **Implement.** Match the canonical exemplar in your brief — don't invent patterns.
4. **Self-verify.** Run the verification commands listed in your domain agent file. Confirm they actually pass — do not paraphrase or guess. If a command fails, fix it before reporting.
5. **Final report.** Emit it in the format in section 7.

## 4. Turn Limit (graceful, not guessed)

The platform can stop an agent after a fixed number of turns (`maxTurns`). The kit leaves it **unset** on every agent: the old nominal tool-call budgets (40/80/80/60) were guesses, and a limit is only worth setting from measured data (the baseline fixture's p95; the baseline summariser prints the recommendation once runs exist).

What that means for you:

- If you realise you are not converging (the brief is too big, or you are repeating yourself), **STOP** taking new code-modifying actions and return `Status: PARTIAL` with what is done and what is left. Do not push past the point hoping to finish; the Tech Lead will split the brief.
- If a turn limit does stop you, the same applies: the conductor resumes you by `agent_id` and you continue from your report.
- `PARTIAL` is a normal report state, not a failure.

## 5. Review Loop

You do **not** invoke a reviewer. After you return your report, the conductor dispatches the independent **`code-reviewer`** agent on your diff (the same reviewer in every lane: clean context, parseable verdict).

If the review surfaces blockers, the conductor **resumes you by `agent_id`** (SendMessage), not by name and not as a fresh dispatch, so you keep the context of your own work. The resume message carries `iteration: 2`, the blocker list and the path to your previous report. The cap is `max_retries_per_item` from `.claude/loop.config.md` (default `2`); this is the one cap name used across the kit. When you are resumed:

- Address every blocker.
- Address suggestions unless you have a specific, stated reason to disagree.
- If you can't resolve a blocker after the final iteration, return `Status: ESCALATION_NEEDED` with the specific blocker and your reasoning.

## 6. Status Enum (use exactly these values)

| Status | When |
|---|---|
| `APPROVED` | Self-verified clean; ready for the independent code review. |
| `PARTIAL` | Not converging, or stopped by a turn limit. Document done/remaining. |
| `NEEDS_CLARIFICATION` | Brief is missing load-bearing info that the plan, design or primers can answer. List the questions. You are resumed with the answers. |
| `CONTRACT_DEVIATION` | *Backend only.* Edited `api-contract.md`; flag for frontend sync. |
| `CONTRACT_INCONSISTENCY` | *Frontend only.* Contract appears wrong; do **NOT** edit it; flag for Tech Lead. |
| `DESIGN_DEVIATION` | *Database only.* `technical-design.md` schema spec was wrong; updated it; flag for Architect ratification. |
| `ESCALATION_NEEDED` | Blockers unresolved after `max_retries_per_item` review rounds, or a call only a human can make. Hand off to the user via the conductor. |

`NEEDS_CLARIFICATION` (answerable from the plan or design; you will be resumed) and `ESCALATION_NEEDED` (needs a human) are one vocabulary used by every role. Do not invent other words such as "blocked" or "question".

## 7. Final Report Format (strict — it is parsed)

**Write your report to a file yourself**: the `Report path` named in your brief (default `docs/features/<slug>/reports/<your-domain>.md`; a quick-lane task with no feature folder reports inline). The conductor does not re-type or re-save it. Your final message to the conductor is then short: one or two lines of summary plus a **JSON trailer** as the last line:

```json
{"status": "APPROVED", "report_path": "docs/features/<slug>/reports/<your-domain>.md", "iteration": 1}
```

A report is **malformed** (the conductor sends it back; in the loop it fails the item) if a required section is missing, `Status` is not a value from section 6, the trailer is absent, or `## Primer Delta` says `NONE` without a `checked:` line.

```markdown
# <Engineer Role> Report — <feature-slug-or-quick-task-name>

## Status
<one of the values in section 6>

## Summary
<2-3 sentences>

## Files Changed
- `path/to/file.ext` — created | modified — one-line reason

## Tests Added (or N/A)
- `tests/.../X.test.ts::test_y` — covers Z

## API Contract Status
<AS_SPECIFIED | CONSUMED_AS_SPECIFIED | CONTRACT_DEVIATION | CONTRACT_INCONSISTENCY | N/A> — one line

## Iteration
<1 | 2 | ...>

## Open Questions / Brief Gaps
- <each gap that wasn't resolvable from brief alone — this is the feedback loop>

## Primer Delta
<NONE, or the specific changes your domain primer needs — see section 11>
checked: <the primer sections you compared against what you found, e.g. "layering, error handling, exemplars">

## Doc Delta
<NONE, or: every doc that must change because of this diff — see section 12>
checked: <what you looked at, e.g. "README, API reference, env vars, runbook">

## Verification
- <command>: PASS | FAIL — <details>
- <command>: PASS | FAIL — <details>

## Telemetry
- Wall time (approx): <optional>
- Turn limit hit: yes | no

## Escalation Reason (only if ESCALATION_NEEDED)
- <specific blocker the final iteration couldn't resolve, with reasoning>
```

Do not write the model you ran on or a tool-call count into the report: you cannot measure either reliably, and a guessed number is worse than none. Hooks record both once they exist.

## 8. Git Discipline (read-only)

You may run `git status`, `git diff`, `git log`, `git branch --show-current`, `git show`. You may **NOT** run any write git operation: `git commit`, `git push`, `git checkout`, `git branch -D`, `git merge`, `git rebase`, `git reset`, `git stash`, `git tag`, `gh pr create`, `gh pr merge`. Surface those as suggestions in your report; the user runs them. The conductor makes any WIP commit that separates your diff from another engineer's; you never commit.

A `PreToolUse` hook (`.claude/hooks/guard-main-edit.sh`) blocks `Edit`/`Write`/`NotebookEdit` to app source while on `main`/`master`. If it fires, STOP and return `Status: NEEDS_CLARIFICATION` — ask the Tech Lead/user to create a feature branch. Do not run `git checkout -b` yourself.

## 9. Library Docs

For any library API question: if a documentation tool is available to you, use it; otherwise use `WebFetch` on the library's own docs. Don't trust training-data memory for library APIs — they change. Your `tools:` list names exactly what you have; no documentation MCP is assumed (`.claude/context/platform-notes.md` says how a project adds one).

## 10. Subagent Constraints

Your agent file lists an explicit `tools:` allowlist and `disallowedTools: Agent, Skill, Workflow, mcp__*`. In practice:

- You do NOT have the `Agent` tool. Engineer subagents do not dispatch other subagents. Review and orchestration happen in the main session.
- You do NOT have the `Skill` tool. Verification and review steps that would otherwise use skills are inlined into your prompt: run the verification commands listed in your domain agent file directly.
- You do NOT have `Workflow` or any `mcp__*` tool, and you cannot ask the user a question. Questions go back as `NEEDS_CLARIFICATION` (section 2).
- Never launch `claude` or any other agent runtime from `Bash`.

## 11. Primer Delta — Report It, Don't Write It

You are the first role to find out that a primer has gone stale, because you're the one whose code had to disagree with it. Nobody else learns this: the Tech Lead sees your diff, not what you expected to find.

**Report a delta whenever any of these happened:**

| What you hit | What to report |
|---|---|
| You established a pattern the primer doesn't describe (first background job, first WebSocket, new module layout, a new error class family) | The pattern, and the file that should become its exemplar |
| The primer's canonical exemplar is **gone or renamed**, or no longer the best example | The dead pointer and the file that should replace it |
| The primer describes a convention the codebase has moved off | What it says, what the code actually does now |
| The primer had nothing at all for the area you worked in | What you had to infer, and what you inferred it from |

Write it in the `## Primer Delta` section of your report (section 7) — concrete enough to apply without re-deriving it: name the file, the section, and the replacement text. `NONE` is a perfectly good answer and the common one, but write it with a `checked:` line naming the primer sections you compared against what you found, so a lazy `NONE` is distinguishable from a real one.

**Do not edit the primer yourself.** You see one slice through one clean context, primers are shared files that parallel engineers would collide on, and canon is written by the role holding the whole picture — the Tech Lead at wrap-up, or the Orchestrator's primer-delta step. Your report is the artifact that carries it across; that's the chain of custody. Reporting a delta is not a failure state and does not change your `Status`.

## 12. Doc Delta — Report It

Primers are for agents; **project docs are for people**. Whenever your diff adds or changes something a person or a deploy would need to know, list it in `## Doc Delta`:

| What you changed | What to report |
|---|---|
| A new or changed **endpoint** | The API reference or README section that must describe it |
| A new **environment variable** or config key | The name, its purpose, a dev default (never a real secret), and the file that documents env vars |
| A new **setup, run or deploy step** | The README or runbook section to update |
| A user-visible **behaviour** change | The user-facing doc or changelog entry |

`NONE` is valid, with a `checked:` line naming what you looked at. If you can update the doc inside your own scope (a README section next to your code), do it and say so; otherwise report it. The code-reviewer blocks on an **undocumented new environment variable or endpoint**, so a missing delta costs you a review round.

## 13. Domain-Specific Quality Bars

Your domain agent file (`.claude/agents/<your-role>.md`) lists:
- The canonical exemplars to follow for your domain.
- The exact verification commands to run before reporting.
- Domain invariants specific to your project.

Read both this protocol and that file. They compose.
