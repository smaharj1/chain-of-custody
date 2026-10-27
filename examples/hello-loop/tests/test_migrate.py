"""The migration runner, exercised on a temporary migrations directory."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from hello_loop import migrate


class Migrate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hello-loop-mig-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.write("0001_one", "CREATE TABLE one (id INTEGER PRIMARY KEY);", "DROP TABLE one;")
        self.write("0002_two", "CREATE TABLE two (id INTEGER PRIMARY KEY);", "DROP TABLE two;")

    def write(self, mid, up, down=None):
        with open(os.path.join(self.dir, mid + migrate.UP), "w") as fh:
            fh.write(up)
        if down is not None:
            with open(os.path.join(self.dir, mid + migrate.DOWN), "w") as fh:
                fh.write(down)

    def tables(self):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))

    def test_up_applies_in_order_and_is_idempotent(self):
        self.assertEqual(migrate.migrate_up(self.conn, self.dir), ["0001_one", "0002_two"])
        self.assertEqual(migrate.migrate_up(self.conn, self.dir), [])
        self.assertEqual(self.tables(), ["one", "schema_migrations", "two"])

    def test_down_reverts_only_the_latest(self):
        migrate.migrate_up(self.conn, self.dir)
        self.assertEqual(migrate.migrate_down(self.conn, self.dir), "0002_two")
        self.assertEqual(self.tables(), ["one", "schema_migrations"])
        self.assertEqual(migrate.applied(self.conn), ["0001_one"])

    def test_down_without_a_down_file_fails_loudly(self):
        self.write("0003_three", "CREATE TABLE three (id INTEGER PRIMARY KEY);")
        migrate.migrate_up(self.conn, self.dir)
        with self.assertRaises(FileNotFoundError):
            migrate.migrate_down(self.conn, self.dir)

    def test_failing_migration_leaves_nothing_half_applied(self):
        self.write("0003_bad", "CREATE TABLE three (id INTEGER PRIMARY KEY); THIS IS NOT SQL;")
        with self.assertRaises(sqlite3.Error):
            migrate.migrate_up(self.conn, self.dir)
        self.assertNotIn("three", self.tables())
        self.assertEqual(migrate.applied(self.conn), ["0001_one", "0002_two"])
