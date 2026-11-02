"""SQLite connection helper. Production is SQLite too, so dev and prod share a dialect."""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from . import config


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or config.database_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
