"""AC-F3-1 .. AC-F3-4 against the real server and a temporary database."""
from __future__ import annotations

import urllib.parse

from tests.helpers import HttpTestCase


class Page(HttpTestCase):
    def get_page(self):
        status, headers, raw = self.request("GET", "/")
        return status, headers, raw.decode("utf-8")

    def post_form(self, **fields):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        return self.request("POST", "/bookmarks", body, "application/x-www-form-urlencoded")

    def test_lists_bookmarks_newest_first_as_links(self):  # AC-F3-1
        self.request_json("POST", "/api/bookmarks", {"url": "https://a.test/", "title": "First"})
        self.request_json("POST", "/api/bookmarks", {"url": "https://b.test/", "title": "Second"})
        status, headers, page = self.get_page()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn('<a href="https://a.test/">First</a>', page)
        self.assertLess(page.index(">Second</a>"), page.index(">First</a>"))

    def test_empty_state(self):  # AC-F3-2
        _, _, page = self.get_page()
        self.assertIn("No bookmarks yet.", page)

    def test_titles_and_urls_are_escaped(self):  # AC-F3-3
        self.post_form(url="https://x.test/?a=1&b=2", title="<script>alert(1)</script>")
        _, _, page = self.get_page()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("a=1&amp;b=2", page)

    def test_form_posts_and_redirects(self):  # AC-F3-4
        _, _, page = self.get_page()
        self.assertIn('<form method="post" action="/bookmarks">', page)
        self.assertIn('name="url"', page)
        self.assertIn('name="title"', page)
        status, headers, _ = self.post_form(url="https://ok.test/", title="Fine")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        _, _, page = self.get_page()
        self.assertIn(">Fine</a>", page)

    def test_invalid_form_is_400_with_the_message_and_saves_nothing(self):  # AC-F3-4
        status, _, raw = self.post_form(url="notaurl", title="Bad")
        self.assertEqual(status, 400)
        self.assertIn("url must start with http:// or https://", raw.decode("utf-8"))
        _, listing = self.request_json("GET", "/api/bookmarks")
        self.assertEqual(listing["bookmarks"], [])
