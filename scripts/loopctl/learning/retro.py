"""`loopctl retro`: a milestone's numbers in one document, for the retro conversation (report 16.1 row 10, F-29, DEC-28).

Every number is a count or a sum over closed vocabularies (failure classes and reasons, gate names, reviewer roles, notification kinds), never a
quotation: an event message or a report section is untrusted text and does not appear here. The text that matters is quoted by `gaps` (fenced) or
read by the person from the report file named by a path. The document is `loopctl.retro.v1`; `render` turns it into the markdown the `/retro`
mode shows.

What it measures, and the report line each answers:

    flow          items by state, attempts, retries per counter, wall time per item                        (the loop's own throughput)
    failures      by class and reason; the share that is `env` (G0's owner metric: it should fall)         (G0, CO-10)
    rescues       the human's control-flow interventions: unblock, env, breaker, cost, demotion approvals,
                  lock takeovers and steals                                                                (16.2: more than 3 per run justifies loopctl)
    gates         per gate: runs, pass, fail, error, blocking, and the finding ids that repeat             (every gate's owner metric, 9)
    spend         total, per item, unknown entries                                                         (10.5)
    reports       form problems by kind, the clean streak, brief gaps and deltas per domain               (L-32, L-33)
    critics       blockers logged and each critic's precision                                              (L-34)
    unattended    notifications by kind, stalls, the demotion in the state header                          (7: stops per run)
    parallel      admissions, rebases, conflicts, footprint violations, lease losses, cascades, and how much
                  item time overlapped in the wall time                                                    (8: faster than serial, equal defects)
    primer        the empty-delta streak (when the caller passes the merged items' structure)               (L-32)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from kitlib import kitpaths, runstate

from .. import common, failure
from . import calibration, token, trailers
from .data import RunData, event_time, milestone_of

__all__ = ["SCHEMA", "build", "render", "compare", "RESCUE_SCOPES"]

SCHEMA = "loopctl.retro.v1"
RESCUE_SCOPES = ("unblock", "env", "breaker", "cost", "demotion")
_GATE_NAMES = ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "g7", "g8", "g9", "g10", "g11", "evidence", "docs", "reviews")
_NOTIFY_KINDS = ("started", "done", "waiting", "stop", "demoted", "stall", "watchdog")
_PARALLEL_EVENTS = ("admit", "admit_wait", "integrate_start", "rebased", "rebase_conflict", "footprint_violation", "lease_lost", "invalidate", "invalidated",
                    "drain", "worker_crash", "worktree_removed", "lease_reaped")
_RESCUE_EVENTS = ("lock_taken_over", "lock_stolen")


def _count(d: Dict[str, int], k: str, n: int = 1) -> None:
    d[k] = d.get(k, 0) + n


def _sorted(d: Dict[str, int]) -> Dict[str, int]:
    return {k: d[k] for k in sorted(d, key=lambda x: (-d[x], x))}


def build(data: RunData, milestone: Optional[str] = None, structural_items: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    ids = data.item_ids(milestone)
    d = data.for_items(ids) if milestone else data
    doc = {"schema": SCHEMA, "milestone": milestone or "all", "items": ids, "problems": list(data.problems)}  # type: Dict[str, Any]
    ev = d.events
    ts = [t for t in (event_time(e) for e in ev) if t is not None]
    runs = sorted({str(e.get("runId")) for e in ev if isinstance(e.get("runId"), str)})
    doc["span"] = {"runs": runs, "events": len(ev), "first": common.iso(min(ts)) if ts else None, "last": common.iso(max(ts)) if ts else None,
                   "wall_s": int(max(ts) - min(ts)) if len(ts) > 1 else 0}

    # -- flow ----------------------------------------------------------------------------------------------------------
    by_status = {}  # type: Dict[str, int]
    counters = {}  # type: Dict[str, int]
    attempts = 0
    for iid in ids:
        r = d.items.get(iid)
        if not isinstance(r, dict):
            _count(by_status, "untracked")
            continue
        _count(by_status, str(r.get("status")))
        attempts += int(r.get("attempt") or 1)
        for k, v in (r.get("counters") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                _count(counters, token(k, default="other"), v)
    spans = _item_spans(ev)
    doc["flow"] = {"items": len(ids), "by_status": _sorted(by_status), "attempts": attempts, "retries": _sorted({k: v for k, v in counters.items() if v}),
                   "item_wall_s": {i: s for i, s in sorted(spans.items(), key=lambda kv: common.natural_key(kv[0]))}}

    # -- failures ------------------------------------------------------------------------------------------------------
    by_class, by_reason = {}, {}  # type: Dict[str, int], Dict[str, int]
    parks = {}  # type: Dict[str, int]
    for e in ev:
        if e.get("event") == "fail":
            _count(by_class, token(e.get("failure_class"), failure.CLASSES))
            _count(by_reason, token(e.get("reason"), failure.KNOWN_REASONS))
        elif e.get("event") == "park":
            _count(parks, _code(e.get("code")))
    total_f = sum(by_class.values())
    doc["failures"] = {"total": total_f, "by_class": _sorted(by_class), "by_reason": _sorted(by_reason), "parks": _sorted(parks),
                       "env_share": round(by_class.get("env", 0) / float(total_f), 3) if total_f else None}

    # -- rescues -------------------------------------------------------------------------------------------------------
    resc = {}  # type: Dict[str, int]
    for a in d.approvals:
        if a.get("scope") in RESCUE_SCOPES and (not ids or a.get("target") in ids or a.get("target") in RESCUE_SCOPES or not a.get("target")):
            _count(resc, str(a["scope"]))
    for e in ev:
        if e.get("event") in _RESCUE_EVENTS:
            _count(resc, str(e["event"]))
    doc["rescues"] = {"total": sum(resc.values()), "by_kind": _sorted(resc), "per_run": round(sum(resc.values()) / float(len(runs)), 2) if runs else None,
                      "kill_criterion": "more than 3 control-flow rescues per run justifies loopctl Tier 2 (16.2)"}

    # -- gates ---------------------------------------------------------------------------------------------------------
    gates = {}  # type: Dict[str, Dict[str, Any]]
    for e in ev:
        if e.get("event") != "gate":
            continue
        g = token(e.get("gate"), _GATE_NAMES)
        r = gates.setdefault(g, {"runs": 0, "pass": 0, "fail": 0, "error": 0, "skipped": 0, "blocking": 0, "ids": {}})
        r["runs"] += 1
        st = token(e.get("status"), ("pass", "fail", "error", "skipped"))
        if st in r:
            r[st] += 1
        r["blocking"] += 1 if e.get("blocking") is True else 0
        for fid in e.get("ids") if isinstance(e.get("ids"), list) else []:
            _count(r["ids"], token(fid.lower() if isinstance(fid, str) else None, default="other"))
    for r in gates.values():
        r["ids"] = _sorted(r["ids"])
    doc["gates"] = {k: gates[k] for k in sorted(gates)}

    # -- spend ---------------------------------------------------------------------------------------------------------
    usd, unknown = 0.0, 0
    per_item = {}  # type: Dict[str, float]
    for s in d.spend:
        if s.get("type") != "spend":
            continue
        v = s.get("total_cost_usd")
        if common.is_number(v):
            usd += float(v)
            per_item[str(s.get("item"))] = per_item.get(str(s.get("item")), 0.0) + float(v)
        else:
            unknown += 1
    doc["spend"] = {"usd": round(usd, 4), "unknown_entries": unknown, "per_item": {k: round(v, 4) for k, v in sorted(per_item.items(), key=lambda kv: common.natural_key(kv[0]))},
                    "per_merged_item": round(usd / float(by_status["merged"]), 4) if by_status.get("merged") else None}

    # -- reports -------------------------------------------------------------------------------------------------------
    mode = d.cfg.get("report_trailers") if d.cfg.get("report_trailers") in ("warn", "block") else "warn"
    tr = trailers.summarize(ev, d.reports, mode)
    gaps_by, pd_by, dd_by = {}, {}, {}  # type: Dict[str, int], Dict[str, int], Dict[str, int]
    for rep in d.reports:
        dom = token(rep.get("domain"), default="other")
        if _meaningful(rep.get("gaps")):
            _count(gaps_by, dom)
        if rep.get("primer_delta_text"):
            _count(pd_by, dom)
        if rep.get("doc_delta_text"):
            _count(dd_by, dom)
    doc["reports"] = {"read": len(d.reports), "trailers": {k: tr[k] for k in ("mode", "clean_streak", "needed", "block_ready", "advice")},
                      "events": tr["events"], "on_disk": tr["reports_on_disk"], "brief_gaps_by_domain": _sorted(gaps_by),
                      "primer_deltas_by_domain": _sorted(pd_by), "doc_deltas_by_domain": _sorted(dd_by)}
    if structural_items is not None:
        doc["primer"] = trailers.empty_delta_streak(structural_items)

    # -- critics -------------------------------------------------------------------------------------------------------
    n_min = d.cfg.get("calibration_min_samples")
    doc["critics"] = calibration.report(d.calibration, n_min if isinstance(n_min, int) and not isinstance(n_min, bool) else 5)
    doc["critics"].pop("schema", None)

    # -- unattended ----------------------------------------------------------------------------------------------------
    nk = {}  # type: Dict[str, int]
    for n in d.notify:
        _count(nk, token(n.get("kind"), _NOTIFY_KINDS))
    dem = data.header.get("demotion") if isinstance(data.header.get("demotion"), dict) else None
    doc["unattended"] = {"notifications": _sorted(nk), "stops": sum(v for k, v in nk.items() if k in ("stop", "waiting", "demoted", "stall", "watchdog")),
                         "demotion": {"reason": token(dem.get("reason"), default="other"), "item": _item(dem.get("item")) if dem.get("item") else ""} if dem else None,
                         "progress_present": bool(data.unattended), "stops_per_run": round(sum(v for k, v in nk.items() if k in ("stop", "waiting", "demoted", "stall", "watchdog"))
                                                                                          / float(len(runs)), 2) if runs and nk else None}

    # -- parallel ------------------------------------------------------------------------------------------------------
    pe = {}  # type: Dict[str, int]
    for e in ev:
        if e.get("event") in _PARALLEL_EVENTS:
            _count(pe, str(e["event"]))
    casc = data.header.get("cascades") if isinstance(data.header.get("cascades"), dict) else {}
    wall = doc["span"]["wall_s"]
    busy = sum(spans.values())
    doc["parallel"] = {"events": _sorted(pe), "cascades": {_item(k): int(v) for k, v in list(casc.items())[:20] if isinstance(v, int) and not isinstance(v, bool)},
                       "item_seconds": busy, "wall_s": wall, "overlap": round(busy / float(wall), 2) if wall and busy else None,
                       "overlap_note": "item_seconds / wall_s: 1.0 is serial, 2.0 means two items were in flight on average; compare with a serial run of the same plan"}
    return doc


_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,39}\Z")


def _code(v: Any) -> str:
    """A park code (`ESCALATION_NEEDED`, `merge_conflict`): letters, digits and `_`, else `other`."""
    return v if isinstance(v, str) and _CODE_RE.match(v) else "other"


def _item(v: Any) -> str:
    return v if common.valid_item_id(v) else "item"


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """What moved between two retro documents (two milestones, or the same plan before and after a rule merged): the fixture metrics of the exit
    criterion "one retro produces a merged rule PR that changes a fixture metric". Negative is better for every number here."""
    def total(d: Dict[str, Any], *path: str) -> int:
        for k in path:
            d = d.get(k, {}) if isinstance(d, dict) else {}
        return sum(d.values()) if isinstance(d, dict) else (d if isinstance(d, int) else 0)
    out = {"failures": total(after, "failures", "total") - total(before, "failures", "total"),
           "rescues": total(after, "rescues", "total") - total(before, "rescues", "total"),
           "attempts_per_item": None, "brief_gaps": total(after, "reports", "brief_gaps_by_domain") - total(before, "reports", "brief_gaps_by_domain"),
           "by_reason": {}}  # type: Dict[str, Any]
    for k in set(before["failures"]["by_reason"]) | set(after["failures"]["by_reason"]):
        out["by_reason"][k] = after["failures"]["by_reason"].get(k, 0) - before["failures"]["by_reason"].get(k, 0)
    if before["flow"]["items"] and after["flow"]["items"]:
        out["attempts_per_item"] = round(after["flow"]["attempts"] / float(after["flow"]["items"]) - before["flow"]["attempts"] / float(before["flow"]["items"]), 3)
    out["by_reason"] = {k: out["by_reason"][k] for k in sorted(out["by_reason"], key=lambda x: (out["by_reason"][x], x))}
    return out


def _meaningful(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    head = t.split(None, 1)[0].strip(".:-").lower() if t else ""
    return bool(t) and head not in ("none", "n/a", "na", "nothing", "no")


def _item_spans(events: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Seconds from an item's first `begin` (or `admit`) to its `merged` event."""
    first, done = {}, {}  # type: Dict[str, float], Dict[str, float]
    for e in events:
        t = event_time(e)
        iid = e.get("item")
        if t is None or not isinstance(iid, str):
            continue
        if e.get("event") in ("begin", "admit") and iid not in first:
            first[iid] = t
        elif e.get("event") in ("merged", "merged_in_git"):
            done[iid] = t
    return {i: int(done[i] - first[i]) for i in done if i in first and done[i] >= first[i]}


def render(doc: Dict[str, Any]) -> str:
    L = ["# Retro: milestone %s" % common.md_text(doc["milestone"], 40), ""]
    sp = doc["span"]
    L.append("%d item(s); %d run(s); %d event(s)%s." % (len(doc["items"]), len(sp["runs"]), sp["events"], "; " + sp["first"] + " to " + sp["last"] if sp["first"] else ""))
    f = doc["flow"]
    L += ["", "## Flow", "", "- items by state: %s" % _kv(f["by_status"]), "- attempts: %d (retries: %s)" % (f["attempts"], _kv(f["retries"]) or "none")]
    if f["item_wall_s"]:
        L.append("- wall time per merged item: %s" % ", ".join("%s %ds" % (i, s) for i, s in f["item_wall_s"].items()))
    fl = doc["failures"]
    L += ["", "## Failures", "", "- %d in all; by class: %s" % (fl["total"], _kv(fl["by_class"]) or "none"), "- by reason: %s" % (_kv(fl["by_reason"]) or "none")]
    if fl["env_share"] is not None:
        L.append("- `env` share: %s (G0's owner metric; it should fall as the doctor and the toolchain pins improve)" % fl["env_share"])
    if fl["parks"]:
        L.append("- parks: %s" % _kv(fl["parks"]))
    rs = doc["rescues"]
    L += ["", "## Rescues (human control-flow interventions)", "", "- %d in all: %s%s" % (rs["total"], _kv(rs["by_kind"]) or "none",
                                                                                           "; %s per run" % rs["per_run"] if rs["per_run"] is not None else ""), "- " + rs["kill_criterion"]]
    L += ["", "## Gates (owner metrics)", ""]
    for g, r in doc["gates"].items():
        rep = ", ".join("%s x%d" % (k, v) for k, v in list(r["ids"].items())[:4])
        L.append("- %s: %d run(s), %d pass, %d fail, %d error, %d blocking%s" % (g, r["runs"], r["pass"], r["fail"], r["error"], r["blocking"], "; finding ids: " + rep if rep else ""))
    if not doc["gates"]:
        L.append("- no gate event in this data")
    sp2 = doc["spend"]
    L += ["", "## Spend", "", "- $%.2f in all (%s unknown entr%s)%s" % (sp2["usd"], sp2["unknown_entries"], "y" if sp2["unknown_entries"] == 1 else "ies",
                                                                        "; $%.2f per merged item" % sp2["per_merged_item"] if sp2["per_merged_item"] is not None else "")]
    r = doc["reports"]
    L += ["", "## Reports and primers", "", "- %d report(s) on disk; report_trailers is %s; %s" % (r["read"], r["trailers"]["mode"], r["trailers"]["advice"]),
          "- brief gaps by domain: %s" % (_kv(r["brief_gaps_by_domain"]) or "none"), "- primer deltas by domain: %s" % (_kv(r["primer_deltas_by_domain"]) or "none"),
          "- doc deltas by domain: %s" % (_kv(r["doc_deltas_by_domain"]) or "none")]
    if "primer" in doc:
        p = doc["primer"]
        L.append("- empty Primer Delta streak: %d structural item(s) in a row%s" % (p["streak"], " (FLAG: the primers are probably stale; read %s)" % ", ".join(p["items"]) if p["flag"] else ""))
    L += ["", "## Critics", ""]
    for c in doc["critics"]["critics"]:
        L.append("- %s: %d logged, %d fixed, %d overruled, %d false positive, %d open; precision %s (%s)" % (
            c["critic"], c["logged"], c["fixed"], c["overruled"], c["false_positive"], c["open"], c["precision"] if c["precision"] is not None else "n/a", c["verdict"]))
    if not doc["critics"]["critics"]:
        L.append("- no blocker logged yet (`loopctl calibration scan --write`)")
    u = doc["unattended"]
    L += ["", "## Unattended and parallel", "", "- notifications: %s; stops %s" % (_kv(u["notifications"]) or "none", u["stops"]),
          "- demotion: %s" % (("%s on %s" % (u["demotion"]["reason"], u["demotion"]["item"] or "the run")) if u["demotion"] else "none")]
    p2 = doc["parallel"]
    L.append("- parallel events: %s; item time / wall time: %s" % (_kv(p2["events"]) or "none", p2["overlap"] if p2["overlap"] is not None else "n/a"))
    if p2["cascades"]:
        L.append("- reject cascades: %s" % _kv(p2["cascades"]))
    if doc["problems"]:
        L += ["", "## Data problems", ""] + ["- " + common.md_text(p, 160) for p in doc["problems"]]
    L += ["", "Next: `%s` drafts rule and primer changes from the repeats above. A person approves every one (H6)." % kitpaths.script_cmd("loopctl", "gaps"), ""]
    return "\n".join(L)


def _kv(d: Dict[str, Any]) -> str:
    return ", ".join("%s %s" % (k, v) for k, v in d.items())
