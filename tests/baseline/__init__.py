"""Tests for tools/baseline/* (Phase 0a)."""
from __future__ import annotations

import importlib.util
import os

from .. import REPO_ROOT

BASELINE_DIR = os.path.join(REPO_ROOT, "tools", "baseline")
FIXTURE = os.path.join(REPO_ROOT, "examples", "hello-loop")


def load_module(path: str, name: str):
    """Import a script that is not on sys.path as a module."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_tool(name: str):
    return load_module(os.path.join(BASELINE_DIR, name + ".py"), "baseline_" + name)
