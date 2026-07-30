## Run the App Locally

```bash
pnpm dev        # starts web (5173), admin (5174), and API (3001) concurrently
```

## Ports / URLs

- Web app: http://localhost:5173
- Admin: http://localhost:5174
- API: http://localhost:3001

## Local Database

- SQLite by default — file at `./.data/dev.db` (no setup needed).
- Connection via `DATABASE_URL=file:./.data/dev.db` in `.env.local`.
- Production is PostgreSQL 16 on RDS; the Drizzle schema is shared and migrations regenerate per-dialect. Local dev never touches RDS.
- Root `pnpm db:*` scripts are aliases for the filtered commands in `packages/db/` (e.g. `pnpm db:migrate` runs `pnpm --filter @taskflow/db migrate`).

## Seed Data

```bash
pnpm db:seed    # demo user (via Clerk dev instance), 1 workspace, 2 boards, ~20 tasks, sample time entries
```

## Reset the Local DB

```bash
pnpm db:reset   # deletes ./.data/dev.db, re-runs pnpm db:migrate, re-runs pnpm db:seed
```

## Apply Migrations (durable DB)

```bash
pnpm db:migrate   # applies pending migrations to ./.data/dev.db
```

- This is the **durable-DB apply command the Orchestrator runs after each merge** — a passing item's migrations land in `./.data/dev.db` only at merge time, never during verification.

## Ephemeral Per-Item DB

The loop verifies each item against a throwaway copy of the durable DB, so a failed item's schema changes are discarded for free:

```bash
cp ./.data/dev.db /tmp/taskflow-item-$ID.db     # $ID = the build-plan item id
DATABASE_URL=file:/tmp/taskflow-item-$ID.db pnpm dev   # run + verify against the copy
rm /tmp/taskflow-item-$ID.db                    # delete after verify, pass or fail
```

> **Postgres alternative** — use this pattern if your durable dev DB is Postgres:
>
> ```bash
> createdb -T taskflow_dev taskflow_item_$ID     # template clone; drop with dropdb after verify
> # or a disposable container:
> docker run -d --rm --name taskflow-item-$ID -e POSTGRES_DB=taskflow -p 5544:5432 postgres:16
> ```
>
> Point `DATABASE_URL` at the clone/container, run migrations + seed, verify, then `dropdb` / `docker stop`.

## Verify-Run Harness

The per-item runtime assertion: boot the app, poll until the item's demo target responds, assert the API health endpoint, tear down.

```bash
pnpm dev & pid=$!
for i in $(seq 30); do curl -sf http://localhost:5173/tasks >/dev/null && break; sleep 1; done
curl -sf http://localhost:3001/api/health; rc=$?
kill $pid
exit $rc
```

- Replace `/tasks` with the item's demo-target route (from its falsifiable acceptance assertion). For API-only items, curl the endpoint itself instead of a page.
- Run with the ephemeral `DATABASE_URL` override from the section above.

## Auto-Demo Navigation

- Start: `pnpm dev`
- Navigate to the route named in the item's acceptance criteria — e.g. `http://localhost:5173/boards/:id` for a kanban item, `http://localhost:5174/users` for an admin item.
- For API-only items, the demo target is the endpoint response (e.g. `curl http://localhost:3001/api/health`).

## Local-Friendly Service Modes

- **Auth**: Clerk dev instance (test-mode publishable/secret keys in `.env.local`) with a seeded test user — no production Clerk account touched.
- **Payments**: Stripe test-mode keys; webhooks via `stripe listen --forward-to localhost:3001/api/webhooks/stripe` when needed.
- **Email**: Resend is bypassed in dev — outbound email is logged to the API console instead of sent.
- **Real-time**: Socket.IO server runs in-process with the API in dev (no Fargate needed).
