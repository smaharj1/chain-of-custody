"""AC-F1-1 .. AC-F1-4: the bookmarks table, its migration, the down migration and the seed."""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

from hello_loop import config, migrate, seed
from tests.helpers import DbTestCase


class Schema(DbTestCase):
    def columns(self, conn):
        return {r["name"]: r for r in conn.execute("PRAGMA table_info(bookmarks)")}

    def test_table_has_the_agreed_columns(self):  # AC-F1-1
        with closing(self.connect()) as conn:
            cols = self.columns(conn)
        self.assertEqual(sorted(cols), ["created_at", "id", "title", "url"])
        self.assertEqual(cols["id"]["type"].upper(), "INTEGER")
        self.assertEqual(cols["id"]["pk"], 1)
        for name in ("url", "title", "created_at"):
            self.assertEqual(cols[name]["type"].upper(), "TEXT", name)
            self.assertEqual(cols[name]["notnull"], 1, name)

    def test_url_is_unique(self):  # AC-F1-1
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO bookmarks (url, title, created_at) VALUES ('https://a.test/', 'A', 'x')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO bookmarks (url, title, created_at) VALUES ('https://a.test/', 'B', 'y')")

    def test_migrate_twice_records_the_migration_once(self):  # AC-F1-2
        with closing(self.connect()) as conn:
            self.assertEqual(migrate.migrate_up(conn, config.MIGRATIONS_DIR), [])
            ids = [r[0] for r in conn.execute("SELECT id FROM schema_migrations")]
        self.assertEqual(ids.count("0001_create_bookmarks"), 1)

    def test_down_removes_table_and_record(self):  # AC-F1-3
        with closing(self.connect()) as conn:
            self.assertEqual(migrate.migrate_down(conn, config.MIGRATIONS_DIR), "0001_create_bookmarks")
            self.assertEqual(self.columns(conn), {})
            self.assertEqual(migrate.applied(conn), [])

    def test_seed_inserts_three_and_is_idempotent(self):  # AC-F1-4
        with closing(self.connect()) as conn:
            self.assertEqual(seed.seed(conn), 3)
            self.assertEqual(seed.seed(conn), 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 3)
