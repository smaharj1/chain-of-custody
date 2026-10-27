"""Tests for the Phase 4b gates (G0, G1, G2, G3, G4, G5, G6, G7, G9, G11, Evidence).

`GateCase` builds one temp repository per test class (a plan with real items, a loop.config, optional files on `main`), and every test works
on its own branch cut from `main`, so the base sha stays fixed and the gates see what they see in a real run: config and plan from the base,
the work from the branch. Nothing here starts `claude`, touches the network or reads the real HOME.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import unittest
from typing import Any, Dict, List, Optional

from .. import LoopRepo, item_doc

from loopctl import gates
from loopctl.context import Context

PY = shlex.quote(sys.executable)
UNITTEST_CMD = "%s -m unittest -v tests.acceptance.test_a" % PY

ACCEPTANCE_OK = '''import unittest
from src import calc


class TestAcceptance(unittest.TestCase):
    def test_ac_a_1_adds(self):
        self.assertEqual(calc.add(1, 2), 3)

    def test_ac_a_2_subtracts(self):
        self.assertEqual(calc.sub(5, 2), 3)
'''
STUB = '''def add(a, b):
    raise NotImplementedError


def sub(a, b):
    raise NotImplementedError
'''
IMPL = '''def add(a, b):
    return a + b


def sub(a, b):
    return a - b
'''
PKG = {"src/__init__.py": "", "tests/__init__.py": "", "tests/acceptance/__init__.py": ""}


def g3_item(iid: str = "A", **ext_g3: Any) -> Dict[str, Any]:
    spec = {"visible": ["tests/acceptance/**"], "stubs": ["src/calc.py"], "cmd": UNITTEST_CMD}
    spec.update(ext_g3)
    return item_doc(iid, acs=2, ext={"g3": spec}, touches={"domains": ["backend"], "paths": ["src/**", "tests/unit/**"], "schema": False, "deps_change": False},
                    verify={"tests": UNITTEST_CMD})


class GateCase(unittest.TestCase):
    """One repo per class. Subclasses set ITEMS (a list of plan items), CFG (extra loop.config yaml) and BASE_FILES (committed to main)."""
    ITEMS = None            # type: Optional[List[Dict[str, Any]]]
    CFG = ""
    BASE_FILES = {}         # type: Dict[str, str]
    DELIVERY = "local"
    lr = None               # type: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.lr = LoopRepo(items=cls.ITEMS or [g3_item()], extra_cfg=cls.CFG, real_gates=True)
        if cls.DELIVERY != "local":
            cls.lr.repo.write(".claude/loop.config.md", cls.lr.cfg_text.replace("delivery: local", "delivery: " + cls.DELIVERY))
        cls.lr.repo.write(".gitignore", "__pycache__/\n*.pyc\n")
        for path, text in cls.BASE_FILES.items():
            cls.lr.repo.write(path, text)
        cls.lr.commit_all("fixture files on main")
        cls.base = cls.lr.repo.head()
        cls._n = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.lr.cleanup()

    def setUp(self) -> None:
        type(self)._n += 1
        r = self.lr.repo
        r.git("clean", "-fdq")
        r.git("switch", "-q", "-f", "-C", "feat/t%d" % type(self)._n, "main")       # a fresh branch off main; local changes are discarded

    # -- building the work ----------------------------------------------------------------------------------------------------------
    def commit(self, files: Dict[str, str], msg: str = "work") -> str:
        for path, text in files.items():
            self.lr.repo.write(path, text)
        return self.lr.commit_all(msg)

    def remove(self, path: str) -> str:
        self.lr.repo.git("rm", "-q", path)
        self.lr.repo.git("commit", "-q", "-m", "remove " + path)
        return self.lr.repo.head()

    def pin_acceptance(self, iid: str = "A") -> None:
        """What a passing G2 leaves behind, without running it: the pin of the visible tests and of everything the acceptance-author committed."""
        from loopctl import locked
        from loopctl.gates import _acrun, _lib
        ctx = self.ctx(iid)
        spec = _lib.g3_spec(ctx.item)[0]
        ents = _lib.tree_entries(ctx, ctx.head_sha)
        auth = {p: ents[p] for _s, p, _o in _lib.diff_status(ctx, untracked=False) if _s != "D" and p in ents and _lib.is_protected(p)}
        _acrun.write_pin(ctx, {p: o for p, o in ents.items() if _lib.match_any(spec["visible"], p)}, auth)
        locked.add_gate_globs(_lib.runstate_of(ctx), "g3", spec["visible"])

    def ctx_rs(self) -> Any:
        from kitlib import runstate
        return runstate.RunState.for_path(self.lr.path)

    def head(self) -> str:
        return self.lr.repo.head()

    # -- running a gate -------------------------------------------------------------------------------------------------------------------
    def ctx(self, iid: str = "A", state: Optional[Dict[str, Any]] = None, head: Optional[str] = None) -> Any:
        # The Context (config and plan read at the base sha) is the same for every test of a class: build it once. The evidence verdict reads a
        # file in the working tree, so it is re-read on every run.
        c = type(self).__dict__.get("_context")
        if c is not None and c.base_sha != self.lr.repo.git("merge-base", "HEAD", "main"):          # a test moved main: the base changed
            c = None
        if c is None:
            c = Context.build(self.lr.path, self.lr.env)
            type(self)._context = c
        c._evidence = None
        return c.gate_context(iid, head, None, dict(state or {}))

    def run_gate(self, name: str, iid: str = "A", state: Optional[Dict[str, Any]] = None, head: Optional[str] = None) -> Dict[str, Any]:
        self.purge_bytecode()
        return gates.run_gate(name, self.ctx(iid, state, head))

    def purge_bytecode(self) -> None:
        """Python keys .pyc files on mtime (one second) and size; two edits of a fixture in one second would run the old code."""
        import shutil
        for dirpath, dirs, _files in os.walk(self.lr.path):
            if ".git" in dirs:
                dirs.remove(".git")
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirs.remove(d)

    def ids(self, res: Dict[str, Any]) -> List[str]:
        return [f["id"] for f in res["findings"]]

    def assertPass(self, res: Dict[str, Any]) -> None:
        self.assertEqual(res["status"], "pass", json.dumps(res["findings"], indent=1)[:1500])

    def assertFails(self, res: Dict[str, Any], *finding_ids: str) -> None:
        self.assertEqual(res["status"], "fail", json.dumps(res["findings"], indent=1)[:1500])
        for f in finding_ids:
            self.assertIn(f, self.ids(res), json.dumps(res["findings"], indent=1)[:1500])
