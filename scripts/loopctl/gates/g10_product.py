"""G10: product checks, tool-agnostic (report section 9 G10, CO-25, RC-10; IMP-1). OPT-IN, EXPERIMENTAL, evidence-gated. No tooling is bundled.

What it checks is yours to say. Four NAMED checks, each a command you configure (empty = that check is not run):

    g10_a11y_cmd           accessibility on the changed routes            (axe, Lighthouse, pa11y, ...)
    g10_perf_cmd           the performance budget                         (Lighthouse CI, a bundle-size check, ...)
    g10_pii_cmd            PII tagging of the new fields                  (a schema scan of your own)
    g10_observability_cmd  a log, metric or trace for each new endpoint   (a grep, a test, your own tool)

This module is the contract around them, not a tool:

* the command is run WITHOUT a shell. It is split into an argument vector once, shell syntax in it is REFUSED rather than guessed at (the same rule as
  `notify_command`), and the program must be an absolute path, a name on PATH, or a file in the worktree. Wrap pipes and redirects in a script of yours;
* a program or argument that is a file THIS ITEM CHANGED is refused (`g10.tool-edited`): an item cannot rewrite the check that judges it;
* it writes a scope file (the changed non-test files) and passes its path in `LOOPCTL_G10_SCOPE`; the command decides what a "route" is;
* it prints one JSON object on its LAST output line, the normalised result (the gate computes the verdict, not the tool):
  `{"schema": "g10.v1", "check": "<the check's own name>", "checked": N, "violations": N}` plus an optional `details` list of short strings (shown, not trusted).
  `checked` must be at least 1 (a check that looked at nothing proves nothing, as in G4) and `violations` must be 0. A result for another check, a
  missing line, a count that is not a whole number and any extra success-looking field are failures.

CONTRACT TESTS against `api-contract.md` are DESIGN-ONLY (IMP-1): they need an app-specific runner. They are written up in `${CLAUDE_PLUGIN_ROOT}/spec/loopctl-extras.md`
and have no key, no code and no test here.

Runs in CI (`g10-product.yml`, which reads its gate state at the base sha) or locally; it refuses to run until the evidence gate has passed. A change
that touches no production file (tests and documents only) skips. Owner metric: the per-check regressions it caught.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .. import common
from . import GateResult
from . import _lib

NAME = "g10"
OWNER_METRIC = "per-check regressions caught"
DEFAULT_MODE = "off"
EVIDENCE_GATED = True

CHECKS = (("a11y", "g10_a11y_cmd"), ("perf", "g10_perf_cmd"), ("pii", "g10_pii_cmd"), ("observability", "g10_observability_cmd"))
MAX_ARGV = 16
MAX_ARG = 300
MAX_DETAILS = 5
_SHELL_SYNTAX = re.compile(r"[;&|<>`$\\(){}*?!]")           # the set notify.py refuses
_SKIP_SUFFIX = (".md", ".rst", ".adoc", ".txt")
_SKIP_PREFIX = ("docs/", "plan/")                          # documents and the plan are not routes


class BadCommand(ValueError):
    """The command breaks the rules above. Nothing was run."""


def parse_command(text: str, worktree: str, path_env: Optional[str] = None) -> List[str]:
    """The argument vector, with the program resolved to a real path. Raises BadCommand."""
    s = (text or "").strip()
    if not s or s == "<command>":
        raise BadCommand("the command is not set")
    if len(s) > 1000 or any(c in s for c in "\n\r\0"):
        raise BadCommand("the command must be one short line")
    m = _SHELL_SYNTAX.search(s)
    if m:
        raise BadCommand("the command is run without a shell, so %r cannot mean what a shell would make of it: write the program and its arguments only, "
                         "and put anything else in a script of yours" % m.group(0))
    try:
        argv = shlex.split(s, posix=True)
    except ValueError as exc:
        raise BadCommand("unbalanced quotes (%s)" % exc)
    if not argv or len(argv) > MAX_ARGV or any(len(a) > MAX_ARG for a in argv):
        raise BadCommand("no program, or too many or too long arguments")
    prog = argv[0]
    root = os.path.realpath(worktree)
    if "/" in prog:
        cand = prog if os.path.isabs(prog) else os.path.join(root, prog)
        real = os.path.realpath(cand)
        if not os.path.isabs(prog) and not (real == root or real.startswith(root + os.sep)):
            raise BadCommand("%s resolves outside the worktree" % common.clean_text(prog, 60))
        found = real if os.path.isfile(real) and os.access(real, os.X_OK) else None
    else:
        found = shutil.which(prog, path=path_env if path_env is not None else os.environ.get("PATH", os.defpath))
        found = os.path.realpath(found) if found else None
    if not found:
        raise BadCommand("%s is not an executable file" % common.clean_text(prog, 80))
    return [found] + argv[1:]


def _tool_files(argv: List[str], worktree: str) -> List[str]:
    """Repository-relative paths this command names (the program and any argument that is a file in the worktree)."""
    root = os.path.realpath(worktree)
    out = []  # type: List[str]
    for a in argv:
        if a.startswith("-") and "=" not in a:
            continue
        cand = a.split("=", 1)[1] if a.startswith("-") else a
        full = os.path.realpath(cand if os.path.isabs(cand) else os.path.join(root, cand))
        if (full == root or full.startswith(root + os.sep)) and os.path.isfile(full):
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return out


def _result(text: str, check: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    doc = None  # type: Any
    for ln in reversed([x for x in text.splitlines() if x.strip()]):
        try:
            cand = common.strict_loads(ln.strip(), max_depth=4, max_bytes=16384)
        except (ValueError, RecursionError):
            continue
        if isinstance(cand, dict) and cand.get("schema") == "g10.v1":
            doc = cand
            break
    if doc is None:
        return None, "printed no g10.v1 JSON object on its output"
    if doc.get("check") != check:
        return None, "its result is for check %r, not %r" % (common.clean_text(doc.get("check"), 20), check)
    for k in ("checked", "violations"):
        v = doc.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None, "%s must be a non-negative integer" % k
    extra = sorted(set(doc) - {"schema", "check", "checked", "violations", "details"})
    if extra:
        return None, "unknown field(s): %s" % ", ".join(common.clean_text(k, 20) for k in extra[:3])
    return doc, None


def run(ctx: Any) -> GateResult:
    cmds = [(name, _lib.command_set(ctx.cfg, key), key) for name, key in CHECKS]
    cmds = [(n, c, k) for n, c, k in cmds if c]
    if not cmds:
        return GateResult.errored("gates.g10 is on but no g10_*_cmd is set in the base config: the kit bundles no product-check tool")
    if _lib.head_of(ctx) is None:
        return GateResult.errored("there is no head commit")
    bad_head = _lib.worktree_head_problem(ctx)
    if bad_head:
        return GateResult.errored(bad_head)
    dirty = _lib.uncommitted_changes(ctx)
    if dirty:
        return GateResult.failed([_lib.finding("g10.uncommitted-changes", "error", "tracked files differ from HEAD (%s): the checks would judge code HEAD does not hold" % ", ".join(dirty[:3]))])
    changed = _lib.changed_paths(ctx)
    scope_files = sorted(p for p in changed if not _lib.is_protected(p) and not p.lower().endswith(_SKIP_SUFFIX) and not p.startswith(_SKIP_PREFIX)
                         and os.path.isfile(os.path.join(ctx.worktree, p)))
    if not scope_files:
        return GateResult.skipped("no production file changed: nothing for the product checks to look at")
    findings = []  # type: List[Dict[str, Any]]
    checks = {}  # type: Dict[str, Any]
    classes = []  # type: List[str]
    changed_set = set(changed)
    tmp = tempfile.mkdtemp(prefix="loopctl-g10-")
    try:
        scope = os.path.join(tmp, "scope.json")
        with open(scope, "w", encoding="utf-8") as fh:
            json.dump({"schema": "g10.scope.v1", "item": ctx.item_id, "base": ctx.base_sha, "head": ctx.head_sha, "files": scope_files}, fh)
        for name, raw, key in cmds:
            try:
                argv = parse_command(raw, ctx.worktree, ctx.env.get("PATH"))
            except BadCommand as exc:
                findings.append(_lib.finding("g10.bad-command", "error", "%s: %s" % (key, exc)))
                classes.append("env")
                continue
            edited = sorted(set(_tool_files(argv, ctx.worktree)) & changed_set)
            if edited:
                findings.append(_lib.finding("g10.tool-edited", "error", "%s runs %s, which this item changed: an item cannot rewrite the check that judges it" % (key, ", ".join(edited[:2]))))
                classes.append("item")
                continue
            stage, text, _f = _lib.run_command(ctx, "g10-" + name, " ".join(shlex.quote(a) for a in argv), {"LOOPCTL_G10_SCOPE": scope, "LOOPCTL_ITEM": ctx.item_id},
                                               timeout_s=int(ctx.cfg.get_int("g10_timeout_s")), count_tests=False)
            if stage["status"] != "pass":
                cls = "transient" if stage["status"] == "timeout" else "env" if stage.get("exit_code") in (126, 127) else "item"
                findings.append(_lib.finding("g10.command-failed", "error", "%s %s (%s): %s" % (key, stage["status"], stage.get("reason", ""), _lib.tail(text, 200))))
                classes.append(cls)
                continue
            doc, why = _result(text, name)
            if doc is None:
                findings.append(_lib.finding("g10.bad-result", "error", "%s %s" % (key, why)))
                classes.append("env")
                continue
            checks[name] = {"checked": doc["checked"], "violations": doc["violations"]}
            if doc["checked"] < 1:
                findings.append(_lib.finding("g10.nothing-checked", "error", "%s checked nothing: a check that looks at no route proves nothing" % name))
                classes.append("env")
            elif doc["violations"] > 0:
                shown = [common.clean_text(d, 120) for d in (doc.get("details") or [])[:MAX_DETAILS] if isinstance(d, str)] if isinstance(doc.get("details"), list) else []
                findings.append(_lib.finding("g10.violations", "error", "%s found %d violation(s) in %d checked%s" % (name, doc["violations"], doc["checked"],
                                                                                                             ": " + "; ".join(shown) if shown else "")))
                classes.append("item")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ev = {"summary": "; ".join("%s %d checked, %d violations" % (n, c["checked"], c["violations"]) for n, c in sorted(checks.items())) or "no check completed", "checks": checks}
    if findings:
        return GateResult.failed(findings, failure_class=_lib.worst_class(classes), **ev)
    return GateResult("pass", evidence=ev)
