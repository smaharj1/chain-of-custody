"""The spend ledger (report section 10.5, CS-26, CO-7; IMP-25).

`<git-common-dir>/loopctl/spend.jsonl`: one JSON line per child session, appended with a single `write()` on an
`O_APPEND` descriptor. loopctl sums `total_cost_usd` from each child's `-p` JSON result and passes the REMAINING
budget as `--max-budget-usd` on every launch, because the flag's own count resets on resume and is a client-side estimate
that can overshoot (P26).

Ceilings (`run_budget_usd`, `item_budget_usd`, `day_budget_usd`; 0 = not set): reaching any of them parks the run
and sets the item `needs_human`.

UNKNOWN COST MEANS PARK. A result without a usable `total_cost_usd` (missing, renamed, a string, negative, NaN, infinite)
is recorded as `unknown`. While an unacknowledged unknown entry exists for the run, `verdict()` says park: loopctl
never guesses a cost. A human acknowledges with `loopctl approve cost` (`acknowledge()`). A line of the ledger that
cannot be parsed counts as unknown too (a tampered or torn ledger cannot prove the run is under budget).

Projection: `loopctl cost --projection` uses the baseline's p50 and p90 spend per item once
`docs/baseline/results/*.json` holds completed runs with a spend; before that it says `unavailable (no baseline)`.
"""
from __future__ import annotations

import decimal
import glob
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from kitlib import jsonio, redact, runstate

from . import common

__all__ = ["Ledger", "Verdict", "parse_cost", "projection", "MAX_PLAUSIBLE_USD"]

MAX_PLAUSIBLE_USD = 100000.0
_D = decimal.Decimal


def parse_cost(result: Any) -> Tuple[Optional[float], str]:
    """(cost, why-not). Reads `total_cost_usd` from a child's `-p` JSON result (a dict)."""
    if not isinstance(result, dict):
        return None, "the result is not a JSON object"
    if "total_cost_usd" not in result:
        return None, "the result has no total_cost_usd field (renamed or absent)"
    return _coerce(result["total_cost_usd"])


def _coerce(v: Any) -> Tuple[Optional[float], str]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None, "total_cost_usd is not a number (%s)" % type(v).__name__
    if not math.isfinite(v):
        return None, "total_cost_usd is not finite"
    if v < 0:
        return None, "total_cost_usd is negative"
    if v > MAX_PLAUSIBLE_USD:
        return None, "total_cost_usd is implausibly large"
    return float(v), ""


class Verdict(object):
    """`ok` is False when the run must be parked. `reasons` says why; `remaining` is what may still be spent."""

    def __init__(self) -> None:
        self.ok = True
        self.reasons = []  # type: List[str]
        self.codes = []  # type: List[str]
        self.remaining = None  # type: Optional[float]
        self.spent = {}  # type: Dict[str, float]
        self.unknown = 0

    def fail(self, code: str, why: str) -> None:
        self.ok = False
        self.codes.append(code)
        self.reasons.append(why)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "codes": self.codes, "reasons": self.reasons, "remaining_usd": self.remaining,
                "spent_usd": self.spent, "unknown_entries": self.unknown}


class Ledger(object):
    def __init__(self, state: runstate.RunState) -> None:
        self.rs = state
        self.path = state.spend_path

    # -- writing ---------------------------------------------------------------------------
    def _append(self, rec: Dict[str, Any]) -> None:
        self.rs.ensure()
        line = (json.dumps(redact.redact_obj(rec), sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def record(self, run_id: str, item: str, attempt: int, session_id: Optional[str], result: Any = None,
               cost: Any = None, role: Optional[str] = None, source: str = "child") -> Dict[str, Any]:
        """Record one child session. Pass the parsed `-p` JSON as `result`, or a bare `cost`. Returns the entry."""
        if not runstate.valid_id(run_id) or not common.valid_item_id(item):
            raise common.UsageError("invalid run id or item id for the ledger")
        if result is not None:
            value, why = parse_cost(result)
            if session_id is None and isinstance(result, dict) and isinstance(result.get("session_id"), str):
                session_id = result["session_id"]
        else:
            value, why = _coerce(cost) if cost is not None else (None, "no cost was reported")
        ts = common.now()
        rec = {"type": "spend", "ts": common.iso(ts), "day": common.iso(ts)[:10], "runId": run_id, "item": item,
               "attempt": int(attempt), "sessionId": session_id if runstate.valid_id(session_id) else None,
               "role": common.clean_text(role, 40) if role else None, "source": common.clean_text(source, 20),
               "total_cost_usd": value, "unknown": value is None, "why": common.clean_text(why, 120) if why else ""}
        self._append(rec)
        return rec

    def acknowledge(self, run_id: str, by: str, note: str = "") -> None:
        """A human accepts the unknown-cost entries of the run so far (`loopctl approve cost`)."""
        _, bad = self.entries()
        self._append({"type": "ack", "ts": common.iso(), "runId": run_id, "by": common.clean_text(by, 60),
                      "note": common.clean_text(note, 200), "bad": bad})

    # -- reading ---------------------------------------------------------------------------
    def entries(self) -> Tuple[List[Dict[str, Any]], int]:
        """(entries, unparsable line count)."""
        out = []  # type: List[Dict[str, Any]]
        bad = 0
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        bad += 1
                        continue
                    if not isinstance(rec, dict) or rec.get("type") not in ("spend", "ack"):
                        bad += 1
                        continue
                    out.append(rec)
        except OSError:
            pass
        return out, bad

    def totals(self, run_id: Optional[str] = None, item: Optional[str] = None, day: Optional[str] = None
               ) -> Dict[str, Any]:
        ents, bad = self.entries()
        total = _D(0)
        n = 0
        acked_runs = {}  # type: Dict[str, int]
        acked_bad = 0
        for i, e in enumerate(ents):
            if e.get("type") == "ack":
                acked_runs[e.get("runId")] = i
                if isinstance(e.get("bad"), int):
                    acked_bad = max(acked_bad, e["bad"])
        unknown = max(0, bad - acked_bad)
        for i, e in enumerate(ents):
            if e.get("type") != "spend":
                continue
            if run_id is not None and e.get("runId") != run_id:
                continue
            if item is not None and e.get("item") != item:
                continue
            if day is not None and e.get("day") != day:
                continue
            v = e.get("total_cost_usd")
            _, why = _coerce(v) if v is not None else (None, "none")
            if e.get("unknown") or why:
                if acked_runs.get(e.get("runId"), -1) < i:
                    unknown += 1
                continue
            total += _D(repr(float(v)))
            n += 1
        return {"usd": float(total), "entries": n, "unknown": unknown}

    # -- the decision -------------------------------------------------------------------------------
    def verdict(self, cfg: Any, run_id: str, item: Optional[str] = None, today: Optional[str] = None) -> Verdict:
        v = Verdict()
        today = today or common.iso()[:10]
        run_t = self.totals(run_id=run_id)
        day_t = self.totals(day=today)
        item_t = self.totals(item=item) if item else None
        v.spent = {"run": run_t["usd"], "day": day_t["usd"]}
        if item_t is not None:
            v.spent["item"] = item_t["usd"]
        # unknown entries anywhere in this run, or corrupt lines anywhere, park the run
        v.unknown = run_t["unknown"]
        if run_t["unknown"]:
            v.fail("cost_unknown", "%d spend entr%s without a usable total_cost_usd (or an unreadable ledger line): the cost is unknown, so "
                   "the run is parked; check the field name, then `loopctl approve cost --by <you>`" % (run_t["unknown"], "y" if run_t["unknown"] == 1 else "ies"))
        caps = (("run", cfg.get("run_budget_usd"), run_t["usd"]), ("item", cfg.get("item_budget_usd"), item_t["usd"] if item_t else None),
                ("day", cfg.get("day_budget_usd"), day_t["usd"]))
        rems = []  # type: List[float]
        for name, ceiling, spent in caps:
            if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling <= 0 or spent is None:
                continue
            left = float(ceiling) - spent
            rems.append(left)
            if left <= 1e-9:
                v.fail("budget_%s" % name, "the %s budget of $%.4f is spent ($%.4f)" % (name, float(ceiling), spent))
        if rems:
            v.remaining = max(0.0, math.floor(min(rems) * 10000) / 10000.0)
        return v

    def launch_budget(self, cfg: Any, run_id: str, item: Optional[str] = None) -> float:
        """The value for `--max-budget-usd` on the next launch. Raises Refused when nothing may be spent or when no ceiling is set."""
        v = self.verdict(cfg, run_id, item)
        if not v.ok:
            raise common.Refused("cannot launch: " + "; ".join(v.reasons))
        if v.remaining is None:
            raise common.Refused("cannot launch: run_budget_usd is not set (0). Set a ceiling in loop.config; there is no projection before a baseline")
        return v.remaining


def projection(repo: str, n_items: int) -> Dict[str, Any]:
    """Budget projection for `n_items` from the baseline's completed runs: p50 and p90 spend per item."""
    per_item = []  # type: List[float]
    for p in sorted(glob.glob(os.path.join(repo, "docs", "baseline", "results", "*.json"))):
        if os.path.basename(p) in ("TEMPLATE.json", "schema.json"):
            continue
        try:
            d = jsonio.read_json(p)
        except (ValueError, OSError):
            continue
        if not isinstance(d, dict) or d.get("kind") != "run" or d.get("status") not in ("completed", "complete"):
            continue
        spend, items = d.get("spend_usd"), d.get("items")
        if isinstance(spend, (int, float)) and not isinstance(spend, bool) and math.isfinite(spend) and spend > 0 \
                and isinstance(items, list) and items:
            per_item.append(float(spend) / len(items))
    if not per_item:
        return {"available": False, "items": n_items, "message": "unavailable (no baseline): set run_budget_usd explicitly"}
    per_item.sort()

    def pct(q: float) -> float:
        k = (len(per_item) - 1) * q
        lo, hi = int(math.floor(k)), int(math.ceil(k))
        return per_item[lo] + (per_item[hi] - per_item[lo]) * (k - lo)

    p50, p90 = pct(0.5), pct(0.9)
    return {"available": True, "items": n_items, "baseline_runs": len(per_item), "per_item_p50_usd": round(p50, 4),
            "per_item_p90_usd": round(p90, 4), "projection_p50_usd": round(p50 * n_items, 2),
            "projection_p90_usd": round(p90 * n_items, 2),
            "message": "about $%.2f (p50) to $%.2f (p90) for %d items, from %d baseline run(s)" % (p50 * n_items, p90 * n_items, n_items, len(per_item))}
