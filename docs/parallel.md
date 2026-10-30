# Parallel items (experimental, off by default)

By default the loop builds one item at a time. `parallel_mode: worktree` lets the driver (`runner: driver`) build up to `max_parallel` items at once, each in its own git worktree, and merge them one at a time. It is OPT-IN and **refuses to start until the evidence gate has passed** (three clean fixture runs and one real feature, `docs/baseline/evidence.json`): a parallel run multiplies every fault the serial loop can have, so the kit does not let it be switched on before the serial loop has earned trust. Whether it is actually faster, at an equal defect rate, is the exit criterion for parallel mode and needs live runs (see "What nobody has measured").

The design is in `${CLAUDE_PLUGIN_ROOT}/spec/loopctl-parallel.md`; this page is what you need to run it.

## Turn it on

1. The serial driver works for you (`${CLAUDE_PLUGIN_ROOT}/docs/unattended.md` and the driver spec) and `loopctl evidence --check` passes.
2. In `.claude/loop.config.md`, in a reviewed commit (all four keys are guardrails, H6):

```yaml
runner: driver
parallel_mode: worktree
max_parallel: 2          # 1 to 4
```

3. `loopctl parallel check` prints what would stop a run and, from the plan, which items could run side by side and which cannot. `loopctl run --dry-run` shows the same `parallel.*` rows.
4. `loopctl run` as before. One terminal, one `loopctl run`.

Optional: copy `${CLAUDE_PLUGIN_ROOT}/templates/worktreeinclude` to `./.worktreeinclude` and commit it if the new worktrees need `.env.local` / `.env.test` (nothing else is ever copied). To get the same worktrees for `claude --worktree` and for `isolation: worktree` subagents (cut off your **local** integration branch, not the remote default), merge `${CLAUDE_PLUGIN_ROOT}/templates/parallel/worktree-hooks.settings.json` into your interactive settings (see "The WorktreeCreate and WorktreeRemove hooks").

## What it does with your plan

The plan decides how much runs together. An item starts only when:

- every item it depends on is **merged** on the integration branch;
- its `touches.paths` do not overlap those of any item in progress or waiting for you (the check is deliberately cautious: when it cannot prove two globs apart, it runs the items one after the other);
- it does not need a lane someone else holds. A migration (or `touches.schema: true`) uses the **schema** lane and a dependency change (`touches.deps_change: true`) the **deps** lane: one item at a time in each, from its start until it has merged;
- an item with **no** `touches.paths` runs alone.

So slice the plan the way the slicing rules ask (`${CLAUDE_PLUGIN_ROOT}/skills/planner/SKILL.md`): vertical slices with a footprint. A plan whose items declare nothing runs exactly like the serial loop. `loopctl parallel check` shows the width you actually get.

Each item gets `.claude/worktrees/<slug>-a<n>` on its own branch `feat/<slug>-a<n>`, its own port block (`KIT_PORT_OFFSET`, a multiple of 100 added to every `verify_ports` entry) and its own child sessions. When it is reviewed and green it waits for the **integrator**, which merges one finished item at a time, in plan order: it checks the item stayed inside its declared footprint, rebases it onto the branch if the branch moved, runs `verify` again on the rebased code, and then merges with the usual `Loop-Item` trailer.

## What stops a run

| What happened | What the run does |
|---|---|
| HALT (`loopctl halt`), a lost run lock, Ctrl-C | every running child is ended within a poll; the run exits as the serial driver does (3, 3, 1) |
| One item parks or hard-stops | no new item starts; the others finish and finished items are still merged; exit 1 |
| An item changed files outside its `touches.paths` | that item parks (`footprint_violation`), nothing of it is merged; read the files it names, then fix the plan's footprint (a plan PR) or the code |
| A rebase conflicts | that item parks (`merge_conflict`); you resolve it |
| An item branch holds a merge commit | that item parks (`ESCALATION_NEEDED`): the integrator rebases linear history only, so a resolution inside a merge commit is never dropped silently |
| An item that a human approved merges and the integration branch turns red | the run stops with `integration_red`; the item stays merged (nothing is reverted for you); fix or revert it, then `loopctl approve breaker --by <you>`. Your approval attests the item's diff, not the merged result |
| The budget (80% of the run, or the day) is reached | the run parks and drains; running children finish; `loopctl approve cost` to go on |
| An autonomy checkpoint | no new item starts; the others finish and are integrated; then the run pauses (exit 0) |
| A worker crashes | internal error, exit 2; the others drain and **every worktree is removed** (its work is saved on the item's branch) |

After a hard crash (kill -9, power) the next `loopctl run` cleans up first: leases of the dead driver are reclaimed, its worktrees removed with their uncommitted work saved on the item's branch, items that were in progress go back to pending and their branches are renamed `dead/*` (kept for `orphan_retention_days`). `loopctl parallel cleanup` (dry run) shows what it would remove; `--apply` does it when no run holds the lock, and is a human step (an agent does not run it).

## Money

Each child gets `--max-budget-usd` equal to the remaining budget divided by the workers running, and never more than what no other live child was already promised, so two workers cannot overspend by launching at the same moment. More workers means a smaller allowance per child, not a larger bill. A day budget and the 80% park apply to the sum of all workers.

## Rejecting work that other items were built on

`loopctl parallel cascade <item>` shows (dry run) what rejecting an item would do; `--apply --by <you> --reason "..."` does it (a human step: you run it in a plain terminal): the items merged after it that depend on it are reverted in reverse merge order on the local integration branch, their down-migrations run on local databases only (never a `lossy` one), and they go back to pending with a fresh attempt. Items still being built on it are invalidated: their worker stops at its next safe point and the work is kept on `dead/*`. If any affected item has reached `main` (or delivery is a pull request), nothing is reverted: you get a revert plan file to run by hand. Each item can be cascaded three times; the fourth is parked as `cascade_loop`.

## The WorktreeCreate and WorktreeRemove hooks

Optional and unverified on a live CLI. A `WorktreeCreate` hook **replaces** the platform's own worktree creation, so `${CLAUDE_PLUGIN_ROOT}/scripts/worktree_hook.py create` makes the worktree itself: under `.claude/worktrees/<name>` and nowhere else, on a branch `worktree-<name>` cut off the **local** integration branch, with `.worktreeinclude` applied and a port slot reserved. `remove` takes it away and gives the slot back. A name with a separator, a path outside `.claude/worktrees`, a symbolic link on the way or any input shape the script does not know is refused with exit 2. The hook does not install dependencies or create a database; key those on `KIT_PORT_OFFSET` in your own setup script. The kit does not wire the hooks (the hook manifest has no opt-in entries): merge the settings snippet yourself.

After a rebase every deterministic gate (`g5 g1 g3 g6 g9 g11 docs g10 g4` and `g7`; the reviewers are not run again) is re-run on the rebased code before the item merges, so the evidence is for the code that merges.

## What nobody has measured

The kit's offline tests prove the mechanics: admission, leases, the schema mutex, port allocation, integration order, the footprint check, the cascade, cleanup after a crash. They cannot prove that a parallel run is **faster than a sequential one with the same defect rate**, which is the exit criterion for parallel mode and needs live runs on the pinned CLI. Until you have those numbers, treat `parallel_mode: worktree` as an experiment: run it on the fixture first, compare wall time, rescues and spend with a serial run, and keep `max_parallel` at 2.
