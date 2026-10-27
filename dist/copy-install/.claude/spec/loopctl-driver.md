# loopctl driver: `runner: driver`

**Status**: Phase 4c, OPT-IN and experimental (IMP-1). Implements report sections 10.6, 11.1 (item 2), 11.2, 11.3, 11.6, 14.2 and the serial cases of 15 item 5 (`docs/design/review-2026-10.md`) under IMP-14, IMP-21, IMP-24 and IMP-25 (`docs/design/v2-implementation-decisions.md`). The state, the lock, the ledger, the failure taxonomy and the gate runner are Phase 4a (`.claude/spec/loopctl.md`); the gates themselves are Phase 4b.

`runner: sequential` (the default) means the prose loop runs the item and `loopctl run` only prints the hand-off. `runner: driver` means `python3 .claude/scripts/loopctl run`, started once by a human, does the conductor's deterministic work and launches a child `claude -p` session for every piece of judgement work. **No agent ever launches `claude`** (CS-2): the exec guard denies it, and this is the one process that does.

## What a run does

```
loopctl run                  (preflight: Tier 2 on, plan approved, budget set, lock free or a dead driver's)
  startrun.start             takes the lock with driverPid, writes run.json, calls driver.run(ctx, loop, run_record, as_json)
  driver.setup               nesting, binary, version, profiles, orphans, signals, worktree, recovery
  driver.run_items           for the first eligible item: begin ... merged | awaiting_human | parked
```

`driver.run` returns 0 (the plan is complete, or the run stopped on purpose for a human: a checkpoint, an item that needs approval), 1 (an item or the whole run is parked or stopped: a hard stop, an exhausted budget, an interrupt), 2 (an internal error) or 3 (refused: HALT, a lost lock, a nesting or version refusal, a checkpoint nobody answered). `--json` prints one `loopctl.driver.v1` document at the end.

## The item pipeline

State is the only memory. Every iteration re-reads the item's record from `refs/loopctl/state/<plan>` and acts on its status; a retry, a restart or a human's `approve` needs no special path.

| Status | The driver | Gates (4a's runner, toggles from the base sha) |
|---|---|---|
| `pending` | checks `requiresEnv`, `Loop.begin`, cuts `feat/<slug>-a<n>` off the integration tip and switches to it | |
| `designing` | writes `docs/features/<slug>/briefs/<domain>.md` from the plan item, WIP commit | `g0` first |
| `design_review` | the plan item is the design (H1 and H2 approved it, slicing gave it a footprint): no child | |
| `stubbing` | only if `gates.g2` is not off, `g2` is installed and the `acceptance-author` agent definition exists at the base sha (Phase 4b ships it): a child writes stubs and tests | `g2` |
| `building` | one child per engineer role in `estimate.engineers`, in dependency order (infra, database, backend, app, admin-app), each with a brief and a report path; the report is read, then a WIP commit | |
| `verifying` | | `g5 g1 g3 g6 g9 g11` (a blocking `g5` ends the round) |
| `reviewing` | `code-reviewer`, and `security-reviewer` per `security_review`; the verdict line is read from each final message; the review is saved under `docs/features/<slug>/reviews/` | `g7` |
| `merged` | `git merge --no-ff` with `Loop-Item: <id>` and `Loop-Attempt: <n>`, then `Loop.advance merged` | |
| `awaiting_human` | an item a human must approve (`humanGate`, an infra footprint, or any `delivery` other than `local`): the driver stops | |

A step back is `Loop.fail`, which counts it against the right cap and answers `retry` (go back where the answer says, sleeping the backoff for a platform failure), `discard` (attempt n+1 from the integration tip), `park` or `hard_stop` (the run ends). A gate round records ONE failure, the worst class winning. Gates are recorded in one compare-and-swap per round (`Loop.record_gates`). Any gate a transition requires (`REQUIRED_FOR`) that has not passed for the current head is run before the transition.

A gate sees `base_sha` = the item's branch point, so its diff is the item's own. `cfg` and the plan stay what they were at the run's start base sha: an item that edits `.claude/loop.config.md` cannot raise its own budget or turn a gate off (CO-11).

## One child

```
claude -p --agent <role> --agents <{role: {description, prompt, tools, disallowedTools, model, effort}}> --settings <file>
       --allowedTools <rule> <rule> ... --permission-mode dontAsk --max-budget-usd <remaining> --output-format json --session-id <uuid>
```

Fixed argv, `shell=False`, nothing from the plan or a report on it; the prompt is a file on stdin. The binary is `KIT_CLAUDE_BIN`, else `claude` on PATH.

- **Role.** The agent definition is read from `.claude/agents/<role>.md` **at the base sha**, so an item cannot edit the file that judges it. `model_profile` and the round-2 escalation (opus, `xhigh`) set `model` and `effort`.
- **Profile.** The kit's `headless-child.settings.json` (engineers) or `critic.settings.json` (reviewers), plus the project's verify commands as `Bash(<command>:*)` allow rules (a command with shell syntax, a placeholder, `claude` or `sudo` is skipped), the runstate directory write-denied and the held-out tests read-denied, `CLAUDE_CODE_FORK_SUBAGENT=0` and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (V18), and the compact hook. `launch.validate_settings` refuses to launch a profile that allows `Workflow` (RC-17) or `claude` (CS-2), has a blanket allow, is not `dontAsk`, has the sandbox off, or lacks the kit hooks. The profile is checked once before any state changes and again before every launch.
- **Environment.** An allowlist: PATH, HOME, locale, toolchain locators, the Claude auth variables, `KIT_*`, `verify_env_allow`. No `AWS_*`, `GH_*`, `GITHUB_*`, `SSH_AUTH_SOCK`, `CLAUDECODE`.
- **Process.** Its own session and process group. The driver polls every `poll_s`: HALT, the lock, the canary, the child's last sign of life. A child is ended (SIGTERM, then SIGKILL after a grace period, the whole group) on `stall` (no tool call, no output for `dispatch_timeout_min`), `timeout` (six stall limits in all), `canary` (no SessionStart nonce within the wait), `halt`, `lock_lost` or `interrupted`. After a normal exit the group is swept once more, so a leaked dev server does not outlive its item (L-27).
- **Canary** (RC-7, 5.8). The child's SessionStart hook writes `nonces/<session id>`. A child that finished without one ran with its guards not loaded: an `env` failure (`canary_missing`), its work is not trusted.
- **Registration.** The session id is registered with the child's role (`registered_by: driver`) for the guards' caller classification (5.7) and unregistered afterwards. `children/<session>.json` names the child, its pid and the driver pid.

## Reports and verdicts

An engineer's report is checked by `report.read_report` (path, sections, status enum, JSON trailer). Anything wrong is `malformed_report`: an `item` failure, never an approval. `APPROVED`, `CONTRACT_DEVIATION` and `DESIGN_DEVIATION` let the item go on; `PARTIAL` is `fail item/partial`; `NEEDS_CLARIFICATION` is `Loop.clarify` (two rounds, answered from the plan in the brief's Clarifications section, then a human); `ESCALATION_NEEDED` and `CONTRACT_INCONSISTENCY` park the item. A reviewer's verdict is the line after `## Verdict`: `CODE_REVIEW_APPROVED`, `CHANGES_REQUESTED`, `SECURITY_APPROVED`, `SECURITY_BLOCKED`. No such line is `malformed_report`; `SECURITY_BLOCKED` parks the item (`security_blocked`).

## What a child's end means

| It showed | Class and reason | Then |
|---|---|---|
| a 529, "overloaded", a 5xx | `platform` / `overloaded`, `api_5xx` | backoff with jitter, no retry budget, at most `max_platform_retries`, then `needs_human: platform_outage` |
| a 429, "rate limit" | `platform` / `rate_limit` | the same |
| a signal, an exit other than 0 or 1, output that is not JSON, an error nobody can read | `platform` / `cli_crash` | the same: the CLI is not the item |
| no tool call for `dispatch_timeout_min` | `transient` / `stall` | up to `max_transient_retries`, then an item failure |
| six stall limits of wall time | `transient` / `timeout` | the same |
| `error_max_turns` | `item` / `partial` | continue the same engineer |
| a missing toolchain | `env` / `toolchain_mismatch` | the item is `blocked: env:...`, the level is capped at L1 |
| no nonce | `env` / `canary_missing` | the same |
| a clean result | none | read the report or the verdict |

## Money (10.5, IMP-25)

Before every launch the driver asks the ledger for the REMAINING budget (`--max-budget-usd`); after every child it records the result's `total_cost_usd`. A cost that is missing, renamed, negative, infinite or not a number is **unknown**, and an unknown cost parks the run until a human runs `loopctl approve cost`. A child that was killed or crashed without printing a result has an unknown cost too: it is recorded as such, the failure is recorded first (so the class, the counter and the backoff are kept), then the run parks. 80% of `run_budget_usd` parks (DEC-34). `run_budget_usd` is per `loopctl run`; `day_budget_usd` spans runs.

## Crashes, signals, humans

- **A driver that dies** (SIGKILL) leaves its child running in its own session and its lock behind. The next `loopctl run` takes over a lock whose driver pid is dead on this host (`RunLock.takeover`, atomic, whatever the heartbeat says; a live pid, another host, a conductor's lock or an unreadable lock is never taken), kills the orphan (`launch.reap_orphans`: only a process that is still a `claude` launched with the recorded session id), reconciles (`merged-recovered` from the `Loop-Item` trailer, in-flight items back to pending, orphan branches renamed `dead/*`) and carries on.
- **SIGTERM or SIGINT** ends the current child within one poll and the run with exit 1, leaving the worktree on the integration branch (WIP committed to the item's branch) and the lock released. A second signal unwinds at once.
- **HALT** (`loopctl halt`, `abort`) ends the child within one poll and the run with exit 3. Only a human's `loopctl resume` clears it.
- **A lost lock** (stolen, replaced, unreadable) ends the child and the run at once; the driver then writes nothing and does not touch the worktree.
- **A competing write** to the state is retried by the store with the driver's mutation re-applied; if the item moved on, the driver's step is refused and the run stops. Nothing a human wrote is overwritten.
- **Checkpoints.** `autonomy: per-feature` pauses after every merge, `per-milestone` at a milestone boundary, and an infra item always. The pause is in the state (`header.parked = checkpoint`); the next start needs `loopctl approve checkpoint <item> --by <name>` (or `--choice stop`).

## Config read

`runner`, `run_budget_usd`, `item_budget_usd`, `day_budget_usd`, `dispatch_timeout_min`, `max_platform_retries`, `max_transient_retries`, `platform_backoff_base_s`, `lock_stale_min`, `max_retries_per_item`, `max_consecutive_failures`, `max_design_rounds`, `autonomy`, `autonomy_level`, `security_review`, `model_profile`, `delivery`, `always_human_paths`, `roster`, `verify_*`, `verify_env_allow`, `gates.*`. No key is registered by this phase.

Test seams (honoured only when the binary is the fake): `KIT_FAKE_CLAUDE_HARNESS=1` allows the driver to run with `CLAUDECODE` set (never with a real binary: the file must carry the fake's marker); `KIT_DRIVER_TEST_TIMING` is a JSON object of `poll_s stall_s wall_s canary_wait_s term_grace_s beat_s`.

## Unverified on a live CLI (probes)

`--session-id` accepted with `-p`; `--allowedTools` taking one argument per rule; `--agents` with `--agent` and the definition's field names (V22); the settings `env` names for fork mode and background tasks (V18); `total_cost_usd` and the `subtype` values of the `-p` JSON result (IMP-25); whether the child's sandbox honours the `denyWrite` and `denyRead` entries (V5, V19, V24); that a SessionStart hook runs in `-p` and sees the passed session id. Each is in one place (`launch.py`) and the fake mirrors the assumed shape.

## Not built here

Pushing a branch or opening a PR (the item stops at `awaiting_human`; `pr_prepare` prints the human's commands), parallel items (Phase 8), notifications and the unattended mode (Phase 7), a measured cost for a child that printed nothing, the architect and design-critic stages (the plan item is the design), the remote lock (Phase 11).
