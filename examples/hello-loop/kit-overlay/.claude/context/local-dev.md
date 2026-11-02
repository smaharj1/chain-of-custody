## Run the App Locally

```bash
python3 -m hello_loop.migrate && python3 -m hello_loop.server
```

After the first plan item has landed, load the sample data once with `python3 -m hello_loop.seed`. The server stays in the foreground; stop it with Ctrl-C.

## Ports / URLs

- App: http://127.0.0.1:8000 (override with `PORT=<n>`)
- Liveness: http://127.0.0.1:8000/healthz
- JSON API (from item F2): http://127.0.0.1:8000/api/bookmarks
- Page (from item F3): http://127.0.0.1:8000/

## Local Database

- SQLite, file at `.data/dev.db` (created on first use, gitignored, no setup needed).
- Connection via `DATABASE_URL=file:<path>`; a relative path is relative to the project root.
- Production is SQLite too, so there is no dialect gap to manage.

## Apply Migrations to the Durable Dev DB

```bash
python3 -m hello_loop.migrate
```

This applies pending `hello_loop/migrations/*.up.sql` to `.data/dev.db`. `python3 -m hello_loop.migrate --down` reverts the most recent migration; it is the down migration the Orchestrator records as `downMigrationRef`. Run after each merge, never during verification.

## Seed Data

```bash
python3 -m hello_loop.seed    # three sample bookmarks; safe to run repeatedly (exists from item F1)
```

## Reset the Local DB

```bash
rm -f .data/dev.db && python3 -m hello_loop.migrate && python3 -m hello_loop.seed
```

## Ephemeral Per-Item DB

```bash
export DATABASE_URL=$(python3 scripts/ephemeral_db.py create "$ITEM_ID")   # copies .data/dev.db to the system temp dir
# ... run verify_tests and verify_run with DATABASE_URL exported ...
python3 scripts/ephemeral_db.py delete "$ITEM_ID"                           # pass or fail
```

`$ITEM_ID` is the build-plan item id (`F1`, `F2`, `F3`). The tests create their own temp databases and ignore `DATABASE_URL`; the override matters for `verify_run`, which migrates and seeds whatever `DATABASE_URL` points at.

## Verify-Run Harness

`scripts/verify_run.py` is the harness: it migrates and seeds the database, boots the server on a free port, polls `/healthz`, requests one route, prints the status, kills the server, and exits 0 only if the status matches.

```bash
python3 scripts/verify_run.py                    # /healthz, expects 200
python3 scripts/verify_run.py /api/bookmarks     # item F2 demo target
python3 scripts/verify_run.py /                  # item F3 demo target
```

Pass the demo route from the item's acceptance criteria. For a route that should be refused, add `--status 404` (or the expected code).

## Auto-Demo Navigation

- Start: `python3 -m hello_loop.migrate && python3 -m hello_loop.seed && python3 -m hello_loop.server`
- F1 has no screen: the demo is `python3 -m hello_loop.migrate` followed by `sqlite3 .data/dev.db ".schema bookmarks"`, or the passing `tests.test_schema` run.
- F2: `curl http://127.0.0.1:8000/api/bookmarks` shows the JSON list.
- F3: open http://127.0.0.1:8000/ to see the list and the add form.
- No browser capability means an HTTP status check on the same route is the demo.

## Local-Friendly Service Modes

No external services: no auth, payments, email or real-time. Nothing needs a credential or the network.
