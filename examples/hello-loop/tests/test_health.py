"""Smoke test: the server boots and answers /healthz."""
from __future__ import annotations

from tests.helpers import HttpTestCase


class Health(HttpTestCase):
    def test_healthz_returns_ok(self):
        status, body = self.request_json("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_path_is_json_404(self):
        status, body = self.request_json("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_wrong_method_is_405(self):
        status, _ = self.request_json("POST", "/healthz", {})
        self.assertEqual(status, 405)
