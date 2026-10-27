"""G3: acceptance, visible and held-out (report section 9 G3, DEC-11, RC-6, CS-15, CS-16, L-21, L-22).

Visible tests are written by the separate acceptance-author in a fresh context and ride in the plan PR (H2). This gate, at `verifying`:

* the visible tests are exactly as they were at the base (they are pinned; an edited acceptance test is a reward-hack signature);
* every criterion has an executed, PASSING test that names it, and none is skipped (a criterion that did not run is not a pass);
* every test that names a criterion has a real assertion: `assert True`, `expect(true)` and a bare `raises(Exception)` are refused
  (Python by `ast`: an error; other languages by regex: a low-confidence warning);
* the HELD-OUT tests, hidden from the engineer, also pass. They live in `<git-common-dir>/loopctl/heldout/<item>/` and are run only
  under `runner: driver` (the child's sandbox denies reading that directory). Under `sequential` no per-subagent sandbox is documented
  (P42), so held-out tests are CI-only (the `heldout` job, `.claude/templates/github/workflows/heldout.yml`) and this gate says so.
  If the plan declares held-out tests for the item (`ext.g3.heldout: true`), a driver run without them is a failure, not a skip;
* a `characterization` item (a refactor) must be green on the base as well (G2 proved it) and is flagged to the human.

Owner metric: the held-out pass rate against the visible pass rate; a gap signals overfitting. The summary prints both.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .. import common, locked
from . import GateResult
from . import _acrun, _lib

NAME = "g3"
OWNER_METRIC = "held-out pass rate vs visible pass rate (a gap signals overfitting)"
REQUIRED_FOR = (("verifying", "reviewing"),)


def _heldout_files(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    for _d, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def run_heldout(ctx: Any, hdir: str, declared: bool) -> Tuple[List[Dict[str, Any]], List[str], Optional[Dict[str, Dict[str, Any]]], Optional[str]]:
    """Run the held-out tests in `hdir` with `heldout_cmd` (the base config's). Returns (findings, failure classes, per-criterion results or None
    when nothing ran, "passed/reported"). Shared with the CI job (`_ci.py heldout`), where `hdir` is the directory the sync step restored."""
    findings = []  # type: List[Dict[str, Any]]
    classes = []  # type: List[str]
    ids = _lib.ac_ids_of(ctx.item)
    hcmd = _lib.command_set(ctx.cfg, "heldout_cmd")
    if not hcmd:
        findings.append(_lib.finding("g3.heldout-cmd", "error", "held-out tests exist for this item but heldout_cmd is not set in the base config"))
        return findings, ["env"], None, None
    stage, text, hp = _acrun.run_visible(ctx, {"junit": None}, "heldout", hcmd, {"LOOPCTL_HELDOUT_DIR": hdir, "LOOPCTL_BASE": ctx.base_sha})
    bad = _acrun.stage_failure(stage, "g3.heldout")
    if bad:
        findings.append(_lib.finding(bad[0], "error", "the held-out command could not be judged: %s" % stage.get("reason", "")))
        return findings, [bad[1]], None, None
    if not hp.results:
        findings.append(_lib.finding("g3.heldout-not-executed", "error", "the held-out command reported no result for any criterion. %s" % _lib.tail(text, 160)))
        return findings, ["env"], None, None
    passed = 0
    for a, r in hp.results.items():
        if r["verdict"] == "pass":
            passed += 1
        else:
            findings.append(_lib.finding("g3.heldout-failed", "error", "%s fails the held-out check (%s): the visible tests passed, so the build may be "
                                         "overfitted to them" % (a, r["verdict"])))
    missing = [a for a in ids if a not in hp.results]
    if declared and missing:
        findings.append(_lib.finding("g3.heldout-incomplete", "error", "the plan declares held-out tests but none ran for: %s" % ", ".join(missing)))
    return findings, classes, hp.results, "%d/%d" % (passed, len(hp.results))


def _isolation_problem(ctx: Any) -> Optional[str]:
    lvl = str(ctx.cfg.get("autonomy_level") or "L1")
    try:
        n = int(lvl[1:])
    except ValueError:
        n = 1
    attested = ctx.cfg.get("platform_attested") or []
    if n >= 2 and ctx.runner != "driver" and "g3_ci_only" not in attested:
        return ("autonomy_level %s needs held-out isolation (RC-6): runner: driver, or g3_ci_only attested once the heldout job exists" % lvl)
    return None


def run(ctx: Any) -> GateResult:
    spec, problems = _lib.g3_spec(ctx.item)
    findings = list(problems)  # type: List[Dict[str, Any]]
    ids = _lib.ac_ids_of(ctx.item)
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings)
    bad_head = _lib.worktree_head_problem(ctx)
    if bad_head and not spec["exempt"]:
        return GateResult.errored(bad_head)
    if spec["exempt"]:
        return GateResult("pass", findings=[_lib.finding("g3.exempt", "warn", "ext.g3.exempt (%s): this item has no executable acceptance tests; a human checks "
                                                         "its criteria at review" % common.clean_text(spec["exempt"], 120))],
                          evidence={"summary": "exempt"})
    classes = []  # type: List[str]
    dirty = _lib.uncommitted_changes(ctx)
    if dirty:
        findings.append(_lib.finding("g3.uncommitted-changes", "error", "tracked files differ from HEAD (%s): the run would judge code HEAD does not hold. Commit first"
                                     % ", ".join(common.clean_text(d, 50) for d in dirty[:3])))
    files = _acrun.visible_files(ctx, spec)
    if spec["visible"] and not files:
        findings.append(_lib.finding("g3.no-visible-tests", "error", "no file at HEAD matches ext.g3.visible: the pinned acceptance tests are gone"))
    if not spec["visible"]:
        findings.append(_lib.finding("g3.not-pinned", "warn", "the plan item declares no ext.g3.visible: its acceptance tests are not pinned, so an edit to them "
                                     "cannot be told from the work. The command ran as the visible suite"))
    changes, pin = _acrun.pinned_changes(ctx, spec)
    for p, how in changes:
        findings.append(_lib.finding("g3.pinned-edited", "error", "%s is a pinned acceptance test and was %s since %s: a reward-hack signature. "
                                     "The attempt is discarded" % (p, how, "G2 pinned it" if pin else "the base"), reward_hack=True, path=p))
    if files:
        findings.extend(_acrun.assertion_findings(ctx, files, ids))

    # -- visible run
    results = {}  # type: Dict[str, Dict[str, Any]]
    visible_pass = 0
    cmd = spec["cmd"]
    if not cmd:
        findings.append(_lib.finding("g3.no-command", "error", "neither ext.g3.cmd nor verify.tests names the command that runs the acceptance tests"))
        classes.append("env")
    else:
        stage, text, parsed = _acrun.run_visible(ctx, spec, "green")
        bad = _acrun.stage_failure(stage, "g3")
        if bad:
            findings.append(_lib.finding(bad[0], "error", "the acceptance command could not be judged: %s %s" % (stage.get("reason", ""), _lib.tail(text, 200))))
            classes.append(bad[1])
        else:
            for a in ids:
                v = parsed.verdict(a)
                if v == "pass":
                    visible_pass += 1
                elif v is None:
                    findings.append(_lib.finding("g3.ac-not-executed", "error", "%s was not executed: no test line names it%s. Name the test with the criterion id and run "
                                                 "the suite verbosely" % (a, " (the output shows import or collection errors: %s)" % _lib.tail(text, 160) if parsed.invalid else "")))
                elif v == "skipped":
                    findings.append(_lib.finding("g3.ac-skipped", "error", "%s is skipped; a skipped criterion is not a pass" % a))
                else:
                    findings.append(_lib.finding("g3.ac-failed", "error", "%s %s: %s" % (a, "errored" if v == "error" else "fails", "; ".join(parsed.tests(a))[:200])))
                results[a] = {"result": "pass" if v == "pass" else "fail", "evidence": (parsed.tests(a) or ["no test line"])[0][:110]}
            if stage["status"] != "pass" and not any(f["id"].startswith("g3.ac-") for f in findings):
                findings.append(_lib.finding("g3.suite-red", "error", "the acceptance command exited %s though every criterion reported a result: %s"
                                             % (stage.get("exit_code"), _lib.tail(text, 200))))

    # -- held-out
    rs = _lib.runstate_of(ctx)
    hdir = os.path.join(rs.heldout_dir, ctx.item_id)
    held_rate = None  # type: Optional[str]
    held_state = "none"
    if spec["heldout"] and _isolation_problem(ctx):
        findings.append(_lib.finding("g3.heldout-isolation", "error", _isolation_problem(ctx) or ""))
        classes.append("env")
    elif ctx.runner == "driver" and _heldout_files(hdir):
        hf, hc, hp, held_rate = run_heldout(ctx, hdir, spec["heldout"])
        findings.extend(hf)
        classes.extend(hc)
        held_state = "run" if hp else held_state
        for a, r in (hp or {}).items():
            if r["verdict"] != "pass":
                results[a] = {"result": "fail", "evidence": "held-out"}
        for a in ids:
            if a in results and results[a]["result"] == "pass" and hp and a in hp and hp[a]["verdict"] == "pass":
                results[a]["evidence"] = (results[a]["evidence"] + " + held-out")[:120]
    elif spec["heldout"]:
        if ctx.runner == "driver":
            findings.append(_lib.finding("g3.heldout-missing", "error", "the plan declares held-out tests (ext.g3.heldout) but %s has none for %s" % (
                os.path.join("heldout", ctx.item_id), ctx.item_id)))
            classes.append("env")
        else:
            held_state = "ci-only"
            findings.append(_lib.finding("g3.heldout-ci-only", "info", "held-out tests are CI-only under runner: %s; the heldout job runs them and its check "
                                         "result is the evidence (heldout.yml)" % ctx.runner))
    if spec["class"] == "characterization":
        findings.append(_lib.finding("g3.characterization", "warn", "refactor item: green on both sides is required and a human reviews behaviour preservation"))

    gap = ""
    if held_rate:
        hp_n, hp_t = (int(x) for x in held_rate.split("/"))
        vis = visible_pass / float(len(ids)) if ids else 0.0
        gap = "; gap %+d%%" % int(round((vis - (hp_n / float(hp_t) if hp_t else 0.0)) * 100))
    ev = {"summary": "visible %d/%d; held-out %s%s" % (visible_pass, len(ids), held_rate or held_state, gap), "ac_results": results}
    if any(f["severity"] == "error" for f in findings):
        cls = _lib.worst_class(classes) if classes else "item"
        return GateResult.failed(findings, failure_class=cls, **ev)
    try:
        if spec["visible"]:
            locked.add_gate_globs(rs, "g3", spec["visible"])
    except (OSError, ValueError) as exc:
        return GateResult.errored("could not pin the acceptance tests: %s" % common.clean_text(exc, 120))
    return GateResult("pass", findings=findings, evidence=ev)
