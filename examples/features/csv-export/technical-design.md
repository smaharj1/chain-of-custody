# Technical Design: CSV Export of Board Tasks

**Slug**: `csv-export`
**Authored by**: Architect
**Status**: LOCKED
**Last revised**: 2026-07-07

> *Illustrative example for the fictional TaskFlow project — see [`examples/README.md`](../../README.md). The files and existing behavior it cites (`TaskRepository.listByBoard()`, `apps/web/pages/BoardPage.tsx`) are invented.*

---

## Overview

A single new read-only endpoint, `GET /api/boards/:boardId/tasks/export`, added to the existing tasks route/service/repository stack in `packages/api/`. The service reuses `TaskRepository.listByBoard()` (already joins assignee for the board view) and serializes rows to CSV in a small pure helper, `lib/csv.ts` — no CSV library dependency; RFC 4180 quoting is ~20 lines and we control the input shapes.

The web app adds an "Export CSV" button to the board topbar that hits the endpoint via the authenticated fetch wrapper and triggers a browser download from the response blob. No schema changes, no new packages, no admin-app impact.

## Data Model

### New Tables

None.

### Modified Tables

None — read-only feature over `tasks` (joined to `users` for assignee display name).

### Migration Notes (for Database Engineer)

- No migration. Database Engineer is not dispatched for this feature.

## API Surface

High-level list. Full request/response shapes are in `api-contract.md`.

| Method | Path | Purpose | Auth |
|---|---|---|---|
| GET | `/api/boards/:boardId/tasks/export` | Download board tasks as CSV | workspace member |

## Frontend Impact

### Web app (`apps/web/`)

- New pages: none.
- Modified pages: `pages/BoardPage.tsx` — "Export CSV" button in the board topbar.
- New hooks: `useExportTasks(boardId)` — calls the endpoint, creates an object URL, triggers download named per `Content-Disposition`.
- New stores: none.
- Out of scope for this app: export progress UI (response is fast enough to just disable the button while pending).

### Admin app (`apps/admin/`)

- Not affected. No admin surface for this feature.

## Cross-Cutting Concerns

- **Auth / permissions**: `requireAuth` + `requireWorkspaceMember` (workspace resolved from the board row). Enforced in middleware like every other board-scoped route.
- **Background jobs**: none — synchronous response.
- **Webhooks**: none.
- **Emails**: none.
- **Caching**: none; always a fresh snapshot. `Cache-Control: no-store`.
- **Observability**: log one info line per export (`boardId`, row count, duration ms) via `req.log`.

## Integration Points

None — no third-party services involved.

## Edge Cases & Risks

- Concurrency: read-only; none.
- Idempotency: naturally idempotent (GET).
- Partial failure: none — single query, single response.
- Race conditions: tasks created mid-export are simply absent from the snapshot; acceptable.
- Performance hot paths: boards are small (<10k tasks); buffering the full CSV in memory is fine for v1. Streaming is the future-work escape hatch if board sizes grow.

## Open Questions

None — the two requirements questions (empty assignee, ISO 8601 UTC timestamps) are resolved and encoded in the contract.

## Out of Scope (Future Work)

- Streaming response for very large boards.
- `.xlsx` export, column selection, time-entry export, scheduled exports.

---

## Revisions (after escalations)

None.
