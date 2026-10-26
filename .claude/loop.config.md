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
max_design_rounds: 2             # design-critic rounds per design (L-34). Round 2 is limited to the
                                 # round-1 blockers plus any NEW Critical; still blocked after the last
                                 # round is a hard-stop (interactive: surfaced to the user).

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
verify_build: ""                 # Optional G6 stages, run by `python3 .claude/scripts/verify.py` before the tests, in this order:
                                 # verify_build, verify_lint, verify_typecheck. Empty = the stage does not run (see verify_require).
verify_lint: ""                  # lint command; must exit 0
verify_typecheck: ""             # typecheck command; must exit 0
verify_require: [tests]          # Stages verify.py refuses to skip: build | lint | typecheck | tests | smoke. A required stage with no
                                 # command is a configuration error, never a silent pass. `tests` always runs when verify_tests is set.
verify_ports: []                 # Dedicated verify ports (not the dev-server defaults, L-27). Checked free before the smoke stage; a busy
                                 # port is an `env` failure, never a retry. Checked again after it: a port still bound means the dev server leaked.
                                 # With verify_dev the first port is where verify.py polls and sends the smoke requests.
verify_dev: ""                   # Optional. A dev-server command that never exits. verify.py starts it in its own process group, polls
                                 # http://127.0.0.1:<first verify port><verify_ready_path> until it answers or verify_run_timeout_s passes,
                                 # requests the item's demo route, then kills the whole group. Takes the place of verify_run for the smoke stage.
                                 # Gets VERIFY_PORT (the first verify port) in its environment.
verify_ready_path: /             # Path polled for readiness when verify_dev is set. Any status below 400 counts as ready.
verify_timeout_s: 600            # Seconds each build, lint, typecheck and test stage may run. On expiry the stage's process group is killed.
verify_run_timeout_s: 120        # Seconds the smoke stage may run (boot, readiness, requests, teardown) before its process group is killed.
verify_env_allow: []             # Names of extra environment variables verify.py passes through. Everything else is dropped: it runs the
                                 # stages under an allowlisted environment (PATH, HOME, locale, toolchain locators), so no token or cloud
                                 # credential of the calling shell reaches test code. Names only; never put a value or a secret here.

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
db_local: sqlite                 # sqlite | postgres. The local engine must match PRODUCTION (DEC-8):
                                 # sqlite only if prod is SQLite or libSQL; otherwise postgres, in-process
                                 # first (PGlite / embedded-postgres), then a container. Set it explicitly.
                                 # No matching engine available: doctor error, level capped at L1.
                                 # See .claude/context/local-dev-rules.md
db_ephemeral: <command>          # EXECUTION-CRITICAL (hard-stop if left as <command>): how to spin
                                 # a throwaway per-item DB + how the DATABASE_URL override reaches the
                                 # verify subprocess (document the exact commands in local-dev.md)
                                 #   postgres: CREATE DATABASE ... TEMPLATE <seeded> (seconds), or a
                                 #             disposable container
                                 #   sqlite (only if prod is SQLite): copy/delete a temp .db file

# --- Git ---
git_strategy: branch-off-main-merge-on-pass   # the only V1 strategy
# The conductor branches each item off main, merges to LOCAL main on verify-pass, and NEVER pushes.
# You review + push at checkpoints.

# --- Infrastructure ---
infra_gated_behind_local: true   # deploy/IaC milestones won't start until the app runs locally
                                 # infra-touching items always force a checkpoint regardless of autonomy

# --- Safety: engineer git boundary ---
enforce_engineer_git_hook: true  # documents whether you keep the shipped PreToolUse hook
                                 # (.claude/hooks/guard-git.sh) wired. NOTHING READS THIS VALUE: the hook
                                 # is on or off by its entry in .claude/settings.json, not by this flag.
                                 # HONEST SCOPE: the hook blocks only universally-forbidden shared-history
                                 # ops (push, rebase, non-feat force-delete, shared hard-reset) for EVERY
                                 # actor; it cannot tell an engineer's `git commit` from the conductor's.
                                 # The "engineers are git-read-only" boundary itself is protocol convention
                                 # (engineer-protocol §8). The hook FAILS OPEN (allows) when python3 is
                                 # missing or the command cannot be parsed, until the v2 guards (Phase 2)
                                 # replace it. If false, you accept the residual risk with no hook backstop.

# --- Model and effort routing (see .claude/context/model-profiles.md) ---
model_profile: quality           # quality | balanced | economy (§11.4). Which model each role runs on.
                                 #   quality  = opus for every engineer and critic (default; frontmatter as shipped)
                                 #   balanced = sonnet for the frontend engineers, opus for the rest
                                 #   economy  = sonnet engineers; opus kept for the three reviewers (experimental)
                                 # The conductor passes a per-invocation model, and omits it when
                                 # CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set. Stays `quality` until fixture data shows parity.

# --- Slicing and the quick lane (planning rules; the mechanical checks arrive with loopctl) ---
max_item_loc: 400                # estimated changed lines per plan item (L-35). The Planner splits anything
                                 # bigger; an item sized L must be split. Planning rule now; loopctl validate
                                 # checks it later.
quick_lane_max_loc: 30           # quick lane (/tech-lead Lane B): the largest change that may skip review (CO-20).
                                 # Also required: no always-human path, no new dependency, no schema, no auth or
                                 # payments. Anything else gets a code-reviewer pass.

# --- Boundary (security model, report §5) ---
# Attestations are things the repo cannot show; a human commits them under H6.
# Everything here is read by `python3 .claude/scripts/doctor.py`.
autonomy_level: L1               # L0 | L1 | L2 | L3 | L4 | L5   (report §6.2). The level you ask for; the doctor prints the
                                 #   EFFECTIVE level, which is lower whenever a prerequisite or cap fails. Run
                                 #   `python3 .claude/scripts/doctor.py --level` to see each cap and its reason.
                                 #   L0 assist | L1 supervised local loop (today's V1) | L2 push-branch | L3 draft-PR factory
                                 #   L4 async factory | L5 low-risk auto-merge. L4 and L5 need docs/baseline/evidence.json.
github_repo: ""                  # owner/name of the GitHub repo the doctor reads rulesets from. Empty = derive from `origin`.
bot:
  kind: none                     # none | github-app | machine-user-pat   (report §5.1 B1). The identity the agent pushes as.
                                 #   github-app        required at L3+
                                 #   machine-user-pat  acceptable at L2 only, a fine-grained PAT that expires within 14 days
                                 #   never a PAT that belongs to a human
  login: ""                      # the bot's GitHub login (for an App, `<slug>[bot]`). Never a human's login.
  id: ""                         # numeric id of the App or machine user, as it would appear in a ruleset's bypass_actors.
  token_expires: ""              # machine-user-pat only: the PAT's expiry date, YYYY-MM-DD. Must be at most 14 days away.
solo_maintainer: false           # true on a one-human install. The human admin may then bypass rulesets in pull_request mode
                                 #   only (RC-9); the bot never may. false (default) means no bypass actors at all.
deploy_triggers: []              # HUMAN ATTESTATION (report §8.4, B5): every workflow that deploys, found by the doctor or declared.
                                 #   - workflow: .github/workflows/deploy.yml
                                 #     fires_on: "push:main"            # push:main | push:branches | tag | release | workflow_run | ...
                                 #     gate: gated-by-environment       # or: none
                                 # A deploy on a bot-pushable ref with gate none caps the level at L1; on main, at L4.
git_integrated_hosts: []         # HUMAN ATTESTATION: hosts that deploy from git with no workflow in the repo (dashboard config).
                                 #   - host: vercel
                                 #     previews_on: none               # none | label | all-branches | pull-requests
                                 #     production_on: main
                                 #     gate: none                      # none | gated-by-environment | manual
deploy_surface:
  attested_at: ""                # date (YYYY-MM-DD) the human last attested deploy_triggers and git_integrated_hosts.
  fingerprint: ""                # sha256 printed by `doctor --deploy-surface`. A change to any deploy-surface file makes it stale
                                 #   and the attestation void until a human re-attests (report §8.4).
sandbox_verified: ""             # date (YYYY-MM-DD) the human confirmed the OS sandbox really is active (V19). Empty = unverified,
                                 #   which keeps the level at L1.
agent_isolation: none            # none | separate-os-user | devcontainer | container   (report §5.1 B3). What the human attests
                                 #   keeps the agent away from their credentials. A devcontainer or container with no credentials
                                 #   also satisfies the managed-settings requirement at L3.
managed_settings_verified: ""    # date (YYYY-MM-DD) managed settings (allowManagedHooksOnly, disableBypassPermissionsMode) were
                                 #   installed for the OS user that runs the loop. Needed at L3+ unless agent_isolation is a container.
platform_attested: []            # GitHub features the human confirmed are available on this repo's plan, for what the doctor
                                 #   cannot read (report section 8.4, V27). Unknown means unavailable. Names: rulesets,
                                 #   code_owner_review, required_status_checks, bot_no_bypass, environment_reviewers,
                                 #   apply_human_dispatch (IaC apply only by a human-started workflow), g3_ci_only (held-out
                                 #   tests run only in CI, RC-6), push_rulesets.
factory_approvers: []            # logins of the humans whose labels count (`factory:go`, `plan-ok`, `preview`). The label-guard
                                 #   workflow template removes the label when anyone else applies it (RC-20).
promotion_attested: ""           # date (YYYY-MM-DD) the human confirmed the §6.3 promotion bar is met. Needed for L5.
mcp_allowed_servers: []          # names of MCP servers or plugins the human accepts in a loop run (report section 5.6), for
                                 #   example a read-only docs server. Any other deploy-capable server caps the level at L1.

# --- Guards (report section 7). Read by .claude/scripts/guards/ ---
roster: {}                       # extra subagent roles the guards should know, as role: engineer | critic (CS-39).
                                 #   The built-in roster (the kit's own agent names) needs no entry. A role that is not on the roster is treated
                                 #   as an engineer (agent-blind default); a `<plugin>:` prefix on agent_type is stripped before matching.
protected_branches: [main, master]   # branches that are shared: no edits to app source while checked out, no push, no force-delete, no hard-reset onto them.
                                 #   main and master are always protected, whatever this says.
integration_branch: ""           # the loop's integration branch, if you use one (DEC-2). Protected like main. Empty = none.
main_edit_allow: []              # extra path globs that may be edited while on a protected branch (repo-relative, `**` crosses directories).
                                 #   Built in: docs/**, plan files, README/CHANGELOG/GLOSSARY/LICENSE/.gitignore, CLAUDE.md and .claude/context/**.
                                 #   There is no allow-list for the rest of .claude/** (report section 7.1).

# --- Delivery (interactive PR lane; see .claude/context/delivery.md) ---
# Agents never push and never create a PR. `python3 .claude/scripts/pr_prepare.py` validates the
# work and prints the exact commands; the human runs them (IMP-25).
delivery: local                  # local | push-branch | draft-pr   (report R5, DEC-1). How finished work leaves the machine.
                                 #   local       = the human merges the feature branch into the local main; nothing is pushed (default)
                                 #   push-branch = pr_prepare prints the push command for the feature branch
                                 #   draft-pr    = pr_prepare prints the push command and `gh pr create --draft --body-file ...`
                                 # Pushing is always the human's act. The doctor suggests draft-pr once the ruleset checks pass.
git_autonomy: none               # none | branch | branch+commit   (DEC-22). What the interactive conductor may do with git itself.
                                 #   none          = it asks you to create the branch and to commit (v1 behaviour, default)
                                 #   branch        = it creates and switches to `feat/<slug>` itself
                                 #   branch+commit = it also makes one WIP commit per engineer so reviewers see `prev..sha`
                                 # Never push, merge, rebase or reset: those stay yours at every setting. Recommended with delivery: draft-pr.
max_ci_fix_attempts: 2           # CI-fix commits allowed on one PR (report F-28). While fixing CI, edits to `.github/workflows/**`,
                                 # acceptance tests and lockfiles are refused; each fix commit carries a `CI-Fix-Of: <sha>` trailer.
                                 # Still red after the last attempt: stop and hand it to a human.
always_human_paths: []           # Extra path globs a human must review on top of the kit's built-in list (report H3): migrations, auth,
                                 # payments, infra, .github/, .claude/, lockfiles, test and CI config, deploy files. pr_prepare lists
                                 # every changed path that matches and refuses a PR body that says "none" when the diff touches one.
                                 #   - src/auth/**
                                 #   - src/payments/**

# --- loopctl (report section 10; python3 .claude/scripts/loopctl <command>) ---
# loopctl owns the deterministic decisions of the loop: state, counters, caps, the lock, spend, abort.
# Read from the BASE sha by loopctl, never from the branch it is judging (CO-11).
runner: sequential               # sequential | driver   (report section 11.2, DEC-23). Where an item runs. Never auto-selected.
                                 #   sequential = the prose loop: the Orchestrator conductor dispatches engineers itself; `loopctl run` prints the
                                 #                hand-off and exits (default)
                                 #   driver     = `loopctl run` launches one `claude -p` child per item or stage (OPT-IN, needs loopctl_tier2: true,
                                 #                run_budget_usd > 0 and the driver module)
loopctl_tier2: false             # OPT-IN (experimental, IMP-1). Tier 2 = the loopctl commands that write loop state: begin, advance, fail, approve,
                                 #   gate, run, abort, reconcile --apply. They refuse while this is false. Tier 1 (validate, status, render, doctor,
                                 #   migrate-plan, lock, evidence, cost, audit, halt, resume) works either way. It is not evidence-gated: three clean
                                 #   runs with loopctl are what the evidence gate asks for.
run_budget_usd: 0                # USD ceiling for one run, summed from each child session's total_cost_usd (report section 10.5). 0 = not set.
                                 #   Set it explicitly before `runner: driver` (no projection exists before a baseline). Reaching it parks the run.
item_budget_usd: 0               # USD ceiling for one item across its attempts. 0 = not set. Reaching it sets the item needs_human.
day_budget_usd: 0                # USD ceiling per UTC day over every run in this repository. 0 = not set.
lock_stale_min: 60               # Minutes without a heartbeat before the run lock may be stolen (report section 19.7). The owner must also be
                                 #   gone: the driver pid dead, or the session not registered. A lock from another host is never stolen unless
                                 #   you say `lock steal --host-dead`.
max_transient_retries: 2         # Retries of a `transient` failure (flaky test, port in use) per item attempt. Each one also uses retry budget.
max_platform_retries: 6          # Retries of a `platform` failure (API 5xx, overloaded, rate limit, CLI crash) per item, with backoff and jitter.
                                 #   They do not use retry budget; past this number the item is parked as needs_human (an outage, not a bug).
platform_backoff_base_s: 30      # Seconds before the first platform retry; it doubles each time, with jitter, up to 15 minutes.
dispatch_timeout_min: 20         # Minutes without a tool call before a dispatched session counts as stalled (a `transient` failure). Read by the
                                 #   driver; loopctl classifies the stall.
orphan_retention_days: 7         # Days a `dead/*` branch is kept before `reconcile --apply` prunes it. `blocked/*` is never auto-deleted.
state_remote: ""                 # Name of the git remote that also holds refs/loopctl/state/<plan>. Empty = local only (L1). At L2 and above set it:
                                 #   state is then written with a compare-and-swap push, so it survives a fresh clone. Advisory, not a boundary:
                                 #   anyone with contents: write can move the ref. Empty or a remote name; never a URL.
db_migrate_down: ""              # Command that runs ONE down-migration against the local or ephemeral database (report section 19.7 L-25). It gets
                                 #   LOOPCTL_ITEM, LOOPCTL_MIGRATION and LOOPCTL_MIGRATION_DOWN in its environment, never on its command line.
                                 #   Empty = `loopctl abort` writes the down-migrations into the human revert plan instead of running them.
```

## Notes

- **Sequential only (V1).** The Orchestrator runs exactly one item at a time. Parallel execution is deferred (it reintroduces migration-number collisions and merge races).
- **Budgets: measurable caps are the real guards.** The token budgets are best-effort estimates the executor cannot actually measure — they only trigger a pause when work is *clearly* over. The hard, enforceable ceilings are `max_items_per_run`, `max_tool_calls_per_item`, `max_tool_calls_per_run`, and `max_retries_per_item`, all counted from the conductor's own tool calls. Never present the token numbers as a guarantee.
- **Security runs in the loop.** `security_review` gates `done`: for `sensitive` items (auth / payments / uploads / user input) the conductor dispatches the `security-reviewer` agent on the diff, and any Critical/High finding is a hard-stop. An unattended run that ships user-facing features should not set this to `off`.
- **Independent review in the loop.** In the autonomous loop the conductor *authored* the design + briefs + dispatch, so it runs code review through a **fresh-context `code-reviewer` subagent** to keep the reviewer independent of the author. The interactive Tech-Lead uses the same agent: one reviewer in every lane.
- **Two readiness gates at startup.** (1) *Autonomy primers* still containing placeholders (`<!-- ... -->`, `TODO(primer)`) → the preamble derives them (`.claude/context/primer-protocol.md`); whatever genuinely can't be known yet **downgrades** autonomy to `per-feature` (you can still run). (2) *Execution-critical config* (`verify_tests`, `verify_run`, `db_ephemeral`, and the run/test/ephemeral commands in `local-dev.md`) still as `<command>` → **hard-stop**. Neither gate is a task for you — the Planner writes these as plan outputs.
- **Capabilities the loop expects.** A way to *see* the running app for auto-demo and a way to *notify* you at checkpoints. Neither is assumed: the preamble picks the best one available, in a fixed fallback order (browser-preview MCP if one is present, then the bundled `/run` skill, then a route plus HTTP status check as text; a push-notification tool if present, then a clearly marked banner in the transcript). See `orchestrate.md` Run Preamble step 8 and `.claude/context/platform-notes.md`.
- **Settings are yours, not the agents'.** The agents never edit `.claude/settings.json` or `settings.local.json`. Where a run would pause on permission prompts, the Planner and Orchestrator print a snippet for you to apply (`.claude/context/platform-notes.md`, Permissions snippet).
