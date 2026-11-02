# Examples — a worked reference, not a running project

Everything in this folder except `hello-loop/` (see [below](#hello-loop-a-real-fixture)) describes **TaskFlow**, a fictional SaaS project management app. TaskFlow does not exist as code — not in this repo, not anywhere. There is no `packages/api/`, no `apps/web/`, no database to migrate.

These files are **hand-authored illustrations** of what the kit's artifacts look like when they're filled in properly. They were written by hand as a reference; they were not captured from an actual `/pm` → `/architect` → `/tech-lead` session. Read them the way you'd read a worked example in a textbook: the shape, depth, and specificity are the lesson, and the contents are invented.

That distinction matters most in [`features/csv-export/reports/backend.md`](features/csv-export/reports/backend.md), which is written in the past tense and quotes test counts, timings, and a `curl` against localhost. No tests were run and nothing was built. What that file teaches is the *format and rigor* an engineer's report should have.

## What this folder is good for

Use it as a calibration reference when you're looking at an empty template and don't know how much detail to write:

- **`CLAUDE.md`** — how specific a stack table and conventions list should get.
- **`context/`** — all eight domain primers, filled in. `local-dev.md` is the most load-bearing one: it carries a concrete verify-run harness and an ephemeral-Postgres variant, and it's what `/orchestrate` executes against.
- **`loop.config.md`** — a filled loop config with plausible verify commands, autonomy setting, and budgets.
- **`features/csv-export/`** — one feature's complete artifact chain, in the order the modes produce it: `requirements.md` (PM) → `technical-design.md` + `api-contract.md` (Architect) → `briefs/backend.md` (Tech Lead) → `reports/backend.md` (engineer).

## Where these files map in a real install

The paths here mirror where each file lives once you've copied the kit, minus one level:

| Here | In your project |
|---|---|
| `examples/CLAUDE.md` | `.claude/CLAUDE.md` |
| `examples/context/*.md` | `.claude/context/*.md` |
| `examples/loop.config.md` | `.claude/loop.config.md` |
| `examples/features/csv-export/` | `docs/features/<your-slug>/` |

Don't copy these files into your own project — they describe someone else's fictional app, and an engineer that reads them as project context will chase files that aren't there. Copy the **templates** (`.claude/context/`, `.claude/templates/`) and keep these open alongside as the answer key.

## hello-loop: a real fixture

[`hello-loop/`](hello-loop/README.md) is the one directory here that is real, runnable code: a small bookmarks service (Python standard library only) with a three-item build plan, pre-filled primers, held-out acceptance checks and an answer key. It is the fixture the kit is measured against ([`docs/baseline/README.md`](../docs/baseline/README.md), `tools/baseline/`). It is hand-written, not the output of a `/planner` and `/orchestrate` run.

## What's missing

The kit still has no published run: a codebase produced by actually running `/planner` and `/orchestrate` end to end, committed with the artifacts that run genuinely emitted. `hello-loop/` is the input for such a run, not a record of one, and no baseline run has been made yet. Until transcripts exist, treat the TaskFlow files as a specification of intent rather than evidence of results.
