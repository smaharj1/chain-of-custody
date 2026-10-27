"""HTTP server: `python3 -m hello_loop.server` (PORT, DATABASE_URL from the environment)."""
from __future__ import annotations

import sys
import urllib.parse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import config, db
from . import handlers  # noqa: F401  (registers routes)
from .router import Request, Response, dispatch

MAX_BODY = 1024 * 1024


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "hello-loop"

    def _handle(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._send(Response.json(413, {"error": "request body too large"}))
            return
        body = self.rfile.read(length) if length else b""
        parts = urllib.parse.urlsplit(self.path)
        try:
            with closing(db.connect()) as conn:
                request = Request(self.command, parts.path, urllib.parse.parse_qs(parts.query),
                                  {k.lower(): v for k, v in self.headers.items()}, body, conn)
                response = dispatch(request)
        except Exception as exc:  # last resort: never leak a traceback to the client
            sys.stderr.write("unhandled error: %r\n" % (exc,))
            response = Response.json(500, {"error": "internal error"})
        self._send(response)

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(response.body)

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)


def main(argv: Optional[list] = None) -> int:
    server = make_server(config.HOST, config.port())
    print("listening on http://%s:%d" % server.server_address[:2], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
