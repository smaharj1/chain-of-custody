"""The harness review at each model release (report 9, 15 item 3, 16.1 row 10, 16.2; docs/harness-review.md).

"Gates whose owner metric never moves are removed at the next model release." Two kinds of evidence say whether a part of the harness still pays
its way, and this module combines them without deciding anything:

* OWNER METRICS from real runs: for every gate, how often it ran, failed, blocked an item, and on how many distinct items. A gate that ran on
  `harness_min_item_runs` items and never failed or blocked is a candidate. (`gate_rows`; `loopctl harness`.)
* ABLATION from the eval suite: `python3 tools/evals/ablate.py` runs the kit's eval cases once as they are and once per harness part with that
  part switched off, through `tools/evals/run_local.py`'s graders, and writes `kit.ablation.v1`. `ablation_verdict` reads one part's cases:
  does removing it make a case fail? (`loopctl harness --ablation FILE`.)

A removal needs both to agree. A part judged by the safety corpus (the guards, G5, G9, G11, the evidence gate) is never recommended for
removal by this review: a safety regression blocks the release with no waiver (16.2), and a corpus, not an owner metric, judges it. The review
prints a recommendation per part; a person edits `loop.config` or the harness in a reviewed commit (H6).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .. import common
from . import token

__all__ = ["SCHEMA", "ABLATION_SCHEMA", "SAFETY_GATES", "MIN_DROP", "MIN_ABLATION_RUNS", "gate_rows", "ablation_verdict", "review"]

SCHEMA = "loopctl.harness.v1"
ABLATION_SCHEMA = "kit.ablation.v1"
SAFETY_GATES = ("g5", "g9", "g11", "evidence")
MIN_DROP = 0.1                   # a quality case must lose this much pass rate for the part to count as paying its way
MIN_ABLATION_RUNS = 3            # k=3 (report 15 item 3)
_GATES = ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "g7", "g9", "g10", "g11", "evidence", "docs")


def gate_rows(events: Sequence[Dict[str, Any]], owner_metrics: Dict[str, str], modes: Dict[str, str], min_item_runs: int) -> List[Dict[str, Any]]:
    """One row per gate that is installed or has run: {gate, owner_metric, mode, runs, items, failed, blocked, moved, safety, candidate}."""
    runs, items, failed, blocked = {}, {}, {}, {}  # type: Dict[str, int], Dict[str, set], Dict[str, int], Dict[str, int]
    for e in events:
        if e.get("event") != "gate" or e.get("status") == "skipped":
            continue
        g = token(e.get("gate"), _GATES)
        if g == "other":
            continue
        runs[g] = runs.get(g, 0) + 1
        items.setdefault(g, set()).add(e.get("item") if isinstance(e.get("item"), str) else "")
        if e.get("status") in ("fail", "error"):
            failed[g] = failed.get(g, 0) + 1
        if e.get("blocking") is True:
            blocked[g] = blocked.get(g, 0) + 1
    rows = []  # type: List[Dict[str, Any]]
    for g in sorted(set(owner_metrics) | set(runs), key=lambda x: (_GATES.index(x) if x in _GATES else 99, x)):
        n_items = len(items.get(g, ()))
        moved = bool(failed.get(g) or blocked.get(g))
        rows.append({"gate": g, "owner_metric": common.clean_text(owner_metrics.get(g, ""), 160), "mode": modes.get(g, "on"), "runs": runs.get(g, 0), "items": n_items,
                     "failed": failed.get(g, 0), "blocked": blocked.get(g, 0), "moved": moved, "safety": g in SAFETY_GATES,
                     "candidate": (not moved) and n_items >= max(1, min_item_runs) and g not in SAFETY_GATES and modes.get(g, "on") != "off"})
    return rows


def ablation_verdict(part: Dict[str, Any], min_runs: int = MIN_ABLATION_RUNS, min_drop: float = MIN_DROP) -> Dict[str, Any]:
    """pending | insufficient | keep | harmful | no-effect | exempt for one part of the harness, from its cases' pass rates with and without it."""
    cases = [c for c in (part.get("cases") or []) if isinstance(c, dict)]
    safety = part.get("class") == "safety" or any(c.get("class") == "safety" for c in cases)
    if not cases:
        return {"verdict": "pending", "reason": "no eval case is listed for this part", "dropped": []}
    ablated = [c for c in cases if isinstance(c.get("ablated_runs"), int) and c["ablated_runs"] > 0]
    if len(ablated) < len(cases):
        return {"verdict": "pending", "reason": "%d of %d case(s) have no run with this part switched off" % (len(cases) - len(ablated), len(cases)), "dropped": []}
    if any(c["ablated_runs"] < min_runs for c in ablated):
        return {"verdict": "insufficient", "reason": "fewer than %d run(s) per case with the part off (k=3)" % min_runs, "dropped": []}
    dropped, gained = [], []
    for c in ablated:
        base, abl = float(c.get("base_rate") or 0.0), float(c.get("ablated_rate") or 0.0)
        need = 1e-9 if c.get("class") == "safety" else min_drop
        if base - abl >= need:
            dropped.append(str(c.get("id"))[:60])
        elif abl - base >= min_drop:
            gained.append(str(c.get("id"))[:60])
    if dropped:
        return {"verdict": "keep", "reason": "switching it off makes %d case(s) fail more" % len(dropped), "dropped": dropped}
    if safety:
        return {"verdict": "exempt", "reason": "a safety part is judged by the corpus and the safety cases, not by an owner metric", "dropped": []}
    if gained:
        return {"verdict": "harmful", "reason": "switching it off makes %d case(s) pass more" % len(gained), "dropped": [], "gained": gained}
    return {"verdict": "no-effect", "reason": "no case changed with the part switched off", "dropped": []}


def review(rows: Sequence[Dict[str, Any]], ablation: Optional[Dict[str, Any]], model: str = "") -> Dict[str, Any]:
    """The review: each gate's owner metric and, when an ablation file is given, its ablation verdict, and a recommendation that needs both."""
    parts = {}  # type: Dict[str, Dict[str, Any]]
    if ablation is not None:
        for p in ablation.get("parts") or []:
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                parts[p["id"]] = p
    out = []  # type: List[Dict[str, Any]]
    for r in rows:
        row = dict(r)
        part = parts.get("gate:" + r["gate"])
        av = ablation_verdict(part) if part is not None else None
        row["ablation"] = av
        if r["safety"]:
            rec = "keep: judged by the safety corpus, not by this metric"
        elif r["mode"] == "off":
            rec = "off already"
        elif r["moved"]:
            rec = "keep: the metric moved (%d failed, %d blocked)" % (r["failed"], r["blocked"])
        elif av is not None and av["verdict"] == "keep":
            rec = "keep: the ablation shows it matters even though no real run tripped it"
        elif not r["candidate"]:
            rec = "keep for now: only %d item(s) so far (needs more runs before it can be judged)" % r["items"]
        elif av is not None and av["verdict"] in ("no-effect", "harmful"):
            rec = "remove candidate: the metric never moved on %d item(s) and the ablation agrees (%s)" % (r["items"], av["verdict"])
        else:
            rec = "review: the metric never moved on %d item(s); run the ablation before removing" % r["items"]
        row["recommendation"] = rec
        out.append(row)
    other = [dict(id=pid, **{k: v for k, v in ablation_verdict(p).items()}) for pid, p in sorted(parts.items()) if not pid.startswith("gate:")]
    return {"schema": SCHEMA, "model": common.clean_text(model, 60), "gates": out,
            "candidates": [r["gate"] for r in out if r["recommendation"].startswith("remove candidate")],
            "other_parts": other, "ablation_present": ablation is not None,
            "note": "a recommendation is advice: a person changes loop.config or the harness in a reviewed commit (H6); a safety regression blocks the release with no waiver (16.2)"}
