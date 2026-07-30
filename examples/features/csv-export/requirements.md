# Feature: CSV Export of Board Tasks

**Slug**: `csv-export`
**Date**: 2026-07-06
**Status**: COMPLETE
**Authored by**: PM

> *Illustrative example for the fictional TaskFlow project — see [`examples/README.md`](../../README.md). No code exists behind it.*

---

## Problem

Team leads on PRO workspaces need to report progress to stakeholders who live in spreadsheets, and today they retype task lists by hand. There is no way to get tasks out of TaskFlow in a tabular format. This blocks weekly reporting workflows and is a recurring churn-risk complaint from paying customers.

## Goal

Any workspace member can download the tasks of a board as a CSV file in one click.

## Users / Personas

- [x] Workspace Member — downloads a board's tasks for personal reporting.
- [x] Workspace Admin / Owner — same flow; no elevated behavior for export.

## In Scope (v1)

- [x] `GET /api/boards/:boardId/tasks/export` endpoint returning CSV.
- [x] Columns: id, title, status, priority, assignee display name, due date, created_at, updated_at.
- [x] "Export CSV" button on the board page (`/boards/:id`) topbar.
- [x] Filename includes board name and export date.

## Out of Scope

- Export of time entries (separate feature; different consumers).
- Excel (`.xlsx`) format, custom column selection, scheduled/emailed exports.
- Workspace-wide (multi-board) export.
- Any import path — this is one-way.

## Acceptance Criteria

- [x] A member of the board's workspace clicks "Export CSV" on `/boards/:id` and receives a `.csv` download containing every task on the board.
- [x] A user who is NOT a member of the board's workspace gets 403, not an empty file.
- [x] A board with zero tasks downloads a CSV containing only the header row.
- [x] Task titles containing commas, quotes, and newlines round-trip correctly when opened in a spreadsheet app.

## Edge Cases / Constraints to Surface

- Empty state: header-only CSV, still HTTP 200.
- Permissions: workspace-scoped — membership in the board's workspace is required; no per-board permissions exist.
- Errors / failure modes: unknown boardId -> 404; unauthenticated -> 401.
- Concurrency: export is a read-only snapshot; no locking concerns.
- Mobile vs desktop: browser-native download; no special mobile UI.
- Large data / pagination: boards are capped well under 10k tasks in practice; single-response export is fine for v1 (no streaming).

## Open Questions

- (resolved in design) Unassigned tasks: `assignee` column is empty string, not `"null"`.
- (resolved in design) Timestamps exported as ISO 8601 UTC; spreadsheet localization is the consumer's problem.

## References / Inspiration

- Trello's board export (CSV on paid plans) — closest prior art.
