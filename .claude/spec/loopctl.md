# loopctl: the deterministic half of the loop

**Status**: Phase 4a (core). Implements report sections 9, 10 and 15 item 5/6 (`docs/design/review-2026-10.md`) under IMP-1, 11, 20-22, 24, 25 (`docs/design/v2-implementation-decisions.md`). Gates are Phase 4b, the driver Phase 4c.

`python3 .claude/scripts/loopctl <command>` is one Python 3.9 standard-library program that owns every *deterministic* decision the LLM conductor used to make from prose: state transitions, counters and caps, the lock, topological order, gate execution, the spend ledger, reconciliation, abort. The conductor keeps judgment: design, briefs, integration conflicts, summaries for humans.

It is **OPT-IN** (IMP-1). The V1 prose loop in `.claude/commands/orchestrate.md` is still the default (`runner: sequential`, `loopctl_tier2: false`), so unproven Tier 2 code never runs unless a human chooses it. If the baseline shows three or fewer control-flow rescues (kill criteria, `docs/baseline/kill-criteria.md`), Tier 2 is deprecated and Tier 1 stays.

| Tier | Commands | Needs |
|---|---|---|
| Read | `validate status render doctor audit report help version`, `lock status`, `evidence --check`, `cost --projection`, `reconcile --dry-run`, `migrate-plan --check\|--dry-run`, `run --dry-run`, `abort --dry-run` | nothing; they write nothing |
| Tier 1 writes | `lock acquire\|release\|steal`, `migrate-plan --write`, `halt`, `resume` | nothing (a kill switch and a lock must always work) |
| Tier 2 writes | `approve begin advance fail clarify park gate spend run abort reconcile --apply` | `loopctl_tier2: true` in the loop.config **at the base sha** |

Tier 2 is not evidence-gated: the evidence gate itself asks for three clean runs *with* loopctl. What the evidence gate caps is everything the report puts behind it: while `docs/baseline/evidence.json` is absent or incomplete (`loopctl evidence --check`), the effective level is capped at L3 and any evidence-gated config value (unattended L4/L5, `parallel_mode: worktree`, and so on) makes `run` and every Tier 2 command refuse.

## Rules every command follows

- **Base sha.** Config and plan are read from the commit a branch was cut from (`merge-base HEAD <default branch>`, or `--base`), never from the branch being judged (CO-11). A change to a protected key on the head branch is listed as `config drift` and takes effect only once a human merges it.
- **Git.** Fixed argument lists, `shell=False`, the user's global and system git config ignored, `core.hooksPath=/dev/null` (`kitlib.gitutil`). Nothing from a plan, report or issue is ever put on a command line unvalidated. loopctl's own git calls are outside the guards (RC-13); the caller rule below is the control that matters.
- **Callers.** Write commands are denied by the exec guard to anyone but the registered conductor session (`guards/g_exec.py`). The first word after `loopctl` is the command; loopctl accepts no option before it, so the guard and the program always agree on which command runs. `tests/loopctl/test_guard_alignment.py` compares the tables.
- **Text.** Everything printed from repository content passes `common.clean_text`: no terminal escapes, no bidi controls.
- **Exit codes.** 0 done; 1 the check found a problem; 2 usage or internal error; 3 refused by policy (Tier 2 off, HALT, lock held, canary missing, evidence gate, lost CAS).

## The plan (tracked, immutable on main)

```
plan/plan.json          {"schema": "loopctl.plan.v1", "name", "goal", "created"?, "milestones"?, "ext"?}
plan/items/<id>.json    one file per item (no merge conflicts in a machine-owned file, CS-22)
```

Item (`loopctl.item.v1`; template `.claude/templates/plan-item.json`; unknown fields are errors):

| Field | Meaning |
|---|---|
| `id` | letters and digits, starts with a letter, at most 24; must equal the file name; unique ignoring case |
| `milestone`, `title`, `slug`, `intent` | `slug` is `[a-z0-9-]`, it becomes `feat/<slug>-a<n>` |
| `size` | `S`, `M` or `L`. **`L` must be split** (`slice.size-l`) |
| `dependsOn` | item ids; the DAG must be acyclic and complete |
| `acceptance` | `[{"id": "AC-<item>-<n>", "text"}]`. At least one criterion must be falsifiable (a code span, a request line, a status code, an exit code, a named test that must pass); "the app boots" is refused. Ids are **frozen at H1**: the approval records a digest per item |
| `requiresEnv` | variable names that must exist before the item starts |
| `touches` | `{domains, paths, schema, deps_change}`; `paths` are safe relative globs. Other items' footprints become `locked_paths.json` while one item runs |
| `estimate` | `{loc, engineers}`; `loc` above `max_item_loc` is an error; more than three engineers is an error, more than two a warning |
| `riskClass` | `low`, `medium`, `high` (high needs a human at merge) |
| `migrations` | at most one per slice. `{name, up, down, dataLossClass, stage?, noDownReason?}`; `dataLossClass` is `none`, `lossy-recoverable` or `lossy` (CO-18). A missing `down` is allowed only for `lossy` with a `noDownReason`. `lossy` is human-gated and its down is never run by the loop |
| `verify` | `{tests, demoRoute}`: feeds `verify.py`'s items file (`plan.verify_items_file`) |
| `rework`, `waivers` | `waivers[]` = `{approver, expiry, finding_ids, reason, commit?}` (H6); an expired waiver is listed under `status --pending-human` |
| `ext` | free-form extension data for gates, at most 8 KiB |

Field spelling follows the report where it spells a field (`deps_change`, `finding_ids`, `failure_class`, `total_cost_usd`) and camelCase elsewhere, as v1 did.

`validate` reports: schema and unknown fields, AC ids and falsifiability, missing dependencies and cycles (the cycle is printed), the topological order with a deterministic tie-break (milestone, then natural id order, L-13), the **critical path** (longest dependency chain), the **DAG width** (the largest set of items with no path between any two, by Dilworth, plus the levels), the slicing rules (L-35), and data-loss classes. Plan files are untrusted input: duplicate JSON keys, NaN, deep nesting, oversize files, symlinks and ids that could become a path or a ref are all refused.

`migrate-plan` converts a v1 `build-plan.json` (mapping in `loopctl/migrate.py`): `reworkNotes` to `rework[]`, `securityWaiver` to `waivers[]` (expiring today: a human renews it), `downMigrationRef` to `migrations[].down`, statuses to a **state seed** applied by `approve plan --seed-v1`. Unknown fields fail loudly and nothing is written.

## State

Item status is on **`refs/loopctl/state/<plan>`** at every level, never in a tracked file. The ref points at a commit whose tree is exactly `state.json`; each write adds a commit (the audit trail) and moves the ref by compare-and-swap (CAS, defined once in report section 10.3):

- L1 (nothing pushed): `git update-ref <ref> <new> <old>`.
- L2+ (`state_remote: <remote name>`): `git push --force-with-lease=<ref>:<old> <remote> <new>:<ref>` first, then the local CAS. Never a bare `--force-with-lease`, never a URL.

A loser of a race re-reads, re-applies its mutation to the fresh state and fails if the mutation no longer makes sense there. A state ref that is not a valid loopctl state (wrong tree, wrong plan, schema violations, a sequence number that does not follow its parent) is refused, never read as "no state" and never repaired. `reconcile` checks every `merged` item against git.

```
pending -> designing -> design_review -> stubbing -> building -> verifying -> reviewing
        -> pr_open -> awaiting_human -> merged | needs_human | blocked | abandoned
```

The enum is closed. `blockReason` is required on `blocked` and `needs_human`. `advance` takes only forward edges (design_review may skip stubbing to building when `gates.g2` is not off); a step back is `fail`. `advance <item> merged` needs `--merge-commit` (it must exist and be on the integration branch; under `delivery: local` it must carry `Loop-Item: <id>`), a human approval record for a human-gated item or one in `awaiting_human`, and every gate that is `on` and marked `REQUIRED_FOR` that transition.

`begin` requires: the lock held by `--session`, no HALT, an approved plan with unchanged digest and unchanged ACs, every dependency merged, no other item in progress, no circuit-breaker stop, no environment demotion.

**Counters** (durable, per attempt, independent, never multiplied, L-8): `verify`, `review`, `partial`, `design`, `ci_fix`, `transient`, `clarify`, plus `platform` and `discards` across attempts.

### Failure taxonomy (`failure_class`, CO-8) and caps

| Class | Examples | Retry? | Uses retry budget | Trips the breaker |
|---|---|---|---|---|
| `platform` | `api_5xx`, `overloaded`, `rate_limit`, `cli_crash`, `network` | yes, backoff with jitter, up to `max_platform_retries`, then `needs_human: platform_outage` | no | no |
| `transient` | `flaky_test`, `port_in_use`, `stall`, `timeout` | yes, up to `max_transient_retries` (2), then treated as `item` | yes | no |
| `env` | `toolchain_mismatch`, `db_down`, `missing_env_var`, `doctor_failed`, `tree_dirty`, `canary_missing` | no: item `blocked` (`env:<reason>`), effective level capped to L1 until `approve env` | no | no |
| `item` | `tests_failed`, `review_blocked`, `malformed_report`, `reward_hack`, `partial`, ... | up to `max_retries_per_item` per loop | yes | yes |

A known reason keeps its class: recording `tests_failed` as `platform` is refused. Exhaustion (cap, the same failing AC ids twice, a reward-hack signature) **discards** (RC-19): attempt a<n+1> from the item's base, counters reset; a second discard is a hard stop (`blocked: retries_exhausted`, breaker +1, `max_consecutive_failures` stops the run). A design critic past `max_design_rounds` or CI past `max_ci_fix_attempts` is a hard stop with no discard. Discard runs only in the registered loop worktree (`run.json`), on the item's own branch: `reset --hard`, `clean -fd`, back to the integration branch, rename to `dead/<slug>-a<n>`. Parked branches become `blocked/<slug>-a<n>` and are never deleted; `dead/*` is pruned after `orphan_retention_days`.

### Spend (section 10.5)

`spend record` adds a child session's `total_cost_usd` to `<git-common-dir>/loopctl/spend.jsonl`. The remaining budget (`min` over `run_budget_usd`, `item_budget_usd`, `day_budget_usd` that are set) is what the driver passes as `--max-budget-usd` (`Ledger.launch_budget`). **An unknown cost parks**: a missing, renamed, non-numeric, negative or infinite cost, or an unreadable ledger line, parks the run until a human runs `approve cost`. `cost --projection` prints `unavailable (no baseline)` until `docs/baseline/results/` holds completed runs with a spend.

### The lock (section 19.7)

`<git-common-dir>/loopctl/lock`, created exclusively, `{runId, sessionId, host, worktree, driverPid, startedAt, heartbeat, planRev}`. Stale only when the heartbeat is older than `lock_stale_min` **and** the owner is gone (driver pid dead; a lock with no driver pid has only the heartbeat to go on). A lock from another host or an unreadable lock is never stolen without an explicit flag. Steal is an atomic rename, then a create-exclusive.

### Other state files

`run.json` (current run: `runId`, `conductor_session_id`, `baseSha`, `worktree`, `runner`), `sessions/<id>.json` (the conductor registration `run` writes), `HALT`, `spend.jsonl`, `locked_paths.json` (`{"globs", "sources"}`; read by the file guard), `abort/revert-plan-<run>.md`, `events/loopctl.jsonl`.

## Gates (plug-in contract, IMP-22)

A gate is a module `loopctl/gates/<file>.py` with `NAME` (the `gates.<NAME>` toggle), `OWNER_METRIC` and `run(ctx) -> GateResult`; optionally `DEFAULT_MODE` (`off` for OPT-IN gates), `EVIDENCE_GATED`, `REQUIRED_FOR` (transitions that need it). Known names: `g0 g1 g2 g3 g4 g5 g6 g7 g9 g10 g11 evidence` (G8 is a view in `status --matrix`).

`GateContext` carries `repo`, `item_id`, `item` (plan definition), `attempt`, `base_sha`, `head_sha`, `cfg` (read at the base sha), `plan`, `state_item` (copy), `paths` (`repo`, `runstate`, `heldout`, `reports`), `runner`, `level`, `env`, `evidence_passed`, `worktree`, and `git(args)`. `GateResult` has `status` (`pass fail error skipped`), `findings`, `evidence`, `failure_class`. Per-AC evidence goes in `evidence["ac_results"] = {"AC-F1-1": {"result": "pass", "evidence": "<test>"}}`; verdicts in `evidence["review"] = {"code": ..., "security": ...}`. The **runner** applies the toggle read from the base sha: `off` skips, `report` records and never blocks, `on` blocks on `fail` and `error`. A gate that crashes or returns nonsense is an `error` with class `env`. An evidence-gated gate refuses to run until the evidence gate passes. `loopctl gate <name> <item>` runs one and records it; the AC x evidence matrix in `status --matrix` is built from the recorded results.

## The driver contract (Phase 4c)

`runner: driver` makes `run` import `loopctl.driver` lazily and call `driver.run(ctx, loop, run_record, as_json) -> int` after taking the lock with `driverPid`. `ctx` is `loopctl.context.Context`, `loop` is `loopctl.state.Loop` (`begin`, `advance`, `fail`, `park`, `clarify`, `record_gate`, `approve`, `reset_inflight`), `ctx.ledger()` is the spend ledger, `ctx.gate_context(...)` builds a gate's input, `failure.classify_exit(...)` maps what a child showed to a class and reason. Not installed: `run` says "driver not installed" and exits 2 before it touches the lock.

## Canary

`run` (sequential) needs `--session` and the `--nonce` that `session_start.py` wrote for that session. A session without a nonce does not dispatch (hooks may not be firing). The same check guards `lock acquire`.

## Audit, reconcile, abort

- `audit render <item>` builds the PR audit block from state, plan, gate results, engineer reports, the ledger and git, in the shape of `.claude/templates/pr-body.md`, leaving `<fill: ...>` where it has no fact; `audit check` runs `pr_prepare.validate_body` plus cross-checks (stale head sha, missing ACs, a PASS the state contradicts, CI-fix count, waivers, the cost line).
- `reconcile` recovers a merge lost to a crash (exact `Loop-Item` trailer on a merge of `feat/<slug>-a<n>`), detects missing merge commits and human reverts, resets orphaned in-flight items, sweeps orphan branches, folds in recorded PR facts (`--prs`) and the ruleset bypass log (`--rule-suites`).
- `abort` sets HALT, lists merged items after `--rollback-to` in reverse merge order, and for items **not yet on main** runs `db_migrate_down` (never a `lossy` one) then `git revert -m 1` in the loop worktree. After merge to main it executes nothing and writes a revert plan for a human (report section 10.7, RC-14).

## Config keys (owner `4a-loopctl-core`, all H6 except two)

`runner`, `loopctl_tier2`, `run_budget_usd`, `item_budget_usd`, `day_budget_usd`, `lock_stale_min`, `max_transient_retries`, `max_platform_retries`, `platform_backoff_base_s`, `dispatch_timeout_min`, `orphan_retention_days`, `state_remote`, `db_migrate_down`. Existing keys it reads: `max_retries_per_item`, `max_consecutive_failures`, `max_design_rounds`, `max_ci_fix_attempts`, `max_item_loc`, `delivery`, `integration_branch`, `protected_branches`, `always_human_paths`, `roster`, `gates.*`.
