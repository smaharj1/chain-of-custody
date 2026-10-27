## Runtime and Framework

- Python 3.9+ standard library only (`http.server.ThreadingHTTPServer`, `sqlite3`, `unittest`, `json`, `html`, `urllib`). `.python-version` pins 3.12; `pyproject.toml` declares `requires-python >=3.9`.
- No third-party packages. A new dependency is a design escalation.

## Code Architecture

Two layers.

- `hello_loop/handlers/<resource>.py` — HTTP only. A function `(request: Request) -> Response` decorated with `@route(METHOD, regex)` from `hello_loop/router.py`. Parses the body, validates, calls a repo function, returns `Response.json(...)`, `Response.html(...)` or `Response.redirect(...)`. Never writes SQL.
- `hello_loop/repos/<table>.py` — SQL only. Plain functions taking a `sqlite3.Connection` (`request.conn`), parameterised queries, returning dicts. A domain exception such as `DuplicateUrl` is raised for conflicts; the handler maps it to a status code. No `repos/` module exists until the first item that needs one adds it.

`hello_loop/server.py` opens one connection per request and passes it in `Request.conn`. A new handler module is registered by importing it in `hello_loop/handlers/__init__.py`; forgetting that import is the usual cause of a 404 on a new route.

## Auth Middleware

None. The service is local-only and unauthenticated.

## Endpoint Catalog

Endpoint catalog: see `hello_loop/handlers/*.py`; every route is a `@route(...)` decorator. Conventions below apply to all of them.

## Error Handling

- JSON API errors are `{"error": "<message>"}` with the right status: 400 for invalid input (never 422), 404 for a missing record or unknown path, 405 for a known path with the wrong method, 409 for a duplicate, 413 for an oversize body.
- Validation raises `ValueError` with a message that is safe to show to a user; the handler turns it into a 400.
- The server catches anything else and returns a 500 `{"error": "internal error"}` without a traceback.

## Logging

`print`/`sys.stderr` only: the server logs one line per request to stderr (`log_message`). No logging framework. Repos do not log.

## External Integrations

None.

## File Naming

Lower-case snake_case modules. Handlers and repos are named after the resource they serve (`bookmarks.py`). Migrations are `NNNN_name.up.sql` and `NNNN_name.down.sql`. Tests are `tests/test_<topic>.py`.

## Verification Commands

- Tests: `python3 -m unittest discover -s tests -t .` (run from the project root; exits 0 and runs more than zero tests)
- One module: `python3 -m unittest tests.test_schema`
- Run check: `python3 scripts/verify_run.py /healthz`
- There is no linter. Keep to Python 3.9 syntax.

## Canonical Exemplar

- Handler: `hello_loop/handlers/health.py`
- Migration runner: `hello_loop/migrate.py` (and its tests, `tests/test_migrate.py`)
- Test with a live server and temp database: `tests/test_health.py`, using `HttpTestCase` from `tests/helpers.py`
- Repo layer: no exemplar yet. The first item that adds `hello_loop/repos/<table>.py` establishes it: plain functions over `sqlite3.Connection`, `?` placeholders, dict results.
