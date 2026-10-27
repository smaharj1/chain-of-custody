"""Fixture run data for the Phase 10 tests: a three-item milestone with the repeats a retro should find."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loopctl.learning.data import RunData

BASE_TS = 1767225600          # 2026-01-01T00:00:00Z


def ts(n: int) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(BASE_TS + n))


def plan_items(ids=("A", "B", "C"), milestone="M1") -> Dict[str, Dict[str, Any]]:
    return {i: {"id": i, "milestone": milestone, "slug": "slice-" + i.lower(), "title": "Item " + i,
                "touches": {"domains": ["backend"], "paths": ["src/api/**", "tests/api/**"]}} for i in ids}


def ev(n: int, name: str, **kw: Any) -> Dict[str, Any]:
    d = {"event": name, "source": "driver", "runId": "run-1", "ts": ts(n)}
    d.update(kw)
    return d


def report(item: str, domain: str = "backend", gaps: str = "NONE", primer: str = "NONE\nchecked: layering", doc: str = "NONE\nchecked: README",
           issues: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    nz = lambda t: "" if t.split(None, 1)[0:1] == ["NONE"] else t      # noqa: E731
    return {"item": item, "slug": "slice-" + item.lower(), "domain": domain, "path": "docs/features/slice-%s/reports/%s.md" % (item.lower(), domain), "status": "APPROVED",
            "iteration": "1", "gaps": gaps, "primer_delta": primer, "doc_delta": doc, "primer_delta_text": nz(primer), "doc_delta_text": nz(doc), "issues": issues or []}


def run_data(**over: Any) -> RunData:
    """Three items. Events: A merged cleanly; B failed twice with tests_failed and once with a g5 finding; C failed with tests_failed, env once, and was rescued."""
    events = [
        ev(0, "begin", item="A"), ev(10, "gate", item="A", gate="g5", status="pass", mode="on", blocking=False),
        ev(11, "gate", item="A", gate="g6", status="pass", mode="on", blocking=False), ev(20, "engineer_report", item="A", role="backend-engineer", status="APPROVED"),
        ev(30, "merged", item="A", commit="a" * 40),
        ev(40, "begin", item="B"), ev(50, "report_warning", item="B", role="backend-engineer", mode="warn", kinds=["no-trailer"], count=1),
        ev(51, "engineer_report", item="B", role="backend-engineer", status="APPROVED"),
        ev(60, "gate", item="B", gate="g5", status="fail", mode="on", blocking=True, ids=["g5.protected-path"]),
        ev(61, "fail", item="B", failure_class="item", reason="reward_hack", action="retry"),
        ev(70, "gate", item="B", gate="g6", status="fail", mode="on", blocking=True),
        ev(71, "fail", item="B", failure_class="item", reason="tests_failed", action="retry"),
        ev(80, "merged", item="B", commit="b" * 40),
        ev(90, "begin", item="C"), ev(91, "gate", item="C", gate="g5", status="fail", mode="on", blocking=True, ids=["g5.protected-path"]),
        ev(92, "fail", item="C", failure_class="item", reason="tests_failed", action="retry"),
        ev(93, "fail", item="C", failure_class="env", reason="toolchain_mismatch", action="park"),
        ev(94, "park", item="C", code="ESCALATION_NEEDED", kind="blocked"),
    ]
    state = {"header": {"runStatus": "stopped", "cascades": {"A": 1}, "demotion": {"reason": "stall_storm", "item": "C"}},
             "approvals": [{"scope": "plan", "target": "demo", "by": "Pat", "choice": "approve"}, {"scope": "unblock", "target": "C", "by": "Pat", "choice": "reset"},
                           {"scope": "env", "target": "env", "by": "Pat", "choice": "approve"}],
             "items": {"A": {"status": "merged", "attempt": 1, "counters": {"verify": 0, "review": 0}, "gates": {}},
                       "B": {"status": "merged", "attempt": 2, "counters": {"verify": 2, "review": 0},
                             "gates": {"g5": {"status": "fail", "blocking": True, "findings": [{"id": "g5.protected-path", "severity": "error", "message": "tests/conftest.py was edited"}]}}},
                       "C": {"status": "needs_human", "attempt": 1, "counters": {"verify": 1, "review": 0},
                             "gates": {"g5": {"status": "fail", "blocking": True, "findings": [{"id": "g5.protected-path", "severity": "error", "message": "package.json scripts changed"}]}}}}}
    reports = [report("A", gaps="- the 422 vs 400 rule for validation errors is not in the brief"),
               report("B", gaps="- which status code for a validation failure? I guessed 422", primer="`.claude/context/backend.md` cites src/old/handler.ts which no longer exists"),
               report("C", gaps="NONE")]
    notify = [{"source": "driver", "kind": "started"}, {"source": "driver", "kind": "stop", "item": "C", "code": "ESCALATION_NEEDED"}]
    spend = [{"type": "spend", "runId": "run-1", "item": "A", "total_cost_usd": 0.5}, {"type": "spend", "runId": "run-1", "item": "B", "total_cost_usd": 0.75},
             {"type": "spend", "runId": "run-1", "item": "C", "total_cost_usd": None, "unknown": True}]
    d = RunData(events=events, notify=notify, state=state, plan_items=plan_items(), spend=spend, reports=reports, cfg={"report_trailers": "warn"})
    for k, v in over.items():
        setattr(d, k, v)
    return d
