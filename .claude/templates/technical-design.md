# Technical Design: <Feature Title>

**Slug**: `<feature-slug>`
**Authored by**: Architect
**Status**: DRAFT | LOCKED | REVISED
**Last revised**: <YYYY-MM-DD>

---

## Overview

2-3 paragraphs summarizing the technical approach: what gets built, where it lives, how it connects.

## Data Model

### New Tables

```sql
-- table_name
column_name        type            constraints
...
-- indexes:
--   idx_table_column on (column)
```

### Modified Tables

- `existing_table`:
  - ADD COLUMN `name type` (default, nullability)
  - DROP COLUMN `name` (data implications?)
  - ALTER COLUMN `name` (type change strategy)

### Migration Notes (for Database Engineer)

- Order of operations: ...
- Stage (existing data only; see `.claude/context/migrations.md`): expand | backfill | contract — each is its own slice
- Backfill strategy (if any): a separate, batched, idempotent, resumable job, never inside the migration file
- Reversibility: down migration must do X, Y, Z
- Data loss risk: <none | calling out specific risk>
- Down-migration data-loss class: none | lossy-recoverable | lossy (a `lossy` down is human-gated)

## API Surface

High-level list. Full request/response shapes are in `api-contract.md`.

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/...` | Create X | <role> |
| GET | `/api/...` | List X | <role> |
| ... | ... | ... | ... |

## Frontend Impact

<!-- List each app that's affected. Delete sections for apps that aren't in scope. -->
<!-- Rename these to match YOUR project's apps. -->

### App 1

- New pages: ...
- Modified pages: ...
- New hooks: ...
- New stores: ...
- Out of scope for this app: ...

### App 2

- New pages: ...
- Modified pages: ...
- New hooks: ...
- Out of scope for this app: ...

## Cross-Cutting Concerns

- **Auth / permissions**: ...
- **Background jobs**: ...
- **Webhooks**: ...
- **Emails**: ...
- **Caching**: ...
- **Observability** (what to log/alert): ...

## Integration Points

<!-- List any third-party services involved. Delete if none. -->

- ...

## Edge Cases & Risks

- Concurrency: ...
- Idempotency: ...
- Partial failure: ...
- Race conditions: ...
- Performance hot paths: ...

## Slices

An ordered DAG of PR-sized slices the Planner turns into plan items. Walking skeleton first, then thicken. A slice sized `L` must be split; expand, backfill and contract of one migration are three slices.

| Slice | Depends on | Size (S/M/L) | Engineers | Touches (domains, paths) | Schema / deps change | Acceptance (at most 3, one falsifiable) | Risk (auth, payments, uploads, user input) |
|---|---|---|---|---|---|---|---|
| S1 | — | M | backend, app | ... | none | ... | none |
| S2 | S1 | S | ... | ... | expand | ... | ... |

Limits per slice: one user-visible vertical slice, at most 2 engineers preferred (3 at most), at most 1 migration, at most `max_item_loc` (default 400) estimated changed lines.

## Open Questions

(Things still to decide. Don't ship a design with unresolved load-bearing questions.)

- ...

## Out of Scope (Future Work)

Boxed off for later. Do NOT silently expand the scope of v1.

- ...

---

## Revisions (after escalations)

### Revision 1 — <YYYY-MM-DD>

Triggered by: `<engineer> ESCALATION_NEEDED` on `<feature-slug>`.

What changed: ...

Why: ...
