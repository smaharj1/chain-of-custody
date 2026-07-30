# Loop Config

Configuration for the loop-engineering layer (`/planner` + `/orchestrate`). The Orchestrator reads this file at the start of every run. Edit the values to match your project; the comments explain each one.

> If this file is missing, `/orchestrate` refuses to start and tells you to create it from this template.

```yaml
# --- Autonomy ---
autonomy: per-milestone          # per-feature | per-milestone | unattended
                                  #   per-feature   = pause after every item (max oversight)
                                  #   per-milestone = pause at milestone boundaries (default)
                                  #   unattended    = no discretionary pauses (hard-stops still fire)

# --- Retry & circuit breaker ---
max_retries_per_item: 2          # caps three within-item loops INDEPENDENTLY (each gets its own
                                 # counter): verify retries, code-review re-dispatches, and PARTIAL
                                 # split re-dispatches. Worst case is bounded by max_tool_calls_per_item.
max_consecutive_failures: 2      # circuit breaker: consecutive `blocked` items (no `done` between) -> hard-stop
                                  #   counts FAILED ITEMS, not within-item retries

# --- Budget: measurable hard caps (the REAL guards — always honored) ---
# These are the ceilings the conductor can actually COUNT, so they are what truly stops a
# runaway. They are tallied from the conductor's OWN tool calls + item/retry counts and logged
# to loop.jsonl. The token estimates below CANNOT be enforced precisely — do not rely on them.
max_items_per_run: 25            # HARD global ceiling on items attempted in one run
max_tool_calls_per_item: 60      # HARD per-item ceiling on the conductor's OWN tool calls
                                 #   (Agent dispatches, code-review, security-review, verify, git,
                                 #   reads/writes). This is the backstop that actually bounds a single
                                 #   item — the token caps below can't. Breach -> budget_halted.
                                 #   Raise it for items that fan out to many engineers; it's a runaway
                                 #   backstop, not a tuning target.
max_tool_calls_per_run: 600      # HARD global ceiling on the conductor's own tool calls across the run

# --- Budget: token estimates (BEST-EFFORT soft-stops — NOT enforceable) ---
# The executor CANNOT read its own exact token count, so these are ESTIMATES only. They make the
# loop pause when work is CLEARLY over budget; they are not a precise gate and must never be
# presented as one. The measurable caps above are the hard guards. (Read these as "_est".)
token_budget_per_item: 150000    # best-effort estimate; pause when clearly exceeded
total_token_budget_per_run: 2000000   # best-effort global estimate

# --- Verification (local-first; see .claude/context/local-dev.md) ---
# EXECUTION-CRITICAL: if any of these is left as <command>, /orchestrate HARD-STOPS at
# the preamble (downgrading autonomy is not enough — the loop can't pass a verify gate).
verify_tests: <command>          # e.g. "pnpm test" — must exit 0 AND collect >0 tests
verify_run: <command>            # how to boot the app locally for the per-item runtime assertion
                                 #   e.g. "pnpm dev" + a preview assertion on the item's demo target

# --- Security review (in-loop gate) ---
security_review: sensitive       # sensitive (default) | every_item | off
                                 #   sensitive  = run the security-reviewer agent on any item whose
                                 #                design touches auth, payments, file uploads, or
                                 #                user-supplied input
                                 #   every_item = run it on every item
                                 #   off        = never run it in the loop (NOT recommended for an
                                 #                unattended run that ships user-facing features)
                                 # Any Critical/High finding -> hard-stop (security_blocked); the item
                                 # does NOT reach `done` until the findings are resolved, or the USER
                                 # records a securityWaiver on the item (justification required —
                                 # only the user can grant it, at the hard-stop or unblock step).

# --- Local database ---
db_local: sqlite                 # sqlite (default, zero-setup) | postgres
db_ephemeral: <command>          # EXECUTION-CRITICAL (hard-stop if left as <command>): how to spin
                                 # a throwaway per-item DB + how the DATABASE_URL override reaches the
                                 # verify subprocess (document the exact commands in local-dev.md)
                                 #   sqlite: copy/delete a temp .db file
                                 #   postgres: template clone or disposable container

# --- Git ---
git_strategy: branch-off-main-merge-on-pass   # the only V1 strategy
# The conductor branches each item off main, merges to LOCAL main on verify-pass, and NEVER pushes.
# You review + push at checkpoints.

# --- Infrastructure ---
infra_gated_behind_local: true   # deploy/IaC milestones won't start until the app runs locally
                                 # infra-touching items always force a checkpoint regardless of autonomy

# --- Safety: engineer git boundary ---
enforce_engineer_git_hook: true  # if true, keep the shipped PreToolUse hook (.claude/hooks/guard-git.sh)
                                 # wired. HONEST SCOPE: the hook blocks only universally-forbidden
                                 # shared-history ops (push, rebase, non-feat force-delete, shared
                                 # hard-reset) for EVERY actor — it cannot tell an engineer's
                                 # `git commit` from the conductor's. The "engineers are git-read-only"
                                 # boundary itself is protocol convention (engineer-protocol §8).
                                 # If false, you accept the residual risk with no hook backstop at all.
```

## Notes

- **Sequential only (V1).** The Orchestrator runs exactly one item at a time. Parallel execution is deferred (it reintroduces migration-number collisions and merge races).
- **Budgets: measurable caps are the real guards.** The token budgets are best-effort estimates the executor cannot actually measure — they only trigger a pause when work is *clearly* over. The hard, enforceable ceilings are `max_items_per_run`, `max_tool_calls_per_item`, `max_tool_calls_per_run`, and `max_retries_per_item`, all counted from the conductor's own tool calls. Never present the token numbers as a guarantee.
- **Security runs in the loop.** `security_review` gates `done`: for `sensitive` items (auth / payments / uploads / user input) the conductor dispatches the `security-reviewer` agent on the diff, and any Critical/High finding is a hard-stop. An unattended run that ships user-facing features should not set this to `off`.
- **Independent review in the loop.** In the autonomous loop the conductor *authored* the design + briefs + dispatch, so it runs code review through a **fresh-context `code-reviewer` subagent** (not the in-context Skill) to keep the reviewer independent of the author. The interactive Tech-Lead keeps the Skill because a human is the backstop there.
- **Two readiness gates at startup.** (1) *Autonomy primers* still containing placeholders (`<!-- ... -->`, `TODO(primer)`) → autonomy **downgrades** to `per-feature` (you can still run). (2) *Execution-critical config* (`verify_tests`, `verify_run`, `db_ephemeral`, and the run/test/ephemeral commands in `local-dev.md`) still as `<command>` → **hard-stop** until filled.
- **Capabilities the loop expects.** A screenshot capability for auto-demo (e.g. a browser-preview MCP such as `Claude_Browser` — `preview_start`, `navigate`, screenshot) and a notification path for checkpoints (a push-notification capability if the environment provides one). Both have text fallbacks if absent (route + HTTP check; transcript banner) — see `orchestrate.md` Run Preamble step 5.
