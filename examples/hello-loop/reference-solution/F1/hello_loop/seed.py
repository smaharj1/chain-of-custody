"""Sample data: `python3 -m hello_loop.seed`. Safe to run any number of times."""
from __future__ import annotations

import sys
from contextlib import closing
from datetime import datetime, timezone
from typing import List, Optional, Tuple

SAMPLES = [
    ("https://www.python.org/", "Python"),
    ("https://sqlite.org/", "SQLite"),
    ("https://developer.mozilla.org/", "MDN Web Docs"),
]  # type: List[Tuple[str, str]]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(conn) -> int:
    """Insert the sample bookmarks that are not there yet. Returns how many were added."""
    added = 0
    for url, title in SAMPLES:
        cur = conn.execute("INSERT OR IGNORE INTO bookmarks (url, title, created_at) VALUES (?, ?, ?)",
                           (url, title, now_iso()))
        added += cur.rowcount
    conn.commit()
    return added


def main(argv: Optional[list] = None) -> int:
    from . import db
    with closing(db.connect()) as conn:
        print("seeded %d bookmark(s)" % seed(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
