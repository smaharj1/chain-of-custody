"""G2: interface stubs first, tests red against them (report section 9 G2, G3 "red on main", CS-15). Lands with G3.

The acceptance-author commits signatures that raise or return a sentinel, THEN the tests. Before any engineer builds, this gate proves:

* the plan item declares its acceptance artifacts (`ext.g3`: `visible` globs, `stubs`, `cmd`; or `exempt` with a reason: an item with no
  executable acceptance tests passes with a human flag, it is never skipped silently);
* every criterion is named by a visible test, and the visible tests are not skipped;
* the stubs are stubs: no logic in them (Python is read with `ast`; other languages need a not-implemented marker);
* the stubs were committed before the tests (a test written first can be shaped to pass a guess);
* **red on the stubs**: running the visible tests, every criterion fails with an ASSERTION failure. A collection, import or syntax error
  is INVALID, not red (the test never ran); a criterion that passes against a stub proves nothing; a criterion that did not run is not red.
  A `characterization` item (a refactor) is the opposite: every criterion must already be green, and a human is told so.

On pass the visible test globs are pinned (`locked_paths.json`, source `g3`), so an engineer can no longer edit them (guard `engineer-locked`).
Needed at `design_review -> building` and `stubbing -> building`: skipping the stubbing stage is only possible for an exempt item.
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional

from .. import common, locked
from . import GateResult
from . import _acrun, _astscan, _lib

NAME = "g2"
OWNER_METRIC = "enabler for G3: share of items whose acceptance tests were red on the stubs before building"
REQUIRED_FOR = (("design_review", "building"), ("stubbing", "building"))

_NOT_IMPL_RE = re.compile(r"not[ _]?implemented|unimplemented|\btodo!?\b|NotImplemented|stub", re.I)


_STATIC_CALLS = ("TypeVar", "NewType", "NamedTuple", "namedtuple", "field", "Enum", "auto")


def _static(node: Any) -> bool:
    """A value that computes nothing: a literal, a name, an attribute, a container or a negation of those."""
    if node is None or isinstance(node, (ast.Constant, ast.Name, ast.Attribute)):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_static(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_static(k) for k in node.keys if k is not None) and all(_static(v) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _static(node.operand)
    if isinstance(node, ast.Subscript):
        return _static(node.value) and _static(getattr(node, "slice", None))
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        return name in _STATIC_CALLS and all(_static(a) for a in node.args) and all(_static(k.value) for k in node.keywords)
    return False


def _function_problems(node: Any, where: str) -> List[str]:
    probs = []  # type: List[str]
    if not all(_static(d) for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]):
        probs.append("%s has a default argument that computes something (line %d)" % (where, node.lineno))
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    for st in body:
        if isinstance(st, (ast.Pass, ast.Raise)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
            continue
        if isinstance(st, ast.Return) and _static(st.value):
            continue
        probs.append("function %s has logic (line %d)" % (where, st.lineno))
        break
    return probs


def _body_problems(body: List[Any], prefix: str, top: bool) -> List[str]:
    probs = []  # type: List[str]
    for st in body:
        if isinstance(st, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
            continue
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            probs += _function_problems(st, prefix + st.name)
        elif isinstance(st, ast.ClassDef):
            probs += _body_problems(st.body, prefix + st.name + ".", False)
        elif isinstance(st, (ast.Assign, ast.AnnAssign)) and _static(getattr(st, "value", None)):
            continue
        elif top and isinstance(st, ast.If) and "TYPE_CHECKING" in ast.dump(st.test) and all(isinstance(x, (ast.Import, ast.ImportFrom, ast.Pass)) for x in st.body):
            continue
        else:
            probs.append("%s code that runs on import (line %d)" % ("module-level" if top else "class-level", st.lineno))
    return probs


def _stub_problems(path: str, text: str) -> List[str]:
    """Why a stub file is not a stub. Empty when it is one. Python is read with `ast`: only imports, constants, classes and functions whose
    bodies raise, pass, or return a constant. Anything that RUNS (a call at import, a loop, a default argument that computes) is logic."""
    lang = _astscan.language(path)
    if lang == "python":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return ["does not parse"]
        return _body_problems(tree.body, "", True)
    if _NOT_IMPL_RE.search(text):
        return []
    return ["no not-implemented marker found (low confidence: regex reading)"] if lang != "other" else []


def _first_commit_index(ctx: Any, order: Dict[str, int], paths: List[str]) -> Optional[int]:
    """Position (0 = oldest) of the earliest commit in base..head that added any of `paths`; -1 when they already exist at the base."""
    best = None  # type: Optional[int]
    for p in paths:
        if _lib.blob(ctx, ctx.base_sha, p, 16) is not None:
            return -1
        out = _lib.git_ok(ctx, ["log", "--diff-filter=A", "--format=%H", "--reverse", "%s..%s" % (ctx.base_sha, _lib.head_of(ctx)), "--", p])
        shas = (out or "").split()
        if shas and shas[0] in order:
            i = order[shas[0]]
            best = i if best is None else min(best, i)
    return best


def run(ctx: Any) -> GateResult:
    spec, problems = _lib.g3_spec(ctx.item)
    findings = list(problems)  # type: List[Dict[str, Any]]
    ids = _lib.ac_ids_of(ctx.item)
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    bad_head = _lib.worktree_head_problem(ctx)
    if bad_head:
        return GateResult.errored(bad_head)
    if spec["exempt"]:
        return GateResult("pass", findings=[_lib.finding("g2.exempt", "warn", "ext.g3.exempt (%s): no acceptance tests to stub; a human sees this item "
                                                      "has none" % common.clean_text(spec["exempt"], 120))], evidence={"summary": "exempt"})
    if not spec["declared"]:
        return GateResult.failed(findings + [_lib.finding("g2.not-declared", "error", "the plan item declares no acceptance artifacts. Add ext.g3 "
                                                           "(visible, stubs, cmd) to the item in the plan PR, or ext.g3.exempt with the reason there are none")])
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings)
    dirty = _lib.uncommitted_changes(ctx)
    if dirty:
        findings.append(_lib.finding("g2.uncommitted-changes", "error", "tracked files differ from HEAD (%s): commit them, the red run must judge what HEAD holds"
                                     % ", ".join(common.clean_text(d, 50) for d in dirty[:3])))
    if _acrun.read_pin(ctx):                # a second run must not re-pin tests that were changed since the first: that would launder the edit
        for p, how in _acrun.pinned_changes(ctx, spec)[0]:
            findings.append(_lib.finding("g2.pin-violated", "error", "%s was %s since G2 pinned it; G2 will not pin it again: a reward-hack signature" % (p, how),
                                         reward_hack=True, path=p))
    files = _acrun.visible_files(ctx, spec)
    if not files:
        findings.append(_lib.finding("g2.no-visible-tests", "error", "no file at HEAD matches ext.g3.visible (%s): the acceptance-author has not committed the tests"
                                     % ", ".join(spec["visible"][:3])))
        return GateResult.failed(findings)
    for a in _acrun.uncovered(ctx, files, ids):
        findings.append(_lib.finding("g2.ac-uncovered", "error", "%s is named by no visible acceptance test" % a))
    for f in _acrun.assertion_findings(ctx, files, ids):
        if f["severity"] == "error":           # a test that asserts nothing is red only because the stub raises: refuse it now, not at verifying
            findings.append(dict(f, id=f["id"].replace("g3.", "g2.")))
    stubs = spec["stubs"]
    stub_files = [f for f in _lib.tree_files(ctx, head) if _lib.match_any(stubs, f)] if stubs else []
    if stubs and not stub_files:
        findings.append(_lib.finding("g2.stub-missing", "error", "no file at HEAD matches ext.g3.stubs (%s)" % ", ".join(stubs[:3])))
    for f in stub_files:
        text = _lib.blob(ctx, head, f)
        for why in _stub_problems(f, text or ""):
            findings.append(_lib.finding("g2.stub-has-logic", "error", "%s is not a stub: %s" % (f, why)))
    if not stubs and spec["class"] != "characterization":
        findings.append(_lib.finding("g2.no-stubs", "info", "ext.g3.stubs is empty: the item adds no new interface to stub"))
    if stub_files and files:
        revs = (_lib.git_ok(ctx, ["rev-list", "--reverse", "%s..%s" % (ctx.base_sha, head)]) or "").split()
        order = {sha: i for i, sha in enumerate(revs)}
        s_i, t_i = _first_commit_index(ctx, order, stub_files), _first_commit_index(ctx, order, files)
        if s_i is not None and t_i is not None and t_i < s_i:
            findings.append(_lib.finding("g2.tests-before-stubs", "error", "the tests were committed before the stubs: stubs come first, so the tests cannot be "
                                         "shaped around a guess"))

    ok_n = 0
    if not any(f["severity"] == "error" for f in findings):
        cmd = spec["cmd"]
        if not cmd:
            findings.append(_lib.finding("g2.no-command", "error", "neither ext.g3.cmd nor verify.tests names the command that runs the acceptance tests"))
        else:
            stage, text, parsed = _acrun.run_visible(ctx, spec, "red")
            bad = _acrun.stage_failure(stage, "g2")
            if bad:
                findings.append(_lib.finding(bad[0], "error", "the acceptance command could not be judged: %s %s" % (stage.get("reason", ""), _lib.tail(text, 200))))
                return GateResult.failed(findings, failure_class=bad[1])
            characterization = spec["class"] == "characterization"
            red_ok = 0
            for a in ids:
                v = parsed.verdict(a)
                if characterization:
                    if v == "pass":
                        red_ok += 1
                    else:
                        findings.append(_lib.finding("g2.not-green", "error", "characterization: %s must be green before the refactor, got %s" % (a, v or "not executed")))
                    continue
                if v == "fail":
                    red_ok += 1
                elif v == "error" or (v is None and parsed.invalid):
                    findings.append(_lib.finding("g2.invalid-red", "error", "%s errored instead of failing an assertion (an import, collection or syntax error): "
                                                 "that is invalid, not red. %s" % (a, _lib.tail(text, 160))))
                elif v == "pass":
                    findings.append(_lib.finding("g2.not-red", "error", "%s passes against the stubs: the test proves nothing" % a))
                elif v == "skipped":
                    findings.append(_lib.finding("g2.skipped", "error", "%s is skipped" % a))
                else:
                    findings.append(_lib.finding("g2.ac-not-run", "error", "%s did not run: no test line names it. Name the test with the criterion id" % a))
            if characterization:
                findings.append(_lib.finding("g3.characterization", "warn", "refactor item: the criteria are green on the base. A human checks that behaviour is "
                                             "preserved (the tests stay pinned and may not change)"))
            ok_n = red_ok

    ev = {"summary": "%s %d/%d" % ("green on the base" if spec["class"] == "characterization" else "red on the stubs", ok_n, len(ids))}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    try:
        locked.add_gate_globs(_lib.runstate_of(ctx), "g3", spec["visible"])
        entries = _lib.tree_entries(ctx, head)
        authorized = {p: entries[p] for _s, p, _o in _lib.diff_status(ctx, untracked=False) if _s != "D" and p in entries and _lib.is_protected(p)}
        _acrun.write_pin(ctx, {f: entries[f] for f in files if f in entries}, authorized)
    except (OSError, ValueError) as exc:
        return GateResult.errored("could not pin the acceptance tests: %s" % common.clean_text(exc, 120))
    # no ac_results: a test that is red against a stub is not an executed PASSING test, and the AC x evidence matrix counts passes
    return GateResult("pass", findings=findings, evidence=ev)
