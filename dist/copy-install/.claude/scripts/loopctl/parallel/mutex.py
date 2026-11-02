"""Leases: who may work on what, right now (report 10.3 / 19.7 Lock, F-7, L-11; D-29; DEC-9).

`<git-common-dir>/loopctl/leases/<name>.json`, one file per claim, shared by every worktree of the repository (the same place as the run lock).

    item-<id>      a worker's claim on one plan item. Two workers cannot hold it: the second `acquire` is refused.
    mutex-schema   the schema mutex, always on: one item with a migration (or `touches.schema`) at a time, from its start until it has merged.
    mutex-deps     the same for dependency changes (one lockfile).
    ports-<n>      a block of ports (`ports.py`).
    integrate      the integration slot: one merge at a time, the single writer of the integration branch (D-6).

A lease record is `{schema, name, token, owner, pid, host, acquired, hb, ttl_s, meta}`. `token` is random and secret to the holder; every renewal,
check and release compares it, so a holder whose lease was reclaimed finds out (`holds()` is False) instead of acting on a claim that is no
longer its own. That is the whole point of the token: a pid or a name can be recycled, a token cannot.

EXPIRY AND RECLAIMING follow the run lock (lock.py). A lease is reclaimable when its owner process is certainly gone (a dead pid on this host: no
heartbeat can ever come, so nothing waits), or when its heartbeat is older than its TTL (`lease_ttl_s`) AND the `alive` callable says the owner is
gone (default: the same pid test; a lease from another host is never judged from here). A caller that knows more passes its own `alive`: that is how
a test, or a second scheduler, says "that owner is dead". Nothing reclaims a lease whose owner is alive and recent, however slow the work is: a
slow gate is not a dead worker.
A lease file that cannot be parsed counts as HELD, never free; `parallel cleanup --apply` is how a human removes it.

Every read-modify-write runs under an exclusive `flock` on `leases/.guard`, so two threads or two processes cannot interleave a reclaim with a
renewal and hand one claim to two holders. The heartbeat is the driver's: it renews on every poll, so TTLs only matter after a crash.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import socket
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from kitlib import jsonio, runstate

from .. import common
from ..common import Refused, UsageError

__all__ = ["LeaseStore", "Lease", "LeaseHeld", "item_lease", "mutex_lease", "ports_lease", "INTEGRATE", "SCHEMA", "acquire_mutexes",
           "release_all", "default_alive"]

SCHEMA = "loopctl.lease.v1"
INTEGRATE = "integrate"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}\Z")
MAX_META_BYTES = 4096


class LeaseHeld(Refused):
    """The claim is someone else's (or unreadable). Never retried silently."""


def item_lease(iid: str) -> str:
    if not common.valid_item_id(iid):
        raise UsageError("invalid item id for a lease")
    return "item-" + iid.lower()


def mutex_lease(lane: str) -> str:
    if lane not in ("schema", "deps"):
        raise UsageError("unknown mutex %r" % common.clean_text(lane, 20))
    return "mutex-" + lane


def ports_lease(slot: int) -> str:
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < 1000:
        raise UsageError("invalid port slot")
    return "ports-%d" % slot


class Lease(object):
    """A claim this process holds. `token` is what proves it."""
    __slots__ = ("name", "token", "owner")

    def __init__(self, name: str, token: str, owner: str) -> None:
        self.name, self.token, self.owner = name, token, owner

    def __repr__(self) -> str:
        return "Lease(%s, %s)" % (self.name, self.owner)


def default_alive(rec: Dict[str, Any]) -> bool:
    """Is the owner of this lease still there? A lease from another host cannot be judged from here and counts as alive."""
    host = rec.get("host")
    if host and host != socket.gethostname():
        return True
    return runstate.pid_alive(rec.get("pid"))


class LeaseStore(object):
    def __init__(self, rs: Any, ttl_s: float = 300.0, clock: Optional[Callable[[], float]] = None,
                 alive: Optional[Callable[[Dict[str, Any]], bool]] = None, pid: Optional[int] = None) -> None:
        self.rs = rs
        self.dir = os.path.join(rs.root, "leases")
        self.ttl_s = max(1.0, float(ttl_s))
        self.clock = clock or common.now
        self.alive = alive or default_alive
        self.pid = os.getpid() if pid is None else pid

    # -- plumbing -----------------------------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        jsonio.ensure_dir(self.rs.root)
        jsonio.ensure_dir(self.dir)
        fd = os.open(os.path.join(self.dir, ".guard"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _path(self, name: str) -> str:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise UsageError("invalid lease name %r" % common.clean_text(name, 40))
        return os.path.join(self.dir, name + ".json")

    def _read(self, name: str) -> Optional[Dict[str, Any]]:
        """The record, None if there is no lease, `{"corrupt": True}` if the file is there but unusable (which counts as held)."""
        path = self._path(name)
        if not os.path.lexists(path):
            return None
        try:
            rec = jsonio.read_json_opt(path)
        except common.READ_ERRORS:
            return {"corrupt": True, "name": name}
        if not isinstance(rec, dict) or rec.get("schema") != SCHEMA or not isinstance(rec.get("token"), str):
            return {"corrupt": True, "name": name}
        return rec

    # -- judging a record ----------------------------------------------------------------------------------------------------------------
    def expired(self, rec: Dict[str, Any]) -> bool:
        hb = rec.get("hb")
        ttl = rec.get("ttl_s")
        if not isinstance(hb, (int, float)) or isinstance(hb, bool):
            return False                                            # unreadable timing is not evidence of death
        return self.clock() - float(hb) > (float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) else self.ttl_s)

    def dead(self, rec: Dict[str, Any]) -> bool:
        """The owner is certainly gone: a process on THIS host that no longer exists. A dead pid cannot stamp a heartbeat, so there is nothing to
        wait for (the run lock's takeover, lock.owner_dead, makes the same call). A recycled pid reads as alive: that errs on the side of leaving the
        lease alone."""
        pid, host = rec.get("pid"), rec.get("host")
        return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and bool(host) and host == socket.gethostname() and not runstate.pid_alive(pid)

    def reclaimable(self, rec: Dict[str, Any]) -> bool:
        if rec.get("corrupt"):
            return False
        return self.dead(rec) or (self.expired(rec) and not self.alive(rec))

    # -- the operations ------------------------------------------------------------------------------------------------------------------
    def acquire(self, name: str, owner: str, ttl_s: Optional[float] = None, **meta: Any) -> Lease:
        """Take the lease. Raises LeaseHeld if it is held, unless the holder's TTL ran out and its owner is gone (then it is reclaimed)."""
        path = self._path(name)
        owner = common.clean_text(owner, 80)
        with self._locked():
            cur = self._read(name)
            if cur is not None and not self.reclaimable(cur):
                if cur.get("corrupt"):
                    raise LeaseHeld("lease %s exists but cannot be read; it counts as held (`loopctl parallel cleanup --apply` removes it)" % name)
                raise LeaseHeld("lease %s is held by %s (heartbeat %s ago)" % (
                    name, common.clean_text(cur.get("owner"), 60), "%ds" % max(0, int(self.clock() - float(cur.get("hb") or 0)))))
            token = secrets.token_hex(16)
            now = self.clock()
            rec = {"schema": SCHEMA, "name": name, "token": token, "owner": owner, "pid": self.pid, "host": socket.gethostname(),
                   "acquired": common.iso(now), "hb": now, "ttl_s": float(ttl_s) if ttl_s else self.ttl_s,
                   "meta": self._meta(meta), "reclaimed": cur.get("owner") if cur is not None else None}
            jsonio.atomic_write_json(path, rec, 0o600)
        return Lease(name, token, owner)

    @staticmethod
    def _meta(meta: Dict[str, Any]) -> Dict[str, Any]:
        out = {}  # type: Dict[str, Any]
        for k, v in meta.items():
            if isinstance(k, str) and re.match(r"^[a-z][A-Za-z0-9_]{0,30}\Z", k):
                try:
                    json.dumps(v)
                except (TypeError, ValueError):
                    continue
                out[k] = v
        if len(json.dumps(out)) > MAX_META_BYTES:
            raise UsageError("lease metadata is too large")
        return out

    def holds(self, lease: Lease) -> bool:
        """True only while the file on disk carries this lease's token. The check to make before anything that cannot be undone."""
        with self._locked():
            cur = self._read(lease.name)
        return bool(cur) and not cur.get("corrupt") and cur.get("token") == lease.token

    def renew(self, lease: Lease) -> bool:
        """Re-stamp the heartbeat. False (and nothing written) if the lease is no longer this holder's."""
        with self._locked():
            cur = self._read(lease.name)
            if not cur or cur.get("corrupt") or cur.get("token") != lease.token:
                return False
            cur["hb"] = self.clock()
            jsonio.atomic_write_json(self._path(lease.name), cur, 0o600)
        return True

    def renew_all(self, leases: Sequence[Lease]) -> List[str]:
        """Renew every lease; the names of the ones that were lost."""
        return [l.name for l in leases if not self.renew(l)]

    def release(self, lease: Lease) -> bool:
        with self._locked():
            cur = self._read(lease.name)
            if not cur or cur.get("corrupt") or cur.get("token") != lease.token:
                return False
            try:
                os.unlink(self._path(lease.name))
            except FileNotFoundError:
                return False
        return True

    def read(self, name: str) -> Optional[Dict[str, Any]]:
        with self._locked():
            return self._read(name)

    def list(self) -> List[Dict[str, Any]]:
        with self._locked():
            return self._list_unlocked()

    def reap(self, keep: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[str]:
        """Remove the leases that are reclaimable and that `keep` does not want. Returns the names removed. Used at a run's start."""
        gone = []  # type: List[str]
        with self._locked():
            for rec in self._list_unlocked():
                if rec.get("corrupt") or not self.reclaimable(rec) or (keep is not None and keep(rec)):
                    continue
                try:
                    os.unlink(self._path(rec["name"]))
                    gone.append(rec["name"])
                except (FileNotFoundError, KeyError):
                    pass
        return gone

    def _list_unlocked(self) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        try:
            names = sorted(n[:-5] for n in os.listdir(self.dir) if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return out
        for n in names:
            if _NAME_RE.match(n):
                rec = self._read(n)
                if rec is not None:
                    out.append(rec)
        return out

    def remove(self, name: str) -> bool:
        """Delete a lease by name, whoever holds it. Only for `parallel cleanup --apply`, which first checks that no driver is running."""
        with self._locked():
            try:
                os.unlink(self._path(name))
                return True
            except FileNotFoundError:
                return False


def acquire_mutexes(store: LeaseStore, owner: str, lanes: Sequence[str], **meta: Any) -> List[Lease]:
    """All of the lanes' mutexes or none (the schema mutex, the dependency mutex), always in the same order so two callers cannot deadlock.
    Raises LeaseHeld naming the lane that was taken; whatever was acquired before it is released."""
    got = []  # type: List[Lease]
    try:
        for lane in sorted(set(lanes)):
            got.append(store.acquire(mutex_lease(lane), owner, **meta))
    except BaseException:
        release_all(store, got)
        raise
    return got


def release_all(store: LeaseStore, leases: Sequence[Lease]) -> None:
    for l in leases:
        try:
            store.release(l)
        except OSError:
            pass
