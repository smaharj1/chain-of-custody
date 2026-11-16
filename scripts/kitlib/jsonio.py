"""Atomic file writes and exclusive create (report §10.3, §19.7 Lock).

* `atomic_write_*`: write a temp file in the same directory, fsync, rename over
  the target, fsync the directory. Readers see the old or the new content, never a
  partial file.
* `create_exclusive`: create a file only if it does not exist, with its full
  content visible from the first instant. It writes a temp file, then `os.link`s it
  to the target (link fails with EEXIST if the target exists), so a concurrent
  reader never sees an empty file. If the filesystem cannot hard-link, it falls
  back to `open(O_CREAT|O_EXCL)`, which is still exclusive but exposes a short
  window with an empty file; readers must treat an unparsable lock as "held".
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

__all__ = ["atomic_write_bytes", "atomic_write_text", "atomic_write_json",
           "create_exclusive", "try_create_exclusive", "read_json", "read_json_opt",
           "ensure_dir", "fsync_dir"]


def ensure_dir(path: str, mode: int = 0o700) -> str:
    os.makedirs(path, mode=mode, exist_ok=True)
    return path


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_temp(directory: str, name: str, data: bytes, mode: int) -> str:
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + name + ".", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink_quiet(tmp)
        raise
    os.close(fd)
    return tmp


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    path = os.path.abspath(path)
    directory, name = os.path.split(path)
    tmp = _write_temp(directory, name, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    fsync_dir(directory)


def atomic_write_text(path: str, text: str, mode: int = 0o644) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write_json(path: str, obj: Any, mode: int = 0o644) -> None:
    atomic_write_bytes(path, _dump(obj), mode)


def create_exclusive(path: str, data: bytes, mode: int = 0o644) -> None:
    """Create `path` with `data` iff it does not exist. Raises FileExistsError otherwise."""
    path = os.path.abspath(path)
    directory, name = os.path.split(path)
    tmp = _write_temp(directory, name, data, mode)
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except OSError:
            # Hard links unsupported here: fall back to O_EXCL on the target.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        _unlink_quiet(tmp)
    fsync_dir(directory)


def try_create_exclusive(path: str, data: bytes, mode: int = 0o644) -> bool:
    """True if this call created the file, False if it already existed."""
    try:
        create_exclusive(path, data, mode)
        return True
    except FileExistsError:
        return False


def read_json(path: str) -> Any:
    """Strict read: raises FileNotFoundError or ValueError (JSON decode)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except RecursionError:
        # A deeply nested file overflows the parser's stack. It is unreadable, not an interpreter fault: callers catch ValueError.
        raise ValueError("JSON nested too deeply to read")


def read_json_opt(path: str, default: Optional[Any] = None) -> Any:
    """`default` if the file is missing; still raises ValueError on corrupt content."""
    try:
        return read_json(path)
    except FileNotFoundError:
        return default
