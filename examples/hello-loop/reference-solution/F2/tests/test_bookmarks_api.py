"""AC-F2-1 .. AC-F2-5 against the real server and a temporary database."""
from __future__ import annotations

from tests.helpers import HttpTestCase

GOOD = {"url": "https://example.com/", "title": "Example"}


class BookmarksApi(HttpTestCase):
    def test_create_returns_201_and_the_record(self):  # AC-F2-1
        status, body = self.request_json("POST", "/api/bookmarks", GOOD)
        self.assertEqual(status, 201)
        self.assertEqual(sorted(body), ["created_at", "id", "title", "url"])
        self.assertEqual((body["url"], body["title"]), (GOOD["url"], GOOD["title"]))
        self.assertRegex(body["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_list_is_newest_first(self):  # AC-F2-2
        for n in range(3):
            self.request_json("POST", "/api/bookmarks", {"url": "https://e.test/%d" % n, "title": "T%d" % n})
        status, body = self.request_json("GET", "/api/bookmarks")
        self.assertEqual(status, 200)
        self.assertEqual([b["title"] for b in body["bookmarks"]], ["T2", "T1", "T0"])

    def test_invalid_input_is_400_with_an_error_message(self):  # AC-F2-3
        bad = [
            {"url": "https://e.test/", "title": ""},
            {"url": "https://e.test/", "title": "   "},
            {"url": "https://e.test/"},
            {"title": "No url"},
            {"url": "ftp://e.test/", "title": "T"},
            {"url": "javascript:alert(1)", "title": "T"},
            ["not", "an", "object"],
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                status, body = self.request_json("POST", "/api/bookmarks", payload)
                self.assertEqual(status, 400)
                self.assertTrue(body["error"])
        status, _, raw = self.request("POST", "/api/bookmarks", b"{not json", "application/json")
        self.assertEqual(status, 400)
        status, body = self.request_json("GET", "/api/bookmarks")
        self.assertEqual(body["bookmarks"], [])

    def test_duplicate_url_is_409_and_adds_no_row(self):  # AC-F2-4
        self.request_json("POST", "/api/bookmarks", GOOD)
        status, body = self.request_json("POST", "/api/bookmarks", dict(GOOD, title="Other"))
        self.assertEqual(status, 409)
        self.assertTrue(body["error"])
        _, listing = self.request_json("GET", "/api/bookmarks")
        self.assertEqual(len(listing["bookmarks"]), 1)

    def test_get_one_and_404s(self):  # AC-F2-5
        _, created = self.request_json("POST", "/api/bookmarks", GOOD)
        status, body = self.request_json("GET", "/api/bookmarks/%d" % created["id"])
        self.assertEqual((status, body), (200, created))
        for path in ("/api/bookmarks/9999", "/api/bookmarks/abc"):
            with self.subTest(path=path):
                status, body = self.request_json("GET", path)
                self.assertEqual(status, 404)
                self.assertTrue(body["error"])
