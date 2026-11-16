"""Engineer reports and the JSON trailer (engineer-protocol sections 6, 7, 11, 12; report section 19.7 Primer Delta L-32, D-14).

An engineer writes its own report file and ends its final message with a JSON trailer
`{"status": "APPROVED", "report_path": "docs/features/<slug>/reports/<domain>.md", "iteration": 1}`. SubagentStop cannot fail
closed (P2), so the real enforcement is here, at the next dispatch: a report that does not parse is `malformed_report`
(an `item` failure; loopctl sends it back and the retry uses the review counter).

Malformed means: a required section missing, a Status outside the protocol enum, `NONE` under Primer Delta or Doc Delta
without a `checked:` line, no trailer, a trailer that is not exactly the three keys, a trailer whose status or path disagrees
with the report, a report path outside `docs/features/<slug>/reports/`, a symlink, or a file over 1 MiB.

Parsing is Phase 3's `pr_prepare.parse_report`; this module adds the path rules, the trailer and the verdict.

Phase 10 (report 16.1 row 10, DEC-29): `report_trailers: warn | block` decides what happens to the soft problems. Every problem is either
HARD (the driver cannot tell what the engineer decided, or the trailer contradicts the report: never tolerated) or SOFT (the form is
wrong but the decision is readable: a missing trailer, a missing non-Status section, `NONE` without `checked:`). `block` fails the item on
both (the 4a/4c behaviour); `warn`, the default, fails it on hard problems only and records the soft ones as `report_warning` events.
A forged trailer is never soft: a well-formed trailer that disagrees with the report, and a last line that is trailer-shaped JSON a strict
parser refuses (duplicate keys, NaN, nesting), are hard in both modes, so a parser that reads the line differently cannot be played.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import common

__all__ = ["ReportResult", "parse_report_text", "parse_trailer", "inspect_trailer", "read_report", "report_path_ok", "MAX_REPORT_BYTES", "MODES", "HARD", "SOFT"]

MAX_REPORT_BYTES = 1 << 20
MODES = ("warn", "block")
HARD, SOFT = "hard", "soft"
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
        self.issues = []  # type: List[Dict[str, str]]   # {kind, severity: hard|soft, message}; `problems` holds the same messages

    def add(self, kind: str, severity: str, message: str) -> None:
        self.issues.append({"kind": kind, "severity": severity, "message": message})
        self.problems.append(message)

    def blocking(self, mode: str = "block") -> List[str]:
        """The problems that fail the item under `report_trailers: <mode>`. An unknown mode is `block` (fail closed)."""
        return [i["message"] for i in self.issues if mode != "warn" or i["severity"] == HARD]

    def warnings(self, mode: str = "warn") -> List[Dict[str, str]]:
        """The problems that are only recorded under `report_trailers: <mode>` (none under block)."""
        return [] if mode != "warn" else [i for i in self.issues if i["severity"] == SOFT]

    @property
    def malformed(self) -> bool:
        return bool(self.problems)

    @property
    def approved(self) -> bool:
        return self.ok and self.status == "APPROVED"

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "malformed": self.malformed, "status": self.status, "iteration": self.iteration,
                "problems": self.problems, "issues": self.issues, "path": self.path, "primer_delta": common.clean_text(self.primer_delta, 300),
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


def inspect_trailer(final_message: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """(trailer, severity, why). `trailer` is the strict three-key trailer or None. When it is None, `severity` says how bad:
    `soft` (no JSON last line, a code fence, a wrong shape: the engineer forgot the form) or `hard` (the last line is JSON-shaped and
    mentions a status but a strict parser refuses it: duplicate keys, NaN, deep nesting, oversize; a parser that reads it another
    way could be played, so it is never tolerated)."""
    t = parse_trailer(final_message)
    if t is not None:
        return t, "", ""
    lines = [ln for ln in (final_message or "").splitlines() if ln.strip()]
    last = lines[-1].strip() if lines else ""
    if not last:
        return None, SOFT, "the final message is empty"
    if last.startswith("{") and '"status"' in last:
        try:
            json.loads(last)                      # the lenient parser accepts it: the strict one refused it
            try:
                common.strict_loads(last, max_depth=3, max_bytes=4096)
            except (ValueError, RecursionError):
                return None, HARD, "the last line is JSON that a strict parser refuses (duplicate keys, NaN or deep nesting)"
            return None, SOFT, "the last line is JSON but not exactly {status, report_path, iteration} with the right types"
        except ValueError:
            return None, SOFT, "the last line looks like a trailer but is not valid JSON"
        except RecursionError:
            return None, HARD, "the last line is JSON nested deeper than a trailer can be"
    return None, SOFT, "the last line of the final message is not the JSON trailer"


def parse_report_text(text: str, trailer: Optional[Dict[str, Any]] = None, require_trailer: bool = False,
                      expect_path: Optional[str] = None, trailer_problem: Optional[Tuple[str, str]] = None) -> ReportResult:
    """`trailer_problem` is (severity, why) from `inspect_trailer` when the trailer could not be read; without it a missing trailer
    is soft."""
    import pr_prepare
    res = ReportResult()
    rep = pr_prepare.parse_report(text)
    res.status = rep["status"]
    res.iteration = rep["iteration"]
    res.sections = rep["sections"]
    for m in rep["missing"]:
        if m == "Status":
            continue                                   # reported once, below, as hard
        res.add("missing-section", SOFT, "required section '## %s' is missing" % m)
    for p in rep["problems"]:
        if p.startswith("Status "):
            res.add("bad-status", HARD, p)
        else:
            res.add("none-without-checked", SOFT, p)
    if "Status" not in rep["sections"]:
        res.add("no-status", HARD, "no '## Status' section")
    res.primer_delta = pr_prepare._delta_text(rep["sections"], "Primer Delta")
    res.doc_delta = pr_prepare._delta_text(rep["sections"], "Doc Delta")
    if require_trailer and trailer is None:
        sev, why = trailer_problem or (SOFT, "")
        msg = "the final message has no valid JSON trailer {status, report_path, iteration}" + (" (%s)" % why if why else "")
        res.add("trailer-ambiguous" if sev == HARD else "no-trailer", sev, msg)
    if trailer is not None:
        res.trailer = trailer
        if trailer.get("status") != res.status:
            res.add("trailer-mismatch", HARD, "the trailer says %r but the report says %r" % (common.clean_text(trailer.get("status"), 30), common.clean_text(res.status, 30)))
        if not report_path_ok(trailer.get("report_path")):
            res.add("trailer-mismatch", HARD, "trailer report_path %r is not docs/features/<slug>/reports/<domain>.md" % common.clean_text(trailer.get("report_path"), 60))
        elif expect_path and trailer["report_path"] != expect_path:
            res.add("trailer-mismatch", HARD, "trailer report_path %r is not the path the brief named (%s)" % (common.clean_text(trailer["report_path"], 60), expect_path))
        if res.iteration and str(trailer.get("iteration")) != res.iteration:
            res.add("trailer-mismatch", HARD, "the trailer's iteration %s differs from the report's %s" % (trailer.get("iteration"), res.iteration))
    res.ok = not res.problems
    return res


def read_report(repo: str, rel_path: str, final_message: Optional[str] = None, slug: Optional[str] = None,
                require_trailer: bool = False) -> ReportResult:
    """Read a report file by its repo-relative path (validated) and check it, with the trailer if the final message is given."""
    res = ReportResult()
    if not report_path_ok(rel_path, slug):
        res.add("bad-path", HARD, "report path %r is not docs/features/%s/reports/<domain>.md" % (common.clean_text(rel_path, 60), slug or "<slug>"))
        return res
    full = os.path.join(repo, rel_path)
    try:
        st = os.lstat(full)
    except OSError:
        res.add("no-report", HARD, "the report file %s does not exist" % rel_path)
        return res
    real = os.path.realpath(full)
    root = os.path.realpath(os.path.join(repo, "docs", "features"))
    if not (os.path.isfile(full) and not os.path.islink(full)) or not real.startswith(root + os.sep) or st.st_size > MAX_REPORT_BYTES:
        res.add("unsafe-report", HARD, "the report is a symlink, not a regular file, outside docs/features, or larger than 1 MiB")
        return res
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    trailer, tsev, twhy = inspect_trailer(final_message) if final_message is not None else (None, "", "")
    out = parse_report_text(text, trailer, require_trailer=require_trailer or final_message is not None, expect_path=rel_path,
                            trailer_problem=(tsev, twhy) if tsev else None)
    out.path = rel_path
    return out
