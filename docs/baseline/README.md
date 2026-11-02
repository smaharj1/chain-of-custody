# Baseline: measuring V1 before anything changes

Status: the tooling is built; **no run has been made**. Every run is human-run and recorded as `PENDING` in [RUNS.md](RUNS.md). An agent cannot start `claude`, so this build could not produce the numbers (IMP-2). The point of this folder is that when you have an hour, the baseline is one command and a few minutes of bookkeeping per run.

Why measure first (report §3.2, §15 item 1, §16.1 Phase 0a): the V1 loop has never been run. The design report proposes a lot of machinery (`loopctl`, gates, a driver). Each piece must earn its place by moving a number on a fixture, and the [kill criteria](kill-criteria.md) say what the baseline has to show before the big pieces are built. Safety and boundary work is exempt; it is judged on the corpora and fixtures instead.

## What is here

| Path | What it is |
|---|---|
| `examples/hello-loop/` | The fixture: a tiny stdlib Python bookmarks service. Three plan items (schema, endpoint, UI), a V1-shape `build-plan.json`, pre-filled primers in `kit-overlay/`, held-out checks in `acceptance/`, an answer key in `reference-solution/` that is never copied into a run. Its README explains the design. |
| `tools/baseline/run-baseline.sh` | Prepares one run directory. Human-run. Never runs `claude`. |
| `tools/baseline/observe.py` | The observation-only hook the preparation step installs. |
| `tools/baseline/summarize.py` | Ranges over the results and the kill-criteria verdicts. `PENDING (n=k/3)` until there are 3 runs. |
| [results/TEMPLATE.json](results/TEMPLATE.json), [results/schema.json](results/schema.json) | What you fill in per run, and the schema `summarize.py` checks it against. |
| [RUNS.md](RUNS.md) | The ledger: three `PENDING` rows and the "1 run + 20 replayed dispatches" alternative. |
| [kill-criteria.md](kill-criteria.md) | Report §16.2, written down before any building. |
| [evidence.template.json](evidence.template.json), [evidence.schema.json](evidence.schema.json) | The later evidence gate (3 clean runs of the new kit plus 1 real feature). Not part of this baseline; here because it uses the same fixture. |

## Preconditions

- A normal terminal, not a Claude Code session. `run-baseline.sh` refuses when `CLAUDECODE` is set.
- `claude` installed and signed in. The script only checks it is on `PATH`; it does not run it.
- `git` and `python3` 3.9 or newer. The fixture pins 3.12 in `.python-version` (`pyproject.toml` allows 3.9+); if your `python3` differs, say so in the results `conditions.python3`.
- The local tag `v1.2.0` (it exists in this repo; push it yourself if other machines need it).
- Ideally a dedicated OS user or a throwaway machine account with no cloud credentials, because the printed command only unsets environment variables. Files such as `~/.aws` or `~/.config/gcloud` stay readable unless you remove them from the run user's reach.

## Run protocol

Repeat for `run-1`, `run-2`, `run-3`, with a fresh directory each time.

1. **Prepare.** `tools/baseline/run-baseline.sh run-1` (add `--dry-run` first to see the plan; `--dest-root DIR` to choose where; default `~/kit-baseline`). It clones the tag into `<dest-root>/run-1`, overlays the fixture without its answer key, removes the admin-app and infra primers and agents, installs the observation hook and the sandbox setting, commits, and prints the exact `claude` command. It also writes `<dest-root>/run-1.baseline.json` (commits, fixture digest, hook list) and will write the hook log to `<dest-root>/run-1.observe.jsonl`.
2. **Check the conditions.** Start the printed command. In the session confirm: `/sandbox` shows the sandbox on; `/mcp` shows whether a browser preview MCP is connected (record it as the demo capability); no cloud credentials are reachable.
3. **Run.** The first message `/orchestrate` loads the plan and asks "Ready?". Answer "go". That is the only free instruction. From then on you are an observer who rescues only when the loop is stuck or wrong (below). Do not coach, hint, fix code or edit primers unless it is a logged rescue.
4. **Checkpoints.** The overlay sets `autonomy: per-milestone`, so the loop pauses after F2 (end of M1) and after F3 (end of M2). Approve them. Approving a designed checkpoint is not a rescue; count them in `checkpoint_approvals`. (`unattended` is evidence-gated in the new kit, so the baseline keeps the V1 default and stays re-runnable against later versions.)
5. **Log every rescue as it happens.** One line in a scratch file: time, item, what you did, why. Fill the structured entries afterwards.
6. **Stop** when all three items are `done` and the last checkpoint is approved, or when you decide V1 cannot finish. Do not stop early because it is slow. If the loop is going in circles and you have made at least the rescues that a competent operator would, record `v1_cannot_finish` with the reason. Stop for machine or quota reasons as `aborted`; aborted runs do not count either way.
7. **Measure the outcome.** Run the held-out checks on the final tree: `python3 examples/hello-loop/acceptance/run_checks.py --project <dest-root>/run-1 --json`. Failed criteria are escaped defects (the loop said done; the black-box check disagrees). Also confirm `git -C <dest-root>/run-1 status` and the plan file reflect what the loop claimed.
8. **Read the hook log.** `python3 tools/baseline/summarize.py --from-log <dest-root>/run-1.observe.jsonl` prints tool-call and dispatch counts, permission requests, session restarts and prompt counts. These are hints; you reconcile them with what you saw.
9. **Record.** Copy [results/TEMPLATE.json](results/TEMPLATE.json) to `results/run-1.json`, fill it, then `python3 tools/baseline/summarize.py` (it validates the file). Update the row in [RUNS.md](RUNS.md).
10. **Publish** the transcript (the session export or terminal log) and the scrubbed hook log with the results (report §15 item 1: transcripts, metrics and every rescue). Scrub before committing: `observe.py` already redacts secret shapes and shows your home directory as `~`, but read the transcript for anything else (hostnames, email addresses, tokens pasted into prompts). Put them wherever the maintainer prefers and set `transcript_ref`.

## Baseline conditions and their deltas (report §15 item 1)

"Baseline conditions" are not "unchanged V1". Each difference is a variable; list them in every results file.

| Delta | Why it matters | Where recorded |
|---|---|---|
| **Sandbox on** | V1 shipped without one. It changes which commands prompt, fail or silently succeed (network, writes outside the project). The preparation step writes `sandbox.enabled: true` and `network.allowLocalBinding: true` (the fixture's server and `verify_run.py` bind to localhost) into the clone's `.claude/settings.json`. | `conditions.sandbox` |
| **No cloud credentials** | The kit's boundary work assumes none are reachable. The printed command unsets the usual variables. | `conditions.cloud_credentials` |
| **Observation hook** | A logging-only hook on 12 events. It writes nothing to stdout, never blocks and always exits 0, so it should not change behaviour, but it is a delta. | `conditions.observation_hook` |
| **Demo capability** | `orchestrate.md` step 5 assumes a browser-preview MCP such as `Claude_Browser` and falls back to an HTTP check. Which one was available changes the checkpoint behaviour. | `conditions.demo_capability` |
| **Pre-filled kit** | `kit-overlay/` supplies filled primers, `CLAUDE.md`, `loop.config.md` and `local-dev.md`, and the plan is pre-approved, so the baseline measures the loop and not primer derivation or planning. Unused primers and agents are removed, as the V1 templates instruct. | fixture digest in the manifest |
| **Existing codebase, not greenfield** | The fixture starts with a runnable server, a migration runner and a passing smoke test, so no "skeleton" item exists and the greenfield rule does not apply. | `examples/hello-loop/README.md` |
| **Whole kit tree in the clone** | The clone of the tag keeps the kit's own `README.md`, `docs/`, `examples/` next to the fixture. Real installs have only `.claude/`. Noted, not removed, so the run matches "V1 at tag v1.2.0" exactly. | manifest |
| **Tag content** | `v1.2.0` points at `cb6218f`, which includes the docs-boundary move that was unreleased when the report was written. That is the tree the report reviewed. | `kit.commit` |

Things this build could not verify, to confirm on the first run (they live in one constant each in `tools/baseline/prepare.py`): the sandbox setting key names, the 12 hook event names, and that `claude "/orchestrate"` starts an interactive session with that first message. If any is wrong the symptom is an ignored setting or a hook log with fewer events; fix the constant and say so in the run notes.

## Rescue taxonomy

A **rescue** is anything the human did that the loop should have done or decided itself. The kill criterion counts only **control-flow** rescues. When in doubt, count it and say why in the description.

**Control-flow rescues** (`class: control_flow`): the loop's sequencing or state broke.

| `type` | Example |
|---|---|
| `resume` | The session stalled, crashed or stopped and you restarted `/orchestrate` or told it to continue |
| `redispatch` | You re-launched an engineer, critic or reviewer the loop should have launched, or launched one it skipped |
| `state_edit` | You edited `build-plan.json`, `BUILD_PLAN.md`, `.claude/loop.jsonl` or the lock by hand to get the loop moving |
| `unblock` | An item was `blocked` or the breaker tripped and you decided the way out outside the designed unblock review |
| `reconcile` | You repaired git state: a stray branch, a missing merge, a dirty tree, a lost commit |

**Other rescues** (`class: other`): the loop worked as designed but needed you for something else.

| `type` | Example |
|---|---|
| `env_fix` | Toolchain, port, dependency or filesystem problem outside the loop's control |
| `permission` | You approved a harness permission prompt (also count them in `permission_prompts`) |
| `decision` | You answered a genuine product or design question |
| `content_fix` | You changed code, tests or a primer by hand |
| `other` | Anything else; explain |

Not rescues: approving a designed checkpoint; saying "go" at the start; answering the designed unblock review; reading.

## Failure classes (what went wrong, regardless of whether you rescued)

Same classes as report §10.4: `platform` (API 5xx, overload, CLI crash), `transient` (flaky, port in use), `env` (toolchain mismatch, missing variable, DB), `item` (tests fail, review blocked, acceptance unclear), plus `plan` (the plan itself was wrong) and `other`. One entry per distinct failure, not per retry.

## What each run records

| Field | Source |
|---|---|
| `kit.tag`, `kit.commit` | the manifest |
| `kit.claude_version`, `kit.model` | `claude --version` and the session, typed by you |
| `conditions.*` | the table above |
| `status`, `cannot_finish_reason` | your judgement at the stop |
| `wall_time_min` | your clock, start of `/orchestrate` to the stop, including waits |
| `active_time_min` | optional: wall time minus waits |
| `spend_usd` | the session's cost display (null if unobservable) |
| `tool_calls`, `permission_prompts`, `dispatches` | the hook log, reconciled |
| `items[]` | the final `build-plan.json` in the run directory |
| `checkpoint_approvals` | your count |
| `rescues[]` | your scratch log |
| `failures[]` | your reading of the transcript |
| `defects_escaped` | the held-out checks (step 7) |
| `transcript_ref` | where you published it |

## The alternative: 1 run plus 20 replayed dispatches per agent type

The report allows it (RC-8) when three full runs are too costly, and it is the only way to get a `maxTurns` p95 for each agent type (CS-32). One complete `run` result plus, for every agent type that run dispatched, a `replay` result with at least 20 replayed dispatches satisfies the sample rule; `summarize.py` then reports the verdicts as provisional.

To replay: take the dispatch prompts for one agent type from the hook log (a `PreToolUse` event for the `Agent` tool carries `tool_input.subagent_type` and `tool_input.prompt`), restore the working tree to the state the original dispatch started from, re-issue the same prompt from a fresh session, and count the tool calls the subagent used (the hook log's events for that dispatch, or the report's count). Record each in a `kind: "replay"` result (`agent_type`, `source_run`, `replayed_dispatches[{tool_calls, turns, outcome}]`). The exact replay mechanism is not settled by this build; whoever does it should write down how.

## After the runs

`python3 tools/baseline/summarize.py` prints the ranges and the three decidable verdicts. Copy the verdicts into the Phase 4 decision. A recorded `v1_cannot_finish` fires its branch immediately, before three runs exist.
