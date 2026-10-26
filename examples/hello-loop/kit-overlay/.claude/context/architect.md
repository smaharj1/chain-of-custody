## System Topology

One Python process. `http.server.ThreadingHTTPServer` accepts requests, a router dispatches them to handlers, and handlers use SQLite through a per-request connection. There is no proxy, queue, cache or second service. It listens on 127.0.0.1 only.

## Service Map

| Component | Service / Tool | Key Config |
|---|---|---|
| CDN | None | |
| API routing | `hello_loop/router.py` | regex route table, 404/405 in `dispatch` |
| Compute | One local Python process (`python3 -m hello_loop.server`) | `PORT`, default 8000 |
| Database | SQLite file | `DATABASE_URL=file:<path>`, default `.data/dev.db` |
| Auth | None | |
| Storage | None | |
| Email | None | |
| IaC | None | |

## Infrastructure Constraints

- Standard library only: no third-party packages.
- The service binds to 127.0.0.1 and has no authentication; it must never be exposed beyond localhost.
- Python 3.9 is the floor (`requires-python >=3.9`); `.python-version` pins 3.12 for the baseline.
- No network access is needed to build, test or run.

## Auth Model

None. All routes are public to local users.

## Performance Targets

None. Correctness first; the data set is a handful of rows.

## Decision Log

- SQLite is the production dialect as well as the local one, so the local-dev rule "match the production engine" is satisfied without a container.
- Hand-written migrations (`hello_loop/migrate.py`) instead of a framework: three files of SQL do not justify a dependency.
- Validation errors are 400, not 422, everywhere (JSON API and HTML form).
- The page reuses the repo layer and the API's validation function rather than calling the JSON API over HTTP.

## Out of Scope (current version)

- Authentication or users, editing or deleting bookmarks, pagination, search, tags.
- Deployment, containers, CI and any cloud or infrastructure work.
