
## Notes

- **Sequential only (V1).** One item at a time; parallel execution is deferred.
- **Durable-DB apply happens at merge.** After an item merges to local main, the Orchestrator runs `pnpm db:migrate` against the durable local dev database. Verification always runs against the per-item ephemeral database (a template clone of the seeded one), never the durable DB. TaskFlow's production database is Postgres, so the local one is Postgres too (in-process), not SQLite.
- **Measurable caps are the real guards.** The token numbers are best-effort estimates; the tool-call and item ceilings are what actually stop a runaway.
- **Security runs in the loop.** `sensitive` items (Clerk auth, Stripe billing, uploads, user input) get a `security-reviewer` pass before `done`; Critical/High findings hard-stop.
