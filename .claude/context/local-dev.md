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

<!-- The default local store and how it's configured. Example: -->
<!-- - SQLite by default — file at ./.data/dev.db (no setup needed) -->
<!-- - Connection via DATABASE_URL in .env.local -->
<!-- - To switch to Postgres later: set DATABASE_URL to a Postgres URL; migrations regenerate per-dialect -->

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
<!-- - SQLite: copy ./.data/dev.db to a temp file, point DATABASE_URL at it, delete after verify -->
<!-- - The throwaway DB is how a failed item's schema changes are discarded for free -->

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
