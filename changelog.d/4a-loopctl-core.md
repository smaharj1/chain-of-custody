# 4a-loopctl-core: loopctl state, lock, taxonomy, ledger, abort, reconcile, status, plan validation and the gate runner (OPT-IN)

## Changes

- `.claude/scripts/loopctl/` (run as `python3 .claude/scripts/loopctl <command>`; Python 3.9, standard library, git as a subprocess with fixed argv, `shell=False`, global config ignored, hooks and `core.fsmonitor` pinned off). Spec: `.claude/spec/loopctl.md`.
  - `plan.py`: `plan/plan.json` + `plan/items/<id>.json` (`loopctl.plan.v1`, `loopctl.item.v1`), strict parsing, AC ids frozen at H1, the dependency DAG with cycle report, deterministic topological order, **critical path**, **DAG width** (largest antichain, by Dilworth), slicing checks (`size: L` must be split, `max_item_loc`, one migration, engineers, AC count), data-loss class on every migration (CO-18), waivers, falsifiable-AC check, `item_human_gate`, `verify_items_file` (feeds `verify.py`).
  - `state.py`: the closed 13-state machine, `refs/loopctl/state/<plan>` written by compare-and-swap (L1 `update-ref`; L2+ `state_remote` push with an explicit lease), forged-ref refusal, `Loop` facade (`approve_plan`, `begin`, `advance`, `fail`, `park`, `clarify`, `record_gate`, `approve`).
  - `lock.py` (O_EXCL lock, heartbeat, stale rule, atomic steal), `failure.py` (platform / transient / env / item, caps, no-progress, discard then hard stop), `ledger.py` (spend, **unknown cost parks**, run/item/day ceilings, 80% park, remaining budget for `--max-budget-usd`, projection), `branches.py` (feat/dead/blocked lifecycle, orphan sweep), `locked.py` (`locked_paths.json`), `lgit.py`.
  - `gates/__init__.py`: the plug-in contract (`NAME`, `OWNER_METRIC`, `run(ctx)`, optional `DEFAULT_MODE`, `EVIDENCE_GATED`, `REQUIRED_FOR`), `GateContext`, `GateResult`, the runner that applies `on|report|off` from the base sha. **No gate is implemented here** (4b).
  - `status.py` (status, `--pending-human`, AC x evidence matrix, bypass log), `render.py`, `reconcile.py`, `abort.py`, `audit.py` (the PR audit block, built from state and validated with `pr_prepare`), `report.py` (engineer reports and the JSON trailer), `migrate.py`, `startrun.py` (`run`, `run --dry-run`, the canary), `context.py`, `cli.py`.
- CLI, with the classification the guards use:
  - **read** (write nothing): `validate status render doctor audit report help version`, `lock status`, `evidence --check`, `cost --projection`, `reconcile --dry-run`, `migrate-plan --check|--dry-run`, `run --dry-run`, `abort --dry-run`
  - **Tier 1 writes** (work with `loopctl_tier2: false`): `lock acquire|release|steal`, `migrate-plan --write`, `halt`, `resume`
  - **Tier 2 writes** (refuse unless `loopctl_tier2: true` in the base sha's config): `approve begin advance fail clarify park gate spend run abort reconcile --apply`
- Guards changed (see "Conductor"): `g_exec.py` (`audit`, `report` are reads; `run`, `abort` take `--dry-run`; `migrate-plan --dry-run`; write flags and options before the command make any invocation a write; shared `loopctl_is_read`), `g_halt.py` (`python3 .claude/scripts/loopctl <read>` stays available under HALT; `resume` is no longer allowed to a halted session), corpus `tests/guards/corpus/loopctl.json` (100 rows), `docs/security/guards.md`.
- `kitlib/jsonio.read_json` turns a `RecursionError` from a deeply nested file into `ValueError` (a real gap: a nested `HALT`, `run.json` or lock made every reader crash); test in `tests/kitlib/test_jsonio.py`.
- `.claude/templates/plan-item.json`, `examples/hello-loop/plan/` (the fixture's `build-plan.json` migrated, plus footprints, estimates, verify commands and the F1 migration), the loopctl hand-off block in `.claude/commands/orchestrate.md`.
- `tools/baseline/prepare.py`: `plan` joins `FIXTURE_EXCLUDE`, so a V1 baseline run never receives the v2 plan.
- Config schema `kitlib/schema/4a-loopctl.json` (13 keys); `.claude/loop.config.md` and `examples/loop.config.md` regenerated.
- Tests: `tests/loopctl/` (285 tests, 16 modules, each under 9 s alone), `tests/guards/corpus/loopctl.json`. Whole suite 1607 tests, about 42 s on 10 cores.

## Upgrade impact

Nothing changes for anyone who does not opt in. `loopctl_tier2` is false and `runner` is `sequential`, so the V1 prose loop in `orchestrate.md` is unchanged and no loopctl state is written. A project that opts in commits `loopctl_tier2: true` (an H6 change a human reviews), `plan/` and a `loop.config.md`, then `python3 .claude/scripts/loopctl approve plan --by <you>`. `loopctl migrate-plan --write` converts a v1 `build-plan.json`; `approve plan --seed-v1 build-plan.json` carries the v1 statuses over (done items only if their merge commit exists).

Guards: after activation (IMP-13) `loopctl audit|report` are open to every caller and `loopctl resume` is denied to a halted session. Both are tightenings or read-only additions.

## New config keys

All owner phase `4a-loopctl-core`, section "loopctl". H6-protected except `platform_backoff_base_s` and `orphan_retention_days`. None is evidence-gated (see Decisions).

- `runner` (`sequential | driver`, default `sequential`): the doctor already read it.
- `loopctl_tier2` (bool, false): Tier 2 writes are OPT-IN.
- `run_budget_usd`, `item_budget_usd`, `day_budget_usd` (numbers, 0 = not set): `run_budget_usd` is the name the doctor already read.
- `lock_stale_min` (60), `max_transient_retries` (2), `max_platform_retries` (6), `platform_backoff_base_s` (30), `dispatch_timeout_min` (20, read by the driver), `orphan_retention_days` (7).
- `state_remote` (string, empty): remote that also holds `refs/loopctl/state/<plan>`; a name, never a URL.
- `db_migrate_down` (command, empty): the L-25 key the report says "is added to loop.config"; nobody had registered it. 4b must not redeclare it.

Read, not registered here: `max_retries_per_item`, `max_consecutive_failures`, `max_design_rounds`, `max_ci_fix_attempts`, `max_item_loc`, `delivery`, `integration_branch`, `protected_branches`, `always_human_paths`, `roster`, `autonomy_level`, `gates.*`.

## Pending-human actions

- Run hello-loop (or a real feature) with `loopctl_tier2: true` at L1 and compare loopctl's view with the human's (the Phase 4 exit criterion needs live runs): `tools/baseline/run-baseline.sh` conditions plus `loopctl run`.
- Confirm on the pinned Claude Code that the SessionStart line (`canary session=... nonce=...`) is what the conductor sees, that `!` commands do or do not carry `CLAUDECODE` (`loopctl resume` follows `run.py clear-halt` and refuses when it is set), and that the child `-p` JSON field is `total_cost_usd` (IMP-25: if not, the ledger records `unknown` and parks).
- Confirm the shapes `reconcile` assumes: `gh pr list --json number,state,isDraft,headRefName,url,mergeCommit,statusCheckRollup` and the ruleset rule-suite entries (`evaluation_result: bypass`, `actor_name`, `ref`, `pushed_at`) used for the bypass log (RC-9). `--live` runs `gh` and is human-only; no test touches the network.
- Run `python3 .claude/scripts/loopctl doctor --level` on the real machine (the tests pin `KIT_CLAUDE_VERSION`; the real `claude` is never started by loopctl).
- Commit `docs/baseline/evidence.json` only when the evidence gate is genuinely met (`loopctl evidence --check`).

## Docs impact

- README: the scripts table gains `loopctl` (and `.claude/spec/loopctl.md`); the V1 vs v2 loop section should say the prose loop is the default and loopctl is OPT-IN.
- GLOSSARY: loopctl, Tier 1 / Tier 2, state ref, CAS, run lock, canary, failure class, discard, breaker, bypass log.
- docs/getting-started.md: "start the loop with loopctl" (approve plan, run, status --pending-human, resume after a HALT, approve breaker after an abort).
- docs/faq.md: "Why does loopctl refuse?" (Tier 2 off, base sha, evidence gate, HALT, lock held, canary).
- `.claude/spec/loop-engineering.md`: the tool table row for loopctl and the LOCKED banner (release stage); `build-plan.md` template gets a note that `plan/` is the v2 form.
- CHANGELOG: "Added" entry for loopctl core.

## Decisions made

- **Tier 2 is not evidence-gated.** The evidence gate asks for three clean runs *with* loopctl; gating the commands those runs need would be circular. Tier 2 is OPT-IN by `loopctl_tier2`. What the evidence gate caps is what the report puts behind it: while `docs/baseline/evidence.json` is incomplete, `run` and every Tier 2 command refuse any evidence-gated config value (`Config.evidence_gated_violations`), the level is shown capped at L3, and evidence-gated gates refuse to run. `runner: driver` is not evidence-gated either (L2 needs it).
- **The base sha is anchored.** Tier 2 refuses a `--base` that is not on the integration branch or is older than the base the run started from, so a caller cannot read a weaker config by naming an old commit.
- **Field spelling.** Keys the report spells (`deps_change`, `finding_ids`, `failure_class`, `total_cost_usd`) keep that spelling; everything else in plan and state is camelCase like v1. Item ids are letters and digits only so `AC-<item>-<n>` splits cleanly and an id is safe as a file name and ref component.
- **Slicing.** Errors: `size: L`, `estimate.loc` over `max_item_loc`, more than one migration, more than three engineers. Warnings: more than two engineers, more than three acceptance criteria (the hello-loop fixture has five-line criteria lists and must stay valid). `validate` also errors when no criterion is falsifiable.
- **Width** is the true maximum antichain (a level count can understate it), with one witness set.
- **Failure classes are fixed per reason.** `tests_failed` cannot be recorded as `platform`. An unknown reason is accepted with the class the caller gave and is still capped. Transient failures escalate to item failures after `max_transient_retries`. An env failure parks the item as `blocked: env:<reason>` and caps the effective level at L1 until `approve env`. A design-critic or CI-fix cap is a hard stop with no discard. The circuit breaker counts hard stops after a second discard, stops the run at `max_consecutive_failures`, and resets on a merge.
- **Unknown cost parks.** Missing, renamed, non-numeric, negative, infinite or implausible `total_cost_usd`, or an unreadable ledger line, parks the run until `approve cost`. At 80% of `run_budget_usd` the run parks (DEC-34); the remaining budget is divided by the live children.
- **Gates.** A gate that crashes or returns nonsense is an `error` with class `env`, never a pass. An evidence-gated gate refuses to run. `REQUIRED_FOR` transitions are enforced only for gates that are installed and `on`, so 4a alone does not block the prose loop.
- **Approvals are records, not authentication.** At L1 the conductor writes `approve ... --by <name>` after the human said so in the chat (L-10); `--by` is not verified. The boundary at L3+ is the PR review and CODEOWNERS, not this record. A plan revision that changes the criteria of an item that already has work (or is merged) sends it to `needs_human` (`ac_changed`).
- **Approvals.** A named human (`--by`), recorded with the plan digest or the attempt; consumed once; a reject at a checkpoint resets the item with the feedback; a human-gated item (lossy down-migration, high risk class, footprint inside an always-human path, waiver, dependency change) and anything in `awaiting_human` need an approval to merge.
- **Reconcile trusts git, not messages.** A merge is recovered from `Loop-Item` only if the commit is a merge of `feat/<slug>-a<n>` with that exact trailer; merged `feat/*` branches are deleted, unmerged orphans renamed `dead/*`, `dead/*` retention counts from the rename, `blocked/*` is never touched.
- **Abort** executes only before merge to main: `delivery: local`, merge commit in no remote protected branch, in the registered loop worktree on the integration branch with a clean tree. Down-migrations run first (never a `lossy` one, never with the caller's environment), then `git revert -m 1`; a conflict or a failed down aborts and turns the rest into a written revert plan under `<git-common-dir>/loopctl/abort/`. After merge nothing is executed.
- **`resume`** refuses when `CLAUDECODE` is set (as `run.py clear-halt` does) and is no longer in `g_halt`'s read set: HALT is not the halted session's to release. `halt` is always allowed.
- **`render`, `status`, `audit` print only.** A file is written by redirecting stdout, which the guards judge as the file write it is.
- **Costly but intentional:** every command re-reads the state ref and the base sha (about 12 git calls); nothing is cached across processes.
- **One accidental real-binary run.** An exploratory run of `loopctl doctor` in a temp repo, before the tests pinned `KIT_CLAUDE_VERSION`, made the Phase 1 doctor execute the `claude` on PATH for `--version` (the doctor only declines inside a Claude Code session, and the test environment had stripped `CLAUDECODE`). Nothing else ran. The test helper now pins the version and points `KIT_CLAUDE_BIN` at a path that does not exist. Worth knowing: `doctor.py` run from a scrubbed environment will start `claude --version` unless a version is given.

Adversarial attempts, each with a test (`tests/loopctl/`):

- forged state ref: ordinary commit, blob, tree, extra or renamed files, hostile JSON (duplicate keys, NaN, deep nesting, empty), another plan's state, a broken sequence, a plausible forged `merged` (caught by `reconcile`; dependents do not start) (`test_store.ForgedRef`)
- stale, corrupt and foreign locks; live driver pid; a tiny TTL; concurrent acquire by eight threads (`test_lock`)
- concurrent compare-and-swap: injected race, four racing `begin`s, exhausted retries leave the state untouched, remote moved or diverged (`test_store`)
- hostile item JSON: duplicate keys, NaN, deep nesting, wrong types, unknown fields, symlinked item files in the tree and in git, case-colliding ids, stray files, oversize files, 300+ items, footprints that lock the whole tree or escape the repo (`test_plan`, `test_adversarial`)
- path traversal and injection through item ids, branch names, session ids, report paths, `--from`, `--merge-commit`, `--rollback-to`, `--base`, `--pr-url`, failing AC ids (`test_adversarial.Injection`, `test_report`, `test_migrate`)
- a config change on the head branch: raised budgets and caps, a loosened LOC limit, a gate turned off, Tier 2 opted into, weakened acceptance criteria, a rolled-back or off-branch `--base` (`test_run`, `test_gates`, `test_adversarial.HeadBranchChanges`)
- unknown, negative, infinite, renamed and string costs; forged ledger lines; a tampered ledger (`test_ledger`, `test_adversarial`)
- a hostile git config: planted hooks, `core.fsmonitor`, pager, editor, external diff, aliases, a global config (control proves plain git runs them; loopctl does not) (`test_adversarial.HostileGitConfig`)
- failure laundering (recording an env failure as platform), approval replay, an approval for the wrong item, reading the working tree instead of the base, a forged conductor registration, terminal escapes and bidi controls in names, a second audit block smuggled in through item text
- found and fixed on the way: unreadable-lock handling (a lock file containing `null`), `RecursionError` from deeply nested state files, a hostile `intent` breaking the audit block markers, frozen ACs checked only at `begin`, `--base` as a way to pick a weaker config, a `--result -` read before validation, dead-branch retention counted from the commit date.

## Conductor: pointers to add

- Merge order: 4a first, then 4b and 4c. Regenerate the config templates after merging 4b/4c: `python3 tools/gen-config.py --write` (all three add schema files; the generated `.claude/loop.config.md` and `examples/loop.config.md` will conflict textually).
- Registry facts for 4b and 4c: registered here (do not redeclare) `runner`, `loopctl_tier2`, `run_budget_usd`, `item_budget_usd`, `day_budget_usd`, `lock_stale_min`, `max_transient_retries`, `max_platform_retries`, `platform_backoff_base_s`, `dispatch_timeout_min`, `orphan_retention_days`, `state_remote`, `db_migrate_down`.
- Files outside this phase's ownership that changed, with the reason: `.claude/scripts/guards/g_exec.py` and `g_halt.py` (command classification; contract in the brief), `tests/guards/test_corpus.py` (one file name added to `FILES`) and `tests/guards/corpus/loopctl.json`, `docs/security/guards.md` (the loopctl paragraph), `.claude/scripts/kitlib/jsonio.py` and `tests/kitlib/test_jsonio.py` (RecursionError), `tools/baseline/prepare.py` (`FIXTURE_EXCLUDE`), `.claude/commands/orchestrate.md` (only the new "loopctl hand-off" block; Phase 9 converts it).
- `.claude/spec/loop-engineering.md`: add `.claude/spec/loopctl.md` to the pointers and the tool table; release stage updates the LOCKED banner (IMP-12).
- Driver (4c): implement `loopctl/driver.py` with `run(ctx, loop, run_record, as_json) -> int`; the lock is already held with `driverPid`; use `ctx.ledger().launch_budget(cfg, run_id, item, live_children)` for `--max-budget-usd`, `failure.classify_exit`, `Loop.fail` (it executes the discard or park branch operations in the registered loop worktree), `loop.record_gate`, `ctx.gate_context`. Gate authors (4b): `gates.GateContext`, `GateResult`, `locked.add_gate_globs` for pinned acceptance tests, `ctx.paths["heldout"]`.
- 4c fault-injection cases that reuse 4a: stale lock (`lock.is_stale`, `lock steal`), CAS conflict (`StateStore._before_cas` hook shows the pattern), abort mid-item (`abort.execute`), crash mid-item (`reconcile --apply`), malformed report (`report.read_report`), 529 (`failure.classify_exit` -> `platform`).
