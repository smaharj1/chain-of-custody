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
- Backfill strategy (if any): ...
- Reversibility: down migration must do X, Y, Z
- Data loss risk: <none | calling out specific risk>

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
