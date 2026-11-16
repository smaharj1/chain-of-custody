"""Liveness for the driver and its children (report 5.8, 10.6, 15 item 5, 19.7; CS-11, CS-23, RC-7).

Three questions, each answered from files the hooks write, never from the child's say-so:

* IS THE DRIVER STILL THE LOCK HOLDER? `Heartbeat.tick()` re-stamps the run lock's heartbeat on a timer (a long child would otherwise
  leave it untouched for minutes) and reports `lock_lost` the moment the lock is gone, stolen or unreadable: a driver that no longer
  holds the lock must stop writing state and kill its child.
* IS THE CHILD DOING ANYTHING? A stall is "no tool call for `dispatch_timeout_min`" (§15 item 5). A tool call shows up as a line in
  the child's telemetry stream (`events/s-<session>.jsonl`, PostToolUse, guards/telemetry.py), plus its output files growing.
  `child_activity` is the time of the newest such sign. A child whose hooks never loaded has no stream at all, which is why the
  canary is checked separately and first.
* DID ITS HOOKS LOAD? The SessionStart hook writes `nonces/<session>`. A child without a nonce is a session whose guards are not
  running (V5, report 5.8): `canary_present` is how the driver refuses to trust it. "No nonce, nothing runs."

The driver calls `Heartbeat.tick` from the launch loop, so HALT (a human's `loopctl halt` or `abort`) stops a running child within one
poll interval.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, List, Optional

from kitlib import runstate

from . import common

__all__ = ["Heartbeat", "child_activity", "canary_present", "REASON_HALT", "REASON_LOCK", "REASON_INTERRUPT"]

REASON_HALT, REASON_LOCK, REASON_INTERRUPT = "halt", "lock_lost", "interrupted"


class Heartbeat(object):
    """The driver's side of the lock. `interrupted` is set by the signal handler; `tick` is passed to `launch.run_child`."""

    def __init__(self, rs: Any, session_id: str, every_s: float = 30.0, clock: Callable[[], float] = time.time) -> None:
        self.rs = rs
        self.session_id = session_id
        self.every_s = every_s
        self.clock = clock
        self.last = 0.0
        self.interrupted = False

    def holds_lock(self) -> bool:
        """True only while the lock file exists, parses, and names this driver's session."""
        try:
            rec = self.rs.read_lock()
        except common.READ_ERRORS:
            return False
        return bool(rec) and rec.get("sessionId") == self.session_id

    def beat(self, force: bool = False) -> bool:
        """Re-stamp the heartbeat when it is due. False if the lock is no longer ours (nothing is written then)."""
        now = self.clock()
        if not force and now - self.last < self.every_s:
            return True
        ok = self.rs.heartbeat(self.session_id)
        if ok:
            self.last = now
        return ok

    def tick(self) -> Optional[str]:
        """A reason to end the running child now, or None. Order: an interrupt, HALT, the lock, then the timer."""
        if self.interrupted:
            return REASON_INTERRUPT
        if self.rs.is_halted():
            return REASON_HALT
        if not self.holds_lock() or not self.beat():
            return REASON_LOCK
        return None


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def child_activity(rs: Any, session_id: str) -> Callable[[float], float]:
    """A function `started -> time of the child's newest sign of life`: its telemetry stream, any subagent stream touched since it
    started, its nonce, and its captured output."""
    d = os.path.join(rs.root, "children")
    own = [os.path.join(rs.events_dir, "s-%s.jsonl" % session_id), os.path.join(rs.nonces_dir, session_id),
           os.path.join(d, session_id + ".out"), os.path.join(d, session_id + ".err")]

    def last(started: float) -> float:
        best = max([_mtime(p) for p in own] + [0.0])
        try:
            names = os.listdir(rs.events_dir)
        except OSError:
            names = []
        for n in names:
            if n.startswith("a-") and n.endswith(".jsonl"):        # subagent streams (the child's own subagents, if any)
                m = _mtime(os.path.join(rs.events_dir, n))
                if m >= started:
                    best = max(best, m)
        return best

    return last


def canary_present(rs: Any, session_id: str, also: Optional[List[str]] = None) -> bool:
    """The SessionStart nonce exists for the session (or for one of the ids the child reported for itself)."""
    for sid in [session_id] + list(also or []):
        if runstate.valid_id(sid) and rs.read_nonce(sid):
            return True
    return False
