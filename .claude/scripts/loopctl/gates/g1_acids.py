"""G1: acceptance-criterion ids trace from plan to tests to executed evidence (report section 9 G1, L-36, CO-5).

For every criterion `AC-<item>-<n>` of the item:

1. the criterion still has the id and text that the human approved (H1: the digest recorded at plan approval); a changed criterion is
   not a failing test, it is a `needs_human` (`ac_changed`), and the finding says so;
2. at least one test file at HEAD names the id (plan -> tests; found with one `git grep`, so a test that merely lives in a folder called
   `tests` does not count);
3. an executed, passing result for it is on record for THIS head, from G3 or G6 (tests -> evidence). A result recorded for an older head
   is stale and does not count.

The matrix a human reads (`loopctl status --matrix`) is built from the same results. Owner metric: AC coverage (100 percent of criteria
have at least one executed test).
"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import common, plan as plan_mod
from . import GateResult
from . import _lib

NAME = "g1"
OWNER_METRIC = "AC coverage: 100% of acceptance criteria have at least one executed test"
REQUIRED_FOR = (("verifying", "reviewing"),)
EVIDENCE_GATES = ("g3", "g6")


def _referenced(ctx: Any, ids: List[str], visible: List[str]) -> Dict[str, List[str]]:
    """{ac_id: [test files at HEAD naming it]}. When the plan pins visible acceptance tests, only those files can trace a criterion: an id
    repeated in a test the engineer wrote proves nothing."""
    head = _lib.head_of(ctx)
    pattern = "|".join("(%s)" % _lib.ere_for(a) for a in ids)
    out = _lib.git_ok(ctx, ["grep", "-I", "-i", "-n", "-o", "-E", "-e", pattern, head, "--"])
    found = {a: [] for a in ids}  # type: Dict[str, List[str]]
    pats = {a: _lib.ac_pattern(a) for a in ids}
    for ln in (out or "").splitlines():
        # <sha>:<path>:<line>:<match>
        parts = ln.split(":", 3)
        if len(parts) < 4:
            continue
        path, match = parts[1], parts[3]
        if not _lib.is_test_path(path) or (visible and not _lib.match_any(visible, path)):
            continue
        for a, rx in pats.items():
            if rx.search(match) and path not in found[a] and len(found[a]) < 5:
                found[a].append(path)
    return found


def run(ctx: Any) -> GateResult:
    ids = _lib.ac_ids_of(ctx.item)
    head = _lib.head_of(ctx)
    if not ids:
        return GateResult.failed([_lib.finding("g1.no-criteria", "error", "the plan item has no acceptance criteria")])
    if head is None:
        return GateResult.errored("there is no head commit to trace against")
    findings = []  # type: List[Dict[str, Any]]
    approved = ctx.state_item.get("approvedAcDigest")
    if approved and approved != plan_mod.ac_digest(ctx.item):
        findings.append(_lib.finding("g1.ac-changed", "error", "the acceptance criteria differ from the ones approved at H1: the item goes to needs_human "
                                     "(ac_changed) until a human re-approves the plan"))
    spec = _lib.g3_spec(ctx.item)[0]
    exempt = spec["exempt"]
    refs = _referenced(ctx, ids, spec["visible"])
    rec = ctx.state_item.get("ac") if isinstance(ctx.state_item.get("ac"), dict) else {}
    gates_rec = ctx.state_item.get("gates") if isinstance(ctx.state_item.get("gates"), dict) else {}
    fresh = {g for g in EVIDENCE_GATES if isinstance(gates_rec.get(g), dict) and gates_rec[g].get("head") == head
             and gates_rec[g].get("status") in ("pass", "fail")}
    matrix = {}  # type: Dict[str, Dict[str, Any]]
    covered = 0
    for a in ids:
        cells = rec.get(a) if isinstance(rec.get(a), dict) else {}
        by = [g for g in EVIDENCE_GATES if g in fresh and isinstance(cells.get(g), dict) and cells[g].get("result") == "pass"]
        failed_by = [g for g in EVIDENCE_GATES if g in fresh and isinstance(cells.get(g), dict) and cells[g].get("result") == "fail"]
        if not refs[a] and not exempt:
            findings.append(_lib.finding("g1.ac-untested", "error", "%s is named by no test file at HEAD: write the test that proves it (or ask for the "
                                         "criterion to change in the plan PR)" % a))
        if failed_by:
            findings.append(_lib.finding("g1.ac-failing", "error", "%s fails in %s" % (a, ", ".join(failed_by))))
        elif not by and not exempt:
            why = "no G3 or G6 result is recorded for this head" if not fresh else "no executed passing test is recorded for it"
            findings.append(_lib.finding("g1.ac-no-evidence", "error", "%s has no executed evidence: %s. Run gates g6 and g3 first" % (a, why)))
        if by and refs[a]:
            covered += 1
        if not exempt:
            matrix[a] = {"result": "pass" if by and refs[a] and not failed_by else "fail",
                         "evidence": (",".join(by) or "no evidence") + (" / " + refs[a][0] if refs[a] else "")}
    if exempt:
        findings.append(_lib.finding("g1.exempt", "warn", "ext.g3.exempt (%s): the criteria have no executable tests; a human checks them at review"
                                     % common.clean_text(exempt, 120)))
    ev = {"summary": "AC coverage %d/%d" % (covered, len(ids)), "ac_results": matrix}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
