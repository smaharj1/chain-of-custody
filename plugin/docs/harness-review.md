# The harness review at each model release

A harness part (a gate, a rule, a primer, a rubric, a hook) is a bet that the model needs it. Models improve, and a part that was load-bearing for one release can be dead weight for the next, or can get in the way. The report says what to do about it: "`--ablation` shows which harness parts still pay their way; this is how the harness gets pruned at each model release" (section 15 item 3), and "gates whose owner metric never moves are removed at the next model release" (16.2). This page is the procedure. It is run by a person, at each new model release, and takes an afternoon plus the cost of the live eval runs.

Nothing in the review edits anything. It produces a table and a recommendation per part; a person changes `loop.config` or the harness in a reviewed commit (H6).

## Two kinds of evidence, and what each can say

| Evidence | Covers | Where it comes from | Can say |
|---|---|---|---|
| **Owner metrics** | the gates (G0 to G11, Docs Delta) | real runs: `events/loopctl.jsonl`, through `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/loopctl harness` | "this gate ran on N items and never failed or blocked" |
| **Ablation** | prompt-level parts: context files, agent rubrics, the default posture, hooks | the eval cases (`evals/cases/`), run again with the part switched off, graded by `tools/evals/run_local.py`'s graders, through `python3 tools/evals/ablate.py` | "with this part off, case X passes less often" |

A gate is judged by its owner metric because the eval cases drive a model, not loopctl. A prompt-level part is judged by ablation because it has no gate to count. A gate with an eval case listed in the manifest gets both, and then the review needs both to agree before it says "remove".

**Safety is exempt from pruning by metric.** The guards, G5 (test integrity), G9 (supply chain), G11 (secrets) and the evidence gate are judged by the safety corpus and the safety cases, which must pass at 1.0; "never fired" is what a working guard looks like. The review never recommends removing one. A safety eval or corpus regression blocks the release with no waiver (16.2).

## The procedure

1. **Pin the new model.** Record it: `--model <id>` on every command below. Run the review on a clean machine, with the pinned CLI, never inside a project session (a live run starts `claude`; no kit script does).
2. **Baseline the evals on the new model.** Run the kit's eval suite as it is (`claude plugin eval`, k=3, safety cases at `--threshold 1.0`, quality at 0.8, `--max-cost-usd` set). Record the transcripts as `evals/transcripts/<case>.json` in the shape `tools/evals/run_local.py` documents. The committed transcripts are synthetic; replace them with the recorded runs for the review (keep the synthetic ones in git history).
3. **Ablate, one part at a time.** `evals/ablation/manifest.json` lists the parts and, for each, the cases that exercise it and `how_to_disable`. In a scratch clone, switch the part off as described, run its cases k=3 and write the transcripts to `evals/ablation/transcripts/<part>/<case>.json` (`:` in the part id becomes `__`). One part is off at a time; never ablate a safety hook in a real project.
4. **Compute the verdicts.** `python3 tools/evals/ablate.py --model <id> --out ablation.json`. A part is `pending` until its cases have runs with it off, `insufficient` below k=3, `keep` when switching it off makes a case pass less (a quality case must lose 10 points; a safety case any), `harmful` when switching it off makes a case pass more, `no-effect` when nothing moved, `exempt` for a safety part that did not change.
5. **Read the owner metrics.** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/loopctl harness --ablation ablation.json --model <id>` prints each gate's runs, failures and blocks, with its ablation verdict where one exists, and a recommendation: `keep`, `keep for now` (too few items to judge: `harness_min_item_runs`, 10), `review` (the metric never moved; run the ablation) or `remove candidate` (the metric never moved on at least ten items **and** the ablation says no effect or harmful).
6. **Decide.** For each remove candidate, read the last retro and the findings that gate would have made, then do one of: remove or turn it to `report` in `loop.config` (H6, a reviewed commit); keep it with the reason written in the PR; or run more items first. For a `harmful` prompt-level part, shorten it or delete it in a PR and re-run its cases. Write the decision, the model id and the numbers in the PR description.
7. **Re-run the whole suite after any change** and the bypass and injection corpora (`tests/guards`), which must stay green.

## Reading a result honestly

- k=3 is small. A quality case moves in steps of a third; the 10-point bar is a floor, not a significance test. If a result decides something that costs money or safety, run more.
- `no-effect` on a part whose job is rare events (an injection that shows up once a month) proves little. Look at what the part guards before removing it.
- An owner metric that never moved can mean the gate is useless, or that the project never exercised it. A plan with no migrations never trips the migration check. `harness_min_item_runs` is about volume, not variety.
- A part can pay for itself on weaker models and not on a stronger one, and the reverse can be true for a rule that corrects a new habit. That is why the review runs at every release rather than once.

## What is built, and what needs a live model

Built and tested offline with the fake runner and fixture transcripts: the manifest and its check against the case list, `tools/evals/ablate.py` (pass rates with and without a part, verdicts), `loopctl harness` (owner metrics from events, the merged recommendation, the safety exemption, an ablation file read from inside the repository only).

Pending a human with a live CLI: the recorded baseline transcripts for the new model, the ablated transcripts for each part, and the judgment. `claude plugin eval --ablation` itself is a probe (P27): if the flag set differs on the pinned CLI, `tools/evals/ablate.py` still works from transcripts you record by hand, because it only needs the files.
