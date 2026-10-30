# Tech Lead: Lane A, full feature

Reference file of the `tech-lead` skill ([SKILL.md](SKILL.md)). Read it when the session is in the full-feature lane.

### A.1 Verify the design is ready

1. Confirm `docs/features/<slug>/technical-design.md` and `api-contract.md` exist.
2. Confirm the Architect ran the **design-critic** subagent and got `DESIGN_APPROVED`. If not, tell the user to switch to `/architect` first. Don't dispatch against an uncritted design.
3. Read `requirements.md`, `technical-design.md`, `api-contract.md` in full.

### A.2 Auto-determine the engineer set

Read the design and dispatch only the engineers it actually needs. Empty/N/A sections mean *no dispatch* for that engineer.

| Section in design | Signal | Engineer needed |
|---|---|---|
| `## Data Model > New Tables` non-empty | yes | **Database** |
| `## Data Model > Modified Tables` non-empty | yes | **Database** |
| `## API Surface` lists at least one endpoint | yes | **Backend** |
| `## Frontend Impact` lists changes for an app | yes | **That app's engineer** |
| `## Cross-Cutting Concerns` / `## Integration Points` requires new infrastructure (new resource, IAM, secret, queue, bucket, pipeline) | yes | **Infra** |
| Item is the greenfield baseline / repo scaffold (no app code exists yet) | yes | **Backend** (scaffold brief: repo layout, package manifests, test harness, lint config, hello endpoint + one smoke test); **App** follows for the visible page |

Pure frontend feature -> no DB, no Backend. Schema-only feature -> only Database. **Don't dispatch agents for empty scopes.** If your project has no infrastructure-as-code, there's no infra-engineer to dispatch — skip that row.

> **Security review is not auto-dispatched per feature in the interactive lanes.** Run the `security-reviewer` subagent explicitly before a deploy or before opening a PR, or whenever a feature touches auth, payments, file uploads, or user-supplied input. It audits; it does not write code. **(In the autonomous `/orchestrate` loop it *is* auto-dispatched** on sensitive items per the `security_review` config — there's no human to remember, so the loop gates `done` on a `SECURITY_APPROVED` verdict.) **CI is the uniform gate for every lane:** once the project's CI runs the review, security review and scanners on every PR, an interactive feature gets them too without anyone remembering. Until then, the explicit run above is the only security review an interactive feature receives.

### A.3 Dispatch order

Default sequential dependency:

1. **Infra** (new resources — queue, bucket, secret — must exist before backend code references them). Skip if no infra change.
2. **Database** (migrations land first)
3. **Backend** (codes against the new schema and infra)
4. **Frontends** (only those in the engineer set), one after another

**There is no in-session parallel option.** Database and Backend run one after the other even for an additive schema change: parallel engineers in one checkout race on migration numbers, shared files and the database, and one engineer's half-written model breaks the other's tests. Frontends may run in parallel with each other only if they work in separate worktrees; by default, run them one after another.

If only some frontends are in scope, dispatch them only after Backend reports `APPROVED` (or `CONTRACT_DEVIATION` resolved).

### A.4 Write briefs

Per dispatched engineer, write `docs/features/<slug>/briefs/<engineer>.md` from `${CLAUDE_PLUGIN_ROOT}/templates/brief.md` (`<engineer>` = the domain shortname: `database`, `backend`, `app`, `admin-app`, `infra` — same for `reports/`). Each brief must include:

- Feature slug
- Pointers to `requirements.md`, `technical-design.md`, `api-contract.md`
- **Specific files to read by absolute path** — name them. Do not say "explore X."
- Exact scope for this engineer
- Canonical exemplar to follow
- Dependencies on other engineers' output (if any)
- Acceptance criteria specific to this engineer
- Out-of-scope fences
- The retry cap: `max_retries_per_item` from `.claude/loop.config.md` (default 2) — the one cap name used everywhere. There is no tool-call budget; `maxTurns` is deliberately unset (engineer-protocol §4)
- **Report path**: `docs/features/<slug>/reports/<engineer>.md`. The engineer writes its own report there (engineer-protocol §7)
- A `## Clarifications` section (empty at first). Answers to the engineer's questions are written here before it is resumed

### A.5 Surface the dispatch plan

**Branch check first**: run `git branch --show-current`. If it reports `main`/`master`: with `git_autonomy: none` (the default in `.claude/loop.config.md`), ask the user to create a feature branch (`git checkout -b feat/<slug>`) before you dispatch; with `branch` or `branch+commit`, create it yourself (`git switch -c feat/<slug>`) and say so. Engineers cannot branch (protocol §8), and the on-main hook (`guard-main-edit.sh`) will block their edits to app source.

Before invoking subagents, tell the user:

- Which engineers will be dispatched and in what order (always sequential)
- Path to each brief
- The rough scope (how many engineers, which areas), not a tool-call count

If interactive, get a "go" before dispatching.

### A.6 Dispatch + review loop

For each engineer in dispatch order:

1. Invoke via `Agent` tool with the matching `subagent_type` and a prompt that points to the brief path. Keep the `agent_id` the call returns: every later contact with that engineer (a clarification answer, a review retry, a `PARTIAL` continuation) is a **resume by `agent_id`** (SendMessage), never by name and never a fresh dispatch.
2. When the engineer returns, read the report's `Status`:
   - `APPROVED` -> with `git_autonomy: branch+commit`, make this engineer's WIP commit first (`${CLAUDE_PLUGIN_ROOT}/context/delivery.md` step 2: stage by path, never `git add -A`; trailers `Feature`, `Engineer`, `Report`), so the next engineer's diff cannot be mixed up with this one. Then dispatch the `code-reviewer` agent on the engineer's diff (see A.7).
   - `PARTIAL` -> engineer was not converging or hit a turn limit. Either resume it to continue, split the brief, or accept partial work if the remaining scope is trivial.
   - `NEEDS_CLARIFICATION` -> answer the gap (or escalate to PM/Architect). **Write the answer into the brief's `## Clarifications` section first**, so the record exists, then resume the engineer by `agent_id`. Re-dispatch fresh only if the resume fails.
   - `CONTRACT_DEVIATION` -> backend edited `api-contract.md`. Re-read it and update **frontend** briefs before dispatching them.
   - `CONTRACT_INCONSISTENCY` -> frontend reports the contract is wrong. STOP frontend dispatch. Reconcile with backend or escalate to Architect.
   - `DESIGN_DEVIATION` -> database engineer updated `technical-design.md`. Notify Architect-mode owner via the user.
   - `ESCALATION_NEEDED` -> STOP that engineer's chain. Surface to user with the engineer's escalation reason; recommend `/architect` for design rethink.
3. Check that the report file the engineer wrote at `docs/features/<slug>/reports/<engineer>.md` exists and has every required section (engineer-protocol §7). If it is malformed, send it back by resuming the engineer; do not re-type it yourself.

### A.7 Run code review (the `code-reviewer` agent)

Dispatch the **`code-reviewer`** agent (`subagent_type: code-reviewer`, clean context) on the engineer's diff. It is the same reviewer the `/orchestrate` loop uses: independent of the engineer and of you, with a parseable verdict. Pass:

- The list of files the engineer changed (from their report), and the commit range `<previous WIP commit>..<this WIP commit>` when you made WIP commits
- The brief path
- The api-contract path (if applicable)
- The technical-design path
- The iteration number

It returns `CODE_REVIEW_APPROVED` or `CHANGES_REQUESTED` with blockers. If `CHANGES_REQUESTED`:

1. **Resume the same engineer by `agent_id`** with: "Iteration 2. Address these blockers: [list]. Suggestions: [list]. The original brief is at <path>; your previous report is at <path>." Do not start a fresh engineer; it would lose the context of its own work.
2. Cap at `max_retries_per_item` (default 2). If still `CHANGES_REQUESTED` after the cap -> engineer's status becomes `ESCALATION_NEEDED`; surface to user. At round 2, re-dispatch at opus with `xhigh` effort (`${CLAUDE_PLUGIN_ROOT}/context/model-profiles.md`).

The bundled `/code-review` skill, if the user has it and asks for it, is an optional **second opinion** on top of this, never a replacement: it shares your context, which is exactly what the clean-context agent avoids.

### A.8 Wrap up

Once all dispatched engineers return `APPROVED`:

1. Summarize what was built per engineer (one paragraph each).
2. Note any `CONTRACT_DEVIATION` resolutions.
3. List `Open Questions / Brief Gaps` from each report (these inform future briefs), and apply or surface each report's `## Doc Delta` (a new endpoint or environment variable the docs don't yet mention).
4. Suggest verification steps for the user (manual smoke test, deploy commands).
5. **Post-feature primer update.** Read the `## Primer Delta` section of **every** report you collected (engineer-protocol §11) plus any `## Primer Staleness` from review, and **apply them now** — you hold the diffs, the reports, and the whole picture, and per `${CLAUDE_PLUGIN_ROOT}/context/primer-protocol.md` this is your contract, not the user's. Don't rely on inferring the delta from the file list: the engineer already told you. Also fix any exemplar pointer the feature invalidated (renamed or deleted file), and promote a new exemplar when this feature shipped a better one. Route to `/architect` only when the pattern needs an architectural call rather than a write-down. All deltas `NONE` and no new pattern → nothing to do, which is the common case for routine work.
6. **Record telemetry** (see section C).
7. **Deliver** (`${CLAUDE_PLUGIN_ROOT}/context/delivery.md`, steps 3-8). Commit the primer updates from step 5 on the branch, run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/verify.py --json > .claude/pr/<slug>.verify.json` and handle a failure by its `failure_class`, run the security review if the feature is sensitive, then `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py init`, fill the audit block, and run `pr_prepare.py prepare`. It prints the exact commands for the configured `delivery` (`local`: merge locally; `push-branch`: push; `draft-pr`: push and open a draft PR) under a header that says who runs them. `# CONDUCTOR STEPS` (`git_autonomy: branch+commit+push`): run them yourself, in order, and tell the user what each printed. `# HUMAN STEP`: show them to the user and stop. With `git_autonomy: none`, also ask the user to commit first and give the paths. Whatever the setting: you run no write git beyond what `git_autonomy` allows, **you never push `main` or a protected branch, and you never merge**; `pr_prepare.py` itself pushes nothing.
8. **After the push** (yours or the user's), if CI fails and the user pastes the failure, fix it within `max_ci_fix_attempts` (delivery.md, "CI-fix"). Workflows, tests and lockfiles stay untouched while fixing.

> The feature folder (`docs/features/<slug>/`) is now scratch. The canonical references for future work are the **primers** (`.claude/context/*.md`) plus the **code itself**. Keeping the primers true is the last thing you do before calling the feature done (step 5) — after that, this feature folder is finished.
