"""The critic calibration log (report 9 "Critic calibration", 19.7 L-34, DEC-28).

"Log every blocker with its outcome: fixed, overruled or false positive." A blocker is a `[BLOCKER]` line in a design-critic or code-reviewer
review (or a Critical or High finding in a security review). This module:

* reads the review files the driver and the conductor save (`docs/features/<slug>/reviews/*.md`, `reports/*review*.md`) and logs each blocker
  once, under an id that does not change between rounds (it hashes the critic, the item and the blocker's words, not its round);
* derives an outcome only where git-visible facts allow it: a blocker the next round no longer lists is `fixed` (derived, marked as such,
  and a person can overrule it); one still listed in the last round of a rejected review stays `open`;
* takes a person's outcome (`calibration record`): fixed, overruled (the reviewer was right and the person chose not to fix it) or
  false_positive, and whether an engineer actually hit the problem (the report's "did an engineer hit it?" precision measure);
* reports each critic's precision: fixed / (fixed + overruled + false_positive), and `pending` until `calibration_min_samples` have an outcome.

The log is `<git-common-dir>/loopctl/calibration.jsonl`, append only, redacted, never read as an instruction. Calibration feeds a person
who may sharpen a rubric (H6); it changes no prompt, rule or agent by itself (DEC-28).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kitlib import jsonio, redact

from .. import common
from . import token

__all__ = ["OUTCOMES", "HITS", "CRITICS", "parse_blockers", "blocker_id", "derive", "append", "record", "report", "latest", "LOW_PRECISION", "FILE"]

FILE = "calibration.jsonl"
OUTCOMES = ("fixed", "overruled", "false_positive")
HITS = ("yes", "no", "unknown")
CRITICS = ("design-critic", "code-reviewer", "security-reviewer")
LOW_PRECISION = 0.5
_FENCE_RE = re.compile(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)")
_LINE_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\[(BLOCKER|CRITICAL|HIGH)(?:[ \t]+[A-Za-z0-9-]{1,12})?\][ \t]*(.{1,400})$", re.M)
_BLOCKED = ("CHANGES_REQUESTED", "DESIGN_NEEDS_REVISION", "SECURITY_BLOCKED")


def parse_blockers(text: str) -> List[Dict[str, str]]:
    """The blockers a review lists: [{sev, where, summary}]. Fenced code is skipped (a review may quote the format), and so is anything
    that is not a list line starting with the marker."""
    body = _FENCE_RE.sub("", text or "")
    out = []  # type: List[Dict[str, str]]
    seen = set()
    for m in _LINE_RE.finditer(body):
        raw = common.clean_text(m.group(2), 300).strip()
        where, _sep, rest = raw.partition(" — ") if " — " in raw else raw.partition(" - ")
        if not rest:
            where, rest = "", raw
        key = re.sub(r"\s+", " ", raw.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"sev": m.group(1).lower(), "where": common.clean_text(where, 80), "summary": common.clean_text(rest, 200)})
    for blk in re.split(r"(?m)^[ \t]*-?[ \t]*\*\*SEVERITY\*\*", body)[1:]:           # the security-reviewer's findings (G7 reads the same shape)
        if re.match(r"\s*:?\s*\[?\s*(Critical|High)\b", blk, re.I):
            sid = re.search(r"SEC-\d+", blk)
            first = common.clean_text(" ".join(ln.strip() for ln in blk.strip().splitlines()[:2]), 200)
            out.append({"sev": "critical", "where": sid.group(0) if sid else "", "summary": first})
    return out[:60]


def blocker_id(critic: str, item: str, blocker: Dict[str, str]) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", ("%s %s" % (blocker.get("where", ""), blocker.get("summary", ""))).lower()).strip()
    return "bk-" + common.sha256_hex("%s|%s|%s" % (critic, item, norm))[:12]


# -- the log -------------------------------------------------------------------------------------------------------------

def read_log(rs: Any) -> List[Dict[str, Any]]:
    from .data import _jsonl
    return _jsonl(os.path.join(rs.root, FILE))


def append(rs: Any, entries: Sequence[Dict[str, Any]]) -> int:
    """Append entries (one `write()` each on an O_APPEND descriptor). Redacted; the file is created 0600 and never followed through a symlink."""
    if not entries:
        return 0
    jsonio.ensure_dir(rs.root)
    path = os.path.join(rs.root, FILE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        for e in entries:
            rec = redact.redact_obj(dict(e))
            rec.setdefault("ts", common.iso())
            os.write(fd, (common.canonical_json(rec) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return len(entries)


def latest(entries: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """({blocker id: its blocker entry}, {blocker id: its newest outcome}). A person's outcome beats a derived one whatever the order."""
    blockers = {}  # type: Dict[str, Dict[str, Any]]
    outcomes = {}  # type: Dict[str, Dict[str, Any]]
    for e in entries:
        if e.get("type") == "blocker" and isinstance(e.get("id"), str):
            blockers.setdefault(e["id"], e)
        elif e.get("type") == "outcome" and isinstance(e.get("blocker"), str) and e.get("outcome") in OUTCOMES:
            cur = outcomes.get(e["blocker"])
            if cur is None or not (cur.get("derived") is False and e.get("derived")):
                outcomes[e["blocker"]] = e
    return blockers, outcomes


def derive(reviews: Sequence[Dict[str, Any]], entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """New log entries from the review files: the blockers not logged yet, and a derived `fixed` for a blocker a later round no longer lists.
    Returns {entries: [...], blockers: n, derived: n, open: n}. Idempotent: running it twice appends nothing the second time."""
    known, outs = latest(entries)
    groups = {}  # type: Dict[Tuple[str, str], List[Dict[str, Any]]]
    for rv in reviews:
        groups.setdefault((str(rv.get("item")), token(rv.get("role"), CRITICS, "code-reviewer")), []).append(rv)
    new = []  # type: List[Dict[str, Any]]
    open_ids = set()  # type: set
    n_derived = 0
    for (item, critic), rows in sorted(groups.items()):
        rows.sort(key=lambda r: int(r.get("round") or 1))
        per_round = []  # type: List[Tuple[Dict[str, Any], List[Tuple[str, Dict[str, str]]]]]
        for rv in rows:
            bl = [(blocker_id(critic, item, b), b) for b in parse_blockers(rv.get("text", ""))]
            per_round.append((rv, bl))
            for bid, b in bl:
                if bid not in known:
                    ent = {"type": "blocker", "id": bid, "critic": critic, "item": item, "round": int(rv.get("round") or 1), "sev": b["sev"], "where": b["where"],
                           "summary": b["summary"], "path": common.clean_text(rv.get("path"), 120)}
                    known[bid] = ent
                    new.append(ent)
        last_rv, last_bl = per_round[-1]
        last_ids = {bid for bid, _b in last_bl}
        blocked_last = last_rv.get("verdict") in _BLOCKED
        for rv, bl in per_round:
            for bid, _b in bl:
                cur = outs.get(bid)
                if cur is not None and (cur.get("derived") is False or cur.get("outcome") == "fixed"):
                    continue
                if bid not in last_ids and rv is not last_rv:
                    ent = {"type": "outcome", "blocker": bid, "outcome": "fixed", "engineer_hit": "unknown", "by": "scan", "derived": True,
                           "note": "a later round no longer lists it"}
                    outs[bid] = ent
                    new.append(ent)
                    n_derived += 1
                elif bid in last_ids and (blocked_last or last_rv.get("verdict") is None):
                    open_ids.add(bid)
    return {"entries": new, "blockers": sum(1 for e in new if e["type"] == "blocker"), "derived": n_derived, "open": len(open_ids)}


def record(rs: Any, blocker: str, outcome: str, hit: str, by: str, note: str, entries: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A person's outcome for a logged blocker. The blocker must be in the log (scan first): an outcome for nothing is not recorded."""
    if outcome not in OUTCOMES:
        raise common.UsageError("--outcome must be one of %s" % ", ".join(OUTCOMES))
    if hit not in HITS:
        raise common.UsageError("--engineer-hit must be one of %s" % ", ".join(HITS))
    if not re.match(r"^bk-[0-9a-f]{12}\Z", blocker or ""):
        raise common.UsageError("--blocker must be a logged blocker id (bk-<12 hex>); `loopctl calibration scan --write` logs them")
    known, _o = latest(read_log(rs) if entries is None else entries)
    if blocker not in known:
        raise common.UsageError("blocker %s is not in the calibration log; run `loopctl calibration scan --write` first" % blocker)
    if not by or not by.strip():
        raise common.UsageError("--by <name> is required: an outcome is a person's call and it is recorded")
    ent = {"type": "outcome", "blocker": blocker, "outcome": outcome, "engineer_hit": hit, "by": common.clean_text(by, 60), "derived": False,
           "note": common.clean_text(note, 300)}
    append(rs, [ent])
    return ent


def report(entries: Sequence[Dict[str, Any]], min_samples: int = 5) -> Dict[str, Any]:
    """Per critic: logged, by outcome, precision and engineer-hit rate, and a verdict (`pending`, `ok`, `low-precision`)."""
    blockers, outs = latest(entries)
    rows = {}  # type: Dict[str, Dict[str, Any]]
    for bid, b in blockers.items():
        c = token(b.get("critic"), CRITICS, "code-reviewer")
        r = rows.setdefault(c, {"critic": c, "logged": 0, "fixed": 0, "overruled": 0, "false_positive": 0, "open": 0, "hit_yes": 0, "hit_known": 0, "derived": 0})
        r["logged"] += 1
        o = outs.get(bid)
        if o is None:
            r["open"] += 1
            continue
        r[o["outcome"]] += 1
        r["derived"] += 1 if o.get("derived") else 0
        if o["outcome"] == "fixed" and o.get("engineer_hit") in ("yes", "no"):
            r["hit_known"] += 1
            r["hit_yes"] += 1 if o["engineer_hit"] == "yes" else 0
    out = []  # type: List[Dict[str, Any]]
    for c in CRITICS:
        r = rows.get(c)
        if r is None:
            continue
        decided = r["fixed"] + r["overruled"] + r["false_positive"]
        prec = round(r["fixed"] / float(decided), 3) if decided else None
        hit = round(r["hit_yes"] / float(r["hit_known"]), 3) if r["hit_known"] else None
        verdict = "pending" if decided < max(1, min_samples) else ("low-precision" if prec is not None and prec < LOW_PRECISION else "ok")
        out.append(dict(r, decided=decided, precision=prec, engineer_hit_rate=hit, verdict=verdict, min_samples=min_samples))
    return {"schema": "loopctl.calibration.v1", "critics": out, "blockers": len(blockers), "with_outcome": len(outs)}
