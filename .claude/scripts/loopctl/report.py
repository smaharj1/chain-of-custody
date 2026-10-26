"""Engineer reports and the JSON trailer (engineer-protocol sections 6, 7, 11, 12; report section 19.7 Primer Delta L-32, D-14).

An engineer writes its own report file and ends its final message with a JSON trailer
`{"status": "APPROVED", "report_path": "docs/features/<slug>/reports/<domain>.md", "iteration": 1}`. SubagentStop cannot fail
closed (P2), so the real enforcement is here, at the next dispatch: a report that does not parse is `malformed_report`
(an `item` failure; loopctl sends it back and the retry uses the review counter).

Malformed means: a required section missing, a Status outside the protocol enum, `NONE` under Primer Delta or Doc Delta
without a `checked:` line, no trailer, a trailer that is not exactly the three keys, a trailer whose status or path disagrees
with the report, a report path outside `docs/features/<slug>/reports/`, a symlink, or a file over 1 MiB.

Parsing is Phase 3's `pr_prepare.parse_report`; this module adds the path rules, the trailer and the verdict.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import common

__all__ = ["ReportResult", "parse_report_text", "parse_trailer", "read_report", "report_path_ok", "MAX_REPORT_BYTES"]

MAX_REPORT_BYTES = 1 << 20
_PATH_RE = re.compile(r"^docs/features/([a-z0-9][a-z0-9._-]{0,60})/reports/([a-z0-9][a-z0-9-]{0,40})\.md\Z")


class ReportResult(object):
    def __init__(self) -> None:
        self.ok = False
        self.status = ""
        self.iteration = ""
        self.problems = []  # type: List[str]
        self.sections = {}  # type: Dict[str, str]
        self.primer_delta = ""
        self.doc_delta = ""
        self.path = None  # type: Optional[str]
        self.trailer = None  # type: Optional[Dict[str, Any]]

    @property
    def malformed(self) -> bool:
        return bool(self.problems)

    @property
    def approved(self) -> bool:
        return self.ok and self.status == "APPROVED"

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "malformed": self.malformed, "status": self.status, "iteration": self.iteration,
                "problems": self.problems, "path": self.path, "primer_delta": common.clean_text(self.primer_delta, 300),
                "doc_delta": common.clean_text(self.doc_delta, 300), "trailer": self.trailer}


def report_path_ok(rel: Any, slug: Optional[str] = None) -> bool:
    """A report lives at docs/features/<slug>/reports/<domain>.md, nowhere else (no traversal, no other directory)."""
    if not isinstance(rel, str) or "\x00" in rel or "\\" in rel:
        return False
    m = _PATH_RE.match(rel)
    return bool(m) and (slug is None or m.group(1) == slug)


def parse_trailer(final_message: str) -> Optional[Dict[str, Any]]:
    """The JSON trailer: the LAST non-empty line of the message. None if absent or not exactly {status, report_path, iteration}."""
    lines = [ln for ln in (final_message or "").splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].strip()
    if last.startswith("```"):
        return None
    try:
        obj = common.strict_loads(last, max_depth=3, max_bytes=4096)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"status", "report_path", "iteration"}:
        return None
    if not isinstance(obj["status"], str) or not isinstance(obj["report_path"], str):
        return None
    if isinstance(obj["iteration"], bool) or not isinstance(obj["iteration"], int) or not 1 <= obj["iteration"] <= 99:
        return None
    return obj


def parse_report_text(text: str, trailer: Optional[Dict[str, Any]] = None, require_trailer: bool = False,
                      expect_path: Optional[str] = None) -> ReportResult:
    import pr_prepare
    res = ReportResult()
    rep = pr_prepare.parse_report(text)
    res.status = rep["status"]
    res.iteration = rep["iteration"]
    res.sections = rep["sections"]
    for m in rep["missing"]:
        res.problems.append("required section '## %s' is missing" % m)
    res.problems.extend(rep["problems"])
    if "Status" not in rep["sections"]:
        res.problems.append("no '## Status' section")
    res.primer_delta = pr_prepare._delta_text(rep["sections"], "Primer Delta")
    res.doc_delta = pr_prepare._delta_text(rep["sections"], "Doc Delta")
    if require_trailer and trailer is None:
        res.problems.append("the final message has no valid JSON trailer {status, report_path, iteration}")
    if trailer is not None:
        res.trailer = trailer
        if trailer.get("status") != res.status:
            res.problems.append("the trailer says %r but the report says %r" % (common.clean_text(trailer.get("status"), 30), common.clean_text(res.status, 30)))
        if not report_path_ok(trailer.get("report_path")):
            res.problems.append("trailer report_path %r is not docs/features/<slug>/reports/<domain>.md" % common.clean_text(trailer.get("report_path"), 60))
        elif expect_path and trailer["report_path"] != expect_path:
            res.problems.append("trailer report_path %r is not the path the brief named (%s)" % (common.clean_text(trailer["report_path"], 60), expect_path))
        if res.iteration and str(trailer.get("iteration")) != res.iteration:
            res.problems.append("the trailer's iteration %s differs from the report's %s" % (trailer.get("iteration"), res.iteration))
    res.ok = not res.problems
    return res


def read_report(repo: str, rel_path: str, final_message: Optional[str] = None, slug: Optional[str] = None,
                require_trailer: bool = False) -> ReportResult:
    """Read a report file by its repo-relative path (validated) and check it, with the trailer if the final message is given."""
    res = ReportResult()
    if not report_path_ok(rel_path, slug):
        res.problems.append("report path %r is not docs/features/%s/reports/<domain>.md" % (common.clean_text(rel_path, 60), slug or "<slug>"))
        return res
    full = os.path.join(repo, rel_path)
    try:
        st = os.lstat(full)
    except OSError:
        res.problems.append("the report file %s does not exist" % rel_path)
        return res
    real = os.path.realpath(full)
    root = os.path.realpath(os.path.join(repo, "docs", "features"))
    if not (os.path.isfile(full) and not os.path.islink(full)) or not real.startswith(root + os.sep) or st.st_size > MAX_REPORT_BYTES:
        res.problems.append("the report is a symlink, not a regular file, outside docs/features, or larger than 1 MiB")
        return res
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    trailer = parse_trailer(final_message) if final_message is not None else None
    out = parse_report_text(text, trailer, require_trailer=require_trailer or final_message is not None, expect_path=rel_path)
    out.path = rel_path
    return out
