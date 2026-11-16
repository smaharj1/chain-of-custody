## Run the App Locally

```bash
pnpm dev        # starts web (5173), admin (5174), and API (3001) concurrently
```

## Ports / URLs

- Web app: http://localhost:5173
- Admin: http://localhost:5174
- API: http://localhost:3001

## Local Database

- Production is PostgreSQL 16 on RDS, so local dev runs **PostgreSQL 16 too** (dialect parity): `embedded-postgres`, a real Postgres binary started by `pnpm db:up`, data in `./.data/pgdata`, port 54329. No Docker needed.
- Connection via `DATABASE_URL=postgres://localhost:54329/taskflow_dev` in `.env.local` (a dev-only value, never a production URL).
- The Drizzle schema and migrations are the same ones production runs. Local dev never touches RDS.
- Root `pnpm db:*` scripts are aliases for the filtered commands in `packages/db/` (e.g. `pnpm db:migrate` runs `pnpm --filter @taskflow/db migrate`).

## Seed Data

```bash
pnpm db:seed    # demo user (via Clerk dev instance), 1 workspace, 2 boards, ~20 tasks, sample time entries
```

## Reset the Local DB

```bash
pnpm db:reset   # drops and recreates taskflow_dev, re-runs pnpm db:migrate, re-runs pnpm db:seed
```

## Apply Migrations (durable DB)

```bash
pnpm db:migrate   # applies pending migrations to taskflow_dev
```

- This is the **durable-DB apply command the Orchestrator runs after each merge** — a passing item's migrations land in `taskflow_dev` only at merge time, never during verification.

## Ephemeral Per-Item DB

The loop verifies each item against a throwaway clone of a seeded database, so a failed item's schema changes are discarded for free. A template clone takes seconds:

```bash
pnpm db:ephemeral $ID    # $ID = the build-plan item id. Runs:
                         #   CREATE DATABASE taskflow_item_$ID TEMPLATE taskflow_seeded
                         # and prints DATABASE_URL=postgres://localhost:54329/taskflow_item_$ID
pnpm db:drop $ID         # DROP DATABASE taskflow_item_$ID; run after verify, pass or fail
```

`taskflow_seeded` is rebuilt by `pnpm db:seed:template` whenever the durable dev DB's migrations change. If `embedded-postgres` cannot run on a machine, the fallback is a disposable container (`docker run -d --rm --name taskflow-item-$ID -e POSTGRES_DB=taskflow -p 5544:5432 postgres:16`). With neither, the doctor reports an error and the plan is capped at L1; SQLite is never the fallback because production is not SQLite.

## Verify-Run Harness

The per-item runtime assertion: check the ports are free, boot the app in its **own process group**, wait for the demo target with a deadline, assert the API health endpoint, then kill the whole group. The verify run uses dedicated ports so it never collides with a dev server the human left running.

```bash
set -m                                              # job control: the background job gets its own process group
for port in 5183 3011; do                           # dedicated verify ports
  if lsof -ti "tcp:$port" >/dev/null; then echo "port $port is busy" >&2; exit 2; fi
done
WEB_PORT=5183 API_PORT=3011 pnpm dev & pid=$!       # pid is also the group id under set -m
trap 'kill -TERM -- "-$pid" 2>/dev/null' EXIT       # kills pnpm AND the servers it spawned
curl -sf --retry 30 --retry-connrefused --retry-delay 1 http://localhost:5183/tasks >/dev/null   # readiness, with a deadline
curl -sf http://localhost:3011/api/health; rc=$?
exit $rc
```

- `pnpm dev & ... kill $pid` leaks: it kills `pnpm` and leaves the web, admin and API servers holding their ports, and the next item fails on "port in use". Killing the group (`-$pid`) is what ends them all. macOS has no `setsid` by default, so `set -m` is the portable form.
- Replace `/tasks` with the item's demo-target route (from its falsifiable acceptance assertion). For API-only items, curl the endpoint itself instead of a page.
- Run with the ephemeral `DATABASE_URL` override from the section above.

## Auto-Demo Navigation

- Start: `pnpm dev`
- Navigate to the route named in the item's acceptance criteria — e.g. `http://localhost:5173/boards/:id` for a kanban item, `http://localhost:5174/users` for an admin item.
- For API-only items, the demo target is the endpoint response (e.g. `curl http://localhost:3001/api/health`).

## Local-Friendly Service Modes

- **Secrets policy**: only dev and test values ever appear locally (`.env.local`, `.env.test`; both gitignored). Production secrets live in a secret manager and are never on this machine. See `docs/security/secrets-policy.md`.
- **Auth**: Clerk dev instance (test-mode publishable/secret keys in `.env.local`) with a seeded test user — no production Clerk account touched.
- **Payments**: Stripe test-mode keys; webhooks via `stripe listen --forward-to localhost:3001/api/webhooks/stripe` when needed.
- **Email**: Resend is bypassed in dev — outbound email is logged to the API console instead of sent.
- **Real-time**: Socket.IO server runs in-process with the API in dev (no Fargate needed).
