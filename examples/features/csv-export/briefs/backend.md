# Brief: Backend Engineer — CSV Export of Board Tasks

**Feature slug**: `csv-export`
**Engineer**: backend-engineer
**Authored by**: Tech Lead
**Date**: 2026-07-08
**Iteration**: 1
**Retry cap**: `max_retries_per_item` from `.claude/loop.config.md` (default 2). There is no tool-call budget (`.claude/context/engineer-protocol.md` section 4)
**Report path**: `docs/features/csv-export/reports/backend.md`

> *Illustrative example for the fictional TaskFlow project — see [`examples/README.md`](../../../README.md). The files this brief points the engineer at are invented; no engineer was ever dispatched against it.*

---

## Required Reading (in order)

1. `.claude/context/engineer-protocol.md` (the shared rulebook — read first)
2. `.claude/context/backend.md` (your primer)
3. `docs/features/csv-export/requirements.md`
4. `docs/features/csv-export/technical-design.md` — API Surface + Cross-Cutting Concerns sections in full
5. `docs/features/csv-export/api-contract.md`

## Specific Files to Read for This Task

- `packages/api/src/routes/tasks.ts` — you'll add the export route here
- `packages/api/src/services/task.service.ts` — you'll add `exportBoardCsv()` here; canonical exemplar
- `packages/api/src/repositories/task.repository.ts` — `listByBoard()` already joins assignee; reuse it
- `packages/api/src/middleware/auth.ts` — `requireWorkspaceMember` usage pattern for board-scoped routes

## Canonical Exemplar

**Follow this file's pattern**: `packages/api/src/services/task.service.ts`

Any new code in this scope should match its style, layering, naming, and testing approach.

## Scope for This Engineer

Implement `GET /api/boards/:boardId/tasks/export` exactly per `api-contract.md`: route in `routes/tasks.ts` (auth middleware, headers, response), service method `exportBoardCsv(boardId)` (fetch rows, order, map to CSV via a new pure helper `lib/csv.ts`), no repository changes expected. The web-app button and download hook are the App Engineer's scope, not yours.

## Dependencies

- [x] None (parallelizable — no DB change; App Engineer consumes the contract, not your code)

## Acceptance Criteria

- [ ] Endpoint matches `api-contract.md`: 200 CSV with `Content-Disposition` filename, 401/403/404 JSON error envelope, header-only CSV for empty boards.
- [ ] `lib/csv.ts` correctly quotes commas, double quotes, and newlines (RFC 4180), with unit tests proving it.
- [ ] Unassigned tasks -> empty `assignee` field; timestamps ISO 8601 UTC.
- [ ] One info log line per export via `req.log` (`boardId`, row count, duration ms). No `console.*`.
- [ ] Lint clean. Existing tests still pass. Domain verification commands all PASS.
- [ ] The independent `code-reviewer` agent returns no blockers (within the retry cap).
- [ ] Report's `## Primer Delta` and `## Doc Delta` filled in; the new endpoint is documented where the project documents endpoints.

## Out of Scope

- Anything in `apps/web/` or `apps/admin/` — frontend is a separate dispatch.
- Schema or migration changes (`packages/db/`) — none are needed; if you think one is, that's an `ESCALATION_NEEDED`, not a migration.
- Streaming responses, `.xlsx`, time-entry export.
- Adding a CSV library dependency — the design explicitly calls for a small pure helper.

## Clarifications

- (none at dispatch)

## Notes from Tech Lead / Architect

- Row ordering (statuses in TODO -> DONE order, `position` ascending within status) is contract-level — spreadsheet consumers rely on it; test it.
- `requireWorkspaceMember` resolves the workspace from the board row — mirror how `GET /api/boards/:boardId/tasks` wires it.
- Errors stay JSON even though success is CSV; do not invent a CSV error body.

---

## Iteration 2 Addendum (only filled in when the engineer is resumed after review)

> Not used — iteration 1 was approved.
