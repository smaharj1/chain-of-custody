"""Test helpers: a throwaway SQLite database and a real HTTP server on an ephemeral port."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from hello_loop import config, db, migrate, server


class DbTestCase(unittest.TestCase):
    """Sets DATABASE_URL to a fresh temp file, migrates it, and restores the environment."""

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="hello-loop-test-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.db_path = os.path.join(self._dir, "test.db")
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "file:" + self.db_path
        self.addCleanup(self._restore, previous)
        conn = db.connect()
        try:
            migrate.migrate_up(conn, config.MIGRATIONS_DIR)
        finally:
            conn.close()

    @staticmethod
    def _restore(previous: Optional[str]) -> None:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    def connect(self):
        return db.connect()


class HttpTestCase(DbTestCase):
    """DbTestCase plus the real server running in a thread on 127.0.0.1:<free port>."""

    def setUp(self) -> None:
        super().setUp()
        self.httpd = server.make_server("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, thread)

    def _stop(self, thread: threading.Thread) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        thread.join(5)

    def request(self, method: str, path: str, body: Optional[bytes] = None,
                content_type: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def request_json(self, method: str, path: str, payload: Any = None) -> Tuple[int, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, _headers, raw = self.request(method, path, body, "application/json" if body else None)
        return status, json.loads(raw.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None
