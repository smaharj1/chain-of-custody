# Baseline runs ledger

Nothing here has been run. Every row is `PENDING` until a human runs it. Protocol: [README.md](README.md). Rules for what a result means: [kill-criteria.md](kill-criteria.md).

## Runs (N >= 3)

| Run | Status | Date | Operator | Outcome | Results file | Transcript |
|---|---|---|---|---|---|---|
| run-1 | PENDING | | | | | |
| run-2 | PENDING | | | | | |
| run-3 | PENDING | | | | | |

Status values: `PENDING`, `COMPLETE` (all three items done), `CANNOT FINISH` (V1 could not finish; fires kill criterion 3), `ABORTED` (not counted). Set the status here and in the results file's `status` together.

Why they are pending: starting `claude` is a human action (H4 in the design; an agent in this build may not run it). Each run is `tools/baseline/run-baseline.sh run-N` followed by the protocol in the README.

## Alternative: 1 run plus 20 replayed dispatches per agent type (RC-8, CS-32)

Use it if three full runs are too costly. It also supplies the per-agent tool-call percentiles that `maxTurns` needs.

| Agent type | Replayed dispatches (need 20) | Status | Results file |
|---|---|---|---|
| backend-engineer | 0 | PENDING | |
| database-engineer | 0 | PENDING | |
| app-engineer | 0 | PENDING | |
| design-critic | 0 | PENDING | |
| code-reviewer | 0 | PENDING | |
| security-reviewer | 0 | PENDING | |

The list is the agents the fixture's loop is expected to dispatch; use the ones the actual run dispatched (`dispatches` in its result). Needs one `COMPLETE` or `CANNOT FINISH` run above.

## Summary

Run `python3 tools/baseline/summarize.py`. Last printed result: `PENDING (n=0/3)`.

## Decisions

None yet. Add one dated line per kill criterion once the sample rule is met.
