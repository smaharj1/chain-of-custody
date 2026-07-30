# Operational FAQ

Answers to the questions real teams hit in their first weeks with the kit — testing, CI, deployment, teams, and non-web projects. For the basics, start with [getting-started.md](getting-started.md); for the design rationale behind the loop, see [design/loop-engineering.md](design/loop-engineering.md).

## What's the testing strategy? My project has no tests.

The kit *enforces* tests but doesn't dictate a framework — pick the mainstream one for your stack: **vitest** for TypeScript/JavaScript, **pytest** for Python, the built-in runner for Go (`go test`) or Rust (`cargo test`). What matters is what the verify gate expects: `verify_tests` in [`.claude/loop.config.md`](../.claude/loop.config.md) must **exit 0 AND collect more than zero tests**. A vacuous run — the command succeeds but no tests actually executed — is a *failure*, by design (design §8). So "we'll add tests later" doesn't just weaken the loop; it stops it.

A minimum smoke test should assert something falsifiable about the running app, not just that imports work: the health endpoint returns 200, the root route renders, a core function returns the expected value for one known input. One real assertion is enough to make the gate meaningful.

**Brownfield with zero tests?** Don't try to backfill a suite first. Make the *first plan item* "install the test framework and add one passing smoke test" — then the gate has something to collect from item one, and every later item adds its own tests on top. This mirrors what `/planner` already mandates for greenfield (a runnable baseline with one passing test); for an existing codebase you just tell the Planner that's item one.

## How does this work with CI?

Cleanly, because the loop never touches your remote. The loop's local verify gate is **not** a CI substitute — it proves the code worked on your machine at merge time. Have CI re-run `verify_tests` (and your lint/build) on push, exactly as it would for human-authored commits. Since "the conductor never pushes" (design §9), CI fires only when *you* push at a checkpoint — the two compose without any special configuration: the loop verifies locally, you review and push, CI verifies independently.

On what to commit: **do** commit `build-plan.json` and `BUILD_PLAN.md` — they're durable state that resumes and survives across machines — and optionally the `docs/features/` folders if you want the design trail in history. **Never** commit `.claude/loop.jsonl` or `.claude/metrics.jsonl` — they're telemetry, and they belong in `.gitignore` (see the team question below for the full ignore list).

## What about deploying? The kit is local-first.

Local-first is deliberate, not a limitation to route around: verification, demos, and the whole checkpoint experience run on your machine so nothing needs a cloud account to be *seen working* (design §8a). Deploy is a later milestone, and the loop won't start it until the app runs locally (`infra_gated_behind_local`). Infra-touching items **always force a checkpoint**, regardless of your autonomy setting — deployment is never unattended.

When the deploy milestone arrives and you've been developing on the default SQLite: set `DATABASE_URL` to your Postgres instance, **regenerate the migrations for the Postgres dialect** (the ORM re-emits them; the design keeps schemas dialect-portable so this is low-friction — but it is a regeneration, not a reuse), and re-run your seed. Budget an item for it rather than treating it as a config flip.

If you're a beginner who deleted the infra agent (as [choosing-your-stack.md](choosing-your-stack.md) suggests): start with a managed platform — the Vercel / Railway / Supabase class — where deploy is "connect the repo and push." Re-add `infra-engineer` (and `context/infra.md`) only when you have real infrastructure-as-code to manage.

## Monorepo or multiple repos?

The kit lives in **one repo**: put `.claude/` in the repository where the code changes happen. A monorepo is the happy path — the example project is one, and a single plan can span backend, frontend, and database because they're all in the same working tree.

The loop **cannot span repos**: one plan, one git history, one local main. If your team runs polyrepo (say, a frontend repo and a backend repo), keep a copy of the kit in each repo and run separate plans. Treat the *backend repo's* primers and `api-contract.md` as the source of truth for the contract; the frontend repo's copies mirror it, and a contract change starts as a backend-repo item.

## Can my team use this? What about multiple developers?

Yes, with one rule: **one conductor per clone**. The run lock (`.claude/orchestrate.lock`) is per-checkout, so two people can't safely drive `/orchestrate` against the same working copy — but two teammates each running the loop in their own clone on different milestones is fine. If a teammate pushes to the remote mid-run, nothing corrupts silently: the next run's preamble does a JSON↔git reconciliation (every `done` item's merge commit is checked against main) and flags anything that moved underneath the plan.

For the shared `.claude/` directory: **commit** the shared files — `CLAUDE.md`, `agents/`, `commands/`, `context/`, `loop.config.md`, hooks. **Gitignore** the personal and telemetry files: `settings.local.json`, `loop.jsonl`, `metrics.jsonl`, and `orchestrate.lock`. That split keeps the team's shared brain in git and each developer's run state out of it.

## How do I abandon an item mid-run?

Three steps, in order. **Stop the run** (or wait for the next checkpoint). **Run `/planner`** and tell it to remove or edit the item — only the Planner edits plan structure, so that's where the change happens, and it re-validates the dependency order for you. Then **run `/orchestrate` again**: its preamble discards the orphaned branch and partial work as part of resume reconciliation, and the loop continues with the revised plan.

Don't hand-edit `build-plan.json` to change an item's status. Resetting a `blocked` item back to `pending` is the Orchestrator's job at its unblock step in the run preamble — it surfaces blocked items and clears them with their `blockReason`, keeping the JSON, the git state, and the log consistent with each other.

## My test runner doesn't report a count.

The gate needs evidence that more than zero tests ran, which means parsing your runner's summary line. The mainstream runners all provide one: **vitest** and **jest** print `Tests: N passed`, **pytest** prints `N passed` in its summary, **go test** prints `ok` per package (with per-test lines under `-v`). Point `verify_tests` at the normal command and the count is right there in the output.

If your runner genuinely can't report a count, wrap it in a small script that fails on zero: run the tests, grep the output for the pass count, and `exit 1` if it's absent or zero. The wrapper becomes your `verify_tests` command — the gate only needs "exit 0 and proof that something ran."

## My project isn't a web SaaS.

**API-only or CLI tool:** delete the app agents (`app-engineer.md`, `admin-app-engineer.md`) and `context/shared-frontend.md`. Nothing else needs rewiring — the loop's Backend→frontend dispatch gate simply never fires when no frontend agent exists in the roster. Keep the `api-contract.md` template but treat it as a generic *interface* contract: endpoints for an API, commands/flags/exit codes for a CLI.

**Mobile:** the frontend agents are web-oriented, but the patterns transfer — duplicate and adjust `app.md` (and the agent file) for React Native or your framework, per [choosing-your-stack.md](choosing-your-stack.md). The one real gap is the auto-demo: the loop screenshots a browser route, and there's no simulator equivalent, so mobile items' demo targets fall back to what the loop can verify — an endpoint assertion or a named passing test — and you check the simulator yourself at checkpoints.

**Desktop apps, data pipelines, games:** same principle. The team structure and the loop are shape-agnostic; only the *demo target* changes. Where a web item's acceptance is "this route renders X," yours is a command that exits 0 with expected output, or a test that asserts the pipeline produced the right rows. As long as each item has a falsifiable local assertion, the verify gate works unchanged.
