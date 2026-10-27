# Model profiles and effort routing

> Shared kit rule. Read by the Orchestrator and the Tech Lead when they dispatch agents. Report: §11.4, R1, CS-39, P7, CO-21, CO-34; decision IMP-17, DEC-16.

Each agent file names `model: opus` in its frontmatter. That is the **`quality` profile**, the default, and it is what runs if nobody does anything. The `model_profile` key in `.claude/loop.config.md` lets a project trade quality for cost. The conductor (the Orchestrator, or the Tech Lead in a feature) applies the profile when it dispatches.

## The profiles

| Role | `quality` (default) | `balanced` | `economy` (experimental) |
|---|---|---|---|
| design-critic | opus, effort `high` | opus, `high` | opus, `high` |
| code-reviewer | opus, `high` | opus, `high` | opus, `high` |
| security-reviewer | opus, `high` | opus, `high` | opus, `high` |
| database-engineer | opus, `high` | opus, `high` | sonnet, `high` |
| backend-engineer | opus, `high` | opus, `high` | sonnet, `high` |
| app-engineer, admin-app-engineer | opus, `high` | sonnet, `high` | sonnet, `medium` |
| infra-engineer | opus, `high` | opus, `high` | sonnet, `high` |

- **Reviewers stay on opus in every profile.** A weaker reviewer defeats the point of an independent review.
- **`quality` is the default until data says otherwise.** Evidence from a real install: sonnet frontends were poor. The default changes only when fixture runs or evals show parity (DEC-16).
- **`economy` is untested.** Expect more review rounds and more rework; the saving is real only if rework stays low. Treat it as an experiment and watch the re-dispatch count.
- **Haiku** is allowed only for the optional PR-body prose summary. Most "render" and "summary" jobs are deterministic code, not model calls.
- **opusplan** (a plan-mode model for planning, a cheaper one for execution) is an option for the *main session* in the `balanced` and `economy` profiles. **Plan mode** is recommended for the interactive Architect and for Planner exploration. The **Advisor** is an optional second opinion; it is never on the critical path.

## How the conductor applies a profile

1. Read `model_profile` from `.claude/loop.config.md` (default `quality`).
2. **Precedence is per-invocation > frontmatter > environment.** The `model` argument on the Agent call beats the agent file's `model:`, which beats `CLAUDE_CODE_SUBAGENT_MODEL` (that variable is only a default).
3. Under `quality`, pass **no** `model` (the frontmatter already says opus). Under `balanced` and `economy`, pass the model from the table for the role being dispatched.
4. **If `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set** (v2.1.257+), the user has forced one model for every subagent. Do not pass a per-invocation model, and say in the run summary that the profile is being overridden.
5. Pass `effort` from the table when the Agent call accepts it; the agent file's `effort:` frontmatter already carries the `quality` values.

## Escalation

On `CHANGES_REQUESTED` in review **round 2**, re-dispatch the engineer (resumed by `agent_id`) at **opus with `xhigh` effort**, whatever the profile. A second round means the first model missed something, so spend more on the retry, not less.

## Verifier

When an independent verifier is added (gate G8, after the evidence gate), it runs on `fable` if available, otherwise opus at higher effort in a fresh context.

## What is not recorded by hand

Nobody types the model used into a log. A model name written by the agent that is running is not evidence (the shipped example once said `opus` while the profile said otherwise). Per-agent model and tool-call counts come from hook telemetry once it exists (Phase 2), and from the baseline fixture before that. `maxTurns` is deliberately **unset** on every agent until a baseline run supports a p95; `tools/baseline/summarize.py` prints the recommendation once data exists.
