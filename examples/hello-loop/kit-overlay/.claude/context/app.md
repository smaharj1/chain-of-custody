## What This App Is

The user-facing part of hello-loop is a single server-rendered HTML page. A visitor sees the list of bookmarks and can add one with a form. There is no JavaScript, no build step and no client-side state.

## Routes

- `GET /` — the bookmarks page (item F3)
- `POST /bookmarks` — the add form's target; redirects to `/` on success (item F3)
- The JSON API under `/api/` is backend territory (item F2); the page does not call it, it uses the same repo functions.

## Layout

One column, browser-default styling: a heading, the add form, then the list (or the empty-state text). No responsive rules.

## Key Interactions

- View the list, newest first. Each bookmark is a link to its url with the title as text.
- Add a bookmark with the form (`url`, `title`). Success is a 303 redirect to `/`. Invalid input re-renders the page with status 400, the message shown, and what the visitor typed kept in the fields.

## Components

Components: see `hello_loop/handlers/pages.py` once it exists. Pages are built with Python string templates and `html.escape`; there is no template engine.

## Auth States

Single state: anonymous. There is no login.

## Responsive Breakpoints

None.

## Verification Commands

- Tests: `python3 -m unittest discover -s tests -t .`
- Page check: `python3 scripts/verify_run.py /` (boots the app, expects 200)
- There is no linter.

## Frontend Stack

None beyond the standard library.
