"""JSON API for bookmarks: POST/GET /api/bookmarks and GET /api/bookmarks/<id>."""
from __future__ import annotations

import json
from typing import Tuple

from ..repos import bookmarks as repo
from ..router import Request, Response, route

MAX_URL = 2048
MAX_TITLE = 200


def parse_new_bookmark(url: object, title: object) -> Tuple[str, str]:
    """Validate user input. Raises ValueError with a message safe to show to the user."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    url = url.strip()
    title = title.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    if len(url) > MAX_URL:
        raise ValueError("url is too long")
    if len(title) > MAX_TITLE:
        raise ValueError("title is too long")
    return url, title


@route("POST", r"/api/bookmarks")
def create_bookmark(request: Request) -> Response:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return Response.json(400, {"error": "request body must be valid JSON"})
    if not isinstance(payload, dict):
        return Response.json(400, {"error": "request body must be a JSON object"})
    try:
        url, title = parse_new_bookmark(payload.get("url"), payload.get("title"))
    except ValueError as exc:
        return Response.json(400, {"error": str(exc)})
    try:
        created = repo.create(request.conn, url, title)
    except repo.DuplicateUrl:
        return Response.json(409, {"error": "that url is already bookmarked"})
    return Response.json(201, created)


@route("GET", r"/api/bookmarks")
def list_bookmarks(request: Request) -> Response:
    return Response.json(200, {"bookmarks": repo.list_all(request.conn)})


@route("GET", r"/api/bookmarks/([^/]+)")
def get_bookmark(request: Request) -> Response:
    raw = request.groups[0]
    found = repo.get(request.conn, int(raw)) if raw.isdigit() else None
    if found is None:
        return Response.json(404, {"error": "bookmark not found"})
    return Response.json(200, found)
