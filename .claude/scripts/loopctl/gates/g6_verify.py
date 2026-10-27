"""G6: verify (report section 9 G6; wraps the standalone `verify.py` of Phase 3, RC-7, CO-11).

`verify.py` runs the kit-owned definition (the commands come from the BASE sha's loop.config, never from `package.json` scripts or the
branch) in its own process groups under an allowlisted environment: build, lint, typecheck, the item's targeted tests, the whole suite
(exit 0 and more than zero tests collected) and the smoke stage with port discipline. This gate runs it and then proves what verify
alone does not:

* **per-AC executed and passed**: every criterion of the item has a passing test line in the output (or a G3 pass recorded for this head);
* **zero skip, xfail or todo among the AC tests**, and a skipped count that does not rise, statically (markers in the changed test
  files) and at runtime (against the baseline G0 recorded);
* **the base's per-file test-name set is a subset of head's**: a test that disappeared is a finding, a rename is followed;
* **migrations up, down, up on an ephemeral database of the production dialect** (`migrate_check_cmd`; SQLite only when `db_local` says
  SQLite). A lossy migration's down is never run. No such database available is an `env` failure, which caps the level at L1 (RC-21).

Owner metric: the verify-pass-but-CI-fail rate, which should be zero.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from kitlib import jsonio

from .. import common
from . import GateResult
from . import _acparse, _acrun, _astscan, _lib

NAME = "g6"
OWNER_METRIC = "verify-pass-but-CI-fail rate (should be 0)"
REQUIRED_FOR = (("verifying", "reviewing"),)

_CLASS = {"none": "item", "config": "env", "env": "env", "timeout": "transient", "leak": "transient"}


def _verify_output(res: Dict[str, Any]) -> str:
    """The text of the item-tests and tests stages from verify's log (a passing stage keeps no tail in the JSON)."""
    log = res.get("log")
    if not isinstance(log, str):
        return ""
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(4 * 1024 * 1024)
    except OSError:
        return ""
    parts = re.split(r"(?m)^===== (\S+) =====$", text)
    out = []  # type: List[str]
    for i in range(1, len(parts) - 1, 2):
        if parts[i] in ("item-tests", "tests"):
            out.append(parts[i + 1])
    return "\n".join(out)


def _baseline_skipped(ctx: Any) -> Optional[int]:
    path = os.path.join(_lib.runstate_of(ctx).root, "baseline", "%s.json" % ctx.item_id)
    try:
        d = jsonio.read_json_opt(path)
    except (ValueError, OSError):
        return None
    if isinstance(d, dict) and d.get("base") == ctx.base_sha and isinstance(d.get("skipped"), int):
        return d["skipped"]
    return None


def _static(ctx: Any, ids: List[str], findings: List[Dict[str, Any]]) -> None:
    """Test names that vanished, skip markers added, skipped AC tests."""
    head = _lib.head_of(ctx)
    removed = []  # type: List[str]
    weaker = []  # type: List[str]
    delta = 0
    for status, path, old in _lib.diff_status(ctx, renames=True):
        if not _lib.is_test_path(path) and not (old and _lib.is_test_path(old)):
            continue
        src_path = old or path
        base_text = _lib.blob(ctx, ctx.base_sha, src_path) if status != "A" else None
        head_text = _lib.blob(ctx, head, path) if status != "D" else None
        if base_text is not None and not _lib.is_binary(base_text):
            names_b = _astscan.test_names(src_path, base_text)
            names_h = _astscan.test_names(path, head_text) if head_text is not None else set()
            for n in sorted(names_b - names_h):
                removed.append("%s::%s" % (src_path, n))
            delta -= len(_astscan.skip_markers(src_path, base_text))
        if head_text is not None and not _lib.is_binary(head_text):
            delta += len(_astscan.skip_markers(path, head_text))
            if base_text is not None and _astscan.language(path) == "python":
                b_fns, h_fns = _astscan.py_test_functions(base_text), _astscan.py_test_functions(head_text)
                if b_fns is not None and h_fns is not None:
                    before = {f["name"]: f for f in b_fns}
                    for f in h_fns:
                        old = before.get(f["name"])
                        if old and (f["real"] < old["real"] or f["fake"] > old["fake"]):
                            weaker.append("%s::%s (real assertions %d -> %d, constant ones %d -> %d)" % (path, f["name"], old["real"], f["real"], old["fake"], f["fake"]))
    if removed:
        findings.append(_lib.finding("g6.test-removed", "error", "%d test(s) present at the base are gone at HEAD: %s%s" % (
            len(removed), ", ".join(common.clean_text(r, 80) for r in removed[:3]), ", ..." if len(removed) > 3 else "")))
    if weaker:
        findings.append(_lib.finding("g6.assertion-weakened", "error", "%d existing test(s) assert less than at the base: %s%s. A deleted or loosened assertion is the reward hack "
                                     "this gate exists for" % (len(weaker), "; ".join(common.clean_text(w, 100) for w in weaker[:3]), ", ..." if len(weaker) > 3 else "")))
    if delta > 0:
        findings.append(_lib.finding("g6.skipped-increased", "error", "the changed test files carry %d more skip, xfail, only or todo marker(s) than at the base" % delta))
    # skip markers on tests that name a criterion
    pattern = "|".join("(%s)" % _lib.ere_for(a) for a in ids)
    out = _lib.git_ok(ctx, ["grep", "-I", "-i", "-l", "-E", "-e", pattern, head, "--"]) or ""
    pats = {a: _lib.ac_pattern(a) for a in ids}
    for ln in out.splitlines():
        path = ln.split(":", 1)[1] if ":" in ln else ln
        if not _lib.is_test_path(path):
            continue
        text = _lib.blob(ctx, head, path)
        if text is None:
            continue
        lang = _astscan.language(path)
        if lang == "python":
            for fn in _astscan.py_test_functions(text, pats) or []:
                if fn["acs"] and fn["skipped"]:
                    findings.append(_lib.finding("g6.skipped-ac-test", "error", "%s::%s names %s and is skipped, xfail or todo" % (path, fn["name"], ", ".join(fn["acs"]))))
        elif lang == "javascript" and _astscan.skip_markers(path, text):
            findings.append(_lib.finding("g6.skipped-ac-test", "warn", "%s names a criterion and has skip, only or todo markers (low confidence: regex reading)" % path, confidence="low"))


def _migrations(ctx: Any, findings: List[Dict[str, Any]], classes: List[str]) -> None:
    migs = [m for m in (ctx.item.get("migrations") or []) if isinstance(m, dict)]
    if not migs:
        return
    cmd = _lib.command_set(ctx.cfg, "migrate_check_cmd")
    if not cmd:
        findings.append(_lib.finding("g6.no-prod-dialect-db", "error", "the item carries %d migration(s) but migrate_check_cmd is not set: there is no ephemeral "
                                     "database of the production dialect to run them on (RC-21). The doctor caps the level at L1 until there is" % len(migs)))
        classes.append("env")
        return
    expected = str(ctx.cfg.get("db_local"))
    if not ctx.cfg.is_set("db_local"):
        findings.append(_lib.finding("g6.db-local-unset", "warn", "db_local is not set explicitly; the default (%s) was assumed to be the production dialect" % expected))
    for m in migs:
        lossy = m.get("dataLossClass") == "lossy"
        env = {"LOOPCTL_ITEM": ctx.item_id, "LOOPCTL_MIGRATION": str(m.get("name")), "LOOPCTL_MIGRATION_UP": str(m.get("up") or ""),
               "LOOPCTL_MIGRATION_DOWN": "" if lossy else str(m.get("down") or ""), "LOOPCTL_MIGRATION_CLASS": str(m.get("dataLossClass"))}
        stage, text, _f = _lib.run_command(ctx, "migrate_check", cmd, env, count_tests=False)
        label = common.clean_text(m.get("name"), 60)
        if stage["status"] != "pass":
            findings.append(_lib.finding("g6.migration-failed", "error", "migration %s: up, down, up failed on the ephemeral database (%s). %s" % (
                label, stage.get("reason", ""), _lib.tail(text, 200))))
            classes.append("transient" if stage["status"] == "timeout" else "item")
            continue
        m_ = re.search(r"(?mi)^dialect:\s*([A-Za-z0-9_.-]+)\s*$", text)
        if not m_:
            findings.append(_lib.finding("g6.dialect-unknown", "error", "migration %s: migrate_check_cmd did not print `dialect: <engine>`, so the engine cannot be "
                                         "compared with db_local (%s)" % (label, expected)))
            classes.append("env")
        elif m_.group(1).lower() != expected.lower():
            findings.append(_lib.finding("g6.dialect-mismatch", "error", "migration %s ran on %s but db_local is %s: SQLite is acceptable only when production is "
                                         "SQLite (DEC-8)" % (label, m_.group(1), expected)))
            classes.append("env")


def run(ctx: Any) -> GateResult:
    import verify
    ids = _lib.ac_ids_of(ctx.item)
    bad_head = _lib.worktree_head_problem(ctx)
    if bad_head:
        return GateResult.errored(bad_head)
    tmp = tempfile.mkdtemp(prefix="loopctl-g6-")
    try:
        items_file = os.path.join(tmp, "items.json")
        with open(items_file, "w", encoding="utf-8") as fh:
            json.dump(_items_doc(ctx), fh)
        res = verify.verify(ctx.worktree, base=ctx.base_sha, item=ctx.item_id, items_file=items_file, attempt=ctx.attempt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    findings = []  # type: List[Dict[str, Any]]
    classes = []  # type: List[str]
    dirty = _lib.uncommitted_changes(ctx)
    if dirty:
        findings.append(_lib.finding("g6.uncommitted-changes", "error", "tracked files differ from HEAD (%s): verify judged code HEAD does not hold. Commit first"
                                     % ", ".join(common.clean_text(d, 50) for d in dirty[:3])))
    for f in res.get("findings", []):
        if isinstance(f, dict):
            findings.append(_lib.finding("g6." + str(f.get("id", "verify")).replace("verify.", ""), f.get("severity", "info"), str(f.get("message", ""))))
    if not res.get("trusted", False):
        findings.append(_lib.finding("g6.untrusted-definition", "error", "verify read its definition from the working tree, not the base sha"))
        classes.append("env")
    status = res.get("status")
    if status == "error":
        findings.append(_lib.finding("g6.verify-error", "error", "verify could not run: %s" % common.clean_text(res.get("message"), 240)))
        classes.append("env")
    elif status != "pass":
        findings.append(_lib.finding("g6.verify-failed", "error", "verify %s (%s): %s" % (status, res.get("failure_class"), common.clean_text(res.get("message"), 200))))
        classes.append(_CLASS.get(str(res.get("failure_class")), "item"))
        for st in res.get("stages", []):
            if isinstance(st, dict) and st.get("status") in ("fail", "timeout", "error") and st.get("output_tail"):
                findings.append(_lib.finding("g6.stage-output", "info", "%s output: %s" % (st.get("name"), _lib.tail(str(st["output_tail"]), 300))))
                break

    # -- what verify alone does not prove
    spec = _lib.g3_spec(ctx.item)[0]
    allowed = _acrun.allowed_for(ctx, _acrun.visible_files(ctx, spec)) if spec["visible"] else None
    parsed = _acparse.parse(_verify_output(res), ids, allowed)
    gates_rec = ctx.state_item.get("gates") if isinstance(ctx.state_item.get("gates"), dict) else {}
    ac_rec = ctx.state_item.get("ac") if isinstance(ctx.state_item.get("ac"), dict) else {}
    g3_fresh = isinstance(gates_rec.get("g3"), dict) and gates_rec["g3"].get("head") == _lib.head_of(ctx)
    results = {}  # type: Dict[str, Dict[str, Any]]
    exempt = _lib.g3_spec(ctx.item)[0]["exempt"]
    for a in ids:
        v = parsed.verdict(a)
        g3_pass = g3_fresh and isinstance(ac_rec.get(a), dict) and isinstance(ac_rec[a].get("g3"), dict) and ac_rec[a]["g3"].get("result") == "pass"
        if v in ("fail", "error"):
            findings.append(_lib.finding("g6.ac-failed", "error", "%s %s in the verify run: %s" % (a, "errored" if v == "error" else "fails", "; ".join(parsed.tests(a))[:160])))
            results[a] = {"result": "fail", "evidence": (parsed.tests(a) or [""])[0][:110]}
        elif v == "pass" or g3_pass:
            results[a] = {"result": "pass", "evidence": (parsed.tests(a) or ["g3 pass recorded for this head"])[0][:110]}
        elif v == "skipped":
            findings.append(_lib.finding("g6.ac-skipped", "error", "%s is skipped in the verify run; a skipped criterion is not a pass" % a))
            results[a] = {"result": "fail", "evidence": "skipped"}
        elif not exempt and status == "pass":
            findings.append(_lib.finding("g6.ac-not-executed", "error", "%s was not executed in the verify run: no test line names it (run the suite verbosely and name the "
                                         "test with the criterion id), and no G3 pass is recorded for this head" % a))
            results[a] = {"result": "fail", "evidence": "not executed"}
    tests_stage = next((s for s in res.get("stages", []) if isinstance(s, dict) and s.get("name") == "tests"), None)
    base_skip = _baseline_skipped(ctx)
    if tests_stage and base_skip is not None and isinstance(tests_stage.get("skipped_tests"), int) and tests_stage["skipped_tests"] > base_skip:
        findings.append(_lib.finding("g6.skipped-increased", "error", "%d test(s) skipped, the baseline had %d: the skipped count may not rise" % (tests_stage["skipped_tests"], base_skip)))
    try:
        _static(ctx, ids, findings)
    except ValueError as exc:
        findings.append(_lib.finding("g6.static-unreadable", "error", "cannot read the diff: %s" % common.clean_text(exc, 100)))
        classes.append("env")
    _migrations(ctx, findings, classes)

    ev = {"summary": "verify %s; %d/%d criteria executed and passed" % (status, sum(1 for r in results.values() if r["result"] == "pass"), len(ids)),
          "ac_results": results}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, failure_class=_lib.worst_class(classes) if classes else "item", **ev)
    return GateResult("pass", findings=findings, evidence=ev)


def _items_doc(ctx: Any) -> Dict[str, Any]:
    from .. import plan as plan_mod
    return plan_mod.verify_items_file(ctx.plan)
