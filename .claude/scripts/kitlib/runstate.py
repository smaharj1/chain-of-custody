"""The `<git-common-dir>/loopctl/` layout: the contract between guards and loopctl.

Guards (Phase 2) read this directory; loopctl (Phase 4) writes it. Both go through
this module so they cannot disagree on names, formats or atomicity. The directory
lives in the git common dir, so every worktree of a repo sees the same one, and it is
outside `.claude/` (report §10.3, CS-13, CS-25).

    <git-common-dir>/loopctl/
        lock                   JSON, O_EXCL-created. {runId, sessionId, host, worktree,
                               driverPid, startedAt, heartbeat, planRev}      (§19.7 Lock)
        HALT                   presence = halt. JSON {reason, by, at}; guards deny everything
                               except read-only tools while it exists (guard-halt, §7.1)
        run.json               current run record {runId, conductor_session_id, ...}
        sessions/<sid>.json    session registry: {session_id, role, registered_at, ...}.
                               role "conductor" or a roster role. Read by caller.classify (§5.7)
        nonces/<sid>           liveness canary: random hex written by the SessionStart
                               hook; loopctl refuses to dispatch without it (§5.8)
        events/<name>.jsonl    per-agent_id (or session) event files, redacted (§7.3, §5.4)
        spend.jsonl            spend ledger (§10.5)
        heldout/               held-out tests; hidden from engineers only under runner: driver
                               (§9 G3, RC-6)

Item status is NOT here: it lives on `refs/loopctl/state/<plan>` (CAS via gitutil).

Rules every reader/writer follows:
  * ids that come from hook input (session ids, agent ids) are validated against
    `^[A-Za-z0-9._-]{1,128}$` before they become a path component. An invalid id is
    "not registered", never a path traversal.
  * writes are atomic (temp + rename) except the lock, which is create-exclusive.
  * a reader that finds an unparsable lock treats it as HELD, never as free.
  * the directory is mode 0700.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
from typing import Any, Callable, Dict, List, Optional

from . import gitutil, jsonio, redact

__all__ = ["RunState", "valid_id", "pid_alive", "lock_is_stale", "now_iso", "parse_iso", "LOCK_FIELDS"]

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
LOCK_FIELDS = ("runId", "sessionId", "host", "worktree", "driverPid", "startedAt", "heartbeat", "planRev")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value)) and value not in (".", "..")


def now_iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def parse_iso(text: str) -> Optional[float]:
    """Parse the UTC timestamps `now_iso` writes; None if malformed."""
    try:
        import calendar
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_is_stale(record: Dict[str, Any], now: float, stale_min: float,
                  session_registered: Optional[Callable[[str], bool]] = None) -> bool:
    """Stale only when the heartbeat is older than `stale_min` minutes AND the owner is not live
    (driver pid gone, or session not registered) (§19.7). A lock from another host cannot be
    judged live or dead from here, so it is never stale."""
    beat = parse_iso(record.get("heartbeat", ""))
    if beat is None:
        return False
    if now - beat <= stale_min * 60:
        return False
    if record.get("host") and record.get("host") != socket.gethostname():
        return False
    pid = record.get("driverPid")
    if pid is not None and pid_alive(pid):
        return False
    sid = record.get("sessionId")
    if session_registered is not None and sid and session_registered(sid):
        return False
    return True


class RunState(object):
    def __init__(self, common_dir: str) -> None:
        self.common_dir = os.path.abspath(common_dir)
        self.root = os.path.join(self.common_dir, "loopctl")

    @classmethod
    def for_path(cls, path: str) -> Optional["RunState"]:
        """RunState for the repo containing `path` (worktree-aware), or None if not in a repo."""
        cd = gitutil.common_dir(path)
        return cls(cd) if cd else None

    # -- paths ---------------------------------------------------------------
    @property
    def lock_path(self) -> str:
        return os.path.join(self.root, "lock")

    @property
    def halt_path(self) -> str:
        return os.path.join(self.root, "HALT")

    @property
    def run_path(self) -> str:
        return os.path.join(self.root, "run.json")

    @property
    def sessions_dir(self) -> str:
        return os.path.join(self.root, "sessions")

    @property
    def nonces_dir(self) -> str:
        return os.path.join(self.root, "nonces")

    @property
    def events_dir(self) -> str:
        return os.path.join(self.root, "events")

    @property
    def spend_path(self) -> str:
        return os.path.join(self.root, "spend.jsonl")

    @property
    def heldout_dir(self) -> str:
        return os.path.join(self.root, "heldout")

    def ensure(self) -> "RunState":
        for d in (self.root, self.sessions_dir, self.nonces_dir, self.events_dir):
            jsonio.ensure_dir(d)
        return self

    def exists(self) -> bool:
        return os.path.isdir(self.root)

    # -- lock ------------------------------------------------------------------
    def read_lock(self) -> Optional[Dict[str, Any]]:
        """Parsed lock record, None if there is no lock. Raises ValueError if it is corrupt:
        callers must treat that as held."""
        data = jsonio.read_json_opt(self.lock_path)
        if data is not None and not isinstance(data, dict):
            raise ValueError("lock is not a JSON object")
        return data

    def lock_held(self) -> bool:
        """True if a lock file exists, including an unparsable one."""
        return os.path.lexists(self.lock_path)

    def acquire_lock(self, record: Dict[str, Any]) -> bool:
        """O_EXCL-create the lock. True iff this call created it."""
        self.ensure()
        rec = dict(record)
        rec.setdefault("host", socket.gethostname())
        rec.setdefault("startedAt", now_iso())
        rec.setdefault("heartbeat", rec["startedAt"])
        return jsonio.try_create_exclusive(self.lock_path, _dump(rec), 0o600)

    def heartbeat(self, session_id: str, ts: Optional[float] = None) -> bool:
        """Re-stamp the heartbeat if `session_id` owns the lock. Atomic replace."""
        try:
            rec = self.read_lock()
        except ValueError:
            return False
        if rec is None or rec.get("sessionId") != session_id:
            return False
        rec["heartbeat"] = now_iso(ts)
        jsonio.atomic_write_bytes(self.lock_path, _dump(rec), 0o600)
        return True

    def release_lock(self, session_id: str) -> bool:
        try:
            rec = self.read_lock()
        except ValueError:
            return False
        if rec is None or rec.get("sessionId") != session_id:
            return False
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            return False
        return True

    def steal_lock(self, expected: Dict[str, Any], record: Dict[str, Any]) -> bool:
        """Replace a stale lock: atomic rename of the current lock out of the way, then
        O_EXCL-create the new one. Only one concurrent stealer wins the rename. Returns
        False if the lock no longer matches `expected` (runId + heartbeat) or another
        process won."""
        try:
            cur = self.read_lock()
        except ValueError:
            cur = None  # corrupt: may be stolen only if caller passes expected={}
        if cur is None and expected:
            return False
        if cur is not None and (cur.get("runId") != expected.get("runId")
                                or cur.get("heartbeat") != expected.get("heartbeat")):
            return False
        aside = "%s.stolen.%s" % (self.lock_path, secrets.token_hex(4))
        try:
            os.rename(self.lock_path, aside)
        except FileNotFoundError:
            return False
        # Verify what we moved is what we meant to steal (a new holder may have slipped in).
        try:
            moved = jsonio.read_json_opt(aside)
        except ValueError:
            moved = None
        if cur is not None and (not isinstance(moved, dict) or moved.get("runId") != cur.get("runId")
                                or moved.get("heartbeat") != cur.get("heartbeat")):
            try:
                os.link(aside, self.lock_path)  # best-effort restore of the fresh holder
            except OSError:
                pass
            os.unlink(aside)
            return False
        os.unlink(aside)
        return self.acquire_lock(record)

    # -- HALT ------------------------------------------------------------------
    def is_halted(self) -> bool:
        """Cheap and fail-closed: any entry named HALT, readable or not, means halted."""
        return os.path.lexists(self.halt_path)

    def set_halt(self, reason: str, by: str) -> None:
        self.ensure()
        jsonio.atomic_write_bytes(self.halt_path, _dump({"reason": reason, "by": by, "at": now_iso()}), 0o600)

    def read_halt(self) -> Optional[Dict[str, Any]]:
        try:
            data = jsonio.read_json_opt(self.halt_path)
        except ValueError:
            return {"reason": "HALT file present but unreadable", "by": "unknown", "at": ""}
        return data if isinstance(data, dict) else None

    def clear_halt(self) -> bool:
        try:
            os.unlink(self.halt_path)
            return True
        except FileNotFoundError:
            return False

    # -- run record ------------------------------------------------------------
    def write_run(self, record: Dict[str, Any]) -> None:
        self.ensure()
        jsonio.atomic_write_bytes(self.run_path, _dump(record), 0o600)

    def read_run(self) -> Optional[Dict[str, Any]]:
        data = jsonio.read_json_opt(self.run_path)
        return data if isinstance(data, dict) else None

    # -- session registry ----------------------------------------------------
    def register_session(self, session_id: str, role: str, **extra: Any) -> Dict[str, Any]:
        if not valid_id(session_id):
            raise ValueError("invalid session id")
        self.ensure()
        rec = dict(extra)
        rec.update({"session_id": session_id, "role": role})
        rec.setdefault("registered_at", now_iso())
        jsonio.atomic_write_bytes(os.path.join(self.sessions_dir, session_id + ".json"), _dump(rec), 0o600)
        return rec

    def get_session(self, session_id: Any) -> Optional[Dict[str, Any]]:
        """Registry record, or None (also for invalid ids and unreadable records)."""
        if not valid_id(session_id):
            return None
        try:
            data = jsonio.read_json_opt(os.path.join(self.sessions_dir, session_id + ".json"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def unregister_session(self, session_id: str) -> bool:
        if not valid_id(session_id):
            return False
        try:
            os.unlink(os.path.join(self.sessions_dir, session_id + ".json"))
            return True
        except FileNotFoundError:
            return False

    def load_registry(self) -> Dict[str, Dict[str, Any]]:
        """All registered sessions as {session_id: record}, for `caller.classify`."""
        out = {}  # type: Dict[str, Dict[str, Any]]
        try:
            names = sorted(os.listdir(self.sessions_dir))
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json"):
                continue
            sid = n[:-5]
            rec = self.get_session(sid)
            if rec is not None:
                out[sid] = rec
        return out

    # -- liveness nonce ----------------------------------------------------------
    def write_nonce(self, session_id: str) -> str:
        if not valid_id(session_id):
            raise ValueError("invalid session id")
        self.ensure()
        nonce = secrets.token_hex(16)
        jsonio.atomic_write_text(os.path.join(self.nonces_dir, session_id), nonce + "\n", 0o600)
        return nonce

    def read_nonce(self, session_id: Any) -> Optional[str]:
        if not valid_id(session_id):
            return None
        try:
            with open(os.path.join(self.nonces_dir, session_id), "r") as fh:
                text = fh.read().strip()
        except OSError:
            return None
        return text or None

    # -- events ----------------------------------------------------------------
    def event_path(self, name: str) -> str:
        if not valid_id(name):
            raise ValueError("invalid event stream name")
        return os.path.join(self.events_dir, name + ".jsonl")

    def append_event(self, name: str, event: Dict[str, Any]) -> None:
        """Append one redacted JSON line. One write() call on an O_APPEND fd, so concurrent
        appenders do not interleave within a line for lines under PIPE_BUF-ish sizes."""
        self.ensure()
        rec = redact.redact_obj(dict(event))
        rec.setdefault("ts", now_iso())
        line = (json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.event_path(name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def read_events(self, name: str) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        try:
            with open(self.event_path(name), "r", encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln:
                        try:
                            out.append(json.loads(ln))
                        except ValueError:
                            out.append({"_unparsable": ln[:200]})
        except OSError:
            pass
        return out


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
