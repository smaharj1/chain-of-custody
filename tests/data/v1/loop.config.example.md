# Loop Config

Configuration for the loop-engineering layer (`/planner` + `/orchestrate`) for TaskFlow. The Orchestrator reads this file at the start of every run.

```yaml
# --- Autonomy ---
autonomy: per-milestone          # pause at milestone boundaries

# --- Retry & circuit breaker ---
max_retries_per_item: 2          # within-item verify retries before `blocked`
max_consecutive_failures: 2      # consecutive blocked items -> hard-stop

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

# --- Security review (in-loop gate) ---
security_review: sensitive       # run security-reviewer on auth / payments / uploads / user-input items

# --- Local database ---
db_local: sqlite                 # SQLite at ./.data/dev.db (prod is Postgres 16 on RDS)
db_ephemeral: cp ./.data/dev.db /tmp/taskflow-item-$ID.db
                                 # verify subprocess gets DATABASE_URL=file:/tmp/taskflow-item-$ID.db;
                                 # delete the copy after verify (see local-dev.md, Ephemeral Per-Item DB)

# --- Git ---
git_strategy: branch-off-main-merge-on-pass   # the only V1 strategy; conductor never pushes

# --- Infrastructure ---
infra_gated_behind_local: true   # CDK milestones wait until the app runs locally; infra items force a checkpoint

# --- Safety: engineer git boundary ---
enforce_engineer_git_hook: true  # PreToolUse hook blocks git-write Bash for engineer subagents
```

## Notes

- **Sequential only (V1).** One item at a time; parallel execution is deferred.
- **Durable-DB apply happens at merge.** After an item merges to local main, the Orchestrator runs `pnpm db:migrate` against `./.data/dev.db`. Verification always runs against the ephemeral copy, never the durable DB.
- **Measurable caps are the real guards.** The token numbers are best-effort estimates; the tool-call and item ceilings are what actually stop a runaway.
- **Security runs in the loop.** `sensitive` items (Clerk auth, Stripe billing, uploads, user input) get a `security-reviewer` pass before `done`; Critical/High findings hard-stop.
