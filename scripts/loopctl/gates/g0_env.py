"""G0: the environment is fit to start this item (report section 9 G0; wraps `doctor.py`, Phase 1).

Checks, all against the BASE sha's config and pins:

* the doctor's effective level is not below the configured level (credentials, sandbox, hook liveness, version range, MCP, deployment-surface
  attestation, squash setting: the doctor owns those; G0 reads its `doctor.v1` JSON and never re-implements a check);
* every variable the item's `requiresEnv` names is set (names only are ever printed, never a value);
* the working tree is clean (design documents under `docs/features/<slug>/` excepted);
* toolchain pins (`.nvmrc`, `.python-version`, `.tool-versions`, `engines`, `requires-python`) match the installed tools;
* the declared verify ports are free;
* `env_check_cmd` (the project's own "database reachable, services up" probe) exits 0;
* the baseline is green: `verify_tests` passes on the untouched base, and its test and skip counts are recorded for G6.

Every failure is class `env`, except a busy port (`transient`): the item is parked, not retried, and the effective level is capped until a
human fixes the machine (taxonomy, report section 10.4). Owner metric: the share of item failures classed `env` should fall.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from kitlib import jsonio

from .. import common, locked
from . import GateResult
from . import _lib, _toolchain

NAME = "g0"
OWNER_METRIC = "share of item failures classed env (should fall)"
REQUIRED_FOR = (("designing", "design_review"),)


def _level_num(v: Any) -> Optional[int]:
    if isinstance(v, str) and len(v) == 2 and v[0] == "L" and v[1].isdigit():
        return int(v[1])
    return None


def _doctor(ctx: Any, findings: List[Dict[str, Any]], classes: List[str]) -> Optional[str]:
    from ..context import SCRIPTS_DIR
    argv = [sys.executable, os.path.join(SCRIPTS_DIR, "doctor.py"), "--json", "--repo", ctx.worktree, "--config-ref", ctx.base_sha]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=180, env=dict(ctx.env),
                              stdin=subprocess.DEVNULL)
        doc = json.loads(proc.stdout.decode("utf-8", "replace"))
        lvl = doc["level"]
        configured, effective = _level_num(lvl.get("configured")), _level_num(lvl.get("effective"))
    except Exception as exc:  # noqa: BLE001 - fail closed: a doctor we cannot read is a machine we cannot vouch for
        findings.append(_lib.finding("g0.doctor-unreadable", "error", "the doctor could not be read (%s: %s)" % (type(exc).__name__, common.clean_text(exc, 100))))
        classes.append("env")
        return None
    bad = [c for c in doc.get("checks", []) if isinstance(c, dict) and c.get("severity") == "error" and c.get("status") in ("fail", "error")]
    for c in bad[:8]:
        findings.append(_lib.finding("g0.doctor.%s" % common.clean_text(c.get("id"), 40), "warn", "doctor: %s" % common.clean_text(c.get("message"), 240)))
    if effective is None or configured is None:
        findings.append(_lib.finding("g0.doctor-level", "error", "the doctor reported no usable level"))
        classes.append("env")
        return None
    if effective < configured:
        caps = [c for c in lvl.get("caps", []) if isinstance(c, dict) and _level_num(c.get("level")) is not None and _level_num(c.get("level")) < configured]
        why = "; ".join("%s: %s" % (common.clean_text(c.get("name"), 30), common.clean_text(c.get("reason"), 120)) for c in caps[:3])
        findings.append(_lib.finding("g0.level-capped", "error", "configured level L%d but the doctor allows only L%d (%s). The run must not start "
                                     "above the level this machine can hold" % (configured, effective, why or "see `loopctl doctor --level`")))
        classes.append("env")
    return "L%d" % effective


def _dirty(ctx: Any) -> List[str]:
    out = _lib.git_ok(ctx, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if out is None:
        return ["<git status failed>"]
    slug = ctx.item.get("slug", "")
    ok_prefix = ("docs/features/%s/" % slug, ".claude/pr/")
    bad = []  # type: List[str]
    for rec in out.split("\0"):
        if len(rec) > 3 and not rec[3:].startswith(ok_prefix):
            bad.append(rec[3:])
    return bad


def _baseline(ctx: Any, findings: List[Dict[str, Any]], classes: List[str]) -> Optional[str]:
    """Run `verify_tests` on the untouched base and record the counts. Returns a short summary."""
    import verify
    rows = _lib.diff_status(ctx, untracked=False)
    if any(not p.startswith("docs/") for _s, p, _o in rows):
        findings.append(_lib.finding("g0.baseline-skipped", "info", "the baseline is recorded on the untouched base; HEAD already differs from it in code, so it was not re-run"))
        return None
    res = verify.verify(ctx.worktree, base=ctx.base_sha, only=["tests"], write_log=False)
    stage = next((s for s in res.get("stages", []) if s.get("name") == "tests"), None)
    if res.get("status") == "error" or stage is None:
        findings.append(_lib.finding("g0.baseline-config", "error", "the baseline cannot run: %s" % common.clean_text(res.get("message"), 200)))
        classes.append("env")
        return None
    if stage["status"] != "pass":
        findings.append(_lib.finding("g0.baseline-red", "error", "the baseline is not green on the untouched base (%s): %s. Failures here are not the item's"
                                     % (stage["status"], common.clean_text(stage.get("reason") or res.get("message"), 160))))
        classes.append("env")
        return None
    rec = {"schema": "loopctl.baseline.v1", "item": ctx.item_id, "attempt": ctx.attempt, "base": ctx.base_sha, "head": ctx.head_sha,
           "collected": stage.get("tests_collected"), "skipped": stage.get("skipped_tests"), "recorded": common.iso()}
    rs = _lib.runstate_of(ctx)
    d = os.path.join(rs.root, "baseline")
    jsonio.ensure_dir(d, 0o700)
    jsonio.atomic_write_json(os.path.join(d, "%s.json" % ctx.item_id), rec, 0o600)
    return "baseline %s tests, %s skipped" % (rec["collected"], rec["skipped"])


def run(ctx: Any) -> GateResult:
    findings = []  # type: List[Dict[str, Any]]
    classes = []  # type: List[str]
    summary = []  # type: List[str]
    try:                # from the first moment of the item the engineer cannot write the reviewers' verdict files (G7)
        locked.add_gate_globs(_lib.runstate_of(ctx), "g7", _lib.review_globs(ctx))
    except (OSError, ValueError) as exc:
        findings.append(_lib.finding("g0.lock-reviews", "warn", "could not pin the review files: %s" % common.clean_text(exc, 100)))

    lvl = _doctor(ctx, findings, classes)
    if lvl:
        summary.append("level " + lvl)

    missing = [n for n in ctx.item.get("requiresEnv", []) if isinstance(n, str) and not ctx.env.get(n)]
    for n in missing:
        findings.append(_lib.finding("g0.missing-env-var", "error", "the item requires the environment variable %s, which is not set" % n))
        classes.append("env")

    dirty = _dirty(ctx)
    if dirty:
        findings.append(_lib.finding("g0.tree-dirty", "error", "the working tree is not clean (%s%s): the loop needs a clean tree at the start of an item"
                                     % (", ".join(common.clean_text(p, 60) for p in dirty[:3]), ", ..." if len(dirty) > 3 else "")))
        classes.append("env")

    def read(path: str) -> Optional[str]:
        return _lib.blob(ctx, ctx.base_sha, path, 1 << 16)

    for row in _toolchain.check(_toolchain.read_pins(read), dict(ctx.env)):
        if row["level"] == "error":
            findings.append(_lib.finding("g0.toolchain", "error", "toolchain mismatch: %s" % row["message"]))
            classes.append("env")
        else:
            findings.append(_lib.finding("g0.toolchain-unparsed", "info", row["message"]))

    ports = ctx.cfg.get_list("verify_ports")
    if ports:
        import verify
        busy = verify.busy_ports([p for p in ports if isinstance(p, int) and not isinstance(p, bool)])
        if busy:
            findings.append(_lib.finding("g0.port-in-use", "error", "verify port(s) %s already in use (a dev server left running?)" % ", ".join(str(b) for b in busy)))
            classes.append("transient")

    check = _lib.command_set(ctx.cfg, "env_check_cmd")
    if check:
        stage, text, _f = _lib.run_command(ctx, "env_check", check, {"LOOPCTL_ITEM": ctx.item_id}, count_tests=False)
        if stage["status"] != "pass":
            findings.append(_lib.finding("g0.env-check", "error", "env_check_cmd %s: %s %s" % (stage["status"], stage.get("reason", ""), _lib.tail(text, 240))))
            classes.append("env")

    if not classes:
        base = _baseline(ctx, findings, classes)
        if base:
            summary.append(base)

    ev = {"summary": "; ".join(summary)}
    if classes:
        return GateResult.failed(findings, failure_class=_lib.worst_class(classes), **ev)
    return GateResult("pass", findings=findings, evidence=ev)
