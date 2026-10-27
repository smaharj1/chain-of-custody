"""GET /healthz: the liveness probe the verify-run harness polls."""
from __future__ import annotations

from ..router import Request, Response, route


@route("GET", r"/healthz")
def healthz(request: Request) -> Response:
    return Response.json(200, {"status": "ok"})
