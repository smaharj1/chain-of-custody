# hello-loop

hello-loop is a tiny bookmarks service: a SQLite table, a JSON API to add and list bookmarks, and a server-rendered page that shows them and lets a visitor add one. It runs locally only and has no authentication. It exists to measure the Chain of Custody kit, not to ship.

## Tech Stack

| Layer | Choice |
|---|---|
| Frontend | Server-rendered HTML from Python string templates (no JavaScript, no build step) |
| Styling | None (browser defaults) |
| State | None on the client; the database is the only state |
| Forms | Plain HTML forms posted as `application/x-www-form-urlencoded` |
| Backend | Python 3.9+ standard library: `http.server` (ThreadingHTTPServer), `sqlite3`, `unittest`; pinned to 3.12 in `.python-version` |
| ORM | None (parameterised SQL in `hello_loop/repos/`) |
| Database | SQLite (the production dialect as well as the local one) |
| Auth | None |
| Payments | None |
| Real-time | None |
| Email | None |
| IaC | None |

## Repo Structure

- `hello_loop/` is the application package.
  - `config.py` reads `DATABASE_URL` and `PORT`; `db.py` opens connections; `migrate.py` runs migrations.
  - `migrations/` holds `NNNN_name.up.sql` and `NNNN_name.down.sql` pairs.
  - `router.py` defines `Request`, `Response` and the `@route` decorator; `server.py` is the HTTP server.
  - `handlers/` holds one module per resource (HTTP only); importing a module in `handlers/__init__.py` registers its routes.
  - `repos/` holds data access, one module per table (created by the first item that needs it).
- `tests/` holds `unittest` tests; `tests/helpers.py` gives a temp database and a live server on a free port.
- `scripts/` holds `verify_run.py` (the verify-run harness) and `ephemeral_db.py` (per-item throwaway database).
- `build-plan.json` is the approved plan. Do not edit its structure.

## Agents and Modes

Available modes: `/pm`, `/architect`, `/tech-lead`

Loop modes: `/planner`, `/orchestrate`

Engineer subagents: database, backend, app

Specialist agents: security-reviewer, design-critic, code-reviewer

**Default posture**: when no mode has been invoked this session, behave as the PM (per `.claude/commands/pm.md`) — gather requirements through questions; don't design or dispatch until the user switches modes.

## Key Conventions

- Python standard library only. Adding a dependency is a design escalation, not an implementation choice.
- Every file starts with `from __future__ import annotations` and must parse under Python 3.9 (no `match`, no `X | Y` types at runtime).
- SQL is always parameterised (`?` placeholders). The only string-built SQL is the migration bookkeeping, whose ids are checked against `^\d{4}_[a-z0-9_]+$`.
- Timestamps are UTC ISO-8601 strings, `YYYY-MM-DDTHH:MM:SSZ`, stored as TEXT.
- IDs are SQLite `INTEGER PRIMARY KEY`.
- Errors in the JSON API are `{"error": "<message>"}`. Bad input is 400 (not 422), a duplicate is 409, a missing record is 404.
- Anything a visitor typed that is put into HTML goes through `html.escape` first.

## Architecture Patterns

Two layers: `handlers/` (parse the request, validate, call a repo function, build the response) and `repos/` (SQL only, plain functions taking a `sqlite3.Connection`, returning dicts). Handlers never write SQL; repos never import the router. Validation lives next to the handler that owns the input and is reused, not copied.

## Quick Start

```bash
python3 -m hello_loop.migrate          # create or update .data/dev.db
python3 -m hello_loop.server           # http://127.0.0.1:8000
python3 -m unittest discover -s tests -t .
```

## Full Spec Reference

`build-plan.json` (goal, items and acceptance criteria). Local-run details are in `.claude/context/local-dev.md`.
