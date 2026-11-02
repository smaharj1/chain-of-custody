## ORM and Tooling

- No ORM. `sqlite3` from the standard library, with `row_factory = sqlite3.Row` and `PRAGMA foreign_keys = ON` (see `hello_loop/db.py`).
- Database: SQLite. It is also the production dialect, so local and production match.
- Migrations: the hand-written runner in `hello_loop/migrate.py`.

## Migration Location and Commands

- Migration files: `hello_loop/migrations/NNNN_name.up.sql` and `NNNN_name.down.sql`. `NNNN` is a zero-padded four-digit sequence number; `name` is lower-case snake_case. Both files are required: the runner raises when a down file is missing.
- Apply: `python3 -m hello_loop.migrate`
- Revert the latest: `python3 -m hello_loop.migrate --down`
- Each migration runs in one transaction with its `schema_migrations` bookkeeping row, so a failing migration leaves nothing half-applied.
- Applied ids are recorded in `schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT)`.
- Never edit a migration that has been applied; add a new one.

## Table Catalog

Table catalog: see `hello_loop/migrations/*.up.sql`. Conventions below apply to all tables. At the start of the plan only `schema_migrations` exists.

## Conventions

- Primary keys are `id INTEGER PRIMARY KEY`.
- Timestamps are TEXT in UTC ISO-8601 form `YYYY-MM-DDTHH:MM:SSZ`, named `created_at` and so on.
- Text columns that must be present are `NOT NULL`. Natural keys such as a url get `UNIQUE`.
- Every migration has a down migration that restores the previous schema; a down migration that drops data says so in a SQL comment.
- Seed data goes in `hello_loop/seed.py`, never in a migration, and must be idempotent (`INSERT OR IGNORE`).

## Relationships

None yet.
