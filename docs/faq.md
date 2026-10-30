# Operational FAQ

Answers to the questions real teams hit in their first weeks with the kit: testing, CI, deployment, teams, and non-web projects. For the basics, start with [getting-started.md](getting-started.md). For the design rationale behind the loop, see [the loop design note](../spec/loop-engineering.md).

## What's the testing strategy? My project has no tests.

The kit enforces tests but doesn't dictate a framework. Pick the mainstream one for your stack: **vitest** for TypeScript/JavaScript, **pytest** for Python, the built-in runner for Go (`go test`) or Rust (`cargo test`). What matters is what the verify gate expects: `verify_tests` in [`.claude/loop.config.md`](../loop.config.md) has to exit 0 and collect more than zero tests. A vacuous run, where the command succeeds but no tests actually executed, counts as a failure by design (design §8). So if your plan is to add tests later, the loop won't run in the meantime.

A minimum smoke test should assert something falsifiable about the running app rather than just checking that imports work: the health endpoint returns 200, the root route renders, a core function returns the expected value for one known input. One real assertion is enough to make the gate meaningful.

**Brownfield with zero tests?** Don't try to backfill a whole suite first. Make the *first plan item* "install the test framework and add one passing smoke test." The gate then has something to collect from item one, and every later item adds its own tests on top. It's the same thing `/planner` already mandates for greenfield (a runnable baseline with one passing test); on an existing codebase you just tell the Planner that's item one.

## How does this work with CI?

Cleanly, because the loop never touches your remote. The local verify gate isn't a CI substitute; it proves the code worked on your machine at merge time. Have CI re-run `verify_tests` (and your lint/build) on push, exactly as it would for human-authored commits. Since no agent pushes (design §9), CI fires only when *you* push, so the two compose without any special configuration. If you want the kit's own CI half, `${CLAUDE_PLUGIN_ROOT}/templates/github/workflows/verify.yml` re-runs the verify commands on every pull request, and [delivery.md](delivery.md) covers sending a feature out as a draft pull request.

On what to commit: **do** commit `build-plan.json` and `BUILD_PLAN.md`, which are durable state that resumes and survives across machines, and optionally the `docs/features/` folders if you want the design trail in history. Keep `.claude/loop.jsonl` and `.claude/metrics.jsonl` out of git; they're telemetry and belong in `.gitignore` (the team question below has the full ignore list).

## What about deploying? The kit is local-first.

Local-first is a deliberate choice. Verification, demos, and the whole checkpoint experience run on your machine so nothing needs a cloud account to be *seen working* (design §8a). Deploy is a later milestone, and the loop won't start it until the app runs locally (`infra_gated_behind_local`). Infra-touching items always force a checkpoint regardless of your autonomy setting, so deployment is never unattended.

When the deploy milestone arrives and you've been developing on the default SQLite: set `DATABASE_URL` to your Postgres instance, regenerate the migrations for the Postgres dialect, and re-run your seed. The ORM re-emits them and the design keeps schemas dialect-portable, so this is low-friction, but it is a regeneration rather than a reuse. Budget an item for it instead of treating it as a config flip.

If you're a beginner who deleted the infra agent (as [choosing-your-stack.md](../context/choosing-your-stack.md) suggests), start with a managed platform in the Vercel / Railway / Supabase class, where deploy is "connect the repo and push." Re-add `infra-engineer` and `context/infra.md` when you have real infrastructure-as-code to manage.

## Monorepo or multiple repos?

The kit lives in **one repo**: put `.claude/` in the repository where the code changes happen. A monorepo is the happy path. The example project is one, and a single plan can span backend, frontend, and database because they're all in the same working tree.

The loop **cannot span repos**: one plan, one git history, one local main. If your team runs polyrepo (a frontend repo and a backend repo, say), keep a copy of the kit in each and run separate plans. Treat the *backend repo's* primers and `api-contract.md` as the source of truth for the contract; the frontend repo's copies mirror it, and a contract change starts as a backend-repo item.

## Can my team use this? What about multiple developers?

Yes, with one rule: **one conductor per clone**. The run lock (`.claude/orchestrate.lock`) is per-checkout, so two people can't safely drive `/orchestrate` against the same working copy. Two teammates each running the loop in their own clone on different milestones is fine. If a teammate pushes to the remote mid-run, nothing corrupts silently: the next run's preamble reconciles the JSON against git (every `done` item's merge commit is checked against main) and flags anything that moved underneath the plan.

For the shared `.claude/` directory, **commit** the shared files: `CLAUDE.md`, `agents/`, `skills/`, `context/`, `loop.config.md`, and the hooks. **Gitignore** the personal and telemetry files: `settings.local.json`, `loop.jsonl`, `metrics.jsonl`, and `orchestrate.lock`. That keeps the team's shared context in git and each developer's run state out of it.

## The loop keeps stopping to ask permission. I set autonomy to unattended.

Those pauses come from Claude Code itself, not the kit. The harness asks before running any command that isn't on the project's permission allowlist, and that gate sits *underneath* the loop's autonomy dial — `unattended` controls the kit's own checkpoints, not the harness's safety prompts. The kit ships an empty allowlist (`.claude/settings.json` → `permissions.allow`) deliberately, so the first run of any command asks you. The kit's guards are a separate layer on top of this: they can deny a command that the allowlist would permit (see "Why was my command blocked?").

The fix is to allowlist your project's recurring commands — the `verify_tests` / `verify_run` commands from [`.claude/loop.config.md`](../loop.config.md), your package manager, your migration commands. Easiest path: ask Claude *"add my project's run and test commands to the permission allowlist in `.claude/settings.json`"*, review what it proposes, and re-run `/orchestrate`. The Planner also offers to do this during planning, and the orchestrate preamble warns you at run start if the allowlist is empty. Leaving it empty is safe, just chattier: the run pauses for a click at each new command.

## How do I abandon an item mid-run?

Three steps, in order. **Stop the run** (or wait for the next checkpoint). **Run `/planner`** and tell it to remove or edit the item; only the Planner edits plan structure, and it re-validates the dependency order for you. Then **run `/orchestrate` again**: its preamble discards the orphaned branch and partial work as part of resume reconciliation, and the loop continues with the revised plan.

Don't hand-edit `build-plan.json` to change an item's status. Resetting a `blocked` item back to `pending` is the Orchestrator's job at its unblock step in the run preamble, where it surfaces blocked items and clears them with their `blockReason`, keeping the JSON, the git state, and the log consistent with each other.

## My test runner doesn't report a count.

The gate needs evidence that more than zero tests ran, which means parsing your runner's summary line. The mainstream runners all print one: **vitest** and **jest** print `Tests: N passed`, **pytest** prints `N passed` in its summary, **go test** prints `ok` per package (with per-test lines under `-v`). Point `verify_tests` at the normal command and the count is right there in the output.

If your runner genuinely can't report a count, wrap it in a small script that fails on zero: run the tests, grep the output for the pass count, and `exit 1` if it's absent or zero. That wrapper becomes your `verify_tests` command. The gate only needs exit 0 plus proof that something ran.

## My project isn't a web SaaS.

**API-only or CLI tool:** delete the app agents (`app-engineer.md`, `admin-app-engineer.md`) and `context/shared-frontend.md`. Nothing else needs rewiring, since the loop's Backend→frontend dispatch gate never fires when no frontend agent exists in the roster. Keep the `api-contract.md` template but treat it as a generic *interface* contract: endpoints for an API, commands/flags/exit codes for a CLI.

**Mobile:** the frontend agents are web-oriented, but the patterns transfer. Duplicate and adjust `app.md` (and the agent file) for React Native or your framework, per [choosing-your-stack.md](../context/choosing-your-stack.md). The gap is the auto-demo, which screenshots a browser route. Where your session has an iOS Simulator tool, the loop can launch the build in the Simulator and screenshot the screen for the demo. Where it doesn't, mobile items' demo targets fall back to what the loop can verify, usually an endpoint assertion or a named passing test, and you check the simulator yourself at checkpoints.

**Desktop apps, data pipelines, games:** same principle. The team structure and the loop are shape-agnostic; only the *demo target* changes. Where a web item's acceptance is "this route renders X," yours is a command that exits 0 with expected output, or a test asserting the pipeline produced the right rows. As long as each item has a falsifiable local assertion, the verify gate works unchanged.

## Why is my level L1? Why does the doctor flag my own credentials?

L1 is where every install starts, on purpose: a supervised local loop with better guards. Nothing is attested yet, and each step up needs something only a person can do. `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --level L2` (then `L3`) lists what blocks the next level. The doctor looks at your own `gh`, git and SSH credentials because the agent runs as you: anything your account can reach, the agent can reach. A credential it can reach caps the level at L1 until you isolate it (a separate OS user, a container, or a credential-free machine). So **can you run the loop as your own user?** Yes, at L1. L2 and above assume a bot identity and a sandbox. See [security/README.md](security/README.md) and [security/credentials.md](security/credentials.md).

## Why was my command blocked? How do I register the conductor?

Every guard deny prints a rule id, for example `BLOCKED by guard-git:push`. To see which rule a command hits and why: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/guards/run.py explain --caller engineer git push origin main`. The guards are guardrails, not the security boundary, and they fail closed, so an ordinary command is sometimes refused; the usual ways out are to run it yourself with `!` in the prompt, raise `git_autonomy`, or add a project path to `main_edit_allow`. Details and the rule list: [security/guards.md](security/guards.md). By default your own session counts as `unknown` and can only read git. A human who runs the prose loop without loopctl and wants the conductor to commit from the session registers it once: `! python3 ${CLAUDE_PLUGIN_ROOT}/scripts/guards/run.py register-session --session <session_id> --role conductor` (the session id is in the line SessionStart prints). Without registration the session works at the `git_autonomy` level. loopctl and the driver register their sessions themselves.

## Why doesn't the kit push for me? (`pr_prepare.py` prints commands)

No agent holds the push. Not the Tech Lead, not the Orchestrator, not the driver. The guards deny `git push` and `gh pr create` for every agent, and from L2 up the push needs your own credentials. So `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py prepare` validates the pull request body and then **prints** the commands, and you run them in a terminal. The same holds for approvals and lifting stops: the conductor prints a `HUMAN STEP:` line and waits. See [delivery.md](delivery.md).

## Why did verify say `env`? Why can't the first kit PR pass verify?

`env` means the code is not at fault: a verify port was busy or a command was not found. Free the port or install the tool; do not send an engineer back to rework. The first pull request that installs the kit cannot pass the `verify` check, because `verify.py` reads its definition from the base branch and the base has no `verify.py` yet. A human merges that one; every later pull request is protected.

## Why does loopctl refuse?

Read its message; it names the cause. The usual ones: Tier 2 is off (`loopctl_tier2: true` is a reviewed setting), the base sha is wrong, an evidence-gated setting is on while `docs/baseline/evidence.json` is missing, a HALT is set (a person clears it with `loopctl resume`), the run lock is held by another run, or the liveness canary line is missing. `loopctl doctor` and `loopctl status` show more. If a command is refused because it is a human step, the guard or the CLI says so; run it yourself from a plain terminal.

## Why does G2 or G3 fail? Where do held-out tests live?

G2 fails when the item has not declared its acceptance tests (`g2.not-declared`: add `ext.g3`, or an `ext.g3.exempt` reason), when the new tests do not fail against the stubs for the right reason (a test that errors on import is *invalid*, not red), or when a pinned test was edited. G3 fails when a criterion's test did not run and pass, when a pinned acceptance test changed, or when the held-out tests do not exit 0. Held-out tests live where the engineer cannot read them: in a directory under the git directory that only the `acceptance-author` child may write (with the driver), or only in CI (`heldout.yml`). See `${CLAUDE_PLUGIN_ROOT}/spec/loopctl-gates.md`.

## Why does the driver refuse? What does `cost_unknown` mean? What if my terminal closes?

`loopctl run` with `runner: driver` refuses when it is started inside a Claude Code session, when a HALT is set, when the Claude Code version is outside the allowed range, when the profile or budget (`run_budget_usd`) is missing, or when a checkpoint is waiting for an answer (`approve checkpoint`). `loopctl run --dry-run` shows each row. A run parks with `cost_unknown` when a child produced no usable cost number (for example it was killed): the kit treats an unknown cost as a stop. Check the ledger, then `approve cost` and `approve unblock`, both human steps. If your terminal closes, the current child is ended, your work stays committed on the item's branch and the lock is released; start again. See [the driver spec](../spec/loopctl-driver.md).

## Why can't the agent run `terraform plan`? Why does the infra check refuse my new provider?

A plan *executes* provider and module code, so it runs in CI, with a plan-only role, after a source policy has judged the pull request's code from the base branch, and only after a person applies the `plan-ok` label. Applying is always a person's decision. A new provider or a module version range is refused because it is a separate, reviewed change to `.claude/infra-policy.json`, and module versions must be exact. See [security/infra-ci.md](security/infra-ci.md).

## Why does an unattended run refuse to start? Why was I demoted to L3? Why a watchdog message?

`loopctl unattended check` prints a row for each reason (`unattended.*`): the evidence gate, the level, a missing spend ceiling, a `notify_command` that is empty or not allowed (it cannot be a shell, interpreter or launcher such as `curl`), a missing watchdog check-in, an unlifted demotion, or nothing to do. A demotion to L3 follows a revert of a merged item, a security block, a test-integrity flag or a stall storm; lift it with `approve demotion` after you have looked. A watchdog message means the driver went silent or is gone; `unattended watch` (run by your scheduler) noticed. See [unattended.md](unattended.md).

## Why is a killed child's cost estimated?

A killed child prints no result, so its real cost is unknown, and an unknown cost would stop every unattended run at the first stall. When the run has already measured at least one child, the cost of a killed one is estimated from this run's own highest rate, capped by the child's budget allowance, and marked `estimated` in the ledger. With nothing measured yet it still parks the run.

## Why did two items not run together? Why was an item parked `footprint_violation`? What is left after a crash?

Parallel mode only runs items together when their declared `touches.paths` provably do not overlap and they need no shared lane (schema, dependencies). `loopctl parallel check` shows the width you actually get. An item parks with `footprint_violation` when it changed files outside what it declared; fix the plan's footprint or the code. After a crash the next `loopctl run` reclaims leases and removes worktrees, saving unfinished work on the item's branch; `loopctl parallel cleanup` shows what is left and applying it is a human step. The WorktreeCreate hook is not wired for you because it is opt-in and its event shape is unverified; merge the snippet in `${CLAUDE_PLUGIN_ROOT}/templates/parallel/` yourself if you want it. See [parallel.md](parallel.md).

## How do I update the kit? Can I use the plugin and the loop?

Use `/migrate` (the plugin form: `/chain-of-custody:migrate`). It plans first; a person applies it from a plain terminal, and it never overwrites a file you changed. See [packaging.md](packaging.md). The plugin form gives you the modes, agents, guards, `verify.py` and `pr_prepare.py`; the loop machinery (`runner: driver`, unattended and parallel runs, the setup script from `/bootstrap`) reads the kit from your project's own `.claude/`, so it needs the copy install.

## Why did my item pass with a missing trailer? What is a `report_warning`?

`report_trailers` defaults to `warn`: a form problem in an engineer's report (no trailer, a missing section other than Status) lets the item go on and records a `report_warning` event; a hard problem (a missing report, a trailer that disagrees with the report) still fails it. `loopctl report scan` says when ten reports in a row were clean and `block` is ready; switching it is your reviewed change. See [learning.md](learning.md).

## Why did the Docs Delta gate flag my change?

The gate found something added in source that needs documenting (a new route, command-line flag or environment variable) and no new documentation line naming it and no `## Doc Delta` entry in the engineer's report that does. An edit under `docs/features/**`, a test, or a blank-line change does not count as documentation. It runs in `report` mode by default, so it records and does not block. See [learning.md](learning.md).

## Can loopctl write a rule for me? Why can't `/retro` run `gaps --write`?

It drafts one; it never applies it. `loopctl gaps --write` creates a folder with a rule skeleton and the commands to propose it, and a person reviews and runs them (rules and settings are guardrail files, so a change is always a reviewed pull request). The exec guard allows `gaps --write` to the registered conductor only, so a `/retro` session in your own session does not run it; run it yourself in a terminal.

## Why doesn't `/bootstrap` edit my config or create my repo?

By design. Almost every `loop.config.md` key is a guardrail only a human changes, and creating a repository, rulesets and labels are things you run yourself, from a plain terminal, after reading what they will do. `/bootstrap` produces a plan and a setup script instead and records what it did. See [bootstrap.md](bootstrap.md).

## Can the loop merge for me? Why was I refused the remote lock? What is a claim?

No. `merge_policy` is `human`, `gh pr merge` is denied to every agent, and `loopctl merge-class` only answers whether an item *would* qualify for a future low-risk lane. The remote lock (`remote_lock`) is a ref on your git remote that keeps two machines off one plan; taking it is a push, so it is a human step, and you are refused when another machine holds it, when `remote_lock` is not configured, or when the evidence gate has not passed. A claim says a named person is working on an item (`loopctl claim take ITEM --by NAME`); the driver skips claimed items. See [loopctl-extras.md](loopctl-extras.md).

## What has not been confirmed against the real Claude Code?

Several platform assumptions (hook event names, a few settings keys, flag names) were not checked against the real binary. The kit assumed the conservative answer for each. `${CLAUDE_PLUGIN_ROOT}/docs/design/probe-assumptions.md` lists what is assumed and which probe settles it; `tools/probes/` is the harness a person runs to confirm or overturn them.
