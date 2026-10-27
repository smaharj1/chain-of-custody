"""Shared plumbing: errors and exit codes, text hygiene, strict JSON, ids, the clock.

Exit codes of the command line (every subcommand uses these):

    0  done / clean
    1  the check ran and found a problem (validate errors, evidence not passed, drift found)
    2  usage error or internal error (also: anything fail-closed that cannot be explained better)
    3  refused by policy (Tier 2 is off, HALT is set, the lock is held, the canary is missing, a CAS lost
       every retry, the evidence gate is not passed)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["READ_ERRORS", "EXIT_OK", "EXIT_FINDINGS", "EXIT_USAGE", "EXIT_REFUSED", "LoopctlError", "UsageError", "Refused",
           "Finding", "ID_RE", "SLUG_RE", "NAME_RE", "AC_RE", "valid_item_id", "valid_slug", "valid_plan_name",
           "ac_id_for", "clean_text", "strict_loads", "canonical_json", "sha256_hex", "natural_key", "now",
           "iso", "set_clock", "safe_rel_path", "safe_glob", "md_cell", "md_text", "is_number"]

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3

# What reading a state file can raise. `json.loads` of a deeply nested file raises RecursionError, which is not a ValueError.
READ_ERRORS = (ValueError, OSError, RecursionError)


class LoopctlError(Exception):
    """An error with a message meant for the person at the terminal."""
    exit_code = EXIT_USAGE


class UsageError(LoopctlError):
    exit_code = EXIT_USAGE


class Refused(LoopctlError):
    """A precondition says no. Never retried silently."""
    exit_code = EXIT_REFUSED


class Finding(object):
    """A validation or check finding. `severity` is error | warn | info."""
    __slots__ = ("severity", "id", "message", "item")

    def __init__(self, severity: str, fid: str, message: str, item: Optional[str] = None) -> None:
        self.severity, self.id, self.message, self.item = severity, fid, message, item

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "severity": self.severity, "message": self.message}  # type: Dict[str, Any]
        if self.item is not None:
            d["item"] = self.item
        return d

    def __repr__(self) -> str:
        return "Finding(%s, %s%s)" % (self.severity, self.id, ", " + self.item if self.item else "")


# -- ids ------------------------------------------------------------------------------------------------

# An item id is alphanumeric only, so that `AC-<item>-<n>` splits unambiguously and the id is safe as a file name,
# a ref component and a JSON key. It is also unique case-insensitively (macOS file systems fold case).
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,23}\Z")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
NAME_RE = SLUG_RE
AC_RE = re.compile(r"^AC-([A-Za-z][A-Za-z0-9]{0,23})-([0-9]{1,3})\Z")


def valid_item_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def valid_slug(value: Any) -> bool:
    return isinstance(value, str) and bool(SLUG_RE.match(value)) and not value.endswith("-")


def valid_plan_name(value: Any) -> bool:
    return valid_slug(value)


def ac_id_for(item_id: str, n: int) -> str:
    return "AC-%s-%d" % (item_id, n)


# -- text hygiene -----------------------------------------------------------------------------------------

_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def clean_text(text: Any, limit: int = 500, keep_newlines: bool = False) -> str:
    """Printable text for a terminal, a markdown table or a commit message: control characters (ANSI escapes
    included), zero-width and bidi override characters are replaced, newlines folded, length capped."""
    s = text if isinstance(text, str) else str(text)
    if keep_newlines:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = "\n".join(_CONTROL_RE.sub("?", ln) for ln in s.split("\n"))
    else:
        s = _CONTROL_RE.sub("?", s.replace("\r", " ").replace("\n", " ").replace("\t", " "))
    if len(s) > limit:
        s = s[:max(0, limit - 3)] + "..."
    return s


def md_text(text: Any, limit: int = 600) -> str:
    """Free text for a markdown paragraph or heading: one line, no control characters, and no `<` or `>`, so it can neither open
    an HTML comment (the audit block's markers are comments) nor inject HTML."""
    return clean_text(text, limit).replace("<", "&lt;").replace(">", "&gt;")


def md_cell(text: Any, limit: int = 200) -> str:
    """Text safe inside a markdown table cell: no pipes, no line breaks, no HTML."""
    s = clean_text(text, limit)
    return s.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


# -- JSON ---------------------------------------------------------------------------------------------------

def _no_constant(name: str) -> Any:
    raise ValueError("JSON constant %s is not allowed" % name)


def _no_dupes(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key %r" % k)
        out[k] = v
    return out


def _depth(obj: Any, limit: int, level: int = 0) -> None:
    if level > limit:
        raise ValueError("JSON nested deeper than %d levels" % limit)
    if isinstance(obj, dict):
        for v in obj.values():
            _depth(v, limit, level + 1)
    elif isinstance(obj, list):
        for v in obj:
            _depth(v, limit, level + 1)


def strict_loads(text: str, max_depth: int = 12, max_bytes: int = 4 * 1024 * 1024) -> Any:
    """json.loads that refuses duplicate keys, NaN and Infinity, deep nesting and oversize input."""
    if len(text.encode("utf-8", "replace")) > max_bytes:
        raise ValueError("JSON larger than %d bytes" % max_bytes)
    obj = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant)
    _depth(obj, max_depth)
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_number(value: Any) -> bool:
    """A real, finite number (bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def natural_key(text: str) -> Tuple[Any, ...]:
    """Sort key so F2 < F10: digit runs compare as numbers."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|\D+", text))


# -- paths ----------------------------------------------------------------------------------------------------

def safe_rel_path(path: Any) -> bool:
    """A relative path that stays inside the repository: no absolute, no `..`, no backslash, no NUL, no leading
    dash, no `.git` component."""
    if not isinstance(path, str) or not path or len(path) > 300 or "\x00" in path or "\\" in path:
        return False
    if path.startswith(("/", "-", "~")) or re.match(r"^[A-Za-z]:", path):
        return False
    parts = path.split("/")
    return not any(p in ("", ".", "..") or p.lower() == ".git" for p in parts)


def safe_glob(pattern: Any) -> bool:
    """Like safe_rel_path, but `*`, `**` and `?` are allowed (a footprint or locked-path glob)."""
    if not isinstance(pattern, str) or not re.search(r"[^*?/]", pattern):
        return False        # a pattern made only of wildcards would claim the whole tree
    return safe_rel_path(pattern.rstrip("/") if pattern.endswith("/") and len(pattern) > 1 else pattern) \
        and not re.search(r"[\[\]{}]", pattern)


# -- the clock (tests replace it) -----------------------------------------------------------------------------------------------------

_clock = time.time  # type: Callable[[], float]


def set_clock(fn: Optional[Callable[[], float]]) -> None:
    global _clock
    _clock = fn or time.time


def now() -> float:
    return _clock()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() if ts is None else ts))
