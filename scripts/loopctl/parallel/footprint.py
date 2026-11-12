"""Footprints: which items may run together, and whether an item stayed in its lane (report 11.5 item 3, 19.7 L-35, D-29).

Two questions, both answered from the plan (read at the run's base sha) and, for the second, from the item's real diff:

* MAY THESE TWO ITEMS RUN AT THE SAME TIME? `conflict(a, b)`. They may not when
    - their `touches.paths` globs may match a common file (`may_overlap`, deliberately conservative: when it cannot prove two globs are
      apart it says they overlap, so a wrong answer costs parallelism and never correctness),
    - either one declares no footprint at all (an item that does not say where it works is run alone), or
    - they need the same lane. The lanes are the "schema mutex that is always on" (a migration or `touches.schema`) and, the same way,
      a dependency change (`touches.deps_change`: two items editing one lockfile).
* DID THE ITEM STAY IN ITS LANE? `undeclared(changed, item)`. A footprint is a promise the scheduler acted on. The item's diff is the fact:
  a changed file that the item's globs do not cover (and that is not the item's own `docs/features/<slug>/` custody trail) is a lie, and
  the integrator refuses to merge it (`integrate.py`).

Glob language: the kit's own (`pr_prepare.glob_to_regex`): `**` crosses directories, `*` and `?` do not, a pattern with no slash matches
at any depth, and a pattern that names a directory covers what is under it (gitignore).
"""
from __future__ import annotations

import functools
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .. import common

__all__ = ["declared", "known", "lanes", "conflict", "may_overlap", "first_overlap", "covers", "undeclared", "custody_globs", "LANES"]

LANES = ("schema", "deps")


def declared(item: Dict[str, Any]) -> List[str]:
    """The safe globs the item's `touches.paths` declares (an unsafe one was already refused by `loopctl validate`; it is dropped here too)."""
    t = item.get("touches") if isinstance(item.get("touches"), dict) else {}
    paths = t.get("paths") if isinstance(t.get("paths"), list) else []
    return [p for p in paths if isinstance(p, str) and common.safe_glob(p)]


def known(item: Dict[str, Any]) -> bool:
    """True when the item says where it works. An item that does not is scheduled alone."""
    return bool(declared(item))


def lanes(item: Dict[str, Any]) -> FrozenSet[str]:
    """The exclusive lanes an item needs: `schema` for a migration or `touches.schema`, `deps` for `touches.deps_change`."""
    t = item.get("touches") if isinstance(item.get("touches"), dict) else {}
    out = set()
    if t.get("schema") is True or (isinstance(item.get("migrations"), list) and item["migrations"]):
        out.add("schema")
    if t.get("deps_change") is True:
        out.add("deps")
    return frozenset(out)


def custody_globs(item: Dict[str, Any]) -> List[str]:
    """What an item may always write besides its footprint: its own briefs, reports and reviews (the driver writes them, report 11.3)."""
    slug = item.get("slug")
    return ["docs/features/%s/**" % slug] if common.valid_slug(slug) else []


# -- glob overlap -----------------------------------------------------------------------------------------------------------

def _segments(glob: str) -> Tuple[str, ...]:
    g = glob.strip("/")
    segs = g.split("/")
    if len(segs) == 1 and segs[0] != "**":
        segs = ["**"] + segs                         # no slash: matches at any depth
    segs = ["**" if "**" in s else s for s in segs]   # `a**b` crosses directories: widen it (an over-estimate is the safe direction)
    if segs[-1] != "**":
        segs.append("**")                            # a pattern naming a directory covers what is under it
    out = []  # type: List[str]
    for s in segs:
        if s == "**" and out and out[-1] == "**":
            continue
        out.append(s)
    return tuple(out)


def _wild(s: str) -> bool:
    return "*" in s or "?" in s


def _seg_regex(s: str) -> "re.Pattern":
    return re.compile("^" + "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in s) + "$")


def _seg_overlap(x: str, y: str) -> bool:
    if x == y:
        return True
    wx, wy = _wild(x), _wild(y)
    if not wx and not wy:
        return False
    if not wx:
        return bool(_seg_regex(y).match(x))
    if not wy:
        return bool(_seg_regex(x).match(y))
    px, py = re.split(r"[*?]", x, maxsplit=1)[0], re.split(r"[*?]", y, maxsplit=1)[0]            # both are patterns: compare what is fixed at each end
    sx, sy = re.split(r"[*?]", x)[-1], re.split(r"[*?]", y)[-1]
    return (px.startswith(py) or py.startswith(px)) and (sx.endswith(sy) or sy.endswith(sx))


@functools.lru_cache(maxsize=4096)
def _overlap(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    memo = {}  # type: Dict[Tuple[int, int], bool]

    def go(i: int, j: int) -> bool:
        if (i, j) in memo:
            return memo[(i, j)]
        if i == len(a) and j == len(b):
            r = True
        elif i == len(a):
            r = all(s == "**" for s in b[j:])
        elif j == len(b):
            r = all(s == "**" for s in a[i:])
        elif a[i] == "**" and b[j] == "**":
            r = go(i + 1, j) or go(i, j + 1)
        elif a[i] == "**":
            r = go(i + 1, j) or go(i, j + 1)             # `**` takes none of b's segments, or one of them
        elif b[j] == "**":
            r = go(i, j + 1) or go(i + 1, j)
        else:
            r = _seg_overlap(a[i], b[j]) and go(i + 1, j + 1)
        memo[(i, j)] = r
        return r

    return go(0, 0)


def may_overlap(a: str, b: str) -> bool:
    """True if some path could match both globs. Conservative: it may say True for two globs that never meet, never False for two that do."""
    return _overlap(_segments(a), _segments(b))


def first_overlap(xs: Iterable[str], ys: Iterable[str]) -> Optional[Tuple[str, str]]:
    ys = list(ys)
    for x in xs:
        for y in ys:
            if may_overlap(x, y):
                return (x, y)
    return None


def conflict(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[str]:
    """Why two plan items may not run at the same time, or None. Reasons are fixed sentences plus the two globs (plan text, cleaned)."""
    shared = lanes(a) & lanes(b)
    if shared:
        return "both need the %s lane (the %s mutex)" % ("/".join(sorted(shared)), "/".join(sorted(shared)))
    if not known(a) or not known(b):
        return "an item that declares no touches.paths runs alone"
    hit = first_overlap(declared(a), declared(b))
    if hit:
        return "their footprints may meet: %s and %s" % (common.clean_text(hit[0], 60), common.clean_text(hit[1], 60))
    return None


# -- the item's diff against its promise ----------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def _regex(glob: str) -> Any:
    import pr_prepare
    return pr_prepare.glob_to_regex(glob)


def covers(globs: Sequence[str], path: str) -> bool:
    """True if `path` is matched by a glob, or lies under a directory a glob matches (gitignore: a directory pattern covers its contents)."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    for g in globs:
        rx = _regex(g)
        if any(rx.match(p) for p in prefixes):
            return True
    return False


def undeclared(changed: Sequence[str], item: Dict[str, Any]) -> List[str]:
    """The changed paths the item's footprint (and its custody trail) does not cover: a footprint lie. An item that declared nothing has
    nothing to lie about; it ran alone."""
    if not known(item):
        return []
    allow = declared(item) + custody_globs(item)
    return sorted(p for p in changed if not covers(allow, p))
