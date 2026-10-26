"""Data access for bookmarks. Plain functions over a sqlite3.Connection; SQL is parameterised."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class DuplicateUrl(Exception):
    """The url is already bookmarked."""


def _to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"], "url": row["url"], "title": row["title"], "created_at": row["created_at"]}


def create(conn: sqlite3.Connection, url: str, title: str) -> Dict[str, Any]:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cur = conn.execute("INSERT INTO bookmarks (url, title, created_at) VALUES (?, ?, ?)",
                           (url, title, created_at))
    except sqlite3.IntegrityError:
        raise DuplicateUrl(url)
    conn.commit()
    return {"id": cur.lastrowid, "url": url, "title": title, "created_at": created_at}


def list_all(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Newest first."""
    return [_to_dict(r) for r in conn.execute("SELECT * FROM bookmarks ORDER BY id DESC")]


def get(conn: sqlite3.Connection, bookmark_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
    return _to_dict(row) if row else None
