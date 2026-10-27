# Local development rules (dialect parity, process groups, ports)

> Shared kit rule (not a project primer). `local-dev.md` is the project's own primer; this file is the rules its commands must follow, so each commented example in that template can stay short. Report: L-27, L-38, DEC-8, RC-21; secrets: CO-2.

## Dialect parity (DEC-8)

The local database must be the **production engine**, because a migration that passes on a different engine proves little.

1. **In-process first.** Use an in-process build of the production dialect. For Postgres on Node: PGlite or `embedded-postgres`.
2. **Then a container.** If the in-process engine cannot match the production version or its extensions, use Docker or testcontainers.
3. **SQLite only if production is SQLite or libSQL.** It is never "the easy default" for a project whose production database is something else.
4. **No matching engine at all** (no in-process build, no Docker): the doctor reports an **error**, and the plan's effective level is **capped at L1** until one is installed. Do not paper over it with SQLite.

Per-item ephemeral databases are created from a seeded template (`CREATE DATABASE ... TEMPLATE <seeded>` on Postgres), which takes seconds; a failed item's database is simply dropped.

Set `db_local` in `.claude/loop.config.md` explicitly. The shipped default (`sqlite`) is kept only so a v1 config still loads; the Planner sets the right value for the chosen stack.

## Process groups and ports (L-27)

The pattern `pnpm dev & pid=$! ... kill $pid` leaks: it kills the package-manager wrapper and leaves the servers it started holding their ports, and the next run fails on "port in use".

- **Launch the dev server in its own process group** and **kill the whole group**. In a shell harness: `set -m`, start the server with `&`, and `kill -TERM -- -$pid` on exit (macOS has no `setsid` by default). In a Python harness (`loopctl` later): `subprocess.Popen(..., start_new_session=True)` and `os.killpg(...)` after a timeout.
- **Check the ports are free before booting**, and use **dedicated verify ports** (not the dev-server defaults) so a server the human left running cannot collide with a verify run. A busy port is a clear error, not a retry.
- **Readiness has a deadline and no `sleep` loops.** In an interactive session, stream the server log with the Monitor tool until the ready line appears. In a script, use `curl --retry N --retry-connrefused --retry-delay 1`. Either way a timeout fails the check; it never waits forever.
- The harness's exit code is the **assertion's** exit code, taken **before** the teardown runs.

## Local-friendly service modes and secrets

Auth, payments and email run in a local-friendly mode (a dev auth instance with a seeded user, payment test keys, email logged to the console). **Only dev and test values appear locally.** They live in `.env.local` / `.env.test`, which are gitignored; they are never committed, never pasted into briefs, reports or PR bodies, and production secrets are never present on the machine. The full policy is `docs/security/secrets-policy.md`.
