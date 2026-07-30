# Backend Engineer Report — csv-export

## Status
APPROVED

## Summary
Implemented `GET /api/boards/:boardId/tasks/export` per the contract: route in `routes/tasks.ts` behind `requireAuth` + `requireWorkspaceMember`, new `TaskService.exportBoardCsv()` reusing `TaskRepository.listByBoard()`, and a pure RFC 4180 helper in `lib/csv.ts` (no new dependency). Empty boards return a header-only CSV; errors remain the standard JSON envelope.

## Files Changed
- `packages/api/src/routes/tasks.ts` — modified — added export route, CSV headers, `Content-Disposition` filename, `Cache-Control: no-store`
- `packages/api/src/services/task.service.ts` — modified — added `exportBoardCsv(boardId)`: ordering (status TODO->DONE, then `position` asc), row mapping, info log with row count + duration
- `packages/api/src/lib/csv.ts` — created — `toCsv(headers, rows)` pure helper with RFC 4180 quoting
- `packages/api/src/lib/csv.test.ts` — created — unit tests for the helper
- `packages/api/src/routes/tasks.export.test.ts` — created — endpoint integration tests

## Tests Added (or N/A)
- `lib/csv.test.ts::quotes fields containing commas, quotes, and newlines` — covers RFC 4180 escaping round-trip
- `lib/csv.test.ts::renders empty and null-ish values as empty string` — covers unassigned assignee / missing due_date
- `routes/tasks.export.test.ts::returns 200 CSV with header row for empty board` — covers empty-state acceptance criterion
- `routes/tasks.export.test.ts::orders rows by status then position` — covers contract-level row ordering
- `routes/tasks.export.test.ts::returns 403 for non-member, 404 for unknown board` — covers auth + not-found envelopes

## API Contract Status
AS_SPECIFIED — implemented exactly to `api-contract.md`; no deviation needed.

## Iteration
1

## Open Questions / Brief Gaps
- Brief didn't specify the slugification rule for the `Content-Disposition` filename; reused the existing workspace `slug` conventions (lowercase, hyphens) from `lib/slugify.ts`. Safe inference — documenting rather than blocking.

## Verification
- `pnpm --filter @taskflow/api test`: PASS — 41 tests (9 new), 0 failed
- `pnpm --filter @taskflow/api lint`: PASS — no warnings
- `pnpm --filter @taskflow/api typecheck`: PASS — strict mode clean
- Manual: `curl -sf -H "Authorization: Bearer <dev token>" http://localhost:3001/api/boards/<seeded-board>/tasks/export`: PASS — valid CSV, correct filename header

## Telemetry
- Tool calls used: ~34
- Tool-call budget hit: no
- Wall time (approx): 12 min
