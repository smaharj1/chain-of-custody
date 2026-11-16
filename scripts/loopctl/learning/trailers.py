"""How well engineers' reports and trailers conform, and when `report_trailers: block` is ready (report 19.7 L-32, 16.1 row 10; DEC-29).

`report_trailers: warn` (the default) tolerates the form problems whose decision is still readable and records each as a `report_warning`
event; `block` fails the item on them. This module answers the question the move from one to the other rests on: how many accepted
reports in a row were clean? It reads the driver's events (the log is diagnostic and forgeable, CS-21, so it informs a human's decision
and never switches anything on its own) and the reports on disk.

It also holds the Primer Delta check that L-32 asks `loopctl status` to flag: N consecutive merged items that add a new top-level
directory or a dependency manifest change and still report an empty Primer Delta. That is a primer going stale in plain sight.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import common
from . import ISSUE_KINDS, token

__all__ = ["BLOCK_READY_STREAK", "EMPTY_DELTA_FLAG", "outcomes", "streak", "summarize", "empty_delta_streak", "MANIFESTS"]

BLOCK_READY_STREAK = 10          # accepted reports in a row with no warning and no malformed_report between them
EMPTY_DELTA_FLAG = 3             # consecutive structural items with an empty Primer Delta before it is flagged
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml", "build.gradle",
             "build.gradle.kts", "mix.exs", "pubspec.yaml", "Package.swift", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")


def outcomes(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chronological outcomes of the reports the driver read: `clean` (accepted, no warning), `warned` (accepted, with the warning kinds)
    or `failed` (an item failure `malformed_report`). The driver emits the warning before the acceptance for the same item and role."""
    out = []  # type: List[Dict[str, Any]]
    pending = {}  # type: Dict[Tuple[str, str], List[str]]
    for e in events:
        name = e.get("event")
        item = e.get("item") if isinstance(e.get("item"), str) else ""
        if name == "report_warning":
            kinds = [token(k, ISSUE_KINDS) for k in (e.get("kinds") if isinstance(e.get("kinds"), list) else [])][:8]
            pending[(item, token(e.get("role"), default="role"))] = kinds
        elif name == "engineer_report":
            kinds = pending.pop((item, token(e.get("role"), default="role")), None)
            out.append({"item": item, "role": token(e.get("role"), default="role"), "outcome": "warned" if kinds is not None else "clean", "kinds": kinds or [],
                        "ts": e.get("ts")})
        elif name == "fail" and e.get("reason") == "malformed_report":
            out.append({"item": item, "role": "", "outcome": "failed", "kinds": [], "ts": e.get("ts")})
    return out


def streak(rows: Sequence[Dict[str, Any]]) -> int:
    n = 0
    for r in reversed(list(rows)):
        if r["outcome"] != "clean":
            break
        n += 1
    return n


def summarize(events: Sequence[Dict[str, Any]], reports: Sequence[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    rows = outcomes(events)
    by = {"clean": 0, "warned": 0, "failed": 0}
    kinds = {}  # type: Dict[str, int]
    for r in rows:
        by[r["outcome"]] += 1
        for k in r["kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    on_disk = {"checked": len(reports), "conforming": 0, "soft": 0, "hard": 0, "kinds": {}}  # type: Dict[str, Any]
    for rep in reports:
        issues = [i for i in rep.get("issues", []) if isinstance(i, dict)]
        hard = [i for i in issues if i.get("severity") == "hard"]
        soft = [i for i in issues if i.get("severity") == "soft"]
        on_disk["hard"] += 1 if hard else 0
        on_disk["soft"] += 1 if soft and not hard else 0
        on_disk["conforming"] += 0 if issues else 1
        for i in issues:
            k = token(i.get("kind"), ISSUE_KINDS)
            on_disk["kinds"][k] = on_disk["kinds"].get(k, 0) + 1
    st = streak(rows)
    ready = st >= BLOCK_READY_STREAK
    if mode == "block":
        advice = "report_trailers is block already: every form problem fails the item."
    elif ready:
        advice = ("the last %d accepted reports were clean: `report_trailers: block` is ready. Switch it in a reviewed commit (it is H6); "
                  "an item with a form problem then fails and is sent back." % st)
    elif not rows:
        advice = "no engineer report has been read yet: stay on warn until %d accepted reports in a row are clean." % BLOCK_READY_STREAK
    else:
        advice = "stay on warn: %d clean in a row, %d needed (%d warned, %d failed so far)." % (st, BLOCK_READY_STREAK, by["warned"], by["failed"])
    return {"schema": "loopctl.trailers.v1", "mode": mode, "events": {"read": len(rows), "by_outcome": by, "kinds": kinds},
            "reports_on_disk": on_disk, "clean_streak": st, "needed": BLOCK_READY_STREAK, "block_ready": ready, "advice": advice}


def empty_delta_streak(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """`items`, merged items oldest first: {id, structural: bool (a new top-level directory or a manifest change), delta_empty: bool}. The
    flag is the TRAILING run of items that were structural and reported nothing. A structural item with a real delta, or a plain item, ends it."""
    n = 0
    ids = []  # type: List[str]
    for it in reversed(list(items)):
        if it.get("structural") and it.get("delta_empty"):
            n += 1
            ids.append(str(it.get("id")))
        elif it.get("structural"):
            break
        # a plain item neither adds to the run nor ends it: it says nothing about the primer
    return {"streak": n, "flag": n >= EMPTY_DELTA_FLAG, "items": list(reversed(ids)), "threshold": EMPTY_DELTA_FLAG}


def structural_change(paths: Sequence[str], parent_top_levels: Sequence[str]) -> Optional[str]:
    """Why a diff is structural: `new-top-level:<dir>` for a path under a first component that did not exist before, or
    `manifest:<file>` for a dependency manifest. None for a plain change. Pure over the diff's added or changed paths."""
    known = set(parent_top_levels)
    for p in paths:
        if not common.safe_rel_path(p):
            continue
        parts = p.split("/")
        if len(parts) > 1 and parts[0] not in known and not parts[0].startswith("."):
            return "new-top-level:" + token(parts[0].lower().replace(" ", "-"), default="dir")
        if parts[-1] in MANIFESTS:
            return "manifest:" + parts[-1]
    return None
