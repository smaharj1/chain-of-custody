
## Notes

- **Sequential only (V1).** One item at a time; parallel execution is deferred.
- **Durable-DB apply happens at merge.** After an item merges to local main, the Orchestrator runs `pnpm db:migrate` against `./.data/dev.db`. Verification always runs against the ephemeral copy, never the durable DB.
- **Measurable caps are the real guards.** The token numbers are best-effort estimates; the tool-call and item ceilings are what actually stop a runaway.
- **Security runs in the loop.** `sensitive` items (Clerk auth, Stripe billing, uploads, user input) get a `security-reviewer` pass before `done`; Critical/High findings hard-stop.
