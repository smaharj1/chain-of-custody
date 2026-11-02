"""Every kit script and test must run on Python 3.9 (IMP-4).

Build and local tests run on a newer Python, so this guards the floor: a 3.9 `python3` on a
fresh Mac must not choke on a fail-closed hook. Three layers:

  1. `ast.parse(..., feature_version=(3, 9))` over `.claude/scripts`, `tools` and `tests`
  2. AST checks for things the parser accepts on newer Pythons but 3.9 cannot run:
     post-3.9 APIs, `X | Y` annotations without `from __future__ import annotations`,
     `isinstance(x, A | B)`, `zip(strict=)`, `except*`, `match`, parenthesised `with ... as`
  3. a token-level grep (comments and strings removed) for `with (` ... `as` blocks

The kit-tests CI workflow also runs the whole suite on a real 3.9 interpreter.
"""
from __future__ import annotations

import ast
import io
import os
import tokenize
import unittest

from . import REPO_ROOT

ROOTS = (os.path.join(".claude", "scripts"), "tools", "tests")

# Names that are new in 3.10+ whatever they hang off (methods and unique names).
POST_39_ANYWHERE = {
    "pairwise", "bit_count", "StrEnum", "ExceptionGroup", "BaseExceptionGroup", "TaskGroup",
    "aiter", "anext", "assertNoLogs", "enterContext", "enterModuleContext", "stdlib_module_names",
    "orig_argv", "get_overloads", "reveal_type", "assert_never", "LiteralString", "TypeGuard",
    "ParamSpec", "TypeAlias", "Concatenate", "NotRequired", "aclosing", "is_integer_ratio",
}
# Common words that are only post-3.9 when they come from `typing`.
POST_39_TYPING = {"Self", "Never", "Required", "Unpack", "override", "TypeVarTuple", "dataclass_transform"}
POST_39_KWARGS = {
    ("zip", "strict"), ("dataclass", "slots"), ("dataclass", "kw_only"), ("dataclass", "match_args"),
    ("run", "process_group"), ("Popen", "process_group"), ("rmtree", "onexc"),
}
POST_39_MODULES = {"tomllib", "tomli"}


def python_files():
    for root in ROOTS:
        base = os.path.join(REPO_ROOT, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def has_future_annotations(tree):
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(a.name == "annotations" for a in node.names):
                return True
    return False


def _annotation_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            yield node.returns


def _has_bitor(node):
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(node))


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def ast_problems(tree, source_name):
    problems = []
    future = has_future_annotations(tree)
    if not future:
        for ann in _annotation_nodes(tree):
            if _has_bitor(ann):
                problems.append("%s:%d: `X | Y` annotation needs `from __future__ import annotations`" % (source_name, ann.lineno))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if type(node).__name__ in ("Match", "TryStar", "TypeAlias"):
            problems.append("%s:%d: %s statement needs Python 3.10+" % (source_name, line, type(node).__name__))
        elif isinstance(node, ast.Attribute) and node.attr in POST_39_ANYWHERE:
            problems.append("%s:%d: attribute .%s needs Python 3.10+" % (source_name, line, node.attr))
        elif isinstance(node, ast.Attribute) and node.attr in POST_39_TYPING \
                and isinstance(node.value, ast.Name) and node.value.id == "typing":
            problems.append("%s:%d: typing.%s needs Python 3.10+" % (source_name, line, node.attr))
        elif isinstance(node, ast.Name) and node.id in POST_39_ANYWHERE and isinstance(node.ctx, ast.Load):
            problems.append("%s:%d: name %s needs Python 3.10+" % (source_name, line, node.id))
        elif isinstance(node, ast.ImportFrom):
            if node.module in POST_39_MODULES:
                problems.append("%s:%d: module %s needs Python 3.11+" % (source_name, line, node.module))
            for a in node.names:
                if a.name in POST_39_ANYWHERE or (node.module == "typing" and a.name in POST_39_TYPING):
                    problems.append("%s:%d: import of %s needs Python 3.10+" % (source_name, line, a.name))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in POST_39_MODULES:
                    problems.append("%s:%d: module %s needs Python 3.11+" % (source_name, line, a.name))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            for kw in node.keywords:
                if (name, kw.arg) in POST_39_KWARGS:
                    problems.append("%s:%d: %s(%s=) needs a newer Python" % (source_name, line, name, kw.arg))
            if name in ("isinstance", "issubclass") and len(node.args) == 2 and _has_bitor(node.args[1]):
                problems.append("%s:%d: isinstance(x, A | B) needs Python 3.10+" % (source_name, line))
    return problems


def paren_with_problems(source, source_name):
    """`with (a as x, b as y):` is 3.10+. Found on the token stream, so strings and comments
    cannot trigger it."""
    problems = []
    toks = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
            if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                              tokenize.DEDENT, tokenize.STRING)]
    for i, t in enumerate(toks):
        if t.type == tokenize.NAME and t.string == "with" and i + 1 < len(toks) and toks[i + 1].string == "(":
            depth, j, saw_as = 0, i + 1, False
            while j < len(toks):
                if toks[j].string == "(":
                    depth += 1
                elif toks[j].string == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif toks[j].string == "as" and depth == 1:
                    saw_as = True
                j += 1
            if saw_as and j + 1 < len(toks) and toks[j + 1].string == ":":
                problems.append("%s:%d: parenthesised multi-item `with ... as` needs Python 3.10+" % (source_name, t.start[0]))
    return problems


class Py39SyntaxTest(unittest.TestCase):
    def test_files_found(self):
        files = list(python_files())
        self.assertGreater(len(files), 10, "expected to find the kit's python files")

    def test_every_file_parses_as_py39_and_avoids_newer_apis(self):
        problems = []
        for path in python_files():
            rel = os.path.relpath(path, REPO_ROOT)
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            try:
                tree = ast.parse(src, filename=rel, feature_version=(3, 9))
            except SyntaxError as exc:
                problems.append("%s:%s: not valid Python 3.9 syntax: %s" % (rel, exc.lineno, exc.msg))
                continue
            problems += ast_problems(tree, rel)
            problems += paren_with_problems(src, rel)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


class CheckerSelfTest(unittest.TestCase):
    """The checks must actually fire; otherwise the test above proves nothing."""

    def _problems(self, src):
        tree = ast.parse(src)
        return ast_problems(tree, "x.py") + paren_with_problems(src, "x.py")

    def test_flags_union_annotation_without_future(self):
        self.assertTrue(self._problems("def f(a: int | None): pass\n"))
        self.assertFalse(self._problems("from __future__ import annotations\ndef f(a: int | None): pass\n"))

    def test_flags_isinstance_union_and_zip_strict(self):
        self.assertTrue(self._problems("isinstance(1, int | str)\n"))
        self.assertTrue(self._problems("list(zip([1], [2], strict=True))\n"))

    def test_flags_new_apis(self):
        self.assertTrue(self._problems("import itertools\nitertools.pairwise([1])\n"))
        self.assertTrue(self._problems("import tomllib\n"))
        self.assertTrue(self._problems("from typing import Self\n"))

    def test_flags_parenthesised_with(self):
        self.assertTrue(self._problems("with (open('a') as a, open('b') as b):\n    pass\n"))
        self.assertFalse(self._problems("with open('a') as a, open('b') as b:\n    pass\n"))
        self.assertFalse(self._problems("with (yield):\n    pass\n"))

    def test_flags_match_statement_on_parse(self):
        src = "match x:\n    case 1:\n        pass\n"
        with self.assertRaises(SyntaxError):
            ast.parse(src, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
