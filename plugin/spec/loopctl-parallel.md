# loopctl parallel items: `parallel_mode: worktree`

**Status**: Phase 8, OPT-IN, experimental and **evidence-gated** (IMP-1). Implements report sections 11.5 item 3, 10.3, 19.7 (L-35 slicing, L-6 reject cascade, the lock), 16.1 row 8 and DEC-9, DEC-33, DEC-34 (`docs/design/review-2026-10.md`) under IMP-1, 6, 14, 24 and 25 (`docs/design/v2-implementation-decisions.md`). The state, the lock, the ledger, the gates and the child launch line are 4a-4c (`${CLAUDE_PLUGIN_ROOT}/spec/loopctl.md`, `loopctl-driver.md`); the unattended hooks are Phase 7 (`${CLAUDE_PLUGIN_ROOT}/docs/unattended.md`). Operator guide: `${CLAUDE_PLUGIN_ROOT}/docs/parallel.md`. The Workflow pipeline host is DESIGN-ONLY: `${CLAUDE_PLUGIN_ROOT}/spec/pipeline-host-workflow.md`.

`parallel_mode: off` (the default) is the 4c driver, untouched: the package `loopctl/parallel/` is not even imported. `parallel_mode: worktree` needs `runner: driver`, a passed evidence gate (`loopctl evidence --check`) and `max_parallel` of 1 to 4 (default 2). It refuses to start otherwise: `loopctl run`'s preflight prints the `parallel.*` rows, `Context.require_tier2` refuses the evidence-gated value, the doctor has `parallel.mode` and `config.evidence-gate`, and the scheduler checks the evidence again when it sets up.

## The shape of a run

`driver.run` builds a `ParallelDriver` (`parallel/scheduler.py`) in place of a `Driver`. It **is** a `Driver`: the same lock holder (`driverPid`), the same `setup` (nesting, version, profiles, orphans, signals, recovery), the same checkpoints, the same budget parking and the same four unattended hooks. Only `run_items` differs.

```
main thread                                              one Worker thread per item (parallel/scheduler.Worker)
  admit      lease + mutexes + port slot, Loop.begin,      designing -> design_review -> (stubbing) -> building
             branch off the integration tip,                 -> verifying -> reviewing -> READY   (or awaiting_human)
             git worktree add, start the worker              the serial driver's stages, in the item's own worktree
  harvest    a worker ended: ready | waiting for a human |
             parked | discarded | drained | crashed
  integrate  ONE ready item at a time, in plan order        (parallel/integrate.py: the only writer of the integration branch)
  invalidate an item whose ancestor is no longer merged     (parallel/cascade.py)
```

A `Worker` is a `Driver` that does not run its own loop: it takes everything shared (config, plan, base sha, run id, session id, heartbeat, timing, event list, observers, the pass counters) from the parent through `__getattr__`, and owns what is per item: its worktree as `repo`, a `Context` whose repo is that worktree (so a gate's `repo`, `worktree` and report path are the worktree's), its own `Loop` (no branch side effects: `worktree_is_loop_worktree()` is false there), and its environment with the port block. It never merges, never switches the loop worktree, never renames a branch. The main thread does all of that, because git does not like two writers in one worktree and the report says there is one writer (D-6).

## Admission (`ParallelDriver.admit`)

An item starts when all of these hold, checked in plan order:

1. it is `pending`, every dependency is `merged` **on the integration branch** (an item that is only ready or waiting for a human does not count), and any platform backoff has passed;
2. `footprint.conflict` finds nothing against **every item that holds a worktree or waits for a human**, nor against an earlier item that was skipped this round (a later item may not overtake a skipped one it conflicts with, so a stream of small items cannot starve a large one):
   - the declared `touches.paths` may not overlap (`footprint.may_overlap`, conservative: when it cannot prove two globs apart it says they meet);
   - an item that declares no `touches.paths` runs alone;
   - the lanes may not meet: **schema** (a migration or `touches.schema`, "the schema mutex that is always on") and **deps** (`touches.deps_change`, one lockfile);
3. its **item lease** is free (two workers cannot hold one item), its lane **mutex leases** are free (the same rule as hard state, not only a computed rule), and a **port slot** is free and its ports are not in use;
4. fewer than `max_parallel` worktrees exist (a finished item still holds its worktree until it is merged);
5. `Loop.begin(max_active=max_parallel)`, the one state change that makes the item `designing`.

A schema or deps mutex is held from the item's start until it has merged, been parked, discarded or put back. While an item waits for a human it still occupies its footprint and lanes in the computed rule, but its leases are released (they are re-derived from the state, so a long wait does not hold a lease for a human).

## Leases (`parallel/mutex.py`)

`<git-common-dir>/loopctl/leases/<name>.json`, one claim per file, every read-modify-write under an exclusive `flock` on `leases/.guard`:

| name | meaning |
|---|---|
| `item-<id>` | a worker's claim on one item |
| `mutex-schema`, `mutex-deps` | the lanes |
| `ports-<n>` | a port slot |
| `integrate` | the integration slot |

A record is `{schema, name, token, owner, pid, host, acquired, hb, ttl_s, meta}`. The **token** is random and secret to the holder; renew, check and release compare it, so a holder whose lease was reclaimed finds out (`holds()` is false) instead of acting on a claim that is no longer its own. A lease is **reclaimable** when its owner is certainly gone (a dead pid on this host, so no heartbeat can come: the lock's takeover rule) or when its heartbeat is older than `lease_ttl_s` **and** an `alive(record)` callable says the owner is gone (default: the same pid test; another host is never judged). An unreadable lease counts as held. The scheduler renews every lease it holds every `lease_ttl_s / 5`; a lease it cannot renew was taken, so the worker is cancelled and the item put back.

## Ports (`parallel/ports.py`)

`verify_ports` are the project's declared ports. A worker holds a slot `n`; every port is shifted by `n * 100`. A slot is skipped when its lease is held or when anything listens on any of its ports (`verify.port_busy`). The shift reaches the child and the gates as `KIT_PORT_OFFSET` (plus `KIT_PORT_SLOT`, `KIT_PORTS` and `KIT_ITEM` for the project's own scripts, for example to name an ephemeral database); G6 hands it to `verify.verify(port_offset=)`. With no free slot the item waits.

## Worktrees (`parallel/worktrees.py`)

The only place a worktree is created or removed. `<repo>/.claude/worktrees/<slug>-a<n>`, `git worktree add <path> <branch>` of the branch the driver cut off the **local** integration tip (D-3). Every path passes `safe_path` or `inside_root`: a plain name, directly under the root, no symbolic link on the way. `.claude/worktrees/` is added to the clone's `info/exclude` so the loop worktree stays clean. `.worktreeinclude` is read at the base sha; only `.env.local` and `.env.test` are ever copied (0600; a symlink is refused). A crash leaves worktrees behind: `cleanup_orphans` removes the ones named `<slug>-a<n>` that no live worker owns, **after** committing their uncommitted work to the item's own branch (`WIP(loopctl)`), so the recovery that renames the branch to `dead/*` keeps it.

## Integration (`parallel/integrate.py`)

A worker that has the item reviewed and green does not merge. For an item that needs no human (`delivery: local`, no `humanGate`, no infra footprint) it ends **READY**; any other item goes to `awaiting_human` as in the serial driver (and is merged by `merge_approved`, unrebased: its head is what the human approved). The integrator takes the `integrate` lease and, for the lowest-numbered ready item:

1. checks both leases (renewed, token compared);
2. **footprint**: the diff `base..head` (`--no-renames`) must be inside the item's declared globs plus its own `docs/features/<slug>/`. A file outside is a **footprint lie**: park `footprint_violation` (needs a human), never merge;
3. **rebase** onto the integration tip when the branch does not contain it (in the item's worktree; a conflict is aborted and parks `merge_conflict`);
4. **migration order**: a migration `up` file the item added must sort after every file in its directory on the integration tip, else `item/migration_order` sends the item back to its engineer to regenerate it (the tooling that numbers migrations is the engineer's);
5. **re-verify**: after a rebase EVERY deterministic gate Evidence will check runs again in the item's worktree: all of `driver.VERIFY_GATES` (`g5 g1 g3 g6 g9 g11 docs g10 g4`, `off` ones skipped, a blocking `g5` ends the round) and `g7` (a read of the committed review files; the LLM reviews are not re-run, the rebase did not change the item's own diff), then whatever the merge transition still requires. Only LINEAR history is rebased: an item branch holding a merge commit parks (`ESCALATION_NEEDED`) instead of losing the resolution inside it. A failure is an ordinary item failure (retry, discard or park by the 4a rules) and the worker is started again in the same worktree;
6. the lease check, the `before_merge` test seam, then `Driver.merge_branch` (`--no-ff`, `Loop-Item` and `Loop-Attempt` trailers, the record). The worktree goes, then the branch.

A lease lost **before** the merge changes nothing and leaves the item `reviewing` (`lost`; a fourth loss parks it `lease_lost`). After the merge commit exists the merge is a fact: the record is written whatever a lease says, and `recover()` would find it from the trailer if the record failed. Two integrators cannot both pass git's own index lock in the loop worktree.

## The reject cascade (`parallel/cascade.py`, `loopctl parallel cascade`)

Rejecting an item that is not on `main`: its merged dependents are reverted in **reverse merge order** on the local integration branch (their down-migrations first, non-`lossy`, local databases only, `abort.run_down`), every item touched goes to `pending` with a fresh attempt and reset counters, and the **in-flight** descendants are invalidated: the worker is cancelled at its next safe point (never mid-child, so its cost is recorded), the work is kept on `dead/*`, the item is put back. Once any affected item has reached `main`, or `delivery` is not `local`, nothing is reverted or migrated: a human revert plan is written (`abort/revert-plan-<run>.md`) and those items become `needs_human` (RC-14, B5, H4). The scheduler runs `cascade.invalid_inflight` every round (an item in progress whose ancestor is no longer merged, for example after `reconcile --apply` found a human revert).

**It cannot loop**: the affected set is computed once with a visited set (a cyclic `dependsOn` terminates); resetting a dependent is not a rejection and starts no cascade; a root is cascaded at most `MAX_CASCADES` (3) times (kept in `header.cascades`, and an item invalidated more than three times in one run), after which it is parked `cascade_loop`.

## Money (DEC-34)

Each child gets `remaining / workers running`, **and** no more than what is not already promised to another live child (`reserved`): the allowances of children alive together add up to no more than the ledger says is left, so workers launching at the same moment cannot overspend. A worker that finds nothing unreserved waits (checking for a stop). A run-wide ceiling (the 80% park, a day or run budget) parks the run and drains; items already in flight are recovered by the next start. `unattended.child_cost(res, allowance)` takes the per-child allowance, unchanged.

## Stops, drains, exit codes

HALT, a lost run lock and a signal reach every worker through the shared `tick` (a running child is ended within one poll). A park or hard stop of **one** item makes the scheduler **drain**: no new item starts, the running workers finish the child they are in, finished items are still merged, the run exits 1. A run-wide stop (budget, an unattended demotion, `notify_failed`) raised in a worker is handed to the main thread, which drains and raises it. A checkpoint does the same and exits 0 once the others have been integrated. A crash in a worker is an internal error: the others drain, every worktree goes, the exception reaches the caller (exit 2). Exit codes are the serial driver's (0, 1, 2, 3). `settle_worktree` always removes the worktrees (saving their work) and gives every lease back.

## Unattended

The supervisor is per run (Phase 7): `Supervisor.attach` is the only user of `observers`, `stop_hooks`, `finishers`, `cost_hook` and `tick`, and the scheduler is a `Driver`, so nothing changes. `unattended.json` gains `items` (every item in progress; `item` stays the newest for old readers). Stall counts and the measured cost rates are per run and shared by the workers; `header.demotion` blocks the whole run.

## Commands

`loopctl parallel check|status|leases` read; `cleanup` and `cascade` do nothing without `--apply` and refuse while a run holds the lock. Classified in `guards/g_exec.py` (`parallel`: reads `check status leases` and `--dry-run`; everything else a write) and `cli.classification()`.

## Config keys (owner `8-parallel`)

`parallel_mode` (off | worktree, H6, evidence-gated for `worktree`), `max_parallel` (int 2, H6), `pipeline_host` (conductor | workflow, H6, evidence-gated for `workflow`; `workflow` is a doctor error), `lease_ttl_s` (int 300). Existing keys read: `runner`, `verify_ports`, `delivery`, `always_human_paths`, `db_migrate_down`, `run_budget_usd`, `lock_stale_min`.

## Test seams (not for real runs)

`integrate.HOOKS["before_merge"](driver, slot)`; `driver.SLEEP`, `Driver.compute_level`, `KIT_DRIVER_TEST_TIMING` (4c); `mutex.LeaseStore(clock=, alive=, pid=)`; `ports.PortAllocator(busy=)`.

## Not built here

The Workflow pipeline host; stack-specific worktree setup (installing dependencies, creating an ephemeral database: the project's own script keys on `KIT_PORT_OFFSET` and `KIT_ITEM`); a re-verify of an approved item BEFORE it merges after other items merged (it is merged three-way, as the human approved it; the driver verifies the merged result afterwards and stops the run with `integration_red` if it is red, see `${CLAUDE_PLUGIN_ROOT}/spec/loopctl-driver.md`); parallel items across machines (the remote lock is Phase 11); `/batch` (quick lane only, D-28).

## Unverified on a live CLI (probes)

That a parallel fixture run is faster than the sequential one with an equal defect rate (the phase's exit criterion); the names and input fields of the WorktreeCreate and WorktreeRemove hooks (V6, V25); that child sessions in sibling worktrees do not interfere through the user-level state they share (credentials cache, session store); that `--max-budget-usd` bounds each child's real spend closely enough for the reservation to hold (P26).
