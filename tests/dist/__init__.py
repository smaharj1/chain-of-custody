"""Phase 9 packaging tests: the generated plugin and copy install, the validator, migrate.py and init_project.py.

Budget (changelog.d/13a-test-speed.md): the build runs in memory (~0.25 s), CLIs run in-process, git is touched only where it is the subject.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

from .. import REPO_ROOT

TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

_BUILD = None


def build_once():
    """The in-memory build of this repository, once per process."""
    global _BUILD
    if _BUILD is None:
        from distlib import build as B
        _BUILD = B.build(REPO_ROOT)
    return _BUILD


def call(main, argv, env=None):
    """(return code, stdout, stderr) of `main(argv, env)` run in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv, env if env is not None else {})
    return rc, out.getvalue(), err.getvalue()
