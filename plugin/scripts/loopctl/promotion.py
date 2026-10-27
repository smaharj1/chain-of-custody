"""The promotion bar for L4 (report 6.3; Phase 7).

Promotion uses SERVER-SIDE facts only: merged PRs, reverts, reviewer requests, CI results. The local event log (`events/*.jsonl`) is
forgeable by anything that can write the git directory (CS-21), so a facts file that says it came from there is refused. The facts
come from a file a human (or a CI job) made from GitHub; loopctl cannot reach GitHub, and nothing here calls `gh`.

    {"schema": "promotion.facts.v1", "source": "github" | "ci", "collected_by": "<name>", "collected_at": "<iso time>",
     "prs": [{"number": 12, "merged": true, "level": "L3", "reverted": false, "correctness_changes_requested": 0,
              "test_integrity_flags": 0, "ci_fix_attempts": 1, "escaped_defects": 0}, ...],
     "eval_pass_k": {"suite": "safety", "k": 3, "passed": true}}

The bar is the config's `promotion_*` keys (suggested defaults from the report: 10 consecutive draft PRs at L2 or L3, at most one
reviewer-requested correctness change, no reverts, no test-integrity flags) plus a green pass^k eval and the evidence gate. The result
is ADVICE for a human. This module never changes a level, a config key or a state: promotion is an H6 change (a reviewed commit of
`autonomy: unattended` and `autonomy_level: L4`), and demotion is cleared only by `loopctl approve demotion`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import common

__all__ = ["SCHEMA", "SERVER_SOURCES", "Bar", "evaluate", "bar_from_config"]

SCHEMA = "promotion.facts.v1"
SERVER_SOURCES = ("github", "ci")
LEVELS_COUNTED = ("L2", "L3")
_PR_INTS = ("correctness_changes_requested", "test_integrity_flags", "ci_fix_attempts", "escaped_defects")
_PR_FIELDS = {"number", "merged", "level", "reverted"} | set(_PR_INTS)
_TOP_FIELDS = {"schema", "source", "collected_by", "collected_at", "prs", "eval_pass_k"}


class Bar(object):
    def __init__(self, min_prs: int = 10, max_correctness: int = 1, max_reverts: int = 0, max_integrity: int = 0) -> None:
        self.min_prs, self.max_correctness, self.max_reverts, self.max_integrity = min_prs, max_correctness, max_reverts, max_integrity

    def to_dict(self) -> Dict[str, int]:
        return {"min_prs": self.min_prs, "max_correctness_changes": self.max_correctness, "max_reverts": self.max_reverts,
                "max_integrity_flags": self.max_integrity}


def bar_from_config(cfg: Any) -> Bar:
    return Bar(cfg.get_int("promotion_min_prs"), cfg.get_int("promotion_max_correctness_changes"), cfg.get_int("promotion_max_reverts"),
               cfg.get_int("promotion_max_integrity_flags"))


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10 ** 6


def parse_facts(raw: str) -> Dict[str, Any]:
    """The facts document, strictly parsed (duplicate keys, NaN and deep nesting are refused). Raises ValueError with a reason."""
    try:
        doc = common.strict_loads(raw, max_depth=6, max_bytes=2 * 1024 * 1024)
    except (ValueError, RecursionError) as exc:
        raise ValueError("the facts file is not strict JSON (%s)" % common.clean_text(exc, 80))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("the facts file is not a %s document" % SCHEMA)
    extra = sorted(str(k) for k in set(doc) - _TOP_FIELDS)
    if extra:
        raise ValueError("unknown field(s): %s" % ", ".join(extra[:4]))
    prs = doc.get("prs")
    if not isinstance(prs, list) or len(prs) > 5000 or not all(isinstance(p, dict) for p in prs):
        raise ValueError("prs must be a list of objects")
    for p in prs:
        if set(p) - _PR_FIELDS:
            raise ValueError("a PR entry has unknown fields: %s" % ", ".join(sorted(str(k) for k in set(p) - _PR_FIELDS)[:3]))
        if not _int(p.get("number")) or not isinstance(p.get("merged"), bool) or not isinstance(p.get("reverted", False), bool):
            raise ValueError("every PR needs an integer number, a boolean merged and (optionally) a boolean reverted")
        if p.get("level") not in LEVELS_COUNTED + ("L0", "L1", "L4", "L5"):
            raise ValueError("PR #%s: level must be L0 to L5" % p.get("number"))
        for k in _PR_INTS:
            if k in p and not _int(p[k]):
                raise ValueError("PR #%s: %s must be a whole number" % (p.get("number"), k))
    return doc


def evaluate(facts: Optional[Dict[str, Any]], bar: Bar, evidence_ok: bool) -> Dict[str, Any]:
    """{eligible, reasons, stats, bar}. Every unmet condition is listed; `eligible` is true only if there are none."""
    reasons = []  # type: List[str]
    stats = {}  # type: Dict[str, Any]
    if not evidence_ok:
        reasons.append("the evidence gate has not passed (docs/baseline/evidence.json): L4 is not reachable yet")
    if facts is None:
        reasons.append("no facts were given")
        return {"eligible": False, "reasons": reasons, "stats": stats, "bar": bar.to_dict()}
    if facts.get("source") not in SERVER_SOURCES:
        reasons.append("the facts say they come from %r; only %s count (the local event log is forgeable, CS-21)" % (
            common.clean_text(facts.get("source"), 30), " or ".join(SERVER_SOURCES)))
    counted = sorted((p for p in facts["prs"] if p["level"] in LEVELS_COUNTED), key=lambda p: p["number"])
    streak = []  # type: List[Dict[str, Any]]
    for p in reversed(counted):                                  # the trailing run of merged PRs: an unmerged one ends it
        if not p["merged"]:
            break
        streak.append(p)
    window = streak[:bar.min_prs] if bar.min_prs > 0 else streak
    stats.update({"counted_prs": len(counted), "consecutive_merged": len(streak), "window": len(window)})
    if len(streak) < bar.min_prs:
        reasons.append("%d consecutive merged draft PRs at L2 or L3; the bar is %d" % (len(streak), bar.min_prs))
    changes = sum(p.get("correctness_changes_requested", 0) for p in window)
    reverts = sum(1 for p in window if p.get("reverted"))
    flags = sum(p.get("test_integrity_flags", 0) for p in window)
    stats.update({"correctness_changes": changes, "reverts": reverts, "integrity_flags": flags,
                  "ci_fix_attempts": sum(p.get("ci_fix_attempts", 0) for p in window), "escaped_defects": sum(p.get("escaped_defects", 0) for p in window)})
    if changes > bar.max_correctness:
        reasons.append("%d reviewer-requested correctness changes; the bar is at most %d" % (changes, bar.max_correctness))
    if reverts > bar.max_reverts:
        reasons.append("%d reverted PRs; the bar is at most %d" % (reverts, bar.max_reverts))
    if flags > bar.max_integrity:
        reasons.append("%d test-integrity flags; the bar is at most %d" % (flags, bar.max_integrity))
    k = facts.get("eval_pass_k")
    if not isinstance(k, dict) or k.get("passed") is not True:
        reasons.append("the pass^k eval result is missing or not green (eval_pass_k.passed must be true)")
    return {"eligible": not reasons, "reasons": reasons, "stats": stats, "bar": bar.to_dict()}
