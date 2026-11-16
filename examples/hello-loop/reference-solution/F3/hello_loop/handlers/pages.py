"""Server-rendered HTML: GET / lists bookmarks, POST /bookmarks adds one from the form."""
from __future__ import annotations

import html
import urllib.parse
from typing import List, Optional

from ..repos import bookmarks as repo
from ..router import Request, Response, route
from .bookmarks import parse_new_bookmark

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Bookmarks</title></head>
<body>
<h1>Bookmarks</h1>
{error}<form method="post" action="/bookmarks">
<label>URL <input type="text" name="url" value="{url}"></label>
<label>Title <input type="text" name="title" value="{title}"></label>
<button type="submit">Add</button>
</form>
{listing}
</body>
</html>
"""


def _listing(items: List[dict]) -> str:
    if not items:
        return "<p>No bookmarks yet.</p>"
    rows = ['<li><a href="%s">%s</a></li>' % (html.escape(b["url"], quote=True), html.escape(b["title"]))
            for b in items]
    return "<ul>\n%s\n</ul>" % "\n".join(rows)


def render(request: Request, error: Optional[str] = None, url: str = "", title: str = "") -> str:
    err = '<p class="error">%s</p>\n' % html.escape(error) if error else ""
    return PAGE.format(error=err, url=html.escape(url, quote=True), title=html.escape(title, quote=True),
                       listing=_listing(repo.list_all(request.conn)))


@route("GET", r"/")
def index(request: Request) -> Response:
    return Response.html(200, render(request))


@route("POST", r"/bookmarks")
def add_bookmark(request: Request) -> Response:
    form = urllib.parse.parse_qs(request.body.decode("utf-8", "replace"), keep_blank_values=True)
    url = (form.get("url") or [""])[0]
    title = (form.get("title") or [""])[0]
    try:
        clean_url, clean_title = parse_new_bookmark(url, title)
        repo.create(request.conn, clean_url, clean_title)
    except ValueError as exc:
        return Response.html(400, render(request, str(exc), url, title))
    except repo.DuplicateUrl:
        return Response.html(400, render(request, "that url is already bookmarked", url, title))
    return Response.redirect("/")
