"""Packaging tests: the plugin validator, migrate.py and init_project.py. CLIs run in-process; git is touched only where it is the subject."""
from __future__ import annotations

import contextlib
import io
import os
import sys

from .. import REPO_ROOT

TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

def call(main, argv, env=None):
    """(return code, stdout, stderr) of `main(argv, env)` run in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv, env if env is not None else {})
    return rc, out.getvalue(), err.getvalue()
