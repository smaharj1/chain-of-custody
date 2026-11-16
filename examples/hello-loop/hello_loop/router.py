"""Request, Response and a tiny route table. Handlers register themselves with @route."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple


class Request(object):
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers: Dict[str, str],
                 body: bytes, conn: sqlite3.Connection) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.conn = conn
        self.groups = ()  # type: Tuple[str, ...]


class Response(object):
    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}

    @classmethod
    def json(cls, status: int, obj: Any) -> "Response":
        return cls(status, json.dumps(obj).encode("utf-8"), "application/json")

    @classmethod
    def html(cls, status: int, text: str) -> "Response":
        return cls(status, text.encode("utf-8"), "text/html; charset=utf-8")

    @classmethod
    def redirect(cls, location: str, status: int = 303) -> "Response":
        return cls(status, b"", "text/plain; charset=utf-8", {"Location": location})


Handler = Callable[[Request], Response]
ROUTES = []  # type: List[Tuple[str, Any, Handler]]


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """Register `handler` for `method` and the regex `pattern` (matched against the whole path)."""
    compiled = re.compile("^" + pattern + "$")

    def decorate(handler: Handler) -> Handler:
        ROUTES.append((method, compiled, handler))
        return handler

    return decorate


def dispatch(request: Request) -> Response:
    path_matched = False
    for method, rx, handler in ROUTES:
        m = rx.match(request.path)
        if not m:
            continue
        path_matched = True
        if method == request.method:
            request.groups = m.groups()
            return handler(request)
    if path_matched:
        return Response.json(405, {"error": "method not allowed"})
    return Response.json(404, {"error": "not found"})
