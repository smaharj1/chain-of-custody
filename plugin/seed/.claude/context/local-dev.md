<!-- LOCAL DEV PRIMER — how to run, see, and reset the app locally -->
<!-- Read by the Orchestrator's verify_run step and the auto-demo checkpoint. -->
<!-- Fill this in for YOUR project. This is what makes a non-technical builder able to SEE the app. -->

## Run the App Locally

<!-- The single command that boots the app for local development. Example: -->
<!-- ```bash -->
<!-- pnpm dev        # starts web app on http://localhost:5173 and API on http://localhost:3001 -->
<!-- ``` -->

## Ports / URLs

<!-- Where each piece runs locally. Example: -->
<!-- - Web app: http://localhost:5173 -->
<!-- - Admin: http://localhost:5174 -->
<!-- - API: http://localhost:3001 -->

## Local Database

<!-- The local store MUST be the production engine (dialect parity, DEC-8; rules in .claude/context/local-dev-rules.md). -->
<!-- In-process build of the production dialect first, then a container. SQLite only if production is SQLite or libSQL. Example for a Postgres prod: -->
<!-- - Postgres 16 in-process (PGlite or embedded-postgres), data in ./.data/pgdata, no Docker needed -->
<!-- - Connection via DATABASE_URL in .env.local (dev-only value, never a production URL) -->
<!-- - If neither an in-process engine nor Docker is available: the doctor reports an error and the plan is capped at L1 -->

## Apply Migrations to the Durable Dev DB

<!-- The command the Orchestrator runs against the DURABLE dev DB right after each item merges -->
<!-- (verification ran on a throwaway copy — without this step the demo and every later ephemeral -->
<!-- copy would run against stale schema). Example: -->
<!-- ```bash -->
<!-- pnpm db:migrate   # applies pending migrations to ./.data/dev.db -->
<!-- ``` -->

## Seed Data

<!-- How to load sample data so the app shows something real. Example: -->
<!-- ```bash -->
<!-- pnpm db:seed    # creates a demo user + sample records -->
<!-- ``` -->

## Reset the Local DB

<!-- How to wipe and rebuild local data. Example: -->
<!-- ```bash -->
<!-- pnpm db:reset   # drops, re-migrates, re-seeds -->
<!-- ``` -->

## Ephemeral Per-Item DB

<!-- How the loop creates a throwaway DB for each item's verification. Example: -->
<!-- - Postgres: CREATE DATABASE item_$ID TEMPLATE <seeded>, point DATABASE_URL at it, drop it after verify (seconds) -->
<!-- - SQLite (only if production is SQLite): copy the .db file to a temp path, point DATABASE_URL at it, delete after verify -->
<!-- - The throwaway DB is how a failed item's schema changes are discarded for free -->

## Verify-Run Harness

<!-- How `verify_run` produces an EXIT CODE from a dev command that never exits on its own. -->
<!-- The canonical pattern (L-27, .claude/context/local-dev-rules.md): check the dedicated verify ports are free → boot in its OWN -->
<!-- process group → wait for the demo route with a deadline (no sleep loops) → assert with curl → capture the exit code as -->
<!-- runExit → kill the whole group. A bare `pnpm dev & ... kill $pid` leaks the child servers and their ports. Example: -->
<!-- ```bash -->
<!-- set -m                                       # job control: the background job gets its own process group -->
<!-- for port in 5183 3011; do lsof -ti "tcp:$port" >/dev/null && { echo "port $port busy" >&2; exit 2; }; done -->
<!-- WEB_PORT=5183 API_PORT=3011 pnpm dev & pid=$!   # under set -m, pid is also the group id -->
<!-- trap 'kill -TERM -- "-$pid" 2>/dev/null' EXIT   # kills pnpm AND the servers it spawned -->
<!-- curl -sf --retry 30 --retry-connrefused --retry-delay 1 http://localhost:5183/ >/dev/null -->
<!-- curl -sf http://localhost:5183/<demo-target-route>; rc=$? -->
<!-- exit $rc -->
<!-- ``` -->
<!-- Replace <demo-target-route> per item (its falsifiable acceptance assertion's route/endpoint). -->

## Auto-Demo Navigation

<!-- How the loop shows a finished feature: which dev server to start and how to reach a route/screen. -->
<!-- The Orchestrator screenshots each item's "demo target" (its falsifiable acceptance assertion's route). Example: -->
<!-- - Start: pnpm dev -->
<!-- - Navigate to the route named in the item's acceptance (e.g., /tasks for a "task board" item) -->
<!-- - For API-only items, the demo target may be a specific endpoint response instead of a screen -->

## Local-Friendly Service Modes

<!-- How auth/payments/etc. run locally without cloud accounts. Example: -->
<!-- - Auth: dev mode with a seeded test user (no real provider needed to log in) -->
<!-- - Payments: Stripe test mode keys -->
<!-- - Email: logged to console instead of sent -->
<!-- Secrets: test keys and dev values only, in .env.local / .env.test (gitignored); never committed, never in briefs or reports. -->
<!-- Production secrets are never on this machine. Policy: docs/security/secrets-policy.md -->
