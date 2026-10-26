"""Test helpers shared by every phase (IMP-10, IMP-14).

    gitrepo   TempRepo (temp git repo factory), make_bare_remote, make_repo_with_remote
    fakehome  FakeHome: a temp HOME that can plant fake credentials; clean_env()
    bins      install_fake_claude / install_fake_gh (scriptable executables for PATH or KIT_CLAUDE_BIN)
    procs     ProcessTracker: start processes in their own session, kill the group in tearDown
    BaseTestCase  unittest.TestCase with tmpdir(), repo(), home() that clean up after themselves

Nothing here touches the network, the real HOME, real credentials or the real `claude`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import List, Optional

from .. import REPO_ROOT, SCRIPTS_DIR  # noqa: F401  (re-exported for convenience)
from .bins import install_fake_claude, install_fake_gh, read_jsonl  # noqa: F401
from .fakehome import FakeHome, clean_env  # noqa: F401
from .gitrepo import BareRemote, TempRepo, make_bare_remote, make_repo_with_remote  # noqa: F401
from .procs import ProcessTracker  # noqa: F401


class BaseTestCase(unittest.TestCase):
    """Convenience base class. Everything created through it is removed in tearDown."""

    def setUp(self) -> None:
        self._tmpdirs = []  # type: List[str]
        self._repos = []  # type: List[TempRepo]
        self.procs = ProcessTracker()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.procs.cleanup()
        for r in self._repos:
            r.cleanup()
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def tmpdir(self, prefix: str = "kit-test-") -> str:
        d = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
        self._tmpdirs.append(d)
        return d

    def repo(self, **kw) -> TempRepo:
        r = TempRepo(**kw)
        self._repos.append(r)
        return r

    def bare(self) -> BareRemote:
        b = BareRemote()
        self._repos.append(b)  # same cleanup() protocol
        return b

    def repo_with_remote(self):
        r, b = make_repo_with_remote()
        self._repos.extend([r, b])
        return r, b

    def home(self) -> FakeHome:
        return FakeHome(self.tmpdir("kit-home-"))
