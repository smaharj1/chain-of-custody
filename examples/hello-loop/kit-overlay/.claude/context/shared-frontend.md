## Stack

Server-rendered HTML from Python. No JavaScript, no CSS framework, no bundler, no package manager.

## State Management Rules

There is no client state. The database is the only state, and every page is rendered from it on each request.

## Client Architecture

The browser talks to the server with plain navigation and form posts. A successful form post answers 303 and redirects, so a refresh never resubmits it.

## Typography

Browser defaults.

## Loading / Error / Empty States

- Empty: a list with no items shows the text `No bookmarks yet.`
- Error: invalid form input re-renders the page with status 400 and the validation message in a `<p class="error">`.
- Loading: not applicable (the page is rendered in one response).

## Accessibility Baseline

- Every input has a visible `<label>`.
- The page has one `<h1>` and a `lang` attribute on `<html>`.
- Links use the title as their text, never "click here".

## Escaping

Everything a visitor typed that is written into HTML (titles, urls, form values echoed back) goes through `html.escape` (`quote=True` inside attributes). A raw `<script>` tag in the output is always a bug.

## Design System / Component Library

None.
