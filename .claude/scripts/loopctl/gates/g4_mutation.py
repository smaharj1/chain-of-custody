"""G4: mutation testing, tool-agnostic (report section 9 G4, CS-15, CO-6). OPT-IN, EXPERIMENTAL, evidence-gated (IMP-1, RC-10).

The kit bundles no mutation tool (Stryker, mutmut and cargo-mutants differ per stack). `mutation_cmd` is YOUR wrapper around one; this gate is
the contract around it:

* it writes a scope file, a JSON document listing the production lines this item changed (new-side line ranges from `git diff -U0`) and the
  acceptance test globs, and passes its path in `LOOPCTL_MUTATION_SCOPE`; the wrapper mutates only those lines and runs the acceptance tests;
* the wrapper prints one JSON object on its last output line:
  `{"schema": "mutation.v1", "killed": N, "survived": N, "timeout": N}` (counts, not a score; a `score` field is ignored but compared);
* the gate computes `score = killed / (killed + survived + timeout) * 100` and passes when `score >= mutation_min` (default 60).

No mutants generated for changed production code is a failure (a wrapper that mutates nothing proves nothing). A change that touches no
production code (tests and documents only) skips. The gate runs in CI (`mutation.yml`) or locally; it refuses to run until the evidence gate
has passed. Owner metric: the surviving-mutant rate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from .. import common
from . import GateResult
from . import _lib

NAME = "g4"
OWNER_METRIC = "surviving-mutant rate"
DEFAULT_MODE = "off"
EVIDENCE_GATED = True

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".rb", ".cs", ".php", ".swift", ".scala", ".c", ".cc", ".cpp", ".h")


def changed_lines(ctx: Any) -> Dict[str, List[List[int]]]:
    """{path: [[first, last], ...]} of added or modified lines in production code (tests, config and non-code files excluded)."""
    out = _lib.git_ok(ctx, ["diff", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames", ctx.base_sha, _lib.head_of(ctx), "--"])
    res = {}  # type: Dict[str, List[List[int]]]
    cur = None  # type: Optional[str]
    for ln in (out or "").splitlines():
        if ln.startswith("+++ "):
            p = ln[6:] if ln.startswith("+++ b/") else None
            cur = p if p and p.endswith(_CODE_EXT) and not _lib.is_protected(p) else None
        elif cur is not None:
            m = _HUNK_RE.match(ln)
            if m:
                start, n = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
                if n > 0:
                    res.setdefault(cur, []).append([start, start + n - 1])
    return res


def run(ctx: Any) -> GateResult:
    cmd = _lib.command_set(ctx.cfg, "mutation_cmd")
    if not cmd:
        return GateResult.errored("gates.g4 is on but mutation_cmd is not set in the base config: the kit bundles no mutation tool")
    if _lib.head_of(ctx) is None:
        return GateResult.errored("there is no head commit")
    bad_head = _lib.worktree_head_problem(ctx)
    if bad_head:
        return GateResult.errored(bad_head)
    dirty = _lib.uncommitted_changes(ctx)
    if dirty:
        return GateResult.failed([_lib.finding("g4.uncommitted-changes", "error", "tracked files differ from HEAD (%s): mutation would judge code HEAD does not hold" % ", ".join(dirty[:3]))])
    scope_files = changed_lines(ctx)
    if not scope_files:
        return GateResult.skipped("no production code changed: nothing to mutate")
    spec, _p = _lib.g3_spec(ctx.item)
    tmp = tempfile.mkdtemp(prefix="loopctl-g4-")
    try:
        scope = os.path.join(tmp, "scope.json")
        with open(scope, "w", encoding="utf-8") as fh:
            json.dump({"schema": "mutation.scope.v1", "item": ctx.item_id, "base": ctx.base_sha, "head": ctx.head_sha, "files": scope_files,
                       "ac_test_globs": spec["visible"], "ac_ids": _lib.ac_ids_of(ctx.item)}, fh)
        stage, text, _f = _lib.run_command(ctx, "mutation", cmd, {"LOOPCTL_MUTATION_SCOPE": scope, "LOOPCTL_ITEM": ctx.item_id},
                                           timeout_s=int(ctx.cfg.get_int("mutation_timeout_s")), count_tests=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if stage["status"] != "pass":
        cls = "transient" if stage["status"] == "timeout" else "env" if stage.get("exit_code") in (126, 127) else "item"
        return GateResult.failed([_lib.finding("g4.command-failed", "error", "mutation_cmd %s (%s): %s" % (stage["status"], stage.get("reason", ""), _lib.tail(text, 240)))],
                                 failure_class=cls)
    doc = None  # type: Any
    for ln in reversed([x for x in text.splitlines() if x.strip()]):
        try:
            cand = common.strict_loads(ln.strip(), max_depth=3, max_bytes=8192)
        except (ValueError, RecursionError):
            continue
        if isinstance(cand, dict) and cand.get("schema") == "mutation.v1":
            doc = cand
            break
    if doc is None:
        return GateResult.failed([_lib.finding("g4.no-result", "error", "mutation_cmd printed no mutation.v1 JSON object on its output")], failure_class="env")
    vals = {}
    for k in ("killed", "survived", "timeout"):
        v = doc.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return GateResult.failed([_lib.finding("g4.bad-result", "error", "mutation.v1 field %s must be a non-negative integer" % k)], failure_class="env")
        vals[k] = v
    total = vals["killed"] + vals["survived"] + vals["timeout"]
    if total == 0:
        return GateResult.failed([_lib.finding("g4.no-mutants", "error", "no mutants were generated for the %d changed production file(s): the wrapper proves nothing" % len(scope_files))],
                                 failure_class="env")
    score = 100.0 * vals["killed"] / total
    need = ctx.cfg.get_int("mutation_min")
    findings = []  # type: List[Dict[str, Any]]
    claimed = doc.get("score")
    if common.is_number(claimed) and abs(float(claimed) - score) > 1.0:
        findings.append(_lib.finding("g4.score-disagrees", "warn", "the tool claims a score of %s; the counts give %.1f. The counts are used" % (claimed, score)))
    ev = {"summary": "mutation score %.1f%% (killed %d, survived %d, timeout %d; need %d)" % (score, vals["killed"], vals["survived"], vals["timeout"], need)}
    if score < need:
        findings.append(_lib.finding("g4.score-low", "error", "mutation score %.1f%% is below mutation_min %d: %d mutant(s) survived the acceptance tests" % (score, need, vals["survived"])))
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
