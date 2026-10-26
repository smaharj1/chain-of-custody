"""Migration runner: `python3 -m hello_loop.migrate [--down]`.

Migrations are pairs of files in hello_loop/migrations/:

    NNNN_name.up.sql      applied in id order
    NNNN_name.down.sql    undoes the matching up migration

Applied ids are recorded in the `schema_migrations` table. `--down` reverts the most
recently applied migration only. Every migration runs in one transaction together with
its schema_migrations bookkeeping, so a failing migration leaves nothing half-applied.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import List, Optional

from . import config, db

UP = ".up.sql"
DOWN = ".down.sql"
_ID_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")


def available(directory: str) -> List[str]:
    ids = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(UP):
            mid = name[: -len(UP)]
            if not _ID_RE.match(mid):
                raise ValueError("bad migration file name: %s" % name)
            ids.append(mid)
    return ids


def applied(conn: sqlite3.Connection) -> List[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    return [r[0] for r in conn.execute("SELECT id FROM schema_migrations ORDER BY id")]


def _run(conn: sqlite3.Connection, sql: str, bookkeeping: str) -> None:
    script = "BEGIN;\n%s\n%s\nCOMMIT;" % (sql, bookkeeping)
    try:
        conn.executescript(script)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _read(directory: str, mid: str, suffix: str) -> str:
    with open(os.path.join(directory, mid + suffix), "r", encoding="utf-8") as fh:
        return fh.read()


def migrate_up(conn: sqlite3.Connection, directory: str) -> List[str]:
    done = set(applied(conn))
    newly = []  # type: List[str]
    for mid in available(directory):
        if mid in done:
            continue
        _run(conn, _read(directory, mid, UP), "INSERT INTO schema_migrations (id) VALUES ('%s');" % mid)
        newly.append(mid)
    return newly


def migrate_down(conn: sqlite3.Connection, directory: str) -> Optional[str]:
    done = applied(conn)
    if not done:
        return None
    mid = done[-1]
    path = os.path.join(directory, mid + DOWN)
    if not os.path.isfile(path):
        raise FileNotFoundError("no down migration for %s" % mid)
    _run(conn, _read(directory, mid, DOWN), "DELETE FROM schema_migrations WHERE id = '%s';" % mid)
    return mid


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in ([], ["--down"]):
        print("usage: python3 -m hello_loop.migrate [--down]", file=sys.stderr)
        return 2
    conn = db.connect()
    try:
        if argv == ["--down"]:
            mid = migrate_down(conn, config.MIGRATIONS_DIR)
            print("reverted %s" % mid if mid else "nothing to revert")
        else:
            newly = migrate_up(conn, config.MIGRATIONS_DIR)
            print("applied %s" % ", ".join(newly) if newly else "up to date")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
