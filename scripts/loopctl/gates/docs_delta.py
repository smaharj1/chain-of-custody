"""Docs Delta: the diff and the engineers' `## Doc Delta` agree, and what the diff adds that a person must know is written down (report 19.7 F-30, 16.1 row 10).

"The engineer report gains `## Doc Delta`: README, API reference, env vars, runbook. Ph 0b adds it as text, and code-reviewer blocks on an undocumented new
env var or endpoint. The gate in Ph 10 checks that the delta matches the diff."

The judgement is `learning/docsdelta.py` (pure, tested on its own); this module reads base..head and the item's reports from git and records the findings:

* `docs.undeclared` (error): the diff adds an environment variable, a route or a command-line flag, and neither an added documentation line nor a Doc Delta
  names it. `docs.no-delta`, `docs.unchecked`, `docs.no-report` (error when there is something to document): the report has no Doc Delta, or it is
  whitespace only, or it says NONE with no `checked:` line, and the diff adds something to document.
* `docs.reported-not-done` (warn): named in a Doc Delta, not yet in any documentation file. The protocol allows "otherwise report it"; a person still writes it.
* `docs.insubstantial` (warn): a documentation file changed and added fewer than `docs_delta_min_words` new words. A blank line, whitespace, a reflow,
  punctuation or a deletion is ignored; it cannot turn a failing gate into a passing one.

`report` by default (it runs, records, and never blocks); switch `gates.docs: on` when its false-positive rate is known. It reads files from git at the head
commit, so an uncommitted doc is not evidence, and the feature's own scratch folder `docs/features/**` (where the report lives) is not documentation.
Owner metric: undocumented endpoints, variables and flags reaching review.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from .. import common
from ..learning import docsdelta
from . import GateResult
from . import _lib

NAME = "docs"
OWNER_METRIC = "undocumented endpoints, env vars and flags that reached review (docs.undeclared per item)"
DEFAULT_MODE = "report"
REQUIRED_FOR = (("verifying", "reviewing"),)


def _deltas(ctx: Any, head: str) -> List[Dict[str, Any]]:
    prefix = "docs/features/%s/reports/" % ctx.item.get("slug", "")
    out = []  # type: List[Dict[str, Any]]
    for path in sorted(_lib.tree_files(ctx, head)):
        name = os.path.basename(path)
        if not path.startswith(prefix) or "/" in path[len(prefix):] or not name.endswith(".md") or "review" in name or "critic" in name:
            continue
        text = _lib.blob(ctx, head, path)
        if text is None:
            continue
        d = docsdelta.parse_delta(text)
        d["name"] = name[:-3]
        out.append(d)
    return out


def run(ctx: Any) -> GateResult:
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    diff = _lib.git_ok(ctx, ["diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", ctx.base_sha, head, "--"])
    if diff is None:
        return GateResult.errored("git diff base..head failed")
    min_words = ctx.cfg.get_int("docs_delta_min_words") or 5
    res = docsdelta.evaluate(docsdelta.parse_diff(diff), _deltas(ctx, head), min_words)
    findings = []  # type: List[Dict[str, Any]]
    for f in res["findings"]:
        extra = {k: f[k] for k in ("name", "reason") if k in f}
        findings.append(_lib.finding(f["id"], f["severity"], f["message"], **extra))
    errs = [f for f in findings if f["severity"] == "error"]
    evidence = {"summary": res["summary"], "triggers": [{"kind": t["kind"], "name": common.clean_text(t["name"], 60)} for t in res["triggers"]][:20]}
    if errs:
        return GateResult.failed(findings, "item", **evidence)
    return GateResult("pass", findings=findings, evidence=evidence)
