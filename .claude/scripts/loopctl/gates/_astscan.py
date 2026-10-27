"""Static reads of test and source files (G3 assertion check, G5 integrity lint, G6 skip counts and test-name sets).

Python is read with the standard-library `ast`, which is exact. Every other language is read with regular expressions, which are a
heuristic: findings from them carry `confidence: low` and callers keep them report-only (IMP-1: the multi-language half of G5 is
opt-in). A Python file that does not parse falls back to the regex reading and says so.
"""
from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


__all__ = ["imports_test_framework", "language", "skip_markers", "test_names", "py_test_functions", "js_assertion_problems", "env_detection_count", "mock_targets",
           "update_snapshot_flags", "ENV_DETECT_RE"]

_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")


def language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return "python"
    if ext in _JS_EXT:
        return "javascript"
    return {".go": "go", ".rs": "rust", ".java": "java", ".kt": "java", ".rb": "ruby"}.get(ext, "other")


# -- Python ----------------------------------------------------------------------------------------------------------------------

_DECORATOR_SKIPS = {"skip", "skipIf", "skipUnless", "expectedFailure", "unittest.skip", "unittest.skipIf", "unittest.skipUnless",
                    "unittest.expectedFailure", "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "mark.skip", "mark.skipif",
                    "mark.xfail", "pytest.mark.todo", "mark.todo"}
_CALL_SKIPS = {"pytest.skip", "pytest.xfail", "pytest.importorskip", "self.skipTest", "cls.skipTest", "unittest.SkipTest", "SkipTest",
               "skipTest", "importorskip"}
_HELPER_RE = re.compile(r"^_?(?:assert|check|expect|verify|ensure|should|must)\w*$", re.I)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    parts = []  # type: List[str]
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        return _dotted(node.func) + "." + ".".join(reversed(parts)) if parts else _dotted(node.func)
    return ".".join(reversed(parts))


_PY_SKIP_FALLBACK = re.compile(r"@(?:unittest\.)?skip\w*|mark\.(?:skip|skipif|xfail|todo)|\.skipTest\(|pytest\.(?:skip|xfail|importorskip)\(|"
                               r"expectedFailure|SkipTest")


def _py_skip_markers(source: str) -> List[Tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return [(source.count("\n", 0, m.start()) + 1, m.group(0)) for m in _PY_SKIP_FALLBACK.finditer(source)]
    out = []  # type: List[Tuple[int, str]]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in node.decorator_list:
                n = _dotted(d)
                if n in _DECORATOR_SKIPS or n.endswith((".skip", ".skipif", ".xfail", ".skipIf", ".skipUnless", ".expectedFailure")):
                    out.append((d.lineno, n))
        elif isinstance(node, ast.Call):
            n = _dotted(node.func)
            if n in _CALL_SKIPS or n.endswith((".skipTest", ".importorskip")):
                out.append((node.lineno, n))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            for sub in ast.walk(node.value):
                n = _dotted(sub) if isinstance(sub, (ast.Attribute, ast.Name)) else ""
                if n.endswith(("mark.skip", "mark.skipif", "mark.xfail")):
                    out.append((node.lineno, n))
    return sorted(set(out))


def _trivially_true(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value) is True
    if isinstance(node, ast.Name) and node.id == "True":
        return True
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)) \
            and isinstance(node.left, ast.Constant) and isinstance(node.comparators[0], ast.Constant):
        return node.left.value == node.comparators[0].value
    return False


def _bare_exception(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in ("Exception", "BaseException")


def _analyse_function(fn: ast.AST) -> Dict[str, Any]:
    real = fake = 0
    helper = False
    reasons = []  # type: List[str]
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            if _trivially_true(node.test):
                fake += 1
                reasons.append("assert on a constant (line %d)" % node.lineno)
            else:
                real += 1
        elif isinstance(node, ast.Raise) and node.exc is not None and _dotted(node.exc).endswith("AssertionError"):
            real += 1
        elif isinstance(node, ast.Call):
            full = _dotted(node.func)
            name = full.rsplit(".", 1)[-1]
            if name in ("raises", "assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex") or full.endswith("pytest.raises"):
                first = node.args[0] if node.args else None
                if first is not None and (_bare_exception(first) or (isinstance(first, ast.Tuple) and any(_bare_exception(e) for e in first.elts))):
                    fake += 1
                    reasons.append("%s(Exception) catches everything (line %d)" % (name, node.lineno))
                elif first is None and not node.keywords:
                    fake += 1
                    reasons.append("%s without an exception type (line %d)" % (name, node.lineno))
                else:
                    real += 1
            elif name.startswith("assert") and name not in ("assert_", ):
                args = node.args
                trivial = False
                if name in ("assertTrue", "assertIsNotNone", "assertIsNot") and args and isinstance(args[0], ast.Constant) and bool(args[0].value):
                    trivial = True
                elif name in ("assertEqual", "assertEquals", "assertIs", "assertIn", "assertGreaterEqual", "assertLessEqual") and len(args) >= 2 \
                        and isinstance(args[0], ast.Constant) and isinstance(args[1], ast.Constant) and args[0].value == args[1].value:
                    trivial = True
                if trivial:
                    fake += 1
                    reasons.append("%s on constants (line %d)" % (name, node.lineno))
                else:
                    real += 1
            elif _HELPER_RE.match(name):
                helper = True
    return {"real": real, "fake": fake, "helper": helper, "reasons": reasons}


def py_test_functions(source: str, patterns: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, Any]]]:
    """Every test function in a Python source: name, line, the AC ids it names (when `patterns` {ac: regex} is given), real and fake
    assertion counts, and whether it carries a skip marker. None when the source does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = source.splitlines()
    markers = [m[0] for m in _py_skip_markers(source)]
    out = []  # type: List[Dict[str, Any]]

    def visit(body: Sequence[ast.AST], prefix: str, class_skipped: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                cs = class_skipped or any(m in markers for m in [d.lineno for d in node.decorator_list])
                visit(node.body, prefix + node.name + ".", cs)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.lower().startswith("test"):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                end = getattr(node, "end_lineno", None) or node.lineno
                text = prefix + node.name + "\n" + "\n".join(lines[start - 1:end])
                info = _analyse_function(node)
                skipped = class_skipped or any(start <= m <= end for m in markers)
                acs = [a for a, rx in (patterns or {}).items() if rx.search(text)]
                out.append({"name": prefix + node.name, "line": node.lineno, "acs": acs, "real": info["real"], "fake": info["fake"],
                            "helper": info["helper"], "reasons": info["reasons"], "skipped": skipped})
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                visit(getattr(node, "body", []), prefix, class_skipped)
    visit(tree.body, "", False)
    return out


# -- JavaScript and the rest (regex) --------------------------------------------------------------------------------------------------

_JS_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:'\"`])//[^\n]*", re.S)
_JS_SKIP_RE = re.compile(r"\b(?:it|test|describe|context|specify|suite)\.(?:skip|only|todo|fixme|failing)\b|\b(?:xit|xtest|xdescribe|xcontext|fit|fdescribe)\s*\(|"
                         r"\.only\s*\(|\bpending\s*\(")
_OTHER_SKIP_RE = {"go": re.compile(r"\bt\.Skip(?:f|Now)?\("), "rust": re.compile(r"#\[ignore"), "java": re.compile(r"@(?:Disabled|Ignore)\b"),
                  "ruby": re.compile(r"\b(?:xit|xdescribe|pending)\b|^\s*skip\b", re.M)}
_JS_NAME_RE = re.compile(r"\b(?:it|test)(?:\.(?:only|skip|todo))?\s*\(\s*(['\"`])((?:\\.|(?!\1).)+)\1")
_GO_NAME_RE = re.compile(r"^func (Test\w+)\(", re.M)
_RUST_NAME_RE = re.compile(r"#\[(?:tokio::)?test\][^\n]*\n(?:\s*#\[[^\n]*\n)*\s*(?:async\s+)?fn (\w+)")
_JAVA_NAME_RE = re.compile(r"@Test\b[^\n]*\n(?:\s*@\w+[^\n]*\n)*\s*(?:public |private |protected )?(?:static )?(?:void|fun) +(\w+)")
_RUBY_NAME_RE = re.compile(r"\bit\s+['\"](.+?)['\"]|\bdef (test_\w+)")


def _strip_js(source: str) -> str:
    return _JS_COMMENT_RE.sub("", source)


def skip_markers(path: str, source: str) -> List[Tuple[int, str]]:
    """Skip, only, todo, xfail and ignore markers: [(line, text)]."""
    lang = language(path)
    if lang == "python":
        return _py_skip_markers(source)
    if lang == "javascript":
        src = _strip_js(source)
        return [(src.count("\n", 0, m.start()) + 1, m.group(0)) for m in _JS_SKIP_RE.finditer(src)]
    rx = _OTHER_SKIP_RE.get(lang)
    return [(source.count("\n", 0, m.start()) + 1, m.group(0).strip()) for m in rx.finditer(source)] if rx else []


def test_names(path: str, source: str) -> Set[str]:
    """The names of the tests defined in a file. Empty for a language this module does not read (the callers treat that as "unknown")."""
    lang = language(path)
    if lang == "python":
        fns = py_test_functions(source)
        if fns is None:
            return set(re.findall(r"def (test\w*)\(", source))
        return {f["name"] for f in fns}
    if lang == "javascript":
        return {m.group(2) for m in _JS_NAME_RE.finditer(_strip_js(source))}
    if lang == "go":
        return set(_GO_NAME_RE.findall(source))
    if lang == "rust":
        return set(_RUST_NAME_RE.findall(source))
    if lang == "java":
        return set(_JAVA_NAME_RE.findall(source))
    if lang == "ruby":
        return {a or b for a, b in _RUBY_NAME_RE.findall(source)}
    return set()


_JS_FAKE_RE = re.compile(r"\bexpect\(\s*(?:true|false|[0-9]+|null|undefined|'[^']*'|\"[^\"]*\")\s*\)|\bassert\(\s*(?:true|1)\s*\)|\bassert\.ok\(\s*true\s*\)")
_JS_REAL_RE = re.compile(r"\bexpect\s*\(|\bassert[\w.]*\s*\(|\.should\b|\bt\.(?:is|true|deepEqual|equal)\(")


def js_assertion_problems(source: str) -> Dict[str, int]:
    """Low-confidence read of a JS/TS test file: {"real": n, "fake": n}. A file that names an acceptance criterion and has no real
    assertion at all, or only constant ones, is flagged by the caller."""
    src = _strip_js(source)
    return {"real": len(_JS_REAL_RE.findall(src)) - len(_JS_FAKE_RE.findall(src)), "fake": len(_JS_FAKE_RE.findall(src))}


# -- G5 lint helpers -------------------------------------------------------------------------------------------------------------------

_FRAMEWORK_IMPORT_RE = re.compile(r"(?m)^\s*(?:import|from)\s+(?:unittest|pytest|_pytest|nose2?|doctest)\b")


def imports_test_framework(source: str) -> bool:
    """Whether a (production) Python file imports the test framework: the way to make every assertion in a test pass is to patch
    `unittest.TestCase.assertEqual` from a module the tests import."""
    return bool(_FRAMEWORK_IMPORT_RE.search(source))


ENV_DETECT_RE = re.compile(r"PYTEST_CURRENT_TEST|JEST_WORKER_ID|VITEST_WORKER_ID|NODE_ENV\s*[!=]==?\s*['\"]test['\"]|"
                           r"['\"](?:pytest|unittest|_pytest)['\"]\s+(?:not\s+)?in\s+sys\.modules|sys\.modules\.get\(\s*['\"](?:pytest|unittest)|"
                           r"RUNNING_TESTS|TEST_MODE\b|IS_TESTING|process\.env\.TESTING|os\.environ(?:\.get)?\(?\[?\s*['\"]TESTING")


def env_detection_count(source: str) -> int:
    return len(ENV_DETECT_RE.findall(source))


_PY_PATCH_RE = re.compile(r"""(?:\bpatch|\bmock\.patch|\bsetattr|\bpatch\.dict)\(\s*['"]([A-Za-z_][\w.]*)['"]""")
_JS_MOCK_RE = re.compile(r"""\b(?:jest|vi)\.(?:mock|doMock|spyOn)\(\s*['"]([^'"]+)['"]""")


def mock_targets(path: str, source: str) -> Set[str]:
    """Repo-relative file candidates (python: dotted names turned into paths; javascript: relative specifiers) that mocks in this file
    replace. A mock of the item's own footprint is mocking the code under test (G5, code-reviewer rule 6)."""
    lang = language(path)
    out = set()  # type: Set[str]
    if lang == "python":
        for dotted in _PY_PATCH_RE.findall(source):
            parts = dotted.split(".")
            for n in range(1, len(parts) + 1):
                rel = "/".join(parts[:n])
                out.add(rel + ".py")
                out.add(rel + "/__init__.py")
        return out
    if lang == "javascript":
        d = os.path.dirname(path)
        for spec in _JS_MOCK_RE.findall(_strip_js(source)):
            if not spec.startswith("."):
                rel = spec
            else:
                rel = os.path.normpath(os.path.join(d, spec)).replace(os.sep, "/")
            for suffix in ("", ".js", ".ts", ".tsx", ".jsx", ".mjs", "/index.js", "/index.ts"):
                out.add(rel + suffix)
    return out


_UPDATE_SNAP_RE = re.compile(r"--update-snapshots?\b|--snapshot-update\b|UPDATE_SNAPSHOTS?\b|\b(?:jest|vitest)\b[^\n]*\s-u\b|\bsnapshot[-_ ]update\b",
                             re.I)


def update_snapshot_flags(source: str) -> int:
    return len(_UPDATE_SNAP_RE.findall(source))
