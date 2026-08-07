# Examples — a worked reference, not a running project

Everything in this folder describes **TaskFlow**, a fictional SaaS project management app. TaskFlow does not exist as code — not in this repo, not anywhere. There is no `packages/api/`, no `apps/web/`, no database to migrate.

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

## What's missing

The kit does not ship a runnable demo: a real, small codebase produced by actually running `/planner` and `/orchestrate` end to end, committed together with the artifacts that run genuinely emitted. That's a real gap — it means the loop's output has never been published in a form anyone can verify. Until that exists, treat this folder as a specification of intent rather than evidence of results.
