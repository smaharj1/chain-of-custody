"""Autonomy levels L0-L5 and the effective-level computation (report §6.2-§6.3, IMP-21).

    effective = min(configured, every cap)

Caps are inputs: the doctor and other phases compute them (prerequisites, B5, platform,
evidence, version) and pass them in. This module only does the min and keeps the
reasons, so every phase prints the same thing. Two helpers encode the caps whose rule
IMP-21 states exactly (evidence, version range).

JSON form (doctor.v1 `level`): levels are the strings "L0".."L5".
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Union

__all__ = ["L0", "L1", "L2", "L3", "L4", "L5", "LEVEL_NAMES", "Cap", "EffectiveLevel",
           "parse_level", "level_name", "effective_level", "evidence_cap", "version_cap"]

L0, L1, L2, L3, L4, L5 = 0, 1, 2, 3, 4, 5

LEVEL_NAMES = {
    L0: "Assist",
    L1: "Supervised local loop",
    L2: "Push-branch",
    L3: "Draft-PR factory",
    L4: "Async factory",
    L5: "Low-risk auto-merge",
}


def parse_level(value: Union[int, str]) -> int:
    """Accept 3, "3", "L3", "l3". Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("not a level: %r" % (value,))
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        s = value.strip().upper()
        if s.startswith("L"):
            s = s[1:]
        if not s.isdigit():
            raise ValueError("not a level: %r" % (value,))
        n = int(s)
    else:
        raise ValueError("not a level: %r" % (value,))
    if not L0 <= n <= L5:
        raise ValueError("level out of range L0-L5: %r" % (value,))
    return n


def level_name(level: int) -> str:
    return "L%d" % level


class Cap(object):
    """A ceiling on the effective level, with the reason to print."""

    __slots__ = ("name", "level", "reason")

    def __init__(self, name: str, level: Union[int, str], reason: str) -> None:
        self.name = name
        self.level = parse_level(level)
        self.reason = reason

    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "level": level_name(self.level), "reason": self.reason}

    def __repr__(self) -> str:
        return "Cap(%r, L%d, %r)" % (self.name, self.level, self.reason)


def _as_cap(item: Any) -> Cap:
    if isinstance(item, Cap):
        return item
    if isinstance(item, Mapping):
        return Cap(item["name"], item["level"], item.get("reason", ""))
    name, level, reason = item
    return Cap(name, level, reason)


class EffectiveLevel(object):
    def __init__(self, configured: int, caps: List[Cap]) -> None:
        self.configured = configured
        self.caps = caps
        self.effective = min([configured] + [c.level for c in caps])

    @property
    def binding(self) -> List[Cap]:
        """Caps that actually lower the level, lowest first."""
        return sorted((c for c in self.caps if c.level < self.configured), key=lambda c: (c.level, c.name))

    def to_dict(self) -> Dict[str, Any]:
        return {"configured": level_name(self.configured), "effective": level_name(self.effective),
                "caps": [c.to_dict() for c in self.caps]}

    def explain(self) -> str:
        lines = ["configured %s, effective %s" % (level_name(self.configured), level_name(self.effective))]
        for c in self.caps:
            mark = "*" if c.level < self.configured else " "
            lines.append("%s %-24s %s  %s" % (mark, c.name, level_name(c.level), c.reason))
        return "\n".join(lines)


def effective_level(configured: Union[int, str], caps: Iterable[Any]) -> EffectiveLevel:
    """min(configured, caps...). `caps` are Cap objects, dicts or (name, level, reason)."""
    return EffectiveLevel(parse_level(configured), [_as_cap(c) for c in caps])


def evidence_cap(evidence_ok: bool, reason: str = "") -> Cap:
    """IMP-21: a missing or incomplete evidence.json caps at L3."""
    if evidence_ok:
        return Cap("evidence", L5, "evidence gate passed")
    return Cap("evidence", L3, reason or "docs/baseline/evidence.json absent or incomplete")


def version_cap(status: str, detail: str = "") -> Cap:
    """IMP-21 / report §14.2, from `semver.range_status`.

    unknown -> L1; tested_max 'none' (untested) -> L2; below min or above tested max -> L2
    (the report refuses L3+ in those cases); ok -> no cap.
    """
    table = {
        "unknown": (L1, "claude version could not be read; run the doctor on your machine"),
        "untested": (L2, "claude_code_tested_max is none: the canary has not passed"),
        "below_min": (L2, "claude version is below claude_code_min"),
        "above_tested": (L2, "claude version is above claude_code_tested_max; run the canary"),
        "ok": (L5, "claude version within the tested range"),
    }
    if status not in table:
        raise ValueError("unknown range status: %r" % (status,))
    level, text = table[status]
    return Cap("version", level, text + (" (" + detail + ")" if detail else ""))
