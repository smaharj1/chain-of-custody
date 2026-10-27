"""Semantic versions and the Claude Code version range (report §14, IMP-8, IMP-21).

`parse("2.0.0-rc.1")` handles prerelease tags; `compare` follows SemVer 2.0.0
precedence (build metadata ignored). `range_status` is what the doctor and
SessionStart use to compare `claude --version` with the range declared in
`.claude/KIT_VERSION`.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

__all__ = ["Version", "parse", "try_parse", "parse_leading", "compare", "in_range",
           "range_status", "parse_kit_version_file"]

_NUM = r"(0|[1-9][0-9]*)"
_IDENT = r"[0-9A-Za-z-]+"
_SEMVER_RE = re.compile(
    r"^" + _NUM + r"\." + _NUM + r"\." + _NUM
    + r"(?:-(" + _IDENT + r"(?:\." + _IDENT + r")*))?"
    + r"(?:\+(" + _IDENT + r"(?:\." + _IDENT + r")*))?$"
)
_LEADING_RE = re.compile(r"(?<![0-9A-Za-z.])v?([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)")


class Version(object):
    """An immutable semantic version. Orderable and hashable (build metadata ignored)."""

    __slots__ = ("major", "minor", "patch", "prerelease", "build")

    def __init__(self, major: int, minor: int, patch: int,
                 prerelease: Tuple[str, ...] = (), build: Tuple[str, ...] = ()) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.build = tuple(build)

    def _key(self) -> tuple:
        # A release sorts after any of its prereleases: (…, 1, ()) > (…, 0, ids).
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.prerelease)
        return (self.major, self.minor, self.patch, 0, ids)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __ge__(self, other: "Version") -> bool:
        return self._key() >= other._key()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        s = "%d.%d.%d" % (self.major, self.minor, self.patch)
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + ".".join(self.build)
        return s

    def __repr__(self) -> str:
        return "Version(%r)" % str(self)


def parse(text: str) -> Version:
    """Strict SemVer 2.0.0 parse. Raises ValueError."""
    m = _SEMVER_RE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError("not a semantic version: %r" % (text,))
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    for p in pre:
        if p.isdigit() and len(p) > 1 and p.startswith("0"):
            raise ValueError("numeric prerelease identifier has a leading zero: %r" % (text,))
    build = tuple(m.group(5).split(".")) if m.group(5) else ()
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, build)


def try_parse(text: Optional[str]) -> Optional[Version]:
    if text is None:
        return None
    try:
        return parse(text)
    except ValueError:
        return None


def parse_leading(text: str) -> Optional[Version]:
    """Find the first version token in free text, e.g. '2.1.302 (Claude Code)'."""
    if not isinstance(text, str):
        return None
    for m in _LEADING_RE.finditer(text):
        v = try_parse(m.group(1))
        if v is not None:
            return v
    return None


def compare(a, b) -> int:
    """-1, 0 or 1. Accepts Version or str."""
    va = a if isinstance(a, Version) else parse(a)
    vb = b if isinstance(b, Version) else parse(b)
    ka, kb = va._key(), vb._key()
    return (ka > kb) - (ka < kb)


def in_range(version, minimum=None, maximum=None) -> bool:
    """Inclusive range check; either bound may be None."""
    if minimum is not None and compare(version, minimum) < 0:
        return False
    if maximum is not None and compare(version, maximum) > 0:
        return False
    return True


# Statuses returned by range_status (the doctor maps them to level caps, IMP-21).
UNKNOWN = "unknown"            # version could not be read: caps at L1
BELOW_MIN = "below_min"        # older than claude_code_min: refuse at L3+
UNTESTED = "untested"          # claude_code_tested_max is 'none': caps at L2
ABOVE_TESTED = "above_tested"  # newer than claude_code_tested_max: refuse at L3+
OK = "ok"


def range_status(version: Optional[str], minimum: Optional[str], tested_max: Optional[str]) -> str:
    """Compare an installed Claude Code version with the declared range (report §14.2).

    `version` is the text of `claude --version` or a bare version, or None when the
    binary could not be run. `tested_max` of None, '' or 'none' means the canary has
    never passed.
    """
    v = parse_leading(version) if version else None
    if v is None:
        return UNKNOWN
    lo = try_parse(minimum) if minimum and minimum.strip().lower() != "none" else None
    if lo is not None and v < lo:
        return BELOW_MIN
    if tested_max is None or not str(tested_max).strip() or str(tested_max).strip().lower() == "none":
        return UNTESTED
    hi = try_parse(str(tested_max).strip())
    if hi is None:
        return UNTESTED
    if v > hi:
        return ABOVE_TESTED
    return OK


def parse_kit_version_file(text: str) -> Dict[str, Optional[str]]:
    """Parse `.claude/KIT_VERSION`.

    v1 is a single line `1.2.0 (2026-07-30)`. v2 adds `claude_code_min: 2.1.302` and
    `claude_code_tested_max: none` lines (IMP-8). Returns
    {version, date, claude_code_min, claude_code_tested_max}; absent fields are None.
    """
    out = {"version": None, "date": None, "claude_code_min": None,
           "claude_code_tested_max": None}  # type: Dict[str, Optional[str]]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("KIT_VERSION is empty")
    m = re.match(r"^(\S+)(?:\s+\((.*)\))?\s*$", lines[0])
    if not m or try_parse(m.group(1)) is None:
        raise ValueError("KIT_VERSION first line must be '<semver> (<date>)': %r" % lines[0])
    out["version"] = m.group(1)
    out["date"] = m.group(2)
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        k = k.strip()
        if sep and k in ("claude_code_min", "claude_code_tested_max"):
            out[k] = v.strip()
    return out
