"""G5: test integrity (report section 9 G5, L-21, F-12, CS-14).

**The glob check (FULL, always on with the gate).** `git diff base..head` is matched against the extended integrity set: test files
(new ones included), `conftest.py` and test-runner config at any depth, `pyproject.toml`, `package.json`, snapshot directories, `.env*`,
CI workflows, lint config. A changed path is acceptable only when the plan allows it, and the plan is read at the BASE sha and was approved
by a human (H1, H2):

* inside the item's declared footprint (`touches.paths`) AND a test file: the engineer is expected to write the item's own tests;
* inside `ext.g5.allow` (globs the plan names for this item, for example a `package.json` the item must edit);
* anything else is a finding. A pinned acceptance test (G3) that changed is a reward-hack signature and the finding says so;
  a deleted test file is always a finding; `package.json` and `pyproject.toml` are judged on their `scripts`, `jest`, `[tool.pytest*]`
  and `[tool.ruff|mypy|coverage]` content, not on every dependency edit (G9 owns dependencies).

**The AST lint (OPT-IN, EXPERIMENTAL, needs the evidence gate; `g5_ast_lint: [python, javascript]`).** Skip, only, todo and xfail
markers added; test-environment detection (`PYTEST_CURRENT_TEST`, `JEST_WORKER_ID`, `NODE_ENV === 'test'`) added to non-test code; mocks that
target the item's own footprint; snapshots created in the same diff as the code they snapshot; `-u` / `--update-snapshots`. Python is read
with `ast` and is trustworthy (errors); JavaScript and TypeScript are regex heuristics and stay report-only warnings, low confidence.

Any hit is a finding for a separate review verdict plus a human flag. Owner metric: integrity flags per item and the false-positive rate.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .. import common
from . import GateResult
from . import _acrun, _astscan, _lib

NAME = "g5"
OWNER_METRIC = "integrity flags per item; false-positive rate"
REQUIRED_FOR = (("verifying", "reviewing"),)

_RUNNER_NAMES = {"unittest", "pytest", "_pytest", "nose", "nose2", "sitecustomize", "usercustomize", "doctest", "coverage", "mock", "hypothesis", "py"}
_STDLIB_FALLBACK = {"json", "os", "sys", "re", "io", "abc", "time", "types", "typing", "subprocess", "socket", "http", "logging", "random", "threading", "collections",
                    "functools", "itertools", "inspect", "importlib", "traceback", "contextlib", "tempfile", "shutil", "pathlib", "argparse", "asyncio", "datetime",
                    "enum", "dataclasses", "decimal", "hashlib", "urllib", "sqlite3", "unittest", "string", "math", "copy", "warnings", "weakref", "signal"}
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | _STDLIB_FALLBACK
_SHADOW_DIRS = ("", "src", "lib", "tests", "test")
_PKG_KEYS = ("scripts", "jest", "vitest", "mocha", "ava", "nyc", "c8", "husky", "simple-git-hooks", "lint-staged", "eslintConfig", "babel", "pytest")
_TOOLS = ("pytest", "coverage", "ruff", "mypy", "pylint", "flake8", "black", "isort", "tox", "nox")


def _pkg_changed(base: Optional[str], head: Optional[str]) -> List[str]:
    """Top-level package.json keys that matter to a test run and differ."""
    try:
        a = json.loads(base) if base else {}
        b = json.loads(head) if head else {}
    except (ValueError, RecursionError):
        return ["<unparsable package.json>"]
    if not isinstance(a, dict) or not isinstance(b, dict):
        return ["<unparsable package.json>"]
    return [k for k in _PKG_KEYS if a.get(k) != b.get(k)]


def _toml_key(src: str, i: int) -> Optional[Tuple[List[str], int]]:
    """A dotted TOML key starting at `src[i]`: ([parts, quotes and spaces stripped], index after it), or None when it is not one."""
    parts = []  # type: List[str]
    n = len(src)
    while True:
        while i < n and src[i] in " \t":
            i += 1
        if i >= n:
            return None
        ch = src[i]
        if ch in "\"'":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if (ch == '"' and src[j] == "\\") else 1
            if j >= n:
                return None
            parts.append(src[i + 1:j])
            i = j + 1
        else:
            m = re.compile(r"[A-Za-z0-9_-]+").match(src, i)
            if not m:
                return None
            parts.append(m.group(0))
            i = m.end()
        while i < n and src[i] in " \t":
            i += 1
        if i < n and src[i] == ".":
            i += 1
            continue
        return parts, i


def _toml_scan(line: str, ml: Optional[str], depth: int) -> Optional[Tuple[Optional[str], int]]:
    """Advance the multi-line string and bracket state over one physical line. None when the line cannot be read (an unterminated basic
    or literal string)."""
    i, n = 0, len(line)
    while i < n:
        if ml:
            j = line.find(ml, i)
            if j < 0:
                return ml, depth
            i, ml = j + 3, None
            continue
        ch = line[i]
        if ch == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml, i = line[i:i + 3], i + 3
        elif ch in "\"'":
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if (ch == '"' and line[j] == "\\") else 1
            if j >= n:
                return None
            i = j + 1
        else:
            depth += 1 if ch in "[{" else -1 if ch in "]}" else 0
            i += 1
    return ml, depth


def _toml_tables(text: Optional[str]) -> Optional[Dict[str, str]]:
    """The test and lint tool content of a pyproject, keyed by normalised table name (`tool.pytest.ini_options`): header spacing and quoting
    do not matter, multi-line values are read as one statement, a dotted `tool.` key at the top level or inside `[tool]` lands in its own
    bucket. None when the file holds something this small reader cannot classify (an unterminated string, unbalanced brackets, a header it
    cannot parse): the caller then treats the whole file as changed (fail closed)."""
    out = {}  # type: Dict[str, List[str]]
    cur = None  # type: Optional[str]
    root = True                 # before the first header: a `tool.…` or `tool = {…}` key here is tool configuration
    capture = None  # type: Optional[List[str]]
    ml, depth = None, 0   # type: Optional[str], int
    for raw in (text or "").splitlines():
        if ml is None and depth == 0:
            ln = raw.strip()
            capture = None
            if not ln or ln.startswith("#"):
                continue
            if ln.startswith("["):
                root = False
                array = ln.startswith("[[")
                got = _toml_key(ln, 2 if array else 1)
                if got is None:
                    return None
                parts, i = got
                close = "]]" if array else "]"
                rest = ln[i:]
                if not rest.startswith(close) or (rest[len(close):].strip() and not rest[len(close):].strip().startswith("#")):
                    return None
                name = ".".join(parts)
                relevant = parts[0] == "tool" and (len(parts) == 1 or parts[1] in _TOOLS)
                cur = ("[[%s]]" % name if array else name) if relevant else None
                if cur is not None:
                    out.setdefault(cur, [])
                continue
            if cur is not None:
                capture = out[cur]
            elif root:
                got = _toml_key(ln, 0)
                if got is None:
                    return None
                if got[0][0] == "tool":
                    capture = out.setdefault("<top-level tool keys>", [])
            if capture is not None:
                capture.append(ln)
        elif capture is not None:
            capture.append(raw.strip())
        st = _toml_scan(raw, ml, depth)
        if st is None:
            return None
        ml, depth = st
        if depth < 0:
            return None
    if ml is not None or depth != 0:
        return None
    return {k: "\n".join(v) for k, v in out.items()}


def _toml_changed(base: Optional[str], head: Optional[str]) -> bool:
    """Whether a pyproject's test and lint tool tables differ (a dependency edit alone is not an integrity change). A file the reader cannot
    classify counts as changed whenever it differs at all."""
    a, b = _toml_tables(base), _toml_tables(head)
    if a is None or b is None:
        return (base or "") != (head or "")
    return a != b


def _allowed(ctx: Any, path: str, status: str, allow: List[str], foot: List[str]) -> bool:
    if _lib.match_any(allow, path):
        return True
    if status in ("A", "M") and _lib.is_test_path(path) and not _lib.match_any(_lib.SNAPSHOT_GLOBS, path) and _lib.match_any(foot, path):
        return True
    return False


def _shadow(path: str) -> Optional[str]:
    """The module name a new Python file or package would shadow on the import path of a test run, or None. `python -m unittest` puts the
    working directory first, so a new `unittest.py`, `json.py` or `sitecustomize.py` next to the tests replaces the real module."""
    if not path.endswith(".py"):
        return None
    parts = path.split("/")
    stem = parts[-1][:-3]
    if stem == "__init__" and len(parts) >= 2:
        stem, parts = parts[-2], parts[:-1]
    parent = "/".join(parts[:-1])
    if stem in _RUNNER_NAMES:
        return stem
    if stem in _STDLIB and parent in _SHADOW_DIRS:
        return stem
    return None


def _glob_check(ctx: Any, rows: List[Any], findings: List[Dict[str, Any]], counts: Dict[str, int]) -> None:
    spec, _p = _lib.g3_spec(ctx.item)
    allow = _lib.g5_allow(ctx.item)
    foot = _lib.footprint(ctx.item)
    changes, pin = _acrun.pinned_changes(ctx, spec)
    changed_pinned = {p for p, _how in changes}
    authorized = _acrun.read_authorized(ctx)
    head_entries = _lib.tree_entries(ctx, _lib.head_of(ctx)) if authorized else {}
    for path, how in changes:
        findings.append(_lib.finding("g5.pinned-test-edited", "error", "%s is a pinned acceptance test and was %s: a reward-hack signature" % (path, how),
                                     path=path, reward_hack=True))
        counts["flags"] += 1
    cmdfiles = set(_lib.command_files(ctx))
    for status, path, _old in rows:
        if status != "D" and not _lib.match_any(allow, path):
            mod = _shadow(path)
            if mod:
                findings.append(_lib.finding("g5.shadow-module", "error", "%s would shadow the module %r on the test run's import path: a new file named like a "
                                             "runner or standard-library module replaces the real one" % (path, mod), path=path))
                counts["flags"] += 1
                continue
        if path in cmdfiles and path not in changed_pinned and not _allowed(ctx, path, "M", allow, foot) and not _lib.match_any(foot, path):
            findings.append(_lib.finding("g5.command-file", "error", "%s is run by a configured verify, gate or test command; changing it changes what a gate means. It needs "
                                         "a plan footprint or ext.g5.allow" % path, path=path))
            counts["flags"] += 1
            continue
        if not _lib.is_protected(path) or path in changed_pinned:
            continue
        if path in authorized and head_entries.get(path) == authorized[path]:
            continue                    # committed by the acceptance-author before G2 passed, and unchanged since
        if _lib.match_any(spec["visible"], path):
            continue                    # an acceptance test identical to its pin (or to the base): the acceptance-author's, not the engineer's
        base_text = head_text = None  # type: Optional[str]
        if path in ("package.json", "pyproject.toml") or path.endswith(("/package.json", "/pyproject.toml")):
            base_text = _lib.blob(ctx, ctx.base_sha, path)
            head_text = _lib.blob(ctx, _lib.head_of(ctx), path)
            if path.endswith("package.json"):
                keys = _pkg_changed(base_text, head_text)
                if not keys:
                    continue
                what = "package.json key(s) %s" % ", ".join(keys)
            else:
                if not _toml_changed(base_text, head_text):
                    continue
                what = "the test or lint tool tables of pyproject.toml"
        else:
            what = {"A": "new", "D": "deleted", "M": "modified"}.get(status, "changed") + " " + (
                "test file" if _lib.is_test_path(path) else "test, CI or lint configuration")
        if status == "D" and _lib.is_test_path(path):
            findings.append(_lib.finding("g5.test-deleted", "error", "%s was deleted: a removed test cannot fail. Deleting a test needs a plan change (ext.g5.allow)"
                                         % path, path=path))
            counts["flags"] += 1
            continue
        if _allowed(ctx, path, status, allow, foot):
            findings.append(_lib.finding("g5.expected-change", "info", "%s: %s, inside the item's plan footprint" % (path, what), path=path))
            counts["expected"] += 1
            continue
        findings.append(_lib.finding("g5.protected-path", "error", "%s: %s, outside what the plan allows for this item. A change to test, CI or lint "
                                     "configuration needs a written rationale and routes to a human (H6)" % (path, what), path=path))
        counts["flags"] += 1


_DOC_EXT = (".md", ".rst", ".txt", ".adoc")
_RESULTS_RE = re.compile(r"""["']results["']""")
_ACID_RE = re.compile(r"""AC-[A-Za-z0-9]+-\d+|["']AC-["']""")


def _read_untracked(ctx: Any, path: str) -> Optional[str]:
    full = os.path.join(ctx.worktree or "", path)
    try:
        if not common.safe_rel_path(path) or os.path.islink(full) or not os.path.isfile(full) or os.path.getsize(full) > (1 << 20):
            return None
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _output_lint(ctx: Any, rows: List[Any], findings: List[Dict[str, Any]], counts: Dict[str, int]) -> None:
    """Production code that prints what a test harness would print (UN-1, GT-2). A product module is not the harness: a JSON object with a
    `results` key naming an acceptance id, added outside tests and test configuration, is a forged verdict until proven otherwise; so is
    `os._exit(`, which ends a run before the runner can report a failure. Always on (no opt-in): the reader (_acparse) cannot tell the two
    apart, so this is where the forgery is caught. A window of four added lines is read together so splitting the literal does not hide it."""
    head = _lib.head_of(ctx)
    for status, path, _old in rows:
        if status == "D" or _lib.is_protected(path) or path.lower().endswith(_DOC_EXT):
            continue
        text_head = _lib.blob(ctx, head, path)
        if text_head is None and status == "A":
            text_head = _read_untracked(ctx, path)          # a new file nobody committed yet still runs in the worktree
        if text_head is None or _lib.is_binary(text_head):
            continue
        old = set((_lib.blob(ctx, ctx.base_sha, path) or "").splitlines())
        added = [ln for ln in text_head.splitlines() if ln not in old]
        if not added:
            continue
        if any("os._exit(" in ln for ln in added):
            findings.append(_lib.finding("g5.hard-exit", "warn", "%s adds os._exit(): it ends the process before a test runner can report a failure" % path, path=path))
        for i in range(len(added)):
            win = "\n".join(added[i:i + 4])
            if _RESULTS_RE.search(win) and _ACID_RE.search(win):
                findings.append(_lib.finding("g5.harness-shaped-output", "error", "%s adds text shaped like an acceptance harness verdict (a \"results\" object "
                                             "naming an AC id): production code must not print test results" % path, path=path))
                counts["flags"] += 1
                break


def _ignored_check(ctx: Any, findings: List[Dict[str, Any]], counts: Dict[str, int]) -> None:
    for path, why in _lib.ignored_problems(ctx)[:20]:
        if path.startswith("<more than"):
            findings.append(_lib.finding("g5.ignored-incomplete", "warn", "%s: %s" % (path, why)))
            continue
        findings.append(_lib.finding("g5.ignored-config", "error", "%s: %s. It is in no diff, so no gate has read it. Delete it, or un-ignore "
                                     "and commit it (clear the flag) so it can be reviewed" % (path, why), path=path))
        counts["flags"] += 1


def _lint(ctx: Any, rows: List[Any], langs: List[str], findings: List[Dict[str, Any]], counts: Dict[str, int]) -> None:
    foot = _lib.footprint(ctx.item)
    head = _lib.head_of(ctx)
    code_changed = any(not _lib.is_protected(p) and s != "D" for s, p, _o in rows)
    for status, path, _old in rows:
        if status == "D":
            continue
        lang = _astscan.language(path)
        name = "python" if lang == "python" else "javascript" if lang == "javascript" else None
        text_head = _lib.blob(ctx, head, path)
        text_base = _lib.blob(ctx, ctx.base_sha, path) or ""
        if text_head is None or _lib.is_binary(text_head):
            continue
        # snapshots made in the same diff as code (any language)
        if _lib.match_any(_lib.SNAPSHOT_GLOBS, path) and code_changed and (status == "A" or text_base != text_head):
            sev = "error" if "python" in langs or "javascript" in langs else "warn"
            findings.append(_lib.finding("g5.lint.snapshot-with-code", sev, "%s: a snapshot was created or changed in the same diff as the code it snapshots; "
                                         "the snapshot is the output, not an expectation" % path, path=path))
            counts["flags"] += 1
        if _astscan.update_snapshot_flags(text_head) > _astscan.update_snapshot_flags(text_base):
            findings.append(_lib.finding("g5.lint.update-snapshots", "error" if name == "python" or name is None else "warn",
                                         "%s adds a snapshot-update flag (-u, --update-snapshots)" % path, path=path))
            counts["flags"] += 1
        if name is None or name not in langs:
            continue
        sev = "error" if name == "python" else "warn"
        low = {"confidence": "low"} if name != "python" else {}
        suffix = "" if name == "python" else " (low confidence: regex reading)"
        if len(_astscan.skip_markers(path, text_head)) > len(_astscan.skip_markers(path, text_base)):
            findings.append(_lib.finding("g5.lint.skip-added", sev, "%s adds a skip, only, todo or xfail marker%s" % (path, suffix), path=path, **low))
            counts["flags"] += 1
        if not _lib.is_test_path(path) and _astscan.env_detection_count(text_head) > _astscan.env_detection_count(text_base):
            findings.append(_lib.finding("g5.lint.test-env-detection", sev, "%s detects the test environment (PYTEST_CURRENT_TEST, JEST_WORKER_ID, "
                                         "NODE_ENV === 'test'); production code must not behave differently under test%s" % (path, suffix), path=path, **low))
            counts["flags"] += 1
        if not _lib.is_test_path(path) and name == "python" and _astscan.imports_test_framework(text_head) and not _astscan.imports_test_framework(text_base):
            findings.append(_lib.finding("g5.lint.test-framework-in-production", sev, "%s imports the test framework from production code: a module the tests import "
                                         "can patch assertEqual and make every assertion pass" % path, path=path))
            counts["flags"] += 1
        if _lib.is_test_path(path):
            new = _astscan.mock_targets(path, text_head) - _astscan.mock_targets(path, text_base)
            hit = sorted(t for t in new if _lib.match_any(foot, t))
            if hit:
                findings.append(_lib.finding("g5.lint.mock-of-footprint", sev, "%s mocks code inside the item's own footprint (%s): that tests the mock, not the "
                                             "code%s" % (path, hit[0], suffix), path=path, **low))
                counts["flags"] += 1


def run(ctx: Any) -> GateResult:
    try:
        rows = _lib.diff_status(ctx)
    except ValueError as exc:
        return GateResult.errored("cannot read the diff: %s" % common.clean_text(exc, 120))
    findings = []  # type: List[Dict[str, Any]]
    counts = {"flags": 0, "expected": 0}
    _glob_check(ctx, rows, findings, counts)
    _output_lint(ctx, rows, findings, counts)
    _ignored_check(ctx, findings, counts)
    langs = [str(x) for x in (ctx.cfg.get("g5_ast_lint") or [])]
    if langs:
        if not ctx.evidence_passed:
            findings.append(_lib.finding("g5.ast-evidence-required", "error", "g5_ast_lint is OPT-IN and evidence-gated: it refuses to run until "
                                         "docs/baseline/evidence.json is complete (IMP-1)"))
            return GateResult.failed(findings, failure_class="env", summary="AST lint refused")
        _lint(ctx, rows, langs, findings, counts)
    ev = {"summary": "integrity flags %d, plan-expected test changes %d%s" % (counts["flags"], counts["expected"], ", AST lint " + "+".join(langs) if langs else "")}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
