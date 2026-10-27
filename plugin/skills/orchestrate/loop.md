# Orchestrate: the loop

Reference file of the `orchestrate` skill ([SKILL.md](SKILL.md)). Re-read it at the start of every iteration.

## The Loop (re-read canonical state each iteration)

### 1. Select
Pick the next **eligible** `pending` item by **topological order** (eligible = every `dependsOn` item is `done`). If none eligible:
- all items `done` → **finish** (summary + notification; prune this run's `dead/*` branches).
- items remain but none eligible → **DEADLOCK**: report the directly-blocked vs transitively-blocked sets, then stop.

### 2. Precheck
- `requiresEnv` present? If not → set `status: blocked`, `blockReason: needs_provisioning`, hard-stop (before burning retries/budget).
- Working tree clean (`git status --porcelain` empty)? If not → cleanup contract (own-branch `reset --hard`/`clean` in the loop's own clone), then proceed.

### 3. Start
- Mark the item `in_progress`; **write `item.branch = "feat/<slug>-a<n>"`** (`<n>` is the attempt, starting at 1; derive it from any existing `dead/<slug>-a*` / `blocked/<slug>-a*` branches and `loop.jsonl`); regenerate `BUILD_PLAN.md`.
- Cut the branch **off current main** (which contains all merged completed dependencies).
- Provision an **ephemeral DB** by running the `db_ephemeral` command from `.claude/loop.config.md` (`local-dev.md` documents the exact commands + how the `DATABASE_URL` override reaches the verify subprocess).

### 4. Per-item pipeline (this session, autonomous)
Run in order. **Budget check between stages** (re-stamp the lock `heartbeat` here too) — two tiers, do not conflate them:

- **Measurable hard caps (real hard-stops).** Keep a running tally of the conductor's own tool calls and check it against `max_tool_calls_per_item` and `max_tool_calls_per_run`; count pipeline iterations / engineer re-dispatches against `max_items_per_run` / `max_retries_per_item`. Breaching any of these is a hard-stop (`budget_halted`). These are the guards that actually bound a runaway, including a single item with no token bound. Log `tool_calls` to `loop.jsonl` as the auditable proxy.
- **Token estimates (best-effort soft-stops).** `token_budget_per_item` / `total_token_budget_per_run` are **estimates** — the executor cannot read its own exact token count. Estimate from work done and pause when *clearly* over; never treat these as a precise gate or report them as one.

**Models.** Dispatch each agent per `model_profile` (`${CLAUDE_PLUGIN_ROOT}/context/model-profiles.md`): pass no `model` under `quality`; pass the profile's model otherwise, unless `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set. Escalate to opus at `xhigh` effort on a round-2 review retry.

1. **Autonomous PM** — derive requirements for this item from its `intent` + `acceptance`.
2. **Autonomous Architect** — write the technical design + api-contract for this item; record decisions inline; flag low-confidence/load-bearing calls as `ESCALATION_NEEDED`.
3. **design-critic** (Agent) — capped at `max_design_rounds` (default 2). **Round 2 is limited to resolving the round-1 blockers plus any new Critical**; it must not open fresh nitpicks. `DESIGN_NEEDS_REVISION` after the last round → hard-stop. Pass this item's requirements **inline in the dispatch prompt** (the loop writes no `requirements.md`; the critic treats inline requirements as equivalent).
4. **Engineer-set detection** — use the real Tech-Lead A.2/A.3 logic: infra-first ordering; **strictly sequential, one engineer at a time (V1 — there is no in-session parallel option)**; scaffold/baseline items → Backend per A.2's scaffold row; **don't dispatch empty scopes**; roster from `.claude/agents/*.md`.
5. **Dispatch engineers** (Agent), in dependency order. **Write each brief to `docs/features/<slug>/briefs/<domain>.md`** exactly as interactive A.4 does, so engineer required-reading paths resolve; requirements are passed inline (no `requirements.md` in loop mode). The dispatch prompt carries the **brief path** and nothing else that is load-bearing. Engineers write their own report file and return `{status, report_path}` plus a JSON trailer (engineer-protocol §7); you do not re-type it. **Keep each engineer's `agent_id`** from the Agent result: every later contact (a clarification answer, a review retry, a `PARTIAL` continuation) is a **resume by `agent_id`** (SendMessage), never by name and never a fresh dispatch. **Frontend dispatch gate**: dispatch frontends only after Backend reaches `APPROVED`, or its `CONTRACT_DEVIATION` is resolved (re-read `api-contract.md`, regenerate frontend briefs first).
6. **Independent code review** — dispatch the **`code-reviewer` Agent** on each engineer's diff. *Why an Agent in the loop:* you authored this item's design, briefs, and dispatch — reviewing in your own session would make you review your own work. The `code-reviewer` subagent has a clean context, so its verdict is independent. Pass it the changed files + brief + api-contract + technical-design paths, and tell it to run the verify commands itself. On `CHANGES_REQUESTED`, **resume the engineer** with the blockers, capped by `max_retries_per_item`. (The interactive Tech Lead dispatches the same agent.)
7. **Security review** — per `security_review` in `.claude/loop.config.md`. If `every_item`, or if `sensitive` (default) and this item's design touches **auth, payments, file uploads, or user-supplied input**, dispatch the **`security-reviewer` Agent** on the item's diff after code review is clean. Its verdict gates `done`: `SECURITY_BLOCKED` (any Critical/High finding) → hard-stop (`security_blocked`); `SECURITY_APPROVED` → continue. Skip only when `off` or the item is plainly non-sensitive (e.g. a copy tweak) — log the skip reason to `loop.jsonl`.
8. **Primer delta — before verify and merge, on the item branch.** Apply the `## Primer Delta` from each engineer report (engineer-protocol §11) and any `## Primer Staleness` the code-reviewer returned, plus anything this item introduced that they missed (first WebSocket, first background job, new module layout) — update the relevant `.claude/context/` primer + exemplar pointer **now**, and apply any `## Doc Delta` the engineer could not (engineer-protocol §12). These edits are part of the item's commit, so the primer changes with the code and a reverted item reverts its primer change too. Same contract as Tech-Lead A.8 step 5, and derivation rules in `${CLAUDE_PLUGIN_ROOT}/context/primer-protocol.md`. An engineer's report whose `Primer Delta` is `NONE` without a `checked:` line is malformed — send it back. If several items in a row add new top-level directories or dependencies and every delta is empty, say so at the next checkpoint: that is the drift signal.

   This step is load-bearing in a long unattended run, more than in the interactive lanes: the next item's autonomous Architect designs from these primers with no human reading over its shoulder, so a delta skipped here compounds — item 12 inherits eleven items' worth of drift. Also **close out `TODO(primer)` markers** this item resolved (a greenfield baseline that just created the exemplar the marker was waiting for): that's what lifts the autonomy downgrade from preamble 6(a), and nothing else re-checks it. Log which primers changed in the `loop.jsonl` item record.

**Signal → status projection** (per `${CLAUDE_PLUGIN_ROOT}/spec/loop-engineering.md` §6.1). Every hard-stop that concerns a specific item **sets that item's `status: blocked` + `blockReason`** before pausing — a hard-stopped item must never be left `in_progress` (a resume would silently re-run it into the same wall). Its branch is **renamed** to `blocked/<slug>-a<n>` (`git branch -m`), preserving the work for a resume; never deleted:

| Signal | Effect |
|---|---|
| engineer `APPROVED` + code review clean + security clean (if run) + verify pass | → `done` (APPROVED alone ≠ done) |
| `ESCALATION_NEEDED` | set `blocked` + `blockReason: ESCALATION_NEEDED`; immediate hard-stop (needs a human) |
| `CONTRACT_INCONSISTENCY` (frontend) | set `blocked` + `blockReason: contract_blocked`; hard-stop |
| `CONTRACT_DEVIATION` (backend) | **continue** — re-read contract, regenerate frontend briefs, then gate frontend dispatch |
| `DESIGN_DEVIATION` (database) | **continue** — record + flag for Architect ratification: at the next checkpoint the user either ratifies (flag cleared, logged) or uses the checkpoint **reject** path |
| `NEEDS_CLARIFICATION` | the gap is answerable from the plan, design or primers: **answer it**. Write the answer into the brief's `## Clarifications` section (so custody records it), then **resume the engineer by `agent_id`**. At most two clarification rounds per engineer per attempt. If you cannot answer it from those documents, or the limit is hit: set `blocked` + `blockReason: NEEDS_CLARIFICATION`; hard-stop (brief/plan gap — a human must answer) |
| `CHANGES_REQUESTED` (code-reviewer) | resume the engineer with blockers, capped by `max_retries_per_item`; still failing → retry table below |
| `SECURITY_BLOCKED` (security-reviewer) | set `blocked` + `blockReason: security_blocked`; hard-stop; surface findings at the checkpoint — the user may record a `securityWaiver` (see Waiver semantics) to unblock |
| `PARTIAL` (engineer not converging, or stopped by a turn limit) | resume the engineer to continue, or split the remaining scope into a new item within `max_retries_per_item`; if still incomplete → retry table below |
| `max_tool_calls_per_item` / `_per_run` / `max_items_per_run` breached | set `blocked` + `blockReason: budget_halted` (item-level breach); hard-stop |
| verify fail after `max_retries_per_item` | retry table below |
| `DEMO_FAILED` (auto-demo route errors) | **forced checkpoint** regardless of autonomy dial — a broken demo never silently proceeds |
| infra change required | **always a checkpoint** |

**Retry table (L-2).** Retries resume the same engineer on the same branch; a *discard* throws the branch away and starts clean. Discard runs **only inside the loop's own clone**, never the human's checkout.

| Failure | Action |
|---|---|
| verify fails | resume the implicated engineer with the failure tail; same branch; fresh ephemeral DB |
| review blockers | resume the engineer with the blocker list |
| `PARTIAL` | split the remaining scope into a new item |
| design-critic cap hit | hard-stop |
| retry cap (`max_retries_per_item`) exhausted on verify or review, **or a reward-hack signature** (a deleted or weakened assertion, a skipped test, an edit to a pinned acceptance test) | **discard**: `git reset --hard && git clean -fd`; rename the branch to `dead/<slug>-a<n>`; drop the item's ephemeral DB. Then start a **fresh attempt** `a<n+1>` from the item's base, giving it the failure summary but **none of the old diff**. A **second discard on the same item** → item `blocked` (branch renamed `blocked/<slug>-a<n>`), **circuit breaker ++**, continue to the next eligible item |

**Waiver semantics (L-14).** A `securityWaiver` is granted only by the user, at the hard-stop or the unblock review, with a written justification. It lists the **specific findings** waived (location + CWE) and is bound to the **commit it was granted on**. It covers that item only; a later commit touching a flagged file voids it and the security review re-runs. Waived findings stay in the report and the checkpoint summary, marked `waived`, never hidden. Record it as an `approval` record in `loop.jsonl` (scope `waiver`).

### 5. Verify locally (evidence-bound — §8)
Mark `verifying`. Run, as **real commands**, capturing exit codes:
1. `verify_tests` — must **exit 0 AND collect > 0 tests** (vacuous run = failure), **and the item's named acceptance test must appear in the output as executed and passed** (a suite that passes without running it proves nothing; L-22).
2. `verify_run` — must satisfy the item's **falsifiable acceptance assertion** locally (named test passes, or the specific route/endpoint responds with the stated response) — not merely "booted". `local-dev.md`'s verify-run harness (ports free → boot in its own process group → wait with a deadline → assert → capture exit → kill the whole group) supplies the exit code for a non-terminating dev command; rules in `${CLAUDE_PLUGIN_ROOT}/context/local-dev-rules.md`. In an interactive session, stream the server log with the Monitor tool for readiness; do not poll in `sleep` loops.
3. The independent `code-reviewer` returned no blockers (`CODE_REVIEW_APPROVED`).
4. If a security review was required for this item, `security-reviewer` returned `SECURITY_APPROVED` (no Critical/High findings).

Append the full output tail to `.claude/loop.jsonl`. `item.verifyEvidence = { testsExit, runExit, logRef }`, where `logRef` points at the `loop.jsonl` record holding the raw output (the structured triple is canonical on the item; the raw text lives in the log).

- **Pass** (both exits 0) → in this order: **commit** the branch, including the primer/doc edits of pipeline step 8 (gated on exit 0), with the trailers `Loop-Item: <id>`, `Loop-Run: <run_start ts>` and `Loop-Attempt: <n>` on the commit and on the merge commit (reconciliation can then match a merge to its item exactly; `git log --grep "<itemId>"` still works) → **merge `--no-ff` to `main` of the loop's own clone** (the `delivery` setting will decide the target once it exists; until then, local) after two checks: the working tree is clean, and `main` has not diverged from the branch's base (`git merge-base --is-ancestor main <branch>` succeeds). If either fails, **do not merge**: set `blocked` + `blockReason: ESCALATION_NEEDED` and hard-stop, naming the cause → **one JSON update** setting `{status: "done", mergeCommit, verifyEvidence, downMigrationRef}` (single write, immediately after the merge — the crash window between merge and record is the dangerous one; reconciliation step 4 covers it) → **apply the item's migrations to the durable dev DB** (the migrate command in `local-dev.md`, e.g. `pnpm db:migrate`; failure → set `blocked` + `blockReason: migration_failed` and hard-stop — the durable DB has drifted from main and the demo would lie) → regenerate `BUILD_PLAN.md` → discard the ephemeral DB. For schema-touching items, `downMigrationRef` names the proven down-migration (the checkpoint reject path and §9 rollback execute it) **and its data-loss class** (`${CLAUDE_PLUGIN_ROOT}/context/migrations.md`). A `lossy` down is never executed by the loop; the reject path produces a revert plan for the human instead.
- **Fail** → follow the retry table above: resume the engineer with the failure, up to `max_retries_per_item`; then discard and start attempt `a<n+1>`; a second discard → `blocked`, **increment the circuit breaker**, and **continue to the next eligible item** (one bad item doesn't halt the run).

### 6. Update + log
Regenerate `BUILD_PLAN.md`; re-stamp the lock `heartbeat`; append a `.claude/loop.jsonl` record `{ts, type: "item", itemId, iteration, attempt, status, blockReason, breaker, verifyEvidence, tool_calls, securityVerdict, primersUpdated}` (`primersUpdated`: the primer paths step 8 wrote, `[]` if none — an unbroken run of `[]` across items that clearly introduced patterns is the drift signal) (`breaker: true` iff this record is a `blocked` landing that incremented the circuit breaker — the re-derivation in preamble 5.6 reads it); refresh the progress surface: **current item, items done/total, blocked count, tool-calls used vs `max_tool_calls_per_run` (the measurable cap), token estimate (best-effort, label it as an estimate — never a precise figure), last verify result.**

### 7. Checkpoint?
Decide per the autonomy dial (`per-feature` / `per-milestone` / `unattended`) **and** the hard-stops below. A checkpoint does **not** change item status — the item just merged and stays `done`; append `{ts, type: "checkpoint", itemId}` to `.claude/loop.jsonl` instead (preamble 5.6 uses it to re-present the demo if the session dies while paused). Then:
- **Auto-demo** (§8a): using the capability chosen in the preamble (Run Preamble step 8) — start the app on its local URL, navigate to the item's **demo target** route, screenshot, and present it: *"here's `<item>` — here's what it looks like running."* The fallback order is browser-preview MCP, then `/run`, then the demo route plus an HTTP status/`curl` check as text. If the demo route **errors**, that is `DEMO_FAILED` — a forced checkpoint with the failure shown, never a silent proceed.
- If this item closes its milestone (every item of the milestone is now `done`), also say: "The milestone is done; run `/retro` to see what repeated." `/retro` is a separate human-only mode; you do not run it and you do not wait for it.
- Surface the branch/commits on local main for review, any `DESIGN_DEVIATION` flags awaiting ratification, any waived findings, and the progress summary.
- **Notify** via the path chosen in the preamble, then pause. The checkpoint has three exits, and **each answer is recorded** (L-10): append `{ts, type: "approval", scope: "checkpoint", itemId, choice: "approve"|"reject"|"stop", by: "user", note}` to `.claude/loop.jsonl` before acting (`loopctl approve` replaces this record when it exists).
  - **approve** → the loop proceeds (any surfaced `DESIGN_DEVIATION` flags are ratified + cleared unless the user says otherwise).
  - **reject** → the item is already merged, so un-ship it: `git revert -m 1 <mergeCommit>` on main; run the item's `downMigrationRef` down-migration against the durable dev DB (**unless its class is `lossy`: then write a revert plan for the human and stop**); reset the item to `pending` with `branch`/`mergeCommit`/`verifyEvidence`/`downMigrationRef` = null and the user's feedback recorded in `reworkNotes` (the next pipeline pass reads it); then continue the loop.
  - **stop** → item stays `done`, checkpoint record already written, release the lock, exit cleanly (resume re-presents this demo via 5.6).

### 8. Loop to step 1.

---
