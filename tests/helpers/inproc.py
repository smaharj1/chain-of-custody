"""Run a kit script in this process, the way `python3 script.py ARGS` would, without starting an interpreter (task 13a).

Starting Python and importing the kit costs ~60 ms of CPU per call, and a CLI test file makes dozens. A test that is about what a
script prints or does for given arguments does not need a new interpreter; a test that is about the process boundary (exit code
from a real shell, signals, stdin that is a pipe, `python3` found on PATH, running from any directory) still uses `subprocess`.

`run_script` executes the file with `runpy` as `__main__` with `sys.argv`, the environment, the working directory and stdin given,
captures stdout and stderr, and turns `SystemExit` into a return code (an uncaught exception prints its traceback and returns 1, as
Python does). It returns a `subprocess.CompletedProcess`, so call sites change by one line. Do not use it for a script that calls
`os._exit`, `os.exec*`, forks, installs signal handlers, or measures its own start-up.
"""
from __future__ import annotations

import contextlib
import io
import os
import runpy
import subprocess
import sys
import traceback
from typing import Any, Dict, Mapping, Optional, Sequence

__all__ = ["run_script", "run_main"]


class _Out(object):
    """A text stream over a byte buffer: scripts write to sys.stdout or sys.stdout.buffer."""

    def __init__(self) -> None:
        self.raw = io.BytesIO()
        self.text = io.TextIOWrapper(self.raw, encoding="utf-8", errors="replace", newline="")

    def value(self) -> bytes:
        self.text.flush()
        return self.raw.getvalue()


@contextlib.contextmanager
def _process_state(env: Optional[Mapping[str, str]], cwd: Optional[str], stdin: Optional[bytes], argv: Sequence[str]):
    saved_env = os.environ.copy()
    saved_cwd = os.getcwd()
    saved = (sys.argv, sys.stdin, sys.stdout, sys.stderr, list(sys.path))
    out, err = _Out(), _Out()
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        if cwd is not None:
            os.chdir(cwd)
        sys.argv = list(argv)
        sys.stdin = io.TextIOWrapper(io.BytesIO(stdin or b""), encoding="utf-8")
        sys.stdout, sys.stderr = out.text, err.text
        yield out, err
    finally:
        sys.argv, sys.stdin, sys.stdout, sys.stderr = saved[:4]
        sys.path[:] = saved[4]
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


def _code(exc: SystemExit, err: _Out) -> int:
    c = exc.code
    if c is None:
        return 0
    if isinstance(c, int):
        return c
    err.text.write(str(c) + "\n")
    return 1


def _finish(args: Sequence[str], rc: int, out: _Out, err: _Out, text: bool) -> "subprocess.CompletedProcess[Any]":
    o, e = out.value(), err.value()
    if text:
        return subprocess.CompletedProcess(list(args), rc, o.decode("utf-8", "replace"), e.decode("utf-8", "replace"))
    return subprocess.CompletedProcess(list(args), rc, o, e)


def run_script(script: str, args: Sequence[str] = (), env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None,
               stdin: Optional[Any] = None, text: bool = True) -> "subprocess.CompletedProcess[Any]":
    """`python3 SCRIPT ARGS` in this process. `env` replaces the environment (None keeps it); `stdin` is str or bytes; `text` picks str or
    bytes output."""
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    argv = [script] + list(args)
    with _process_state(env, cwd, data, argv) as (out, err):
        rc = 0
        try:
            runpy.run_path(script, run_name="__main__")
        except SystemExit as exc:
            rc = _code(exc, err)
        except Exception:
            traceback.print_exc()
            rc = 1
        return _finish([sys.executable] + argv, rc, out, err, text)


def run_main(main: Any, args: Sequence[str] = (), env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None,
             stdin: Optional[Any] = None, text: bool = True, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    """Call `main(list(args), **kw)` (a script's own `main`) with the same capture as `run_script`; its return value is the exit code."""
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    argv = ["script"] + list(args)
    with _process_state(env, cwd, data, argv) as (out, err):
        rc = 0
        try:
            got = main(list(args), **kw)
            rc = got if isinstance(got, int) else 0
        except SystemExit as exc:
            rc = _code(exc, err)
        except Exception:
            traceback.print_exc()
            rc = 1
        return _finish(argv, rc, out, err, text)
