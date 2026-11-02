"""The remote ref lock (report 10.3 "Remote lock", 19.7 Lock, RC-10, RC-12; F-7; IMP-1). OPT-IN, EXPERIMENTAL, evidence-gated.

ONE RUN PER PLAN ACROSS MACHINES. The local run lock (lock.py) is one file in one git directory; two laptops, or a laptop and a cloud routine, do not
share it. This lock is a git ref on the remote named by `remote_lock`:

    refs/loopctl/lock/<plan>     a commit whose tree is exactly one file, `lock.json`

The record has the lease shape (parallel/mutex.py): `{schema, plan, name, token, owner, pid, host, acquired, hb, ttl_s, meta}`. `token` proves
ownership: renew and release compare it, so a holder whose lock was taken over finds out instead of acting on a claim that is no longer its own.

CREATE-ONLY. Taking the lock is `git push --force-with-lease=<ref>: <remote> <new>:<ref>` (`kitlib.gitutil.push_cas` with no expected value): the empty
expected value means "must not exist". A plain push would succeed on a fast-forward. Heartbeat and takeover are the same push with the expected value
set to what loopctl just read; release is a lease-guarded delete. A bare `--force-with-lease` is never used (it compares with the last fetch, not with
what loopctl read). Two machines that race for a free lock cannot both win: git applies one push and refuses the other.

ADVISORY, NOT A BOUNDARY (RC-12). GitHub rulesets target branches and tags only, so nothing can protect `refs/loopctl/*`: anyone with `contents: write`
can move or delete this ref, and a well-formed forgery cannot be told from a real record. This coordinates cooperating machines; it does not stop an
attacker. `guards/g_git.py` denies an agent every push of `refs/loopctl/*`: only loopctl's own process pushes it.

STALE means the heartbeat is older than the record's ttl AND the owner is gone: a dead pid on THIS host, or (another host) a human saying
`--host-dead`. A lock whose heartbeat lapsed but whose owner is alive here is not stale: a slow gate is not a dead run. A lock from another host is never
judged from here. A ref that does not hold a well-formed record (a forgery, a hand edit, a different plan's record, a heartbeat from the future) counts as
HELD, never free; `steal --corrupt` replaces it after a human has looked.

TESTED only against a local bare remote. `remote_lock` is a remote NAME that is configured in this repository, never a URL.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kitlib import gitutil, jsonio, runstate

from . import common, lgit
from .common import LoopctlError, Refused, UsageError

__all__ = ["RemoteLock", "Keeper", "ClaimsFilter", "rows", "attach", "Held", "Observed", "RefDoc", "RemoteUnreachable", "judge", "validate_lock", "lock_ref", "SCHEMA", "LOCK_FILE",
           "MIN_TTL_S", "MAX_TTL_S", "state_path", "identity"]

SCHEMA = "loopctl.remote-lock.v1"
LOCK_FILE = "lock.json"
MAX_BYTES = 8192
MIN_TTL_S = 60.0
MAX_TTL_S = 7 * 86400.0
FUTURE_SKEW_S = 600.0                    # a heartbeat further ahead than this is not a clock difference, it is a forgery (it would never expire)
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@ :+=/-]{0,79}\Z")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
_IDENT = {"GIT_AUTHOR_NAME": "loopctl", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}


_REMOTES_SEEN = {}  # type: Dict[Tuple[str, str], float]


class RemoteUnreachable(LoopctlError):
    """git could not talk to the remote (not 'the ref is absent'). The lock's state is unknown; nothing was changed."""
    exit_code = common.EXIT_FINDINGS


def lock_ref(plan: str) -> str:
    if not common.valid_plan_name(plan):
        raise UsageError("invalid plan name %r" % common.clean_text(plan, 40))
    return "refs/loopctl/lock/" + plan


def identity(env: Optional[Dict[str, str]] = None) -> str:
    """Who is running this: `LOOPCTL_USER`, else the OS user. A label for people to read, not a credential."""
    env = os.environ if env is None else env
    who = env.get("LOOPCTL_USER") or env.get("USER") or env.get("LOGNAME") or ""
    if not who:
        try:
            who = os.getlogin()
        except OSError:
            who = "uid-%d" % os.getuid()
    return common.clean_text(re.sub(r"[^A-Za-z0-9._@-]", "-", who), 60) or "unknown"


# -- the record ---------------------------------------------------------------------------------------------------------------------

def validate_lock(doc: Any, plan: str, now: float) -> Optional[str]:
    """Why this is not a loopctl lock record for `plan`, or None. Strict: unknown fields, wrong types and impossible times are all refusals."""
    if not isinstance(doc, dict):
        return "the record is not a JSON object"
    extra = sorted(set(doc) - {"schema", "plan", "name", "token", "owner", "pid", "host", "acquired", "hb", "ttl_s", "meta", "runId"})
    if extra:
        return "unknown field(s): %s" % ", ".join(common.clean_text(k, 20) for k in extra[:3])
    if doc.get("schema") != SCHEMA:
        return "schema is not %s" % SCHEMA
    if doc.get("plan") != plan or doc.get("name") != "lock/" + plan:
        return "the record is for another plan"
    if not isinstance(doc.get("token"), str) or not _TOKEN_RE.match(doc["token"]):
        return "the token is not 32 hex characters"
    if not isinstance(doc.get("owner"), str) or not _OWNER_RE.match(doc["owner"]):
        return "the owner is empty or has unusual characters"
    if not isinstance(doc.get("host"), str) or not _HOST_RE.match(doc["host"]):
        return "the host is empty or has unusual characters"
    pid = doc.get("pid")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 1 << 31):
        return "the pid is not a process id"
    if not isinstance(doc.get("acquired"), str) or runstate.parse_iso(doc["acquired"]) is None:
        return "acquired is not a time"
    hb, ttl = doc.get("hb"), doc.get("ttl_s")
    if isinstance(hb, bool) or not isinstance(hb, (int, float)) or hb != hb or hb in (float("inf"), float("-inf")) or hb < 0:
        return "the heartbeat is not a time"
    if hb > now + FUTURE_SKEW_S:
        return "the heartbeat is in the future (%ds ahead): a lock like that would never expire" % int(hb - now)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl != ttl or not MIN_TTL_S <= ttl <= MAX_TTL_S:
        return "ttl_s is outside %d to %d seconds" % (MIN_TTL_S, MAX_TTL_S)
    meta = doc.get("meta", {})
    if not isinstance(meta, dict) or len(json.dumps(meta, sort_keys=True)) > 2048:
        return "meta is not a small object"
    run = doc.get("runId")
    if run is not None and not (isinstance(run, str) and runstate.valid_id(run)):
        return "runId is not a run id"
    return None


def judge(rec: Dict[str, Any], now: float, host_dead: bool = False, after_s: Optional[float] = None, here: Optional[str] = None,
          pid_alive: Callable[[Any], bool] = runstate.pid_alive) -> Tuple[bool, str]:
    """(stale, why). Pure. Stale needs BOTH an expired heartbeat and an owner that is gone."""
    ttl = float(rec.get("ttl_s") or MIN_TTL_S)
    if after_s is not None:
        ttl = max(ttl, float(after_s))                         # a caller can wait longer than the record says, never shorter
    age = now - float(rec.get("hb") or 0)
    who = common.clean_text(rec.get("owner"), 60)
    if age <= ttl:
        return False, "the heartbeat of %s is %ds old; the limit is %ds" % (who, max(0, int(age)), int(ttl))
    host = rec.get("host")
    me = here if here is not None else socket.gethostname()
    if host == me:
        if pid_alive(rec.get("pid")):
            return False, "the heartbeat of %s is %ds old but its process (pid %s) is alive on this host: a slow run is not a dead one" % (who, int(age), rec.get("pid"))
        return True, "the heartbeat of %s is %ds old and its process (pid %s) is gone" % (who, int(age), rec.get("pid"))
    if host_dead:
        return True, "the heartbeat of %s on %s is %ds old, and you said that host is gone" % (who, common.clean_text(host, 60), int(age))
    return False, "the heartbeat of %s is %ds old, but it runs on %s, which cannot be judged from here; if you know it is gone, pass --host-dead" % (
        who, int(age), common.clean_text(host, 60))


# -- a ref that holds one JSON file -----------------------------------------------------------------------------------------------------

class Observed(object):
    """What the remote held when we looked. state: none | ok | corrupt. `oid` is the commit (also for corrupt, when there is one)."""

    def __init__(self, state: str, oid: Optional[str] = None, doc: Optional[Dict[str, Any]] = None, why: str = "") -> None:
        self.state, self.oid, self.doc, self.why = state, oid, doc, why

    def __repr__(self) -> str:
        return "Observed(%s %s %s)" % (self.state, (self.oid or "")[:10], self.why)


class RefDoc(object):
    """`remote/ref` points at a commit whose tree is one JSON file. Reads fetch and check; writes are compare-and-swap pushes loopctl runs itself.

    Nothing here trusts the remote's content: a ref that is not exactly that shape is `corrupt`, which callers treat as held."""

    def __init__(self, repo: str, remote: str, ref: str, filename: str, check: Callable[[Any], Optional[str]]) -> None:
        self.repo, self.remote, self.ref, self.filename, self.check = repo, remote, ref, filename, check
        self.before_push = None  # type: Optional[Callable[[], None]]       # test seam: a race injected between reading and writing
        self._checked = False

    def _git(self, args: Sequence[str], **kw: Any) -> Any:
        return lgit.run_git(args, cwd=self.repo, **kw)

    def check_remote(self) -> None:
        r = self.remote
        if not isinstance(r, str) or not _REMOTE_RE.match(r):
            raise Refused("remote_lock %r is not a remote name (a URL is never accepted)" % common.clean_text(r, 40))
        if self._checked:
            return
        key = (os.path.realpath(self.repo), r)
        seen = _REMOTES_SEEN.get(key)
        if seen is None or time.time() - seen > 60.0:                       # a remote that is configured stays configured for the minute a run takes to ask again
            if r not in self._git(["remote"]).stdout.split():
                raise Refused("remote_lock %r is not a configured remote of this repository" % r)
            _REMOTES_SEEN[key] = time.time()
        self._checked = True

    # -- reading --------------------------------------------------------------------------------------------------------------------
    def cache_ref(self) -> str:
        return "refs/loopctl/cache/" + self.ref[len("refs/loopctl/"):].replace("/", "-")

    def observe(self) -> Observed:
        """The ref as it is now: none | ok | corrupt. It is fetched into a local cache ref and read from there, so what is judged is exactly what was
        fetched. Two git calls: fetch and one batched read."""
        self.check_remote()
        f = self._git(["fetch", "--no-tags", "--quiet", "--", self.remote, "+%s:%s" % (self.ref, self.cache_ref())], timeout=120, env={"LC_ALL": "C", "LANGUAGE": "C"})
        if f.returncode != 0:
            if "couldn't find remote ref" in f.stderr:
                return Observed("none")                                      # the wording is pinned by LC_ALL=C above; any other failure is "cannot tell", never "free"
            raise RemoteUnreachable("cannot fetch %s from %s: %s" % (self.ref, self.remote, common.clean_text(f.stderr, 120)))
        return self.examine()

    def _batch(self, names: Sequence[str]) -> List[Tuple[Optional[str], str, bytes]]:
        """`git cat-file --batch` for revision expressions we built ourselves: [(oid, type, body)], (None, "missing", b"") for one that is not there."""
        proc = self._git(["cat-file", "--batch"], input_text="\n".join(names) + "\n", binary=True)
        buf, pos, out = proc.stdout, 0, []  # type: bytes, int, List[Tuple[Optional[str], str, bytes]]
        for _n in names:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                out.append((None, "missing", b""))
                continue
            head = buf[pos:nl].decode("ascii", "replace").split()
            pos = nl + 1
            if len(head) != 3 or not head[2].isdigit():
                out.append((None, "missing", b""))
                continue
            size = int(head[2])
            out.append((head[0], head[1], buf[pos:pos + size]))
            pos += size + 1
        return out

    def examine(self) -> Observed:
        """Judge the cached copy of the ref. One batched read of the commit, its tree and the record file. A forged object is read whole (it was
        fetched whole); an oversize or malformed one is `corrupt`, which callers treat as held."""
        ref = self.cache_ref()
        rows = self._batch([ref, ref + "^{tree}", "%s:%s" % (ref, self.filename)])
        (oid, typ, _commit), (_toid, ttyp, tree), (_boid, btyp, body) = rows
        if oid is None or not gitutil.valid_oid(oid):
            return Observed("corrupt", None, None, "the ref could not be read after fetching it")
        if typ != "commit":
            return Observed("corrupt", oid, None, "the ref points at a %s, not at a commit" % common.clean_text(typ, 12))
        entries = []  # type: List[Tuple[bytes, bytes]]
        pos = 0
        while ttyp == "tree" and pos < len(tree):
            sp = tree.find(b" ", pos)
            nul = tree.find(b"\0", sp)
            if sp < 0 or nul < 0 or nul + 21 > len(tree):
                return Observed("corrupt", oid, None, "its tree cannot be read")
            entries.append((tree[pos:sp], tree[sp + 1:nul]))
            pos = nul + 21
        if ttyp != "tree" or entries != [(b"100644", self.filename.encode())] or btyp != "blob":
            return Observed("corrupt", oid, None, "its tree is not exactly %s" % self.filename)
        if len(body) > MAX_BYTES:
            return Observed("corrupt", oid, None, "%s is larger than %d bytes" % (self.filename, MAX_BYTES))
        try:
            doc = common.strict_loads(body.decode("utf-8", "replace"), max_depth=4, max_bytes=MAX_BYTES)
        except (ValueError, RecursionError) as exc:
            return Observed("corrupt", oid, None, "%s is not strict JSON (%s)" % (self.filename, common.clean_text(exc, 60)))
        problem = self.check(doc)
        if problem:
            return Observed("corrupt", oid, None, problem)
        return Observed("ok", oid, doc)

    # -- writing --------------------------------------------------------------------------------------------------------------------
    def make_commit(self, doc: Dict[str, Any], parent: Optional[str], message: str) -> str:
        """A commit holding exactly `filename` = the document, on top of `parent` (a commit this repository already has). Same plumbing as the state ref."""
        text = json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n"
        blob = self._git(["hash-object", "-w", "--stdin"], input_text=text)
        if blob.returncode != 0:
            raise LoopctlError("git hash-object failed: %s" % common.clean_text(blob.stderr, 120))
        tree = self._git(["mktree"], input_text="100644 blob %s\t%s\n" % (blob.stdout.strip(), self.filename))
        if tree.returncode != 0:
            raise LoopctlError("git mktree failed: %s" % common.clean_text(tree.stderr, 120))
        args = ["commit-tree", tree.stdout.strip()] + (["-p", parent] if parent else []) + ["-m", common.clean_text(message, 200) or "loopctl"]
        c = self._git(args, env=dict(_IDENT))
        if c.returncode != 0 or not gitutil.valid_oid(c.stdout.strip()):
            raise LoopctlError("git commit-tree failed: %s" % common.clean_text(c.stderr, 120))
        return c.stdout.strip()

    def push(self, new: str, old: Optional[str]) -> bool:
        """Compare-and-swap push: `old=None` creates (must not exist). False when git refused (someone else moved or created the ref)."""
        if self.before_push is not None:
            self.before_push()
        res = gitutil.push_cas(self.remote, self.ref, new, old, self.repo, isolated=True)
        if res:
            return True
        return self._lost(res.stderr + "\n" + res.stdout, "push to")

    def _lost(self, text: str, what: str) -> bool:
        """False when the compare-and-swap lost (the ref is not what we read); an error when the remote refused or could not be reached."""
        low = text.lower()
        if "remote rejected" in low or "declined" in low or "denied" in low or "permission" in low or "protected" in low:
            raise Refused("the remote refused %s %s: %s" % (what, self.ref, common.clean_text(text, 160)))
        if "stale info" in low or "already exists" in low or "fetch first" in low or "non-fast-forward" in low or "[rejected]" in low or "failed to update" in low:
            return False
        raise RemoteUnreachable("%s %s on %s failed: %s" % (what, self.ref, self.remote, common.clean_text(text, 160)))

    def delete(self, old: str) -> bool:
        """Delete the ref only if the remote still holds `old`."""
        if not gitutil.valid_oid(old) or not gitutil.valid_ref(self.ref):
            raise UsageError("unsafe ref or object id")
        if self.before_push is not None:
            self.before_push()
        proc = gitutil.run_git(["push", "--porcelain", "--force-with-lease=%s:%s" % (self.ref, old), "--", self.remote, ":" + self.ref],
                               cwd=self.repo, isolated=True, pin_hooks=True, timeout=120)
        if proc.returncode == 0:
            return True
        return self._lost(proc.stderr + "\n" + proc.stdout, "delete of")


# -- the lock -----------------------------------------------------------------------------------------------------------------------

class Held(object):
    """A lock this process took: the token that proves it and the commit it last wrote."""
    __slots__ = ("token", "oid", "owner")

    def __init__(self, token: str, oid: str, owner: str) -> None:
        self.token, self.oid, self.owner = token, oid, owner


class RemoteLock(object):
    def __init__(self, repo: str, remote: str, plan: str, ttl_s: float = 3600.0, clock: Optional[Callable[[], float]] = None,
                 host: Optional[str] = None, pid: Optional[int] = None, alive: Callable[[Any], bool] = runstate.pid_alive) -> None:
        self.plan = plan
        self.ref = lock_ref(plan)
        self.ttl_s = min(MAX_TTL_S, max(MIN_TTL_S, float(ttl_s)))
        self.clock = clock or common.now
        self.host = host or socket.gethostname()
        self.pid = os.getpid() if pid is None else pid
        self.alive = alive
        self.doc = RefDoc(repo, remote, self.ref, LOCK_FILE, lambda d: validate_lock(d, plan, self.clock()))

    # -- reading -------------------------------------------------------------------------------------------------------------------
    def status(self, host_dead: bool = False) -> Dict[str, Any]:
        obs = self.doc.observe()
        out = {"ref": self.ref, "remote": self.doc.remote, "state": obs.state, "oid": obs.oid, "why": obs.why, "record": None, "stale": False,
               "stale_why": "", "age_s": None}  # type: Dict[str, Any]
        if obs.state == "ok":
            rec = dict(obs.doc or {})
            rec.pop("token", None)                           # the token is for the holder; status is for anyone's eyes (and logs)
            out["record"] = rec
            out["age_s"] = max(0, int(self.clock() - float((obs.doc or {}).get("hb") or 0)))
            out["stale"], out["stale_why"] = judge(obs.doc or {}, self.clock(), host_dead, None, self.host, self.alive)
        return out

    def _record(self, owner: str, meta: Dict[str, Any], token: Optional[str] = None, acquired: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(owner, str) or not _OWNER_RE.match(owner):
            raise UsageError("the owner must be a short plain name")
        now = self.clock()
        rec = {"schema": SCHEMA, "plan": self.plan, "name": "lock/" + self.plan, "token": token or secrets.token_hex(16), "owner": owner,
               "pid": self.pid, "host": self.host, "acquired": acquired or common.iso(now), "hb": now, "ttl_s": self.ttl_s, "meta": dict(meta or {})}
        if run_id:
            rec["runId"] = run_id
        return rec

    # -- taking --------------------------------------------------------------------------------------------------------------------
    def acquire(self, owner: str, token: Optional[str] = None, run_id: Optional[str] = None, **meta: Any) -> Held:
        """Create the lock. Refused when it exists (held, or unreadable, which counts as held). `token`: this machine's own earlier token, so a
        restarted run gets its lock back instead of waiting for it to expire."""
        obs = self.doc.observe()
        if obs.state == "none":
            rec = self._record(owner, meta, None, None, run_id)
            new = self.doc.make_commit(rec, None, "lock %s by %s" % (self.plan, owner))
            if not self.doc.push(new, None):
                raise Refused("another machine took the remote lock for %s first (compare-and-swap lost): see `loopctl remote-lock status`" % self.plan)
            return Held(rec["token"], new, owner)
        if obs.state == "corrupt":
            raise Refused("%s exists on %s but is not a loopctl lock (%s); it counts as held. Look at it, then `loopctl remote-lock steal --corrupt`" % (
                self.ref, self.doc.remote, obs.why))
        cur = obs.doc or {}
        if token and cur.get("token") == token:
            held = Held(token, obs.oid or "", owner)
            if self.renew(held, observed=obs):
                return held
        stale, why = judge(cur, self.clock(), False, None, self.host, self.alive)
        raise Refused("the remote lock for %s is held by %s on %s (run %s). %s%s" % (
            self.plan, common.clean_text(cur.get("owner"), 60), common.clean_text(cur.get("host"), 60), common.clean_text(cur.get("runId") or "-", 40),
            why, ". It looks stale: `loopctl remote-lock steal`" if stale else ""))

    def steal(self, owner: str, host_dead: bool = False, corrupt: bool = False, after_s: Optional[float] = None, run_id: Optional[str] = None,
              **meta: Any) -> Held:
        """Replace a stale lock (or, with `corrupt`, an unreadable one). Never replaces a lock that is alive or recent, and the replacement is a
        compare-and-swap from the exact record that was judged: if anything moved in between, nothing changes."""
        obs = self.doc.observe()
        if obs.state == "none":
            raise Refused("there is no remote lock to steal; use `remote-lock acquire`")
        if obs.state == "corrupt":
            if not corrupt:
                raise Refused("%s is not a loopctl lock (%s); inspect it, then `remote-lock steal --corrupt`" % (self.ref, obs.why))
            parent = None
        else:
            if corrupt:
                raise Refused("--corrupt is only for a ref that is not a lock record")
            stale, why = judge(obs.doc or {}, self.clock(), host_dead, after_s, self.host, self.alive)
            if not stale:
                raise Refused("the remote lock is not stale: %s" % why)
            parent = obs.oid
        rec = self._record(owner, meta, None, None, run_id)
        new = self.doc.make_commit(rec, parent, "steal lock %s by %s" % (self.plan, owner))
        if not self.doc.push(new, obs.oid):
            raise Refused("the remote lock changed while it was being taken over (compare-and-swap lost); look at `remote-lock status` and retry")
        return Held(rec["token"], new, owner)

    # -- keeping and giving back ---------------------------------------------------------------------------------------------------------
    def holds(self, held: Held) -> bool:
        obs = self.doc.observe()
        return obs.state == "ok" and (obs.doc or {}).get("token") == held.token

    def renew(self, held: Held, observed: Optional[Observed] = None) -> bool:
        """Re-stamp the heartbeat. False (nothing written) if the lock is no longer this holder's. Raises RemoteUnreachable when the remote cannot be
        reached: that is not 'lost'."""
        obs = observed or self.doc.observe()
        if obs.state != "ok" or (obs.doc or {}).get("token") != held.token:
            return False
        cur = obs.doc or {}
        rec = self._record(cur.get("owner") or held.owner, cur.get("meta") or {}, held.token, cur.get("acquired"), cur.get("runId"))
        new = self.doc.make_commit(rec, obs.oid, "heartbeat %s" % self.plan)
        if not self.doc.push(new, obs.oid):
            return False
        held.oid = new
        return True

    def release(self, held: Held) -> bool:
        obs = self.doc.observe()
        if obs.state != "ok" or (obs.doc or {}).get("token") != held.token:
            return False
        return self.doc.delete(obs.oid or "")


# -- the holder inside a run ---------------------------------------------------------------------------------------------------------------

def state_path(rs: Any) -> str:
    return os.path.join(rs.root, "remote-lock.json")


class Keeper(object):
    """What a driver run holds: the lock, its token on disk (so `remote-lock release` can free it after a crash), a heartbeat and the stop hook.

    `beat()` is cheap when no heartbeat is due. A lost lock stops the run at once; an unreachable remote stops it only once half the ttl has gone by
    without a successful heartbeat (another machine may take the lock after the full ttl, and this run must be gone before it does)."""

    def __init__(self, lock: RemoteLock, rs: Any, owner: str, interval_s: Optional[float] = None, run_id: Optional[str] = None) -> None:
        self.lock, self.rs, self.owner, self.run_id = lock, rs, owner, run_id
        self.interval_s = float(interval_s) if interval_s else max(5.0, lock.ttl_s / 4.0)
        self.held = None  # type: Optional[Held]
        self.last_ok = 0.0
        self.last_try = 0.0
        self.lost = ""

    def _remember(self) -> None:
        if self.held is None:
            return
        self.rs.ensure()
        jsonio.atomic_write_json(state_path(self.rs), {"plan": self.lock.plan, "remote": self.lock.doc.remote, "token": self.held.token,
                                                       "oid": self.held.oid, "owner": self.owner, "at": common.iso()}, 0o600)

    def start(self) -> Held:
        token = None
        try:
            prior = jsonio.read_json_opt(state_path(self.rs))
        except common.READ_ERRORS:
            prior = None
        if isinstance(prior, dict) and prior.get("plan") == self.lock.plan and prior.get("remote") == self.lock.doc.remote and isinstance(prior.get("token"), str):
            token = prior["token"]
        self.held = self.lock.acquire(self.owner, token=token, run_id=self.run_id)
        self.last_ok = self.last_try = self.lock.clock()
        self._remember()
        return self.held

    def beat(self, force: bool = False) -> str:
        """ok | lost | unreachable | idle (nothing was due)."""
        if self.held is None:
            return "lost"
        now = self.lock.clock()
        if not force and now - self.last_try < self.interval_s:
            return "idle"
        self.last_try = now
        try:
            ok = self.lock.renew(self.held)
        except LoopctlError:                          # unreachable, or the remote now refuses the push: either way we cannot prove we still hold it
            return "unreachable"
        if not ok:
            self.lost = "the remote lock for %s is no longer ours (another machine took it over, or it was removed)" % self.lock.plan
            return "lost"
        self.last_ok = now
        self._remember()
        return "ok"

    def stop_reason(self) -> Optional[Tuple[str, str, int]]:
        """The driver's stop hook: (code, reason, exit) when this run must stop, else None."""
        r = self.beat()
        if r == "lost":
            return ("remote_lock_lost", self.lost or "this run no longer holds the remote lock", common.EXIT_REFUSED)
        if r == "unreachable" and self.lock.clock() - self.last_ok > self.lock.ttl_s / 2.0:
            return ("remote_lock_unreachable", "the remote has not answered a heartbeat for %d minutes; another machine may take the lock at %d minutes, so this run stops" % (
                int((self.lock.clock() - self.last_ok) // 60), int(self.lock.ttl_s // 60)), common.EXIT_REFUSED)
        return None

    def release(self) -> bool:
        ok = False
        if self.held is not None:
            try:
                ok = self.lock.release(self.held)
            except LoopctlError:
                ok = False
        try:
            os.unlink(state_path(self.rs))
        except OSError:
            pass
        self.held = None
        return ok

    @classmethod
    def for_context(cls, ctx: Any, owner: Optional[str] = None, run_id: Optional[str] = None, interval_s: Optional[float] = None) -> "Keeper":
        remote = ctx.cfg.get("remote_lock")
        lock = RemoteLock(ctx.repo, remote if isinstance(remote, str) else "", ctx.plan_name(), ttl_s=float(ctx.cfg.get_int("lock_stale_min") or 60) * 60.0)
        return cls(lock, ctx.rs, owner or identity(ctx.env), interval_s, run_id)


# -- wiring into a run ---------------------------------------------------------------------------------------------------------------------------

class ClaimsFilter(object):
    """Removes the items somebody else has claimed from what the driver may pick (F-25). The remote is asked at most once per `every_s`; an answer that
    cannot be had falls back to the last one (for up to 10 minutes) and then to 'nothing is eligible', never to 'everything is'."""

    def __init__(self, claims: Any, me: str, every_s: float = 30.0, say: Optional[Callable[[str], None]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.claims, self.me, self.every_s, self.say = claims, me, every_s, say or (lambda _m: None)
        self.clock = clock or time.time
        self.cache = None  # type: Optional[Dict[str, Dict[str, Any]]]
        self.at = 0.0
        self.noted = set()  # type: set

    def held(self) -> Optional[Dict[str, Dict[str, Any]]]:
        now = self.clock()
        if self.cache is not None and now - self.at < self.every_s:
            return self.cache
        try:
            self.cache = self.claims.held_by_others(self.me)
            self.at = now
        except LoopctlError as exc:
            self.say("claims could not be read (%s)" % common.clean_text(exc, 120))
            if self.cache is None or now - self.at > 600.0:
                return None
        return self.cache

    def __call__(self, items: List[str]) -> List[str]:
        held = self.held()
        if held is None:
            return []
        out = [i for i in items if i not in held]
        for i in items:
            if i in held and i not in self.noted:
                self.noted.add(i)
                c = held[i].get("claim") or {}
                self.say("item %s is claimed by %s and is skipped (`loopctl claim list`)" % (i, common.clean_text(c.get("by") or "an unreadable claim", 40)))
        return out


def rows(ctx: Any) -> List[Dict[str, Any]]:
    """The preflight rows for `remote_lock` (run, run --dry-run). Reads the remote; writes nothing. A run that cannot take the lock does not start."""
    from . import claims as claims_mod
    out = []  # type: List[Dict[str, Any]]
    remote = ctx.cfg.get("remote_lock")
    try:
        lock = RemoteLock(ctx.repo, remote if isinstance(remote, str) else "", ctx.plan_name(), ttl_s=float(ctx.cfg.get_int("lock_stale_min") or 60) * 60.0)
        st = lock.status()
    except LoopctlError as exc:
        return [{"id": "remote-lock", "ok": False, "message": "the remote lock cannot be read: %s" % common.clean_text(exc, 160)}]
    mine = False
    if st["state"] == "ok":
        try:
            prior = jsonio.read_json_opt(state_path(ctx.rs))
            obs = lock.doc.observe()
            mine = isinstance(prior, dict) and bool(obs.doc) and prior.get("token") == obs.doc.get("token")
        except (LoopctlError, ValueError, OSError):
            mine = False
    if st["state"] == "none":
        out.append({"id": "remote-lock", "ok": True, "message": "the remote lock on %s is free" % remote})
    elif mine:
        out.append({"id": "remote-lock", "ok": True, "message": "this machine already holds the remote lock on %s" % remote})
    elif st["state"] == "corrupt":
        out.append({"id": "remote-lock", "ok": False, "message": "%s on %s is not a loopctl lock (%s): it counts as held; `loopctl remote-lock steal --corrupt` after you have looked" % (
            st["ref"], remote, common.clean_text(st["why"], 120))})
    else:
        rec = st["record"] or {}
        out.append({"id": "remote-lock", "ok": False, "message": "the remote lock is held by %s on %s (heartbeat %ss ago)%s" % (
            common.clean_text(rec.get("owner"), 40), common.clean_text(rec.get("host"), 40), st["age_s"], "; it looks stale: `loopctl remote-lock steal`" if st["stale"] else "")})
    try:
        held = claims_mod.Claims(ctx.repo, remote, ctx.plan_name(), float(ctx.cfg.get_int("claim_ttl_h") or 72)).held_by_others(identity(ctx.env))
        out.append({"id": "claims", "ok": True, "message": ("%d item(s) are claimed by someone else and will be skipped: %s" % (len(held), ", ".join(sorted(held)[:6]))) if held
                    else "no item is claimed by anyone else"})
    except LoopctlError as exc:
        out.append({"id": "claims", "ok": False, "message": "claims cannot be read: %s" % common.clean_text(exc, 140)})
    return out


def attach(drv: Any, keeper: Keeper) -> None:
    """Give the driver the stop hook (a lost lock ends the run) and the claims filter. Called from `Driver.__init__` only when a keeper exists."""
    from . import claims as claims_mod
    drv.stop_hooks.append(keeper.stop_reason)
    cl = claims_mod.Claims(drv.ctx.repo, keeper.lock.doc.remote, keeper.lock.plan, float(drv.cfg.get_int("claim_ttl_h") or 72))
    drv.item_filters.append(ClaimsFilter(cl, keeper.owner, say=drv.say))
