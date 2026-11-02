# hello-loop

A fixture, not an example of a real project. It is the small service the Chain of Custody kit is measured against: the V1 baseline (`docs/baseline/README.md`) and, after that, every phase that claims to improve the loop. It is also the one `examples/` directory that is real, runnable code (the others describe a fictional app).

It is a bookmarks service written with the Python standard library only (`http.server`, `sqlite3`, `unittest`), so it runs offline, in CI and on a fresh machine. SQLite is the declared production dialect, which satisfies the dialect-parity rule honestly (DEC-8): there is no gap between dev and prod to paper over.

## The plan

`build-plan.json` is a V1-shape plan (the shape `.claude/templates/build-plan.md` defines at v1.2.0): three items with real dependencies and acceptance criteria that carry ids (`AC-F2-4`) a command can falsify.

| Item | Milestone | Depends on | Delivers | Demo target |
|---|---|---|---|---|
| F1 bookmarks-schema | M1 Data and API | none | first migration with its down migration, seed command | `python3 -m hello_loop.migrate`, then the table |
| F2 bookmarks-api | M1 Data and API | F1 | `POST/GET /api/bookmarks`, `GET /api/bookmarks/<id>` | `GET /api/bookmarks` |
| F3 bookmarks-page | M2 Interface | F2 | server-rendered list and add form at `/` | `GET /` |

F1 touches the schema (so the loop exercises `downMigrationRef`), F2 and F3 take user input (so the loop's security review is triggered), and F3 reuses F2's validation and data access (a real dependency, not an ordering convention). The pitfalls the report's real-use evidence names are pinned in the acceptance criteria on purpose: 400 vs 422, escaping, a duplicate.

## What a run starts from

The tree in this directory is an existing, working codebase: a migration runner (`hello_loop/migrate.py`), a router and server, one handler (`handlers/health.py`) with its smoke test, test helpers, and the verify-run harness (`scripts/verify_run.py`). The three items add the table, the API and the page. Because the codebase exists, the V1 "greenfield skeleton" item is not needed, and the primers are written from real code.

`kit-overlay/` is what a Planner would have produced, written in advance so the baseline measures the loop rather than primer derivation:

- `.claude/CLAUDE.md` (stack table and structure stamped, no template sentinels)
- `.claude/context/*.md` (app, architect, backend, database, shared-frontend, local-dev), with no placeholder comments and no `TODO(primer)`
- `.claude/loop.config.md`: the v1.2.0 template with only the three execution-critical commands filled in (`verify_tests`, `verify_run`, `db_ephemeral`); `autonomy: per-milestone`, all other values at their defaults
- `remove.txt`: the admin-app and infra primers and agents, deleted from the clone as the v1.2.0 templates tell a project without them to do

`reference-solution/F1`, `F2` and `F3` are the known-good implementation, one directory per item, applied cumulatively. They exist only to prove the plan is satisfiable and each acceptance criterion is falsifiable; `tests/test_hello_loop.py` applies them to a temporary copy and runs the verify commands. `tools/baseline/prepare.py` never copies them, nor `acceptance/`, `verify.json` or this file, into a run.

## Held-out checks

`acceptance/run_checks.py` re-states every acceptance criterion as a black-box check (it runs the project's commands and talks to its server over HTTP; it imports nothing from it). After a baseline run, `python3 acceptance/run_checks.py --project <run dir>` reports which criteria the final tree really satisfies; the failures are the run's escaped defects. `verify.json` lists, per item, the engineer-facing test command, the held-out command and the demo route.

## Toolchain

- `.python-version`: 3.12, the toolchain the baseline declares.
- `pyproject.toml`: `requires-python = ">=3.9"`, no dependencies. The code uses `from __future__ import annotations` and runs on 3.9 and newer; the repo's CI runs the fixture on its Python matrix (DEC-27).

## Commands

```bash
python3 -m hello_loop.migrate           # create or update .data/dev.db
python3 -m hello_loop.server            # http://127.0.0.1:8000
python3 -m unittest discover -s tests -t .
python3 scripts/verify_run.py /healthz  # boots the app, asserts one route
```
