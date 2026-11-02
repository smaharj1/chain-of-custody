"""Kit test suite. Run with `tools/test.sh` (stdlib unittest, offline, Python 3.9+).

Importing this package puts `.claude/scripts` on sys.path so tests can `import kitlib`
exactly the way kit scripts do.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, ".claude", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
