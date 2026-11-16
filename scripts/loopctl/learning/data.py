"""The run data the learning commands read, loaded once and held in plain dicts and lists (report 7.3, 10.3, 10.5).

Sources, all read-only and all UNTRUSTED as text (an event message or a report section was written by an agent that read untrusted content):

    events/loopctl.jsonl   the driver's, the lock's and the state's events (`event`, `item`, `runId`, `ts`, ...), redacted at write time
    events/notify.jsonl    the notifications sent (`kind`, `level`, `item`, `code`)
    unattended.json        the unattended run's progress record
    the state document     items (status, attempt, counters, gates, history), approvals, and the header (demotion, cascades, breaker)
    spend.jsonl            the ledger
    docs/features/<slug>/reports/*.md   the engineers' reports; .../reviews/*.md and reports/*review*.md the critics' output
    calibration.jsonl      the critic calibration log (calibration.py)

`RunData` can be built by hand from fixtures; `load(ctx)` builds it from a repository. A file that cannot be read, a line that is not JSON,
a report that is a symlink or larger than 1 MiB is skipped and counted in `problems`, never fatal.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from kitlib import jsonio, runstate

from .. import common, report as report_mod
from . import MAX_EVENTS

__all__ = ["RunData", "load", "parse_review_file", "report_domain", "milestone_of", "event_time"]

CALIBRATION_FILE = "calibration.jsonl"
_REVIEW_NAME_RE = re.compile(r"(?i)review|critic")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,50}\.md\Z")


class RunData(object):
    def __init__(self, events: Optional[List[Dict[str, Any]]] = None, notify: Optional[List[Dict[str, Any]]] = None,
                 unattended: Optional[Dict[str, Any]] = None, state: Optional[Dict[str, Any]] = None,
                 plan_items: Optional[Dict[str, Dict[str, Any]]] = None, spend: Optional[List[Dict[str, Any]]] = None,
                 reports: Optional[List[Dict[str, Any]]] = None, reviews: Optional[List[Dict[str, Any]]] = None,
                 calibration: Optional[List[Dict[str, Any]]] = None, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.events = events or []
        self.notify = notify or []
        self.unattended = unattended or {}
        self.state = state or {}
        self.plan_items = plan_items or {}
        self.spend = spend or []
        self.reports = reports or []
        self.reviews = reviews or []
        self.calibration = calibration or []
        self.cfg = cfg or {}
        self.problems = []  # type: List[str]

    # -- views ---------------------------------------------------------------------------------------------------------
    @property
    def items(self) -> Dict[str, Dict[str, Any]]:
        v = (self.state or {}).get("items")
        return v if isinstance(v, dict) else {}

    @property
    def header(self) -> Dict[str, Any]:
        v = (self.state or {}).get("header")
        return v if isinstance(v, dict) else {}

    @property
    def approvals(self) -> List[Dict[str, Any]]:
        v = (self.state or {}).get("approvals")
        return [a for a in v if isinstance(a, dict)] if isinstance(v, list) else []

    def milestones(self) -> List[str]:
        seen = []  # type: List[str]
        for iid in sorted(self.plan_items, key=common.natural_key):
            m = milestone_of(self.plan_items[iid])
            if m not in seen:
                seen.append(m)
        return seen

    def item_ids(self, milestone: Optional[str] = None) -> List[str]:
        return [i for i in sorted(self.plan_items, key=common.natural_key) if milestone is None or milestone_of(self.plan_items[i]) == milestone]

    def for_items(self, ids: List[str]) -> "RunData":
        """The same data narrowed to some items (a milestone). Events and notifications that name no item stay: they belong to the run."""
        keep = set(ids)
        d = RunData(events=[e for e in self.events if not e.get("item") or e.get("item") in keep],
                    notify=[n for n in self.notify if not n.get("item") or n.get("item") in keep],
                    unattended=self.unattended, state=self.state, plan_items={i: self.plan_items[i] for i in ids if i in self.plan_items},
                    spend=[s for s in self.spend if s.get("item") in keep], reports=[r for r in self.reports if r.get("item") in keep],
                    reviews=[r for r in self.reviews if r.get("item") in keep], calibration=[c for c in self.calibration if not c.get("item") or c.get("item") in keep],
                    cfg=self.cfg)
        d.problems = list(self.problems)
        return d


def milestone_of(item: Dict[str, Any]) -> str:
    m = item.get("milestone") if isinstance(item, dict) else None
    return m if isinstance(m, str) and m else "M1"


def event_time(rec: Dict[str, Any]) -> Optional[float]:
    ts = rec.get("ts") if isinstance(rec, dict) else None
    return runstate.parse_iso(ts) if isinstance(ts, str) else None


def report_domain(path: str) -> str:
    """`docs/features/<slug>/reports/backend.md` -> `backend`."""
    return os.path.basename(path)[:-3] if path.endswith(".md") else os.path.basename(path)


# -- reading files safely --------------------------------------------------------------------------------------------

def _read_regular(path: str, limit: int = 1 << 20) -> Optional[str]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or os.path.islink(path) or st.st_size > limit:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _jsonl(path: str, limit: int = MAX_EVENTS) -> List[Dict[str, Any]]:
    text = _read_regular(path, 64 << 20)
    out = []  # type: List[Dict[str, Any]]
    if text is None:
        return out
    for ln in text.splitlines()[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = common.strict_loads(ln, max_depth=8, max_bytes=1 << 20)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_review_file(rel: str, text: str) -> Dict[str, Any]:
    """{role, round, verdict, path}: the role from the file name, the round from `-r<N>` or `(iteration N)`, the verdict from `## Verdict`."""
    name = os.path.basename(rel).lower()
    role = "security-reviewer" if "security" in name else ("design-critic" if ("design" in name or "critic" in name) else "code-reviewer")
    m = re.search(r"-r(\d{1,2})\b", name) or re.search(r"(?i)\(iteration (\d{1,2})\)", text[:400])
    verdict = None
    vm = re.search(r"(?ms)^##\s*Verdict\s*\n(.*?)(?=^##\s|\Z)", text)
    if vm:
        found = re.findall(r"\b(CODE_REVIEW_APPROVED|CHANGES_REQUESTED|DESIGN_APPROVED|DESIGN_NEEDS_REVISION|SECURITY_APPROVED|SECURITY_BLOCKED)\b", vm.group(1))
        verdict = found[0] if len(set(found)) == 1 else None
    return {"role": role, "round": int(m.group(1)) if m else 1, "verdict": verdict, "path": rel}


def _load_reports(repo: str, plan_items: Dict[str, Dict[str, Any]], data: RunData) -> None:
    import pr_prepare
    for iid in sorted(plan_items, key=common.natural_key):
        slug = plan_items[iid].get("slug")
        if not common.valid_slug(slug):
            continue
        base = os.path.join(repo, "docs", "features", slug)
        for sub in ("reports", "reviews"):
            d = os.path.join(base, sub)
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for fn in names:
                if not _NAME_RE.match(fn):
                    data.problems.append("skipped a file in docs/features/%s/%s whose name is not lower-case words and digits" % (slug, sub))
                    continue
                rel = "docs/features/%s/%s/%s" % (slug, sub, fn)
                text = _read_regular(os.path.join(d, fn))
                if text is None:
                    data.problems.append("skipped %s (unreadable, a symlink or over 1 MiB)" % rel)
                    continue
                if sub == "reviews" or _REVIEW_NAME_RE.search(fn):
                    rv = parse_review_file(rel, text)
                    rv.update(item=iid, slug=slug, text=text[:60000])
                    data.reviews.append(rv)
                    continue
                rep = pr_prepare.parse_report(text)
                res = report_mod.parse_report_text(text)
                data.reports.append({
                    "item": iid, "slug": slug, "domain": fn[:-3], "path": rel, "status": rep["status"], "iteration": rep["iteration"],
                    "gaps": rep["sections"].get("Open Questions / Brief Gaps", ""), "primer_delta": rep["sections"].get("Primer Delta", ""),
                    "doc_delta": rep["sections"].get("Doc Delta", ""),
                    "primer_delta_text": pr_prepare._delta_text(rep["sections"], "Primer Delta"),
                    "doc_delta_text": pr_prepare._delta_text(rep["sections"], "Doc Delta"),
                    "issues": [dict(i) for i in res.issues if i["kind"] != "no-trailer"]})


def load(ctx: Any, milestone: Optional[str] = None) -> RunData:
    """Everything the learning commands need from a repository (`ctx` is a `loopctl.context.Context`)."""
    rs = ctx.rs
    items = {}  # type: Dict[str, Dict[str, Any]]
    if ctx.plan is not None and not getattr(ctx.plan, "fatal", True):
        items = {i: v for i, v in ctx.plan.items.items() if isinstance(v, dict)}
    data = RunData(plan_items=items)
    data.events = _jsonl(rs.event_path("loopctl"))
    data.notify = _jsonl(rs.event_path("notify"))
    data.calibration = _jsonl(os.path.join(rs.root, CALIBRATION_FILE))
    up = jsonio.read_json_opt(os.path.join(rs.root, "unattended.json"))
    data.unattended = up if isinstance(up, dict) else {}
    try:
        data.spend = [e for e in ctx.ledger().entries()[0] if isinstance(e, dict)]
    except (OSError, ValueError, common.LoopctlError):
        data.problems.append("the spend ledger could not be read")
    if ctx.plan is not None and getattr(ctx.plan, "name", None):
        try:
            st, _head = ctx.store().read()
            data.state = st.d if st is not None else {}
        except common.LoopctlError as exc:
            data.problems.append("the state could not be read: %s" % common.clean_text(exc, 120))
    for key in ("report_trailers", "gaps_min_occurrences", "calibration_min_samples", "harness_min_item_runs", "docs_delta_min_words", "max_item_loc"):
        try:
            data.cfg[key] = ctx.cfg.get(key)
        except Exception:  # noqa: BLE001 - a key the registry lacks reads as unset
            data.cfg[key] = None
    _load_reports(ctx.repo, items, data)
    return data.for_items(data.item_ids(milestone)) if milestone else data
