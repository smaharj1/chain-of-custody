"""Phase 10 tests: the learning loop (report 16.1 row 10; L-32, L-33, L-34, F-29, F-30; DEC-28, DEC-29; H6).

Most of it is pure functions over fixture data (`fixtures.py`): events, a state document, engineer reports and review files held in
`RunData`. The few tests that need a repository use the 4a `LoopRepo` (a template copy) or the 4c `DriverRepo` with the fake `claude`;
commands run in-process. Nothing here starts the real `claude`, touches the network or sleeps.
"""
from __future__ import annotations

from ... import REPO_ROOT, SCRIPTS_DIR  # noqa: F401
