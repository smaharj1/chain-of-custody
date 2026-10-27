"""Start processes in their own session and kill the whole group afterwards (IMP-14)."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import List, Optional, Sequence

__all__ = ["ProcessTracker"]


class ProcessTracker(object):
    def __init__(self) -> None:
        self._procs = []  # type: List[subprocess.Popen]

    def spawn(self, argv: Sequence[str], **kw) -> subprocess.Popen:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
        kw["start_new_session"] = True
        p = subprocess.Popen(list(argv), **kw)
        self._procs.append(p)
        return p

    def kill_group(self, proc: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    def cleanup(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                self.kill_group(p)
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (p.stdout, p.stderr, p.stdin):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        self._procs = []


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
