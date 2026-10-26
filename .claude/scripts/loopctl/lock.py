"""The run lock (report section 10.3, 19.7 Lock; CS-19, L-11, F-7).

`<git-common-dir>/loopctl/lock`, created with O_EXCL semantics by `kitlib.runstate` (the content is visible from the first
instant). Record: `{runId, sessionId, host, worktree, driverPid, startedAt, heartbeat, planRev}`. The lock lives outside
`.claude/` and is shared by every worktree of the repository.

* Acquire: create-exclusive. The same session acquiring again gets its own lock back (resume). Anyone else is refused,
  and an unparsable lock counts as HELD, never free.
* Heartbeat: re-stamped by the PostToolUse hook of the owning session (guards/telemetry) and by every state write.
* Stale: only when the heartbeat is older than `lock_stale_min` AND the owner is gone. The owner is gone when the driver
  pid is dead; a lock with no driver pid (the prose conductor) has no process to test, so a heartbeat that old is the
  evidence. A lock from another host is never judged from here (`--host-dead` says you know it is dead).
* Steal: an atomic rename of the stale lock out of the way (only one stealer wins), then a create-exclusive of the new one.
  It never overwrites a live lock, and it re-checks that the lock is still the one it judged stale.
* Release: only by the owning session.
"""
from __future__ import annotations

import os
import socket
from typing import Any, Dict, Optional, Tuple

from kitlib import runstate

from . import common
from .common import Refused

__all__ = ["RunLock", "is_stale"]


def is_stale(record: Dict[str, Any], now: float, minutes: float, host_dead: bool = False) -> bool:
    beat = runstate.parse_iso(record.get("heartbeat", "")) if isinstance(record.get("heartbeat"), str) else None
    if beat is None:
        return False
    if now - beat <= max(0.0, minutes) * 60:
        return False
    host = record.get("host")
    if host and host != socket.gethostname() and not host_dead:
        return False
    pid = record.get("driverPid")
    if pid is not None and runstate.pid_alive(pid) and (not host or host == socket.gethostname()):
        return False
    return True


class RunLock(object):
    def __init__(self, rs: runstate.RunState, stale_min: float = 60.0) -> None:
        self.rs = rs
        self.stale_min = float(stale_min)

    # -- reading ---------------------------------------------------------------------------------
    def _read(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        """("none", None) | ("ok", record) | ("corrupt", None). A lock file that exists but is not a JSON object is corrupt, and
        corrupt counts as held, never as free."""
        if not self.rs.lock_held():
            return "none", None
        try:
            rec = self.rs.read_lock()
        except common.READ_ERRORS:
            return "corrupt", None
        if not isinstance(rec, dict):
            return ("corrupt", None) if self.rs.lock_held() else ("none", None)
        return "ok", rec

    def status(self) -> Dict[str, Any]:
        out = {"held": False, "corrupt": False, "stale": False, "record": None, "age_min": None,
               "stale_after_min": self.stale_min}  # type: Dict[str, Any]
        kind, rec = self._read()
        if kind == "none":
            return out
        out["held"] = True
        if kind == "corrupt":
            out["corrupt"] = True
            return out
        out["record"] = rec
        beat = runstate.parse_iso(rec.get("heartbeat", "")) if isinstance(rec.get("heartbeat"), str) else None
        if beat is not None:
            out["age_min"] = round((common.now() - beat) / 60.0, 1)
        out["stale"] = is_stale(rec, common.now(), self.stale_min)
        if rec.get("host") and rec.get("host") != socket.gethostname():
            out["foreign_host"] = True
        return out

    def holder(self) -> Optional[str]:
        kind, rec = self._read()
        return rec.get("sessionId") if kind == "ok" and rec else None

    def held_by(self, session_id: str) -> bool:
        return runstate.valid_id(session_id) and self.holder() == session_id

    # -- writing ---------------------------------------------------------------------------------
    def _record(self, run_id: str, session_id: str, worktree: str, driver_pid: Optional[int], plan_rev: str) -> Dict[str, Any]:
        ts = common.iso()
        return {"runId": run_id, "sessionId": session_id, "host": socket.gethostname(), "worktree": worktree,
                "driverPid": driver_pid, "startedAt": ts, "heartbeat": ts, "planRev": plan_rev}

    def acquire(self, run_id: str, session_id: str, worktree: str, driver_pid: Optional[int] = None,
                plan_rev: str = "") -> Dict[str, Any]:
        if not runstate.valid_id(session_id) or not runstate.valid_id(run_id):
            raise common.UsageError("invalid session or run id")
        rec = self._record(run_id, session_id, worktree, driver_pid, plan_rev)
        if self.rs.acquire_lock(rec):
            self.rs.append_event("loopctl", {"event": "lock_acquired", "runId": run_id, "sessionId": session_id})
            return rec
        kind, cur = self._read()
        if kind == "corrupt":
            raise Refused("the run lock exists but cannot be read; treating it as held. Inspect %s; `lock steal --corrupt` replaces "
                          "it after you have looked" % self.rs.lock_path)
        if cur is None:
            raise Refused("the run lock changed while it was being taken; try again")
        if cur.get("sessionId") == session_id:
            self.rs.heartbeat(session_id)
            return dict(cur, heartbeat=common.iso())
        who = "session %s on %s, run %s, heartbeat %s" % (common.clean_text(cur.get("sessionId"), 40), common.clean_text(cur.get("host"), 40),
                                                         common.clean_text(cur.get("runId"), 40), common.clean_text(cur.get("heartbeat"), 24))
        raise Refused("the run lock is held (%s). Wait, or `loopctl lock status`; a stale lock can be stolen with `lock steal`" % who)

    def release(self, session_id: str) -> bool:
        ok = self.rs.release_lock(session_id)
        if ok:
            self.rs.append_event("loopctl", {"event": "lock_released", "sessionId": session_id})
        return ok

    def heartbeat(self, session_id: str) -> bool:
        return self.rs.heartbeat(session_id)

    def steal(self, new_run_id: str, session_id: str, worktree: str, after_min: Optional[float] = None,
              host_dead: bool = False, corrupt: bool = False, driver_pid: Optional[int] = None,
              plan_rev: str = "") -> Dict[str, Any]:
        """Replace a stale lock. Refuses unless the lock is stale by `after_min` (never below 1 minute)."""
        minutes = max(1.0, float(after_min if after_min is not None else self.stale_min))
        kind, cur = self._read()
        if kind == "none":
            raise Refused("there is no lock to steal; use `lock acquire`")
        if kind == "corrupt":
            if not corrupt:
                raise Refused("the lock is unreadable; inspect it, then `lock steal --corrupt`")
            expected = {}  # type: Dict[str, Any]
            cur = None
        else:
            if corrupt:
                raise Refused("--corrupt is only for an unreadable lock")
            if not is_stale(cur, common.now(), minutes, host_dead):
                beat = cur.get("heartbeat")
                why = "the heartbeat (%s) is not older than %d minutes" % (common.clean_text(beat, 24), minutes)
                if cur.get("host") and cur.get("host") != socket.gethostname() and not host_dead:
                    why = "it belongs to another host (%s); pass --host-dead only if you know that host is gone" % common.clean_text(cur.get("host"), 40)
                elif cur.get("driverPid") is not None and runstate.pid_alive(cur.get("driverPid")):
                    why = "its driver process (pid %s) is still alive" % cur.get("driverPid")
                raise Refused("the lock is not stale: %s" % why)
            expected = {"runId": cur.get("runId"), "heartbeat": cur.get("heartbeat")}
        rec = self._record(new_run_id, session_id, worktree, driver_pid, plan_rev)
        if not self.rs.steal_lock(expected, rec):
            raise Refused("another process changed the lock first (compare-and-swap lost); look at `lock status` and retry")
        self.rs.append_event("loopctl", {"event": "lock_stolen", "from": (cur or {}).get("runId"), "runId": new_run_id,
                                         "sessionId": session_id})
        return rec
