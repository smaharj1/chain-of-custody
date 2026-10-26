"""`<git-common-dir>/loopctl/locked_paths.json`: the globs engineers may not edit (read by guards/g_file.py, rule `engineer-locked`).

Shape: `{"globs": [...], "sources": {"plan": [...], "footprints": [...], "<gate>": [...]}}`. The guards read only `globs`.

* `plan`: `plan/**`. The plan definition is immutable on main and changes through a plan PR (H2); an engineer never edits it.
* `footprints`: the `touches.paths` of the OTHER unmerged items while one item is active, so a slice stays in its lane. A glob
  that overlaps the active item's own footprint is left out (a shared file such as a router is not locked to either).
* a gate name (G3 pins the acceptance tests, for example): `add_gate_globs()`.

A glob must be a safe relative glob (`common.safe_glob`); anything else is dropped, so a hostile plan cannot make the file
lock the whole tree or name `..`.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from kitlib import jsonio

from . import common

__all__ = ["refresh", "add_gate_globs", "read", "clear_gate"]

PLAN_GLOBS = ["plan/**"]
MAX_GLOBS = 400


def _path(rs: Any) -> str:
    return os.path.join(rs.root, "locked_paths.json")


def read(rs: Any) -> Dict[str, Any]:
    try:
        d = jsonio.read_json_opt(_path(rs))
    except (ValueError, OSError):
        d = None
    if isinstance(d, dict) and isinstance(d.get("sources"), dict):
        return d
    return {"globs": [], "sources": {}}


def _write(rs: Any, sources: Dict[str, List[str]]) -> None:
    clean = {k: sorted({g for g in v if common.safe_glob(g)})[:MAX_GLOBS] for k, v in sources.items() if isinstance(v, list)}
    globs = sorted({g for v in clean.values() for g in v})[:MAX_GLOBS]
    rs.ensure()
    jsonio.atomic_write_json(_path(rs), {"globs": globs, "sources": clean}, 0o600)


def _overlap(a: str, b: str) -> bool:
    import pr_prepare
    return bool(pr_prepare.glob_to_regex(a).match(b) or pr_prepare.glob_to_regex(b).match(a))


def refresh(ctx: Any, st: Any, lp: Any, active: Optional[str]) -> None:
    """Recompute the plan and footprint sources, keeping every gate's globs."""
    cur = read(ctx.rs)["sources"]
    sources = {k: v for k, v in cur.items() if k not in ("plan", "footprints")}
    sources["plan"] = list(PLAN_GLOBS)
    foot = []  # type: List[str]
    if active and active in lp.items:
        mine = [g for g in _footprint(lp.items[active])]
        for iid, it in lp.items.items():
            if iid == active or st.items.get(iid, {}).get("status") in ("merged", "abandoned"):
                continue
            for g in _footprint(it):
                if not any(_overlap(g, m) for m in mine):
                    foot.append(g)
    sources["footprints"] = foot
    _write(ctx.rs, sources)


def _footprint(item: Dict[str, Any]) -> List[str]:
    t = item.get("touches")
    if isinstance(t, dict) and isinstance(t.get("paths"), list):
        return [g for g in t["paths"] if isinstance(g, str) and common.safe_glob(g)]
    return []


def add_gate_globs(rs: Any, name: str, globs: Sequence[str]) -> None:
    sources = read(rs)["sources"]
    sources[name] = list(globs)
    _write(rs, sources)


def clear_gate(rs: Any, name: str) -> None:
    sources = read(rs)["sources"]
    sources.pop(name, None)
    _write(rs, sources)
