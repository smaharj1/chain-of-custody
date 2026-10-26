# Loop Config

Configuration for the loop-engineering layer (`/planner` + `/orchestrate`) for TaskFlow. The Orchestrator reads this file at the start of every run.

```yaml
# --- Autonomy ---
autonomy: per-milestone          # pause at milestone boundaries

# --- Retry & circuit breaker ---
max_retries_per_item: 2          # within-item verify retries before `blocked`
max_consecutive_failures: 2      # consecutive blocked items -> hard-stop
max_design_rounds: 2             # design-critic rounds before a hard-stop

# --- Budget: measurable hard caps (the real guards) ---
max_items_per_run: 25            # hard global ceiling on items per run
max_tool_calls_per_item: 60      # hard per-item ceiling on the conductor's own tool calls
max_tool_calls_per_run: 600      # hard global ceiling across the run

# --- Budget: token estimates (best-effort soft-stops, not enforceable) ---
token_budget_per_item: 150000
total_token_budget_per_run: 2000000

# --- Verification (local-first; see .claude/context/local-dev.md) ---
verify_tests: pnpm test          # vitest across the workspace; must exit 0 AND collect >0 tests
verify_run: pnpm dev             # boot web (5173) + admin (5174) + API (3001), then run the
                                 # verify-run harness in local-dev.md: poll the item's demo-target
                                 # route until it responds, curl -sf http://localhost:3001/api/health,
                                 # kill the dev server, exit with the health check's code
verify_build: pnpm build         # production build
verify_lint: pnpm lint           # lint
verify_typecheck: pnpm typecheck   # typecheck
verify_require: [tests, lint, typecheck, smoke]   # none of these may be left unconfigured
verify_ports: [5183, 3011]       # web and API verify ports
verify_timeout_s: 600            # per-stage timeout (seconds)
verify_run_timeout_s: 120        # smoke-stage timeout (seconds)
verify_env_allow: [NODE_OPTIONS]   # extra variables the toolchain needs

# --- Security review (in-loop gate) ---
security_review: sensitive       # run security-reviewer on auth / payments / uploads / user-input items

# --- Local database ---
db_local: postgres               # in-process Postgres (PGlite); prod is Postgres 16 on RDS
db_ephemeral: pnpm db:ephemeral $ID
                                 # CREATE DATABASE taskflow_item_$ID TEMPLATE taskflow_seeded; verify subprocess
                                 # gets DATABASE_URL pointing at it; drop it after verify (see local-dev.md,
                                 # Ephemeral Per-Item DB)

# --- Git ---
git_strategy: branch-off-main-merge-on-pass   # the only V1 strategy; conductor never pushes

# --- Infrastructure ---
infra_gated_behind_local: true   # CDK milestones wait until the app runs locally; infra items force a checkpoint

# --- Safety: engineer git boundary ---
enforce_engineer_git_hook: true  # keep guard-git.sh wired: blocks push/rebase/shared hard-reset for every
                                 # actor, fails open without python3 (engineer git-read-only is convention)

# --- Model and effort routing (see .claude/context/model-profiles.md) ---
model_profile: quality           # opus for engineers and critics (see model-profiles.md)

# --- Slicing and the quick lane ---
max_item_loc: 400                # estimated changed lines per item; larger items are split
quick_lane_max_loc: 30           # largest quick-lane change that may skip review

# --- Delivery ---
# Agents never push and never create a PR. `python3 .claude/scripts/pr_prepare.py` validates the
# work and prints the exact commands; the human runs them (IMP-25).
delivery: draft-pr               # branch, verify, then pr_prepare prints the push and draft-PR commands for me to run
git_autonomy: branch+commit      # the conductor branches and makes the WIP commits; I push
max_ci_fix_attempts: 2           # CI-fix attempts per PR
always_human_paths: [src/auth/**, src/payments/**]   # project paths a human always reviews (the kit's own list always applies)

# --- loopctl ---
# loopctl owns the deterministic decisions of the loop: state, counters, caps, the lock, spend, abort.
# Read from the BASE sha by loopctl, never from the branch it is judging (CO-11).
runner: sequential               # the Orchestrator runs the loop in my session
loopctl_tier2: false             # leave off until you want loopctl to own the item state
run_budget_usd: 0                # 0 = not set; the driver refuses to start until you set a ceiling
```

## Notes

- **Sequential only (V1).** One item at a time; parallel execution is deferred.
- **Durable-DB apply happens at merge.** After an item merges to local main, the Orchestrator runs `pnpm db:migrate` against the durable local dev database. Verification always runs against the per-item ephemeral database (a template clone of the seeded one), never the durable DB. TaskFlow's production database is Postgres, so the local one is Postgres too (in-process), not SQLite.
- **Measurable caps are the real guards.** The token numbers are best-effort estimates; the tool-call and item ceilings are what actually stop a runaway.
- **Security runs in the loop.** `sensitive` items (Clerk auth, Stripe billing, uploads, user input) get a `security-reviewer` pass before `done`; Critical/High findings hard-stop.
