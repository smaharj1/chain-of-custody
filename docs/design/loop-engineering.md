# Design Note: Loop Engineering Layer

**Status**: LOCKED v3 (converged after two adversarial review rounds; final check returned LOCKABLE — 6/6 original + 4/4 v2-introduced blockers closed, 0 new blockers)
**Scope**: Adds a Planner + Orchestrator layer on top of the existing PM/Architect/Tech-Lead/engineer system, enabling "describe the goal → plan with you → run until done," with a **local-first** development experience so a non-technical builder can *see* what's being built without deploying anything.

> **v3 changelog** — fixes the 4 blocker-severity defects the v2-verification round found, plus folds in local-first development:
> - **Circuit breaker was logically dead** (every increment coincided with a pause whose resume cleared it). Fixed: counter resets **only** on an item reaching `done`, never on resume; verify-fail-after-retries → `blocked` + *continue*, so the breaker meaningfully catches systemic failure (§7).
> - **`DESIGN_DEVIATION` wrongly made a hard-stop.** Real protocol is Database-only notify-and-continue. Fixed to continue + flag for ratification (§6.1, §7, §10).
> - **`CONTRACT_DEVIATION` was missing** from the projection table though frontend dispatch ordering depends on it. Added, with the Backend→frontend gate (§6.1).
> - **Engineer git read-only was claimed as tooling but is only convention** (agent files grant `Bash`). Reworded + recommended an enforcing hook (§3, §9).
> - Worth-fixing folded in: ephemeral-DB/down-migration roles disentangled (§8/§9); JSON-`done`↔git reconciliation at run start (§6); primer-placeholder sentinel for autonomy downgrade (§6/§7); `DEADLOCK` clarified as run-level (§4); frontend roster sourced from agent files (§5/§6.1).
> - **Local-first (new):** `verify_run` is local; **auto-demo** at checkpoints (the orchestrator shows you the running app); **SQLite local, Postgres-ready** (makes ephemeral per-item DBs trivial); **infra deferred + gated** behind a working local app; new `local-dev.md` primer (§8a).

---

## 1. Goal

Today the kit builds **one feature at a time**, with a human manually switching modes. Loop engineering means:

1. The user gives a detailed goal.
2. The user works with a **Planner** to produce a durable **build plan** (dependency-ordered backlog + progress tracking).
3. The user starts the **Orchestrator**, which works through the plan — design, build, review, verify per item — **until the plan is done, blocked, or a guardrail stops it**, with configurable human checkpointing and a **local, visible** app at every step.

The build plan is the persistent spine every agent reads, so the loop survives context limits.

---

## 2. Design Principles

- **Heavy human involvement in planning; configurable involvement in execution.** Plan approval is the highest-leverage checkpoint.
- **Autonomy is a dial, not a mode.** `per-feature` / `per-milestone` / `unattended`, all honoring hard-stops (§7).
- **State lives on disk, not in the conversation.** The conductor re-reads canonical state each iteration.
- **Local-first.** The app runs and is *seen* locally; cloud infrastructure is a later, gated milestone (§8a).
- **Done means *verified by evidence*.** An item is done only when recorded command exit codes prove tests passed and the new code ran (§8).
- **Every item is reversible.** Failed items are discarded (branch deleted + throwaway DB discarded). Schema changes only ever touch a throwaway DB during the loop, so discard reverses them for free; every migration *also* ships a down-migration proven in that DB, recorded for application against the durable DB at push/revert time (§8/§9).
- **Reuse, don't reinvent.** The conductor runs the *actual* design-critic (Agent) and code-review (Skill), and the *actual* Tech-Lead engineer-set detection + status-signal semantics — not paraphrases.

---

## 3. Architecture & Execution Model

```
PLAN        You + Planner ──► build-plan.json (+ BUILD_PLAN.md view) ──(you approve)──┐
                                                                                       ▼
ORCHESTRATE  /orchestrate — runs IN THE MAIN SESSION (has Agent + Skill + git):
              loop: select next eligible item
                    → run per-item pipeline (§6.1, main session)
                    → verify locally with captured exit codes (§8)
                    → on pass: commit + merge to local main + update plan + log
                    → checkpoint? → auto-demo the running app + pause for You
              until done / DEADLOCK / guardrail
```

**Execution model.** The conductor **is** the pipeline runner, in the main session, because it needs the **Agent tool** (to dispatch design-critic, engineers, the independent code-reviewer, and the security-reviewer) plus **scoped git writes** — none available to isolated Workflow agents or engineer subagents. A Workflow inner fan-out for parallel engineers is **deferred to post-V1** (V1 is strictly sequential, §15); the table row below is marked accordingly.

**Review independence (loop vs interactive).** The interactive Tech-Lead runs code review as the `code-review` **Skill** in its own session — fine, because the Tech-Lead is *not* the author (the engineer is) and a human reviews the verdict. In the **loop**, the conductor *is* the author (it ran the autonomous PM/Architect, wrote the briefs, dispatched the engineers), so a same-session Skill review would review its own work with no human backstop. The loop therefore dispatches an independent **`code-reviewer` Agent** (clean context) instead (§6.1, §6.2).

**Tool availability per layer**:

| Layer | Runs in | Agent tool | Skill tool | git writes |
|---|---|---|---|---|
| Conductor (`/orchestrate`) | main session | yes | yes | yes — scoped, §9 |
| design-critic | Agent subagent | no | no | no |
| code-reviewer (loop) | Agent subagent | no | no | no — read-only critic, §6.2 |
| security-reviewer (loop) | Agent subagent | no | no | no — read-only auditor, §6.2 |
| Engineers | Agent subagents | no | no | restricted by **convention**, not tooling — see §9 |
| code-review (interactive only) | Skill (Tech-Lead runs) | n/a | n/a | no |
| Parallel engineer fan-out | Workflow | no | no | no — **deferred, not used in V1** |

---

## 4. The Build Plan

Canonical machine state is **`build-plan.json`**; **`BUILD_PLAN.md` is a generated view**, regenerated from the JSON on every write and never hand-parsed for control flow. This kills the dual-source-of-truth and markdown-fragility risks.

**Location**: project root.

**Per-item fields**:
```jsonc
{
  "id": "F2",                       // immutable once assigned, never reused
  "milestone": "M1",
  "title": "Workspace CRUD",
  "slug": "workspace-crud",
  "dependsOn": ["F1"],
  "intent": "…",
  "acceptance": ["…"],             // MUST include a falsifiable runtime assertion (§8) — also the demo target (§8a)
  "requiresEnv": ["DATABASE_URL"],
  "status": "pending",
  "blockReason": null,
  "branch": null,
  "mergeCommit": null,              // set on merge-to-main; used for JSON↔git reconciliation (§6)
  "verifyEvidence": null
}
```

**Item statuses**: `pending → in_progress → verifying → done`, with resting branches `blocked`, `needs_review`, and `blockReason`-qualified halts (`budget_halted`, `contract_blocked`, `needs_provisioning`). **`DEADLOCK` is a run-level terminal outcome (the loop stops and reports) — not an item status.**

**Build order**: solely the dependency DAG (topological order). IDs are *not* a tiebreaker (they're immutable, so inserting an item never forces renumbering). An item is *eligible* only when every `dependsOn` item is `done`. V1 picks one eligible item at a time.

**Validation (mandatory at Planner write-time AND every plan load)**: every `dependsOn` ID exists; the graph is acyclic; every detail item ↔ exactly one row. Failure aborts before any work.

---

## 5. The Planner (`/planner`)

"PM for the whole project." Collaborative and human-heavy.

**Responsibilities**
- Interview the user; decompose the goal into milestones → features; build + validate the dependency DAG.
- Write per-item intent, **acceptance with a falsifiable runtime assertion** (which doubles as the demo target, §8a), and `requiresEnv`.
- **Pre-bake the big architectural decisions** (stack, auth model, data conventions) as global constraints the autonomous Architect inherits.
- Make the **mandatory first item** "a **locally-runnable app you can see in a browser** + test harness + one passing smoke test." `/orchestrate` refuses to start without a runnable/test baseline (§8).
- Sequence **infrastructure/deploy work as a later milestone**, gated behind a working local app (§7, §8a).
- Produce `build-plan.json` (+ rendered `BUILD_PLAN.md`); get explicit user approval.

> The **engineer set / frontend roster** is sourced from `.claude/agents/*.md` (which engineers exist) + each item's `## Frontend Impact` in the design — **not** from `CLAUDE.md`'s prose list, which may name apps that have no engineer.

**Mid-run plan changes**: only the Planner edits plan structure. If execution finds the plan itself wrong, the conductor raises `PLAN_REVISION_NEEDED` (§7) and routes the user back to `/planner`.

---

## 6. The Orchestrator (`/orchestrate`) — sequential V1

**Run preamble** (once per run):
1. Acquire run lock (`.claude/orchestrate.lock`; refuse if a fresh lock exists; document stale-lock override).
2. **Assert sequential precondition** (V1): refuse if any concurrency flag is set — B1/B2/migration-numbering correctness depends on strictly-sequential execution.
3. Load + validate the plan (§4). Load `.claude/loop.config.md` and primers.
4. **Primer-readiness check**: grep primers + `CLAUDE.md` for template sentinels (`<!--` placeholder comments, `TODO(primer)`). If any context the run needs is still a template, **downgrade autonomy to `per-feature`** and tell the user which files to fill in.
5. **Resume reconciliation**: reset any `in_progress`/`verifying` item to `pending` (V1 discards partial work, restarts from stage 1); `needs_review`/`blocked` surface, never auto-selected.
6. **JSON↔git reconciliation**: for every `done` item, confirm its `mergeCommit` is still present on main. If a human reverted a passed item, mark it (and its transitive dependents) for rebuild — otherwise a later branch would cut from a base missing that code (silent B1 regression).

**Loop** (re-reads canonical state each iteration):
1. **Select** the next eligible `pending` item by topological order. If none eligible: all `done` → **finish**; items remain but none eligible → **DEADLOCK** report (directly-blocked vs transitively-blocked sets) and stop.
2. **Precheck**: `requiresEnv` present? If not → `needs_provisioning` hard-stop (before burning retries/budget). Working tree clean? If not → cleanup contract (§9).
3. **Start**: mark `in_progress`; cut `feat/<slug>` **off current main** (contains all completed deps); provision an **ephemeral DB** (a throwaway SQLite file by default, §8a).
4. **Run the per-item pipeline** (§6.1) in the main session. Between stages, check the **measurable** caps — `max_tool_calls_per_item` / `max_tool_calls_per_run` / `max_items_per_run` (§7) — breach → hard-stop (`budget_halted`); the token budgets are best-effort soft-stops only. (Checks are between-stage, so a single runaway stage can overshoot the per-item cap by at most one stage — bounded.)
5. **Verify locally** (§8) with captured exit codes. **Pass** → record `verifyEvidence`, mark `done`, **commit** (gated on exit 0), **merge `--no-ff` to local main**, store `mergeCommit`, discard the ephemeral DB. **Fail** after `max_retries_per_item` → discard branch + ephemeral DB, mark `blocked`, increment the circuit breaker, **continue to the next eligible item** (one bad item does not halt the run).
6. **Update** plan + regenerate `BUILD_PLAN.md` + append `loop.jsonl` + refresh the progress surface (§13).
7. **Checkpoint?** Per the autonomy dial + hard-stops (§7). At a checkpoint, **auto-demo** (§8a): start the local app, screenshot the item's demo target, present it, then pause (push notification).
8. Loop to 1.

### 6.1 Per-item pipeline (main session, autonomous)

`autonomous PM (requirements) → autonomous Architect (design + api-contract) → design-critic gate (Agent, 2-round cap → hard-stop) → Tech-Lead engineer-set detection (the ACTUAL A.2/A.3 logic: infra-first, additive-schema DB+Backend parallel, don't-dispatch-empty-scopes) → dispatch engineers via Agent → independent code-reviewer per engineer (Agent, clean context — §6.2) → security-reviewer on sensitive items (Agent — §6.2) → §8 verify`

**Frontend dispatch gate** (faithful to Tech-Lead A.3): frontends are dispatched **only after Backend reaches `APPROVED`, or its `CONTRACT_DEVIATION` is resolved** (conductor re-reads `api-contract.md` and regenerates the affected frontend briefs first).

**Budget checks between stages** are two-tier (§7): the **measurable caps** (`max_tool_calls_per_item`, `max_tool_calls_per_run`, `max_items_per_run`, `max_retries_per_item`) are the real hard-stops; the **token budgets are best-effort estimates** the executor cannot measure and must never be treated as a precise gate.

**Signal → item-status projection** (the enums are distinct and role-scoped; this is the rule):

| Pipeline signal | Source role | Item effect |
|---|---|---|
| `APPROVED` + code-reviewer clean + security clean (if run) + verify pass | any engineer | → `done` (engineer `APPROVED` alone is **not** done) |
| `ESCALATION_NEEDED` | any | immediate hard-stop, `blockReason` = signal |
| `CHANGES_REQUESTED` | code-reviewer | re-dispatch the engineer with blockers, capped by `max_retries_per_item`; unresolved after cap → `blocked` + breaker++ |
| `SECURITY_BLOCKED` | security-reviewer | hard-stop (`security_blocked`); surface findings at checkpoint — item does **not** reach `done` |
| `CONTRACT_INCONSISTENCY` | frontend only | hard-stop (`contract_blocked`) |
| `CONTRACT_DEVIATION` | backend only | **continue** — conductor re-reads `api-contract.md`, regenerates frontend briefs, then gates frontend dispatch on it |
| `DESIGN_DEVIATION` | database only | **continue** — record rationale, flag for Architect ratification (surfaced at the next checkpoint / run summary; notify-and-continue, per Tech-Lead A.6) |
| `NEEDS_CLARIFICATION` | any | hard-stop (brief/plan gap — the unattended projection of A.6's interactive "answer + re-dispatch"; no in-loop human to answer, so pause) |
| measurable budget cap breached | — | hard-stop (`budget_halted`) |
| verify fail after `max_retries_per_item` | — | item `blocked` + continue; circuit breaker ++ |
| infra change required | — | **always a checkpoint** (§10) |

### 6.2 Independent code review + security gate (loop only)

Two quality gates run between dispatch and verify. Both are **Agent subagents**, not Skills, and both are **read-only** (no code edits):

- **Independent code review.** The conductor authored this item end-to-end, so it must not also be its reviewer. After an engineer returns `APPROVED`, the conductor dispatches the **`code-reviewer` Agent** with the changed files + brief + api-contract + technical-design. Its clean context is what makes the verdict independent. `CHANGES_REQUESTED` → re-dispatch the engineer (capped by `max_retries_per_item`); `CODE_REVIEW_APPROVED` → proceed. (The interactive Tech-Lead keeps the `code-review` Skill — a human backstop is present there.)
- **Security gate.** Per `security_review` in `loop.config.md` (`sensitive` default | `every_item` | `off`): on any item whose design touches **auth, payments, file uploads, or user-supplied input** (or every item, if configured), the conductor dispatches the **`security-reviewer` Agent** on the diff. Verdict rule: any **Critical/High** finding → `SECURITY_BLOCKED` → hard-stop (`security_blocked`); else `SECURITY_APPROVED` → proceed. This closes the gap where an unattended run could ship auth/payments/upload code with no security pass — the interactive flow relied on a human remembering to run it, and the loop removes that human. Skips are logged to `loop.jsonl` with a reason.

---

## 7. Autonomy & Hard-Stops

**Config** (`.claude/loop.config.md`):
```
autonomy: per-milestone            # per-feature | per-milestone | unattended
max_retries_per_item: 2
max_consecutive_failures: 2        # circuit breaker
# Measurable hard caps (the REAL runaway guards — counted from the conductor's own tool calls):
max_items_per_run: 25              # GLOBAL hard ceiling on items attempted
max_tool_calls_per_item: 60        # per-item hard ceiling → budget_halted (bounds a single item)
max_tool_calls_per_run: 600        # GLOBAL hard ceiling on conductor tool calls
# Token budgets are BEST-EFFORT estimates (the executor cannot read its own token count) — soft-stops only:
token_budget_per_item: 150000      # estimate; pause when clearly over (NOT a precise gate)
total_token_budget_per_run: 2000000  # estimate; pause when clearly over
verify_tests: <command>
verify_run: <local command | preview assertion>   # LOCAL (§8a)
security_review: sensitive         # sensitive (default) | every_item | off — gates `done` (§6.2)
db_local: sqlite                   # sqlite (default) | postgres
db_ephemeral: <how to spin a throwaway DB>          # for sqlite: copy/delete a temp file
git_strategy: branch-off-main-merge-on-pass
infra_gated_behind_local: true     # deploy/IaC items only after the app runs locally
```

**Circuit breaker** — a "failure" increments the counter when **an item lands in `blocked` after exhausting `max_retries_per_item`** (it counts *failed items*, not within-item retries). The counter **resets to 0 only when an item reaches `done`** — *not* on human resume, so failures accumulate across resumes and the breaker can actually trip. `ESCALATION_NEEDED` is a separate immediate hard-stop and does **not** increment the breaker (it's a pause for input, not a thrash). Tripping (`max_consecutive_failures` consecutive `blocked` items with no intervening `done`) → hard-stop. The breaker is the systemic guard for DAGs with ≥2 independent fail-able branches; in a strictly **linear** plan a single block makes all dependents ineligible and the loop terminates via `DEADLOCK` (§6 step 1) before a second failure can accumulate — both are hard-stops, so coverage is complete either way.

**Discretionary checkpoints** (the dial): `per-feature` after each item; `per-milestone` (default) at milestone boundaries; `unattended` none.

**Hard-stops (always honored, even in `unattended`)** — pause + push notification when:
- any pipeline stage returns `ESCALATION_NEEDED`;
- the **circuit breaker** trips (systemic verify failure);
- a **measurable** budget cap is breached — `max_tool_calls_per_item` / `max_tool_calls_per_run` / `max_items_per_run` (→ `budget_halted`). *(The token budgets are best-effort estimates, not enforceable ceilings — see below.)*
- the **security-reviewer** returns `SECURITY_BLOCKED` (any Critical/High finding) on a reviewed item (`security_blocked`, §6.2);
- `CONTRACT_INCONSISTENCY` (`contract_blocked`);
- design-critic returns `DESIGN_NEEDS_REVISION` after the 2-round cap;
- a required env/secret is missing (`needs_provisioning`);
- an **infra change** is required (always a checkpoint), or an infra/deploy milestone is reached **before the app runs locally** (`infra_gated_behind_local`);
- `PLAN_REVISION_NEEDED`;
- `DEADLOCK`.

> **Budgets are two-tier.** The *measurable* caps above (tool-call and item counts, tallied from the conductor's own calls) are the real hard-stops. `token_budget_per_item` / `total_token_budget_per_run` are **best-effort estimates** — the executor cannot read its own token count — so they only trigger a pause when work is *clearly* over, and must never be presented (in config, progress surface, or logs) as a precise ceiling. `max_tool_calls_per_item` is what actually bounds a single runaway item.
>
> `DESIGN_DEVIATION` and `CONTRACT_DEVIATION` are **not** hard-stops — they're notify/continue signals (§6.1).

**Autonomy precondition**: `per-milestone`/`unattended` require non-placeholder primers (detected by the §6 sentinel grep). Otherwise the run downgrades to `per-feature`.

---

## 8. Verification Gate ("done means verified — with evidence")

An item reaches `done` only when, **as captured tool-call evidence**:
1. `verify_tests` exits 0 **and** collected a non-zero test count (a vacuous run is a **failure**), AND
2. `verify_run` satisfies the item's **falsifiable runtime assertion** from `acceptance` (a named test that must exist and pass, or a specific endpoint/route/preview assertion — *not* merely "the app booted"), AND
3. code-review returned no blockers.

The conductor runs these as real commands, appends raw exit code + output tail to `loop.jsonl` as `verifyEvidence`, and **the commit is gated on exit 0** — commit and proof are atomic.

> **Accepted residual**: the "proves the *new* code ran" strength rests on the Planner authoring a genuinely falsifiable assertion; the non-vacuous check guards against *zero* tests but not a weak test. Mitigation: design-critic and code-review sanity-check that the assertion exercises the new behavior. This is a known limit, not a mechanism gap.

**Database**: verification runs against the item's **ephemeral DB**, so discarding the branch discards the schema for free (no down-migration needed on the fail path). Separately, every migration must ship a **down-migration proven (up→down→up) in the ephemeral DB** and recorded — its value is reversing a *merged* item's schema against the durable DB later (§9), not the discard path.

**Greenfield**: until a runnable/test baseline exists, a vacuous verify is a failure; the Planner's mandatory first item creates the baseline; `/orchestrate` refuses to start without one.

## 8a. Local Development & Observability (local-first)

The "app runs" half of the verify gate **is** local development — so local-dev is both the verification substrate and the non-technical builder's window into the product.

- **`verify_run` is local by default** — a local dev server + local DB, exercised via the run/preview tooling. No cloud needed to verify or to see the app.
- **Auto-demo at checkpoints** — when the loop pauses, the conductor starts the local app, navigates to the item's **demo target** (= its falsifiable acceptance assertion's route/screen), screenshots it, and presents it: *"here's F3 — approve to continue."* The human reviews the *running product*, not a diff.
- **SQLite local, Postgres-ready** — default local store is SQLite (zero-setup, a file). Schema is authored to **minimize Postgres-specific constructs so the later switch is low-friction** (the ORM regenerates migrations per-dialect rather than reusing them verbatim — it is low-friction, not free). Bonus: an **ephemeral per-item DB is just a temp SQLite file** to copy and delete — making the §8 isolation/discard trivially cheap.
- **Local-friendly service modes** — dev/mock auth, payments in test mode — so none of auth/DB/payments forces a cloud account just to see it work.
- **Infrastructure deferred + gated** — deploy/IaC is a later milestone the loop won't start until the app runs locally (`infra_gated_behind_local`); infra items also remain always-checkpoint (§10).
- **New primer `.claude/context/local-dev.md`** — the dev command, ports, seed data, how to reset the local DB; read by `verify_run` and the auto-demo step.

---

## 9. Git Strategy

- Each item's branch is cut **off current main** (which contains all merged, completed dependencies) — dependency code/schema/types are present, and the ORM generates correct next migration numbers (no cross-item collision; V1 is sequential).
- On verify-pass: commit the feature branch, then **merge `--no-ff` to LOCAL main**; store `mergeCommit`. The conductor **never pushes**.
- **You review + push at checkpoints** — local main accumulates verified commits; the remote stays clean until you approve.
- **Scoped conductor git exception** (active only during a run): may `checkout -b`, `commit`, `merge --no-ff` to local main, and `reset --hard`/`clean` **on its own feature branch only**. Never `push`, `rebase`, force-anything, or touch the remote. **This should be enforced by a hook/allowlist, not just prose.**
- **Engineer git restriction is convention, not tooling.** Engineer agent files grant `Bash`, so `git` writes are technically possible; the on-main Edit/Write hook does not catch them, and the loop removes the human reviewer the convention relied on. **In an unattended loop, add a PreToolUse hook that blocks `git`-write Bash for engineer subagents**, or explicitly accept the residual risk in `loop.config.md`.
- **Cleanup contract**: require a clean working tree before creating/switching any item branch; a crashed item's dirty branch is reset (own-branch `reset --hard`) and the item restarts from stage 1.
- **Rollback**: a *failed* item (never merged) = delete branch + discard ephemeral DB. A *passed* item (already on main) = `git revert` the code **and run its proven down-migration against the durable DB** (the migration file revert alone does not undo applied schema).

---

## 10. Autonomous PM / Architect / Tech-Lead (the real refactor)

All three upper modes are interactive and each needs an **autonomous variant**:

- It **makes** the call from the plan's pre-baked decisions + primers + item acceptance — instead of asking.
- It **records rationale** inline (a "Decisions (autonomous)" block in `technical-design.md`).
- It **flags only low-confidence / load-bearing decisions** as `ESCALATION_NEEDED` → hard-stop. Routine work proceeds.
- **Interactive variants stay** for manual use; one mode file per role with both paths (autonomous path activated by the conductor's dispatch).
- The autonomous **Architect** runs the identical Agent-based design-critic loop (2 rounds → hard-stop).
- The autonomous **Database engineer** treats `DESIGN_DEVIATION` per protocol: update the design's schema spec, record rationale, **continue**, flag for Architect ratification — *not* a stop.
- The autonomous **Tech-Lead** uses the real A.2/A.3 detection; maps interactive branches to loop control (A.5 "go" skipped in unattended; `NEEDS_CLARIFICATION`/`CONTRACT_INCONSISTENCY` → hard-stop; `CONTRACT_DEVIATION` → re-read contract + regenerate frontend briefs + continue; review re-dispatch capped by `max_retries_per_item`).
- The autonomous **review path differs from interactive** (§6.2): because the conductor authored the work, code review is dispatched to the independent **`code-reviewer` Agent** (clean context), *not* the in-session `code-review` Skill; and the **`security-reviewer` Agent** is auto-dispatched on sensitive items per `security_review` (the interactive flow left this to a human, which an unattended loop has none of).
- **Infra is never fully autonomous**: an infra-touching item forces a checkpoint regardless of the dial, and infra/deploy milestones are gated behind a working local app (§8a).

---

## 11. Integration with the Existing System

**Reused as-is**: design-critic (Agent), engineer subagents, the role-scoped status enum (with the §6.1 projection rule honoring the real notify-vs-stop semantics), Tech-Lead A.2/A.3 detection.

**Reused, re-scoped**: `code-review` (Skill) stays the **interactive** Tech-Lead's reviewer (human backstop present); the loop uses the independent `code-reviewer` Agent instead (§6.2). `security-reviewer` (Agent), previously human-invoked only, is now **auto-dispatched in the loop** on sensitive items (§6.2) — its output gains a `SECURITY_APPROVED | SECURITY_BLOCKED` verdict so the conductor can gate on it.

**New**: `.claude/commands/planner.md`; `.claude/commands/orchestrate.md`; `.claude/agents/code-reviewer.md` (independent loop reviewer, §6.2); `docs/features/_templates/build-plan.md`; `build-plan.json` + `BUILD_PLAN.md`; `.claude/loop.config.md`; `.claude/orchestrate.lock`; `.claude/loop.jsonl`; `.claude/context/local-dev.md`; `.claude/hooks/guard-git.sh` (PreToolUse) enforcing the git boundaries (§9).

**Modified**: PM + Architect + Tech-Lead gain autonomous paths (§10); Tech-Lead's "Skill, not subagent" review rationale is scoped to interactive use (§6.2); `security-reviewer.md` gains a parseable verdict; `CLAUDE.md` documents the new top flow + config; telemetry split into `loop.jsonl`.

---

## 12. Failure Modes & Guardrails

| Failure mode | Mitigation |
|---|---|
| Non-termination | Finite plan; per-item retry cap; global token + item ceilings; **a circuit breaker that can actually trip** (§7). |
| Thrashing (systemic verify failure) | verify-fail → `blocked` + continue; breaker trips on consecutive blocks with no `done`. |
| Compounding bad decisions | Pre-baked planning decisions; design-critic gate; per-item verify; reversibility. |
| Stale-base / dependency invisible | Branch off main + merge-on-pass; **JSON↔git reconciliation at run start** catches human reverts (§6). |
| Cross-item migration-number collision | Off-main base + sequential V1 → ORM numbers correctly. |
| Broken work marked done | Evidence-bound gate; commit gated on exit 0; vacuous verify = fail (§8). |
| Conductor reviews its own work | Loop dispatches an **independent `code-reviewer` Agent** (clean context), not the in-session Skill — the author is not the reviewer (§6.2). |
| Insecure feature shipped unattended | **`security-reviewer` auto-dispatched** on auth/payments/upload/user-input items; Critical/High → `security_blocked` hard-stop (§6.2). |
| DB poisoned by discarded item | Ephemeral per-item DB (throwaway SQLite file) (§8/§8a). |
| Crash mid-item (dirty branch) | Resume resets `in_progress`→`pending`; own-branch `reset --hard` (§9). |
| Engineer runs a forbidden git write | Convention + recommended enforcing hook (§9). |
| Context loss | Canonical state on disk; conductor re-reads each iteration. |
| Budget runaway | **Measurable** caps are the hard-stops: per-item + per-run tool-call caps and item count, tallied from the conductor's own calls (§7). Token budgets are best-effort estimates only — never relied on as a precise ceiling. |
| Cyclic / dangling deps | Mandatory graph validation at write + load (§4). |
| Deadlock (no eligible item) | Run-level `DEADLOCK` hard-stop with directly- vs transitively-blocked sets. |
| Plan itself wrong | `PLAN_REVISION_NEEDED` → `/planner`. |
| Missing secret/env | `requiresEnv` precheck → `needs_provisioning`. |
| Double `/orchestrate` | Run lock (§6). |
| Cloud setup blocks a beginner | Local-first: infra deferred + gated behind a working local app (§8a). |
| User blind during a long run | Progress surface + auto-demo + checkpoint push notifications (§13/§8a). |

---

## 13. State, Progress Tracking & Resumability

- **Canonical state**: `build-plan.json`; `BUILD_PLAN.md` is generated.
- **Progress surface**: a live status line refreshed each iteration — current item, items done/total, blocked count, **tool-calls used vs `max_tool_calls_per_run`** (the measurable cap), token estimate (labeled best-effort, never a precise figure), last verify result — plus **auto-demo screenshots** and a **push notification** on every checkpoint and hard-stop.
- **Loop log**: append-only `.claude/loop.jsonl`, one record per item attempt: `{ts, itemId, iteration, status, verifyEvidence, blockReason, tool_calls, securityVerdict}` (`securityVerdict` is the security-reviewer's verdict, or a skip reason when no review was required). Separate file/shape from `metrics.jsonl`.
- **Resumability**: state on disk + the run preamble's reconciliation (resume + JSON↔git) means a crashed/new session resumes deterministically.

---

## 14. V1 Scope vs. Later

**V1**: Planner + `build-plan.json`/`.md` + template; sequential conductor (main session); autonomy dial (default `per-milestone`, sentinel-gated); evidence-bound local verify gate; SQLite-local ephemeral DB + proven down-migrations; branch-off-main/merge-on-pass + git-boundary hook; autonomous PM/Architect/Tech-Lead; auto-demo checkpoints; infra deferred + gated; run lock; progress surface; `loop.jsonl`; `local-dev.md` primer.

**Deferred**: parallel execution (reintroduces migration collisions + merge races — needs per-item worktrees); a dedicated project-bootstrap/scaffold mode (the greenfield baseline item is the stopgap); cross-feature contract registry; richer cost analytics; cloud deploy automation.

---

## 15. Resolved Decisions & Remaining Open Questions

**Resolved**: execution model = main-session conductor; integration = off-main/merge-on-pass; DB = ephemeral (SQLite-local) + proven down-migrations; config in `.claude/loop.config.md`; plan format = JSON canonical + MD view; greenfield = mandatory locally-runnable first item + refuse-to-start; **seeing the product = auto-demo at checkpoints; local store = SQLite-local/Postgres-ready; infra = deferred + gated behind a working local app**.

**Remaining open** (non-blocking; decide during build):
1. **Contract drift across features** — primers + per-feature contracts only, or a durable project-level contract registry? (Defer until a multi-feature run shows it's needed.)
2. **Parallelism** — deferred to post-V1; will need per-item worktrees + an integration-branch revisit (reintroduces collisions/races the sequential V1 avoids).
3. **Assertion strength** — whether to add a machine check that each item's runtime assertion actually exercises new code, beyond the design-critic/code-review sanity check (§8 accepted residual).

---

## Revision 1 — Build Hardening (operability)

Applied after an implementation-verification round (design↔impl conformance was clean; these close operability gaps a literal executor would hit). These refine §6–§9/§13 without changing behavior:

- **Budgets are best-effort.** The executor cannot read its own exact token count, so `token_budget_per_item` / `total_token_budget_per_run` are best-effort estimates; the *measurable* guards are `max_items_per_run`, `max_retries_per_item`, and the tool-call tally (logged to `loop.jsonl`). The circuit breaker and item ceilings remain hard.
- **Two distinct startup gates.** Placeholder *autonomy primers* → downgrade to `per-feature`; placeholder *execution-critical config* (`verify_tests` / `verify_run` / `db_ephemeral` / `local-dev.md` commands) → **hard-stop** (the loop can't pass a verify gate without them).
- **Named capabilities + fallbacks.** Auto-demo uses a screenshot tool (`Claude_Preview` MCP: `preview_start` / `preview_screenshot`; text fallback = route + HTTP check). Checkpoint notification uses a `PushNotification` tool (fallback = transcript banner). Both are preamble preconditions.
- **Run-lock liveness.** Stale = pid not alive (`kill -0`) or timestamp older than 30 min; else live → refuse.
- **State ownership made explicit.** Orchestrator writes `item.branch` at start; `verifyEvidence = {testsExit, runExit, logRef}` on the item, raw output in `loop.jsonl`. Checkpoint pauses set the item to `needs_review`. `PARTIAL` engineer returns map to split/re-dispatch then `blocked`.
- **`guard-git.sh` parses the git subcommand with `shlex`** (quote-respecting) so a commit message containing "git push" isn't blocked; shared-branch matches are token-boundary-anchored. Tested.

---

## Revision 2 — Loop Trust Hardening (autonomy safety)

Applied after a critical review of the autonomous loop's *trust* properties (the prior rounds hardened correctness/termination; these close three holes that only bite once a human is out of the loop). They refine §3/§6/§7/§10–§12 without changing the plan format or the sequential model.

- **Budget caps made honest and enforceable (#2).** The token budgets were presented as "always-honored hard-stops," but the executor cannot read its own token count — so the only real bound on a runaway was item count, and a *single* item had no hard bound. Fix: tokens are now explicitly **best-effort soft-stops**; the hard guards are **measurable tool-call caps** — new `max_tool_calls_per_item` (bounds one item) and `max_tool_calls_per_run`, tallied from the conductor's own tool calls alongside `max_items_per_run`. The config, hard-stop list, progress surface, and `BUILD_PLAN.md` no longer print tokens as a precise ceiling (§7, §6.1, loop.config).
- **Security review runs in the loop (#3).** Security review was human-invoked only ("not auto-dispatched per feature"), so an *unattended* loop could ship auth/payments/upload code with no security pass — the human who was supposed to remember is gone. Fix: new `security_review` config (`sensitive` default | `every_item` | `off`); the conductor auto-dispatches the **`security-reviewer` Agent** on sensitive items; `security-reviewer.md` gains a `SECURITY_APPROVED | SECURITY_BLOCKED` verdict (Critical/High ⇒ blocked); `SECURITY_BLOCKED` is a new `security_blocked` hard-stop that prevents `done` (§6.2, §7).
- **Code review is independent in the loop (#4).** The interactive Tech-Lead runs the `code-review` Skill in-session — fine, because it's not the author and a human reviews the verdict. In the loop the **conductor is the author** (autonomous PM/Architect + briefs + dispatch), so a same-session Skill review reviews its own work. Fix: new read-only **`code-reviewer` Agent** (clean context, mirrors design-critic) dispatched per engineer diff in the loop; the Skill is now scoped to interactive use. Tool-availability table + Tech-Lead "Skill, not subagent" rationale updated accordingly (§3, §6.2, §10).
