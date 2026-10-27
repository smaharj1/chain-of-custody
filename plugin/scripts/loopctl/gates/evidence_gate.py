"""Evidence (report section 9, F-35, CS-18): the proof that travels with the work is complete, current and, once there is a PR, green in CI.

"A CI check run on the merge-candidate tree is the evidence. The local evidence file is a convenience. Required status checks are the
gate." So this gate does three things:

1. **Completeness.** Every other gate whose mode is `on` (read at the base sha) has a recorded PASS. Gates that judge the code (everything
   after building) must have passed for THIS head: a pass for an older commit is stale unless the only change since is a saved report or
   review under `docs/features/<slug>/reports/`. The pre-build gates (G0, G2) judged the start of the
   item and only need to have passed.
2. **The local evidence file**, written for the human and the audit block: `<git-common-dir>/loopctl/evidence/<item>-a<n>.json` with the
   per-gate result, the AC x evidence matrix, the base and head shas and a digest. It is a convenience; nothing trusts it.
3. **CI.** From `pr_open` and `awaiting_human`, with a delivery that opens PRs (`delivery` other than `local`), the PR's check overlay
   (folded in by `reconcile --prs` from recorded `gh` output) must be `pass`. Missing or pending is not a pass.

Owner metric: none (it is the aggregator of the others). Needed at every move from `reviewing` onward.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from kitlib import jsonio

from .. import common
from . import GateResult
from . import _lib

NAME = "evidence"
OWNER_METRIC = "none (aggregates the other gates' recorded results)"
REQUIRED_FOR = (("reviewing", "pr_open"), ("reviewing", "awaiting_human"), ("reviewing", "merged"), ("pr_open", "awaiting_human"),
                ("pr_open", "merged"), ("awaiting_human", "merged"))
PRE_BUILD = ("g0", "g2")
SKIPPABLE = ("g4",)             # G4 skips when no production code changed; a skip there is not a missing gate


def run(ctx: Any) -> GateResult:
    from . import discover, mode_for
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    gates_rec = ctx.state_item.get("gates") if isinstance(ctx.state_item.get("gates"), dict) else {}
    findings = []  # type: List[Dict[str, Any]]
    summary = {}  # type: Dict[str, Any]
    for name, mod in sorted(discover().items()):
        if name.startswith("!") or name == NAME:
            continue
        if mode_for(ctx.cfg, name, mod) != "on":
            continue
        rec = gates_rec.get(name)
        ok = isinstance(rec, dict) and (rec.get("status") == "pass" or (rec.get("status") == "skipped" and name in SKIPPABLE))
        if not ok:
            findings.append(_lib.finding("evidence.gate-missing", "error", "gate %s is on but has no recorded pass for this item (%s)" % (
                name, "status " + str(rec.get("status")) if isinstance(rec, dict) else "never run")))
            continue
        if name not in PRE_BUILD and rec.get("head") and not _lib.same_code(ctx, rec["head"], head):
            findings.append(_lib.finding("evidence.stale", "error", "gate %s passed for %s, not for HEAD %s: its evidence is stale" % (name, str(rec["head"])[:10], head[:10])))
            continue
        summary[name] = {"status": rec.get("status"), "head": rec.get("head"), "at": rec.get("at")}
    status = ctx.state_item.get("status")
    delivery = str(ctx.cfg.get("delivery"))
    overlay = ctx.state_item.get("overlay") if isinstance(ctx.state_item.get("overlay"), dict) else None
    ci = "n/a"
    if status in ("pr_open", "awaiting_human") and delivery != "local":
        if overlay is None:
            findings.append(_lib.finding("evidence.ci-unknown", "error", "no PR check state is recorded for this item: run `loopctl reconcile --prs <gh json>` so the check run "
                                         "on the merge-candidate tree is known (the required status check is the gate)"))
            ci = "unknown"
        elif overlay.get("checks") != "pass":
            findings.append(_lib.finding("evidence.ci-not-green", "error", "CI on PR #%s is %s, not pass: the check run is the evidence" % (overlay.get("pr"), overlay.get("checks"))))
            ci = str(overlay.get("checks"))
        else:
            ci = "pass"
    elif delivery == "local":
        findings.append(_lib.finding("evidence.local-only", "info", "delivery is local: no CI check run exists, the recorded gate results are the evidence"))
    doc = {"schema": "loopctl.evidence.v1", "item": ctx.item_id, "attempt": ctx.attempt, "base": ctx.base_sha, "head": head, "gates": summary,
           "ac": ctx.state_item.get("ac") if isinstance(ctx.state_item.get("ac"), dict) else {}, "review": ctx.state_item.get("review") or {},
           "ci": ci, "written": common.iso()}
    doc["digest"] = common.sha256_hex(common.canonical_json({k: v for k, v in doc.items() if k != "written"}))
    try:
        d = os.path.join(_lib.runstate_of(ctx).root, "evidence")
        jsonio.ensure_dir(d, 0o700)
        jsonio.atomic_write_json(os.path.join(d, "%s-a%d.json" % (ctx.item_id, ctx.attempt)), doc, 0o600)
    except (OSError, ValueError) as exc:
        findings.append(_lib.finding("evidence.file", "warn", "the local evidence file could not be written (%s); it is a convenience" % common.clean_text(exc, 80)))
    ev = {"summary": "%d gate(s) recorded, ci %s, digest %s" % (len(summary), ci, doc["digest"][:12])}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
