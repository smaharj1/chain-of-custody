"""Runtime settings, read from the environment.

DATABASE_URL  `file:<path>` to a SQLite file; a relative path is relative to the project root.
              Default: file:.data/dev.db
PORT          TCP port for the dev server. Default: 8000
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "127.0.0.1"
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
DEFAULT_DATABASE_URL = "file:.data/dev.db"


def database_path() -> str:
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if not url.startswith("file:"):
        raise ValueError("DATABASE_URL must be file:<path> (SQLite), got %r" % url)
    path = url[len("file:"):]
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    return path


def port() -> int:
    return int(os.environ.get("PORT", "8000"))
