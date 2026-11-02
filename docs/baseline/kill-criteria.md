# Kill criteria

Report §16.2, written into the repo in Phase 0a before any building (CS-34). They are the answer to over-engineering: each says what evidence turns an expensive phase down, so the roadmap can shrink instead of only grow. [tools/baseline/summarize.py](../../tools/baseline/summarize.py) evaluates the first three from the baseline results; the rest are checked later, phase by phase.

## Decided by the baseline

1. **loopctl is justified only if the baseline needs more than 3 control-flow human rescues per run**, taken across the N >= 3 runs (resuming, re-dispatching, fixing state by hand; see the rescue taxonomy in the [README](README.md)). If it needs 3 or fewer, the Phase 4 MVP shrinks to `validate`, `status` and `doctor`.
   - Evaluated as: the **median** control-flow rescues per run is greater than 3. The report does not say how to combine runs; the median is robust to one bad run. The summary also prints min..max and says when the range straddles 3, in which case read the runs before deciding.
2. **If most failures are `env`** (toolchain, ports, DB, dependencies), the doctor and G0 come first, ahead of everything in Phases 2-4.
   - Evaluated as: `env` failures are more than half of all recorded failures across the runs.
3. **If V1 cannot finish the fixture at all** (RC-8), Phase 2 guards and the doctor come first, and the loopctl decision is deferred until a run finishes.
   - Evaluated as: no run reached all three items `done`. If some runs finished and some did not, the summary says **partial** and the branch does not fire. A recorded `v1_cannot_finish` is reported at once, even before three runs exist.

Sample rule: N >= 3 completed or `v1_cannot_finish` runs, or one run plus at least 20 replayed dispatches for each agent type that run dispatched (provisional). Until then the summary prints `PENDING (n=k/3)`. Runs marked `aborted` (machine, quota) are not evidence either way.

## Decided phase by phase, afterwards

4. **Every capability phase must improve a fixture metric** (rescues, failure count by class, defect escape, spend, wall time), or it is reverted. **Safety and boundary work is exempt** (Phase 1, the Phase 2 corpus and ConfigChange blocker, and the boundary parts of 0b and 6): it is judged on the bypass corpus, the injection corpus and the boundary fixtures (§15 item 9), not on a fixture metric (RC-8). The metrics are the fields of a results file, so a re-run of hello-loop after a phase is compared with the baseline ranges.
5. **Gates whose owner metric never moves are removed** at the next model release (the ablation review in Phase 10).
6. **A safety eval or corpus regression blocks the release**, with no waiver.

## Recording a decision

When the baseline is complete, add a dated line to [RUNS.md](RUNS.md) under "Decisions" for each of criteria 1-3 with the verdict printed by `summarize.py`, who read it, and what changed in the plan.
