# Build Plan Template

The build plan is the durable spine of a loop-engineering run. It has **two files at the project root**:

- **`build-plan.json`** — the **canonical machine state**. The Orchestrator reads and writes this. Control flow is driven from here only.
- **`BUILD_PLAN.md`** — a **generated human view**, regenerated from the JSON on every write. Never hand-parsed for control flow; safe for you to read at a glance.

The Planner writes both. The Orchestrator edits the JSON and regenerates the markdown.

---

## `build-plan.json` shape

```jsonc
{
  "goal": "One-paragraph statement of the overall goal.",
  "createdAt": "<YYYY-MM-DD>",
  "items": [
    {
      "id": "F1",                       // immutable once assigned, never reused
      "milestone": "M1 Foundation",
      "title": "Runnable skeleton + test harness",
      "slug": "skeleton",
      "dependsOn": [],                  // list of item IDs; build order = topological sort of these
      "intent": "2-3 sentences: what and why.",
      "acceptance": [                   // MUST include one falsifiable runtime assertion (the demo target)
        "App boots locally via `verify_run`",
        "GET / returns 200 and renders the home route",   // <- falsifiable, and what auto-demo screenshots
        "Smoke test `home.test.ts` exists and passes"
      ],
      "requiresEnv": [],                // env/secrets that must exist before the item starts
      "status": "pending",              // see lifecycle below
      "blockReason": null,              // set when blocked/halted; survives for resume + report
      "branch": null,                   // "feat/<slug>", set when work starts
      "mergeCommit": null,              // set on merge-to-main; used for JSON<->git reconciliation
      "verifyEvidence": null,           // { testsExit, runExit, logRef } recorded at done
      "downMigrationRef": null,         // proven down-migration id/path, set at done for schema-touching items
      "reworkNotes": null,              // user feedback recorded when a checkpoint REJECT resets the item
      "securityWaiver": null            // { by: "user", reason, findings, ts } — user-granted at a security_blocked stop
    }
  ]
}
```

### Item status lifecycle

```
pending ──► in_progress ──► verifying ──► done
   ▲              │              │
   │              ▼              ▼
   │          (crash: reset    blocked   ← verify failed after max_retries_per_item, or any
   │           to pending)               item-level hard-stop; blockReason-qualified:
   │                                       budget_halted | contract_blocked | needs_provisioning |
   │                                       security_blocked | migration_failed | ESCALATION_NEEDED |
   │                                       NEEDS_CLARIFICATION
   └── user-approved reset at the Orchestrator's unblock step (or a checkpoint REJECT of a done item)
```

**Checkpoints do not change item status.** A checkpoint pause is a run-level `.claude/loop.jsonl` record (`{type:"checkpoint", itemId}`) — the paused item stays `done`. There is no `needs_review` status.

`DEADLOCK` is a **run-level terminal outcome** (the loop stops and reports) — *not* an item status.

### Rules the Planner and Orchestrator both enforce

- **Build order is the dependency DAG** (topological), never ID order. IDs are immutable, so inserting an item never renumbers anything.
- **Eligibility**: an item is eligible only when every `dependsOn` item is `done`.
- **Validation** (at write-time and every load): every `dependsOn` ID exists; the graph is acyclic; every JSON item maps to exactly one `BUILD_PLAN.md` row.
- **First item, greenfield**: must be "a locally-runnable app you can see in a browser + test harness + one passing smoke test." `/orchestrate` refuses to start without a runnable/test baseline.
- **Infra/deploy items**: sequenced as a later milestone; the loop won't start them until the app runs locally (`infra_gated_behind_local`).
- **State ownership**: the **Planner** writes `goal`, `createdAt` (display-only), `milestone`, `title`, `slug`, `dependsOn`, `intent`, `acceptance`, `requiresEnv`. The **Orchestrator** owns `status`, `blockReason`, `branch` (set when work starts), `mergeCommit`, `verifyEvidence`, `downMigrationRef`, `reworkNotes`, and `securityWaiver`. A user-approved reset of a non-`done` item to `pending` (at the Orchestrator's unblock step, or via a checkpoint **reject**) is a legitimate Orchestrator write — users do not hand-edit this file. `verifyEvidence = { testsExit, runExit, logRef }` lives on the item; the raw command output it summarizes lives in `.claude/loop.jsonl` (pointed to by `logRef`). `build-plan.json` and `BUILD_PLAN.md` are **commit-intended** durable state (unlike the gitignored telemetry files).

---

## `BUILD_PLAN.md` generated view (what the Orchestrator renders)

```markdown
# Build Plan: <Goal Title>

**Goal**: <one paragraph>   ·   **Autonomy**: per-milestone   ·   **Updated**: <timestamp>

## Status Board

| ID | Milestone | Title | Depends on | Status |
|----|-----------|-------|------------|--------|
| F1 | M1 Foundation | Runnable skeleton + test harness | — | done |
| F2 | M1 Foundation | Auth + user model | F1 | in_progress |
| F3 | M2 Core | Task board | F2 | pending |

Progress: 1/3 done · 0 blocked · tool-calls: 142 / 600 (hard cap) · tokens ~180k (best-effort est.)
```
