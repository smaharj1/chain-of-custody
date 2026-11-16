"""Regular-file-only reads for files an agent can write.

A hostile repository can replace `.claude/settings.json`, `CODEOWNERS`, a workflow or `loop.config.md`
with a named pipe (a plain `open()` then blocks forever, which would hang a hook or the doctor), a
symlink to a device, or a multi-gigabyte file. Every read of repo-controlled or home-controlled content
goes through here: open with O_NONBLOCK, `fstat` the descriptor, refuse anything that is not a regular
file, and cap the size.
"""
from __future__ import annotations

import os
import stat
from typing import Optional, Tuple

__all__ = ["read_bytes", "read_text", "read_text_status", "NOT_REGULAR", "MISSING"]

NOT_REGULAR = "not a regular file"
MISSING = "missing"


def read_bytes(path: str, limit: int = 1 << 20) -> Tuple[Optional[bytes], Optional[str]]:
    """(data, problem). problem is None on success, MISSING if the path does not exist, NOT_REGULAR for a
    pipe, device, socket or directory, or an OS error text."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, MISSING
    except NotADirectoryError:
        return None, MISSING
    except OSError as exc:
        return None, "unreadable: %s" % exc.strerror
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, NOT_REGULAR
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), None
    except OSError as exc:
        return None, "unreadable: %s" % exc.strerror
    finally:
        os.close(fd)


def read_text_status(path: str, limit: int = 1 << 20) -> Tuple[Optional[str], Optional[str]]:
    data, problem = read_bytes(path, limit)
    if data is None:
        return None, problem
    return data.decode("utf-8", "replace"), None


def read_text(path: str, limit: int = 1 << 20) -> Optional[str]:
    """The text of a regular file, or None (missing, not regular, unreadable)."""
    return read_text_status(path, limit)[0]
