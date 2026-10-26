"""Tests for .claude/scripts/verify.py (Phase 3, report §9 G6, §19.7 L-27).

Helpers build a temp git repo with a committed `.claude/loop.config.md` on `main` and a feature branch on top,
so every test exercises the real base-sha reading. Commands run through the real shell, in their own session, under
the real allowlisted environment; nothing touches the network, the real HOME or the real `claude`.
"""
from __future__ import annotations

import os
import shlex
import sys
from typing import Any, Dict, List, Mapping, Optional

from kitlib import miniyaml

from .. import REPO_ROOT  # noqa: F401
from ..helpers import BaseTestCase, TempRepo

PY = shlex.quote(sys.executable)
UNITTEST = "%s -m unittest discover -s tests -t ." % PY


def config_text(values: Optional[Mapping[str, Any]] = None) -> str:
    lines = []  # type: List[str]
    for k, v in (values or {}).items():
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(miniyaml.dump_scalar(x) for x in v)))
        else:
            lines.append("%s: %s" % (k, miniyaml.dump_scalar(v)))
    return "# Loop Config\n\n```yaml\n%s\n```\n" % "\n".join(lines)


PASSING_TEST = (
    "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n")
FAILING_TEST = PASSING_TEST.replace("assertTrue(True)", "assertTrue(False)")


class VerifyCase(BaseTestCase):
    """A repo with `main` (config + a passing test) and the working tree on `feat/x`."""

    def make_repo(self, config: Optional[Mapping[str, Any]] = None, files: Optional[Dict[str, str]] = None,
                  branch: bool = True) -> TempRepo:
        r = self.repo()
        cfg = {"verify_tests": UNITTEST}
        cfg.update(config or {})
        r.write(".claude/loop.config.md", config_text(cfg))
        r.write("tests/__init__.py", "")
        r.write("tests/test_a.py", PASSING_TEST)
        for rel, text in (files or {}).items():
            r.write(rel, text)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "base")
        self.base = r.head()
        if branch:
            r.git("switch", "-q", "-c", "feat/x")
        return r

    def commit(self, r: TempRepo, files: Dict[str, str], msg: str = "work") -> str:
        for rel, text in files.items():
            r.write(rel, text)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", msg)
        return r.head()

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill_later(self, pid_file: str) -> None:
        import signal

        def _k():
            try:
                with open(pid_file) as fh:
                    os.kill(int(fh.read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(_k)
