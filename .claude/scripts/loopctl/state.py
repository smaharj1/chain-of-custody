"""Item state and its store (report section 10.3, 10.4; CS-13, CS-19, CS-22, CS-25, RC-13, RC-15, RC-28).

WHERE STATE LIVES. Item status is never in a tracked file (main moving would conflict). It is on the ref
`refs/loopctl/state/<plan>`, at every level. The ref points at a commit whose tree holds exactly one file, `state.json`;
every write adds a commit on top of the previous one, so `git log refs/loopctl/state/<plan>` is the audit trail. The ref
is moved by compare-and-swap run by loopctl's own git subprocess (outside the guards, RC-13):

    L1, nothing pushed   git update-ref <ref> <new> <old>
    L2+ (`state_remote`)  git push --force-with-lease=<ref>:<old> <remote> <new>:<ref>, then the local CAS

A bare `--force-with-lease` is never used (`kitlib.gitutil.push_cas` builds the explicit form). Two writers cannot both
win: the loser re-reads, re-applies its mutation to the fresh state and, if the mutation no longer makes sense there
(the item moved on), fails instead of overwriting.

THE STATE MACHINE (closed enum, shared by the spec, orchestrate and loopctl):

    pending -> designing -> design_review -> stubbing -> building -> verifying -> reviewing
            -> pr_open -> awaiting_human -> merged | needs_human | blocked | abandoned

`advance` only moves forward along the table below. A step back (verify failed, review blockers, a CI fix) goes through
`fail`, which counts it against the right cap. `blockReason` is required on `blocked` and `needs_human`.

A FORGED REF. A state ref that does not hold a loopctl state commit (wrong tree, wrong plan, unparsable or out-of-schema
JSON, a sequence number that does not follow its parent) is refused with an error: it is never read as "no state" and
never repaired silently. A well-formed forgery cannot be told from a real record locally; `reconcile` checks every
`merged` item against git (the merge commit must exist on the integration branch), and promotion uses server-side facts.

PR AND CHECK OVERLAY (L3+, RC-15): `item.overlay` holds the PR state `reconcile` read from GitHub. It is authoritative for
in_review, merged and ci_failed; pre-PR stages live only here.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from kitlib import gitutil, runstate

from . import branches, common, failure, plan as plan_mod
from .common import LoopctlError, Refused, UsageError

__all__ = ["STATUSES", "IN_FLIGHT", "ACTIVE", "TERMINAL", "RESTING", "TRANSITIONS", "FORWARD", "State", "StateStore",
           "StateCorrupt", "Loop", "new_item_record", "ref_for", "validate_state_doc", "STATE_SCHEMA", "BLOCK_REASONS"]

STATE_SCHEMA = "loopctl.state.v1"
STATUSES = ("pending", "designing", "design_review", "stubbing", "building", "verifying", "reviewing",
            "pr_open", "awaiting_human", "merged", "needs_human", "blocked", "abandoned")
ACTIVE = ("designing", "design_review", "stubbing", "building", "verifying", "reviewing")     # an engineer or critic is working
IN_FLIGHT = ACTIVE + ("pr_open", "awaiting_human")
TERMINAL = ("merged", "abandoned")
RESTING = ("blocked", "needs_human")

# the forward edges `advance` may take
FORWARD = {
    "designing": ("design_review",),
    "design_review": ("stubbing", "building"),
    "stubbing": ("building",),
    "building": ("verifying",),
    "verifying": ("reviewing",),
    "reviewing": ("pr_open", "awaiting_human", "merged"),
    "pr_open": ("awaiting_human", "merged"),
    "awaiting_human": ("merged",),
}
# every edge that exists at all (forward, retries through `fail`, parks, resets, reject cascade)
TRANSITIONS = {
    "pending": ("designing", "blocked", "needs_human", "abandoned"),
    "designing": ("design_review", "designing", "blocked", "needs_human", "abandoned", "pending"),
    "design_review": ("designing", "stubbing", "building", "blocked", "needs_human", "abandoned", "pending"),
    "stubbing": ("building", "blocked", "needs_human", "abandoned", "pending"),
    "building": ("verifying", "building", "blocked", "needs_human", "abandoned", "pending"),
    "verifying": ("reviewing", "building", "blocked", "needs_human", "abandoned", "pending"),
    "reviewing": ("building", "pr_open", "awaiting_human", "merged", "blocked", "needs_human", "abandoned", "pending"),
    "pr_open": ("awaiting_human", "building", "merged", "blocked", "needs_human", "abandoned", "pending"),
    "awaiting_human": ("merged", "building", "pending", "blocked", "needs_human", "abandoned"),
    "merged": ("needs_human", "pending"),
    "blocked": ("pending", "needs_human", "abandoned"),
    "needs_human": ("pending", "blocked", "abandoned"),
    "abandoned": (),
}
BLOCK_REASONS = ("ESCALATION_NEEDED", "NEEDS_CLARIFICATION", "contract_blocked", "needs_provisioning", "security_blocked",
                 "migration_failed", "budget_halted", "design_cap", "ci_fix_cap", "retries_exhausted", "platform_outage",
                 "cost_unknown", "budget_run", "budget_item", "budget_day", "human_revert", "ac_changed", "plan_changed",
                 "aborted", "stall", "merge_missing", "pr_closed", "breaker_tripped")
MAX_HISTORY = 40
MAX_ITEMS = 400
MAX_APPROVALS = 2000
MAX_STATE_BYTES = 4 * 1024 * 1024

_IDENT = {"GIT_AUTHOR_NAME": "loopctl", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}


class StateCorrupt(LoopctlError):
    """The state ref is not a valid loopctl state. Fail closed: nothing proceeds on it."""
    exit_code = common.EXIT_USAGE


def ref_for(plan_name: str) -> str:
    if not common.valid_plan_name(plan_name):
        raise UsageError("invalid plan name %r" % (plan_name,))
    return "refs/loopctl/state/" + plan_name


def new_item_record() -> Dict[str, Any]:
    counters = {c: 0 for c in failure.COUNTERS}
    counters["platform"] = 0
    counters["discards"] = 0
    return {"status": "pending", "blockReason": None, "attempt": 1, "branch": None, "mergeCommit": None, "prUrl": None,
            "headSha": None, "counters": counters, "lastFailure": None, "notBefore": None, "gates": {}, "ac": {},
            "review": {}, "rework": [], "overlay": None, "history": [], "approvedAcDigest": None, "humanGate": [],
            "updated": None, "deadBranches": []}


# -- the document ----------------------------------------------------------------------------------------------

class State(object):
    """A thin wrapper over the state document (`d`)."""

    def __init__(self, d: Dict[str, Any]) -> None:
        self.d = d

    @classmethod
    def new(cls, plan_name: str) -> "State":
        return cls({"schema": STATE_SCHEMA, "plan": plan_name, "seq": 0, "updated": common.iso(),
                    "header": {"approvedPlan": None, "breaker": 0, "levelCap": None, "runStatus": "idle", "parked": None,
                               "abort": None, "runId": None, "bypass": []},
                    "items": {}, "approvals": []})

    @property
    def items(self) -> Dict[str, Dict[str, Any]]:
        return self.d["items"]

    @property
    def header(self) -> Dict[str, Any]:
        return self.d["header"]

    @property
    def seq(self) -> int:
        return self.d["seq"]

    def item(self, iid: str) -> Dict[str, Any]:
        if not common.valid_item_id(iid) or iid not in self.items:
            raise UsageError("no item %r in the state (is the plan approved? `loopctl approve plan`)" % common.clean_text(iid, 30))
        return self.items[iid]

    def status_of(self, iid: str) -> str:
        return self.item(iid)["status"]

    def by_status(self, *statuses: str) -> List[str]:
        return sorted((i for i, r in self.items.items() if r["status"] in statuses), key=common.natural_key)

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for r in self.items.values():
            out[r["status"]] += 1
        return out

    def log(self, iid: str, frm: str, to: str, by: str, note: str = "") -> None:
        r = self.items[iid]
        r["history"].append({"seq": self.seq + 1, "at": common.iso(), "from": frm, "to": to, "by": common.clean_text(by, 60),
                             "note": common.clean_text(note, 160)})
        del r["history"][:-MAX_HISTORY]
        r["updated"] = common.iso()

    def set_status(self, iid: str, to: str, by: str, note: str = "", block_reason: Optional[str] = None) -> None:
        r = self.item(iid)
        frm = r["status"]
        if to not in TRANSITIONS.get(frm, ()) and not (to == frm and to in ACTIVE):
            raise Refused("item %s: %s -> %s is not a legal transition" % (iid, frm, to))
        if to in RESTING:
            if not block_reason:
                raise Refused("item %s: blockReason is required on %s" % (iid, to))
            r["blockReason"] = common.clean_text(block_reason, 120)
        else:
            r["blockReason"] = None
        r["status"] = to
        self.log(iid, frm, to, by, note)

    def eligible(self, order: Sequence[str], deps: Dict[str, List[str]]) -> List[str]:
        """Pending items whose dependencies are all merged, in `order` (the plan's deterministic topological order)."""
        out = []
        for i in order:
            r = self.items.get(i)
            if r and r["status"] == "pending" and all(self.items.get(d, {}).get("status") == "merged" for d in deps.get(i, [])):
                out.append(i)
        return out


_STATUS_SET = set(STATUSES)
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?\Z")


def _is_int(v: Any, lo: int = 0, hi: int = 10 ** 9) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate_state_doc(d: Any, plan_name: str) -> Dict[str, Any]:
    """Strict structural validation of a state document. Raises StateCorrupt."""
    def bad(msg: str) -> "StateCorrupt":
        return StateCorrupt("state ref for %s is not a valid loopctl state: %s" % (plan_name, msg))
    if not isinstance(d, dict):
        raise bad("not a JSON object")
    if d.get("schema") != STATE_SCHEMA:
        raise bad("schema is not %s" % STATE_SCHEMA)
    if d.get("plan") != plan_name:
        raise bad("it belongs to plan %r" % common.clean_text(d.get("plan"), 40))
    if not _is_int(d.get("seq")):
        raise bad("seq is not a whole number")
    if set(d) - {"schema", "plan", "seq", "updated", "header", "items", "approvals"}:
        raise bad("unknown top-level field")
    h = d.get("header")
    if not isinstance(h, dict) or not isinstance(h.get("breaker"), int) or isinstance(h.get("breaker"), bool):
        raise bad("header is malformed")
    if not isinstance(h.get("bypass", []), list) or len(h.get("bypass", [])) > 1000:
        raise bad("header.bypass is malformed")
    ap = h.get("approvedPlan")
    if ap is not None and (not isinstance(ap, dict) or not re.match(r"^[0-9a-f]{64}\Z", str(ap.get("digest", "")))
                           or not isinstance(ap.get("acDigests"), dict)):
        raise bad("header.approvedPlan is malformed")
    items = d.get("items")
    if not isinstance(items, dict) or len(items) > MAX_ITEMS:
        raise bad("items is malformed")
    for iid, r in items.items():
        if not common.valid_item_id(iid) or not isinstance(r, dict):
            raise bad("item id %r is invalid" % common.clean_text(iid, 30))
        st = r.get("status")
        if st not in _STATUS_SET:
            raise bad("item %s has status %r outside the closed enum" % (iid, common.clean_text(st, 30)))
        if st in RESTING and not (isinstance(r.get("blockReason"), str) and r["blockReason"].strip()):
            raise bad("item %s is %s without a blockReason" % (iid, st))
        if not _is_int(r.get("attempt"), 1, 10000):
            raise bad("item %s has a bad attempt number" % iid)
        c = r.get("counters")
        if not isinstance(c, dict) or not all(isinstance(k, str) and _is_int(v, 0, 10 ** 6) for k, v in c.items()):
            raise bad("item %s counters are malformed" % iid)
        mc = r.get("mergeCommit")
        if mc is not None and not (isinstance(mc, str) and _SHA_RE.match(mc)):
            raise bad("item %s mergeCommit is not a sha" % iid)
        if st == "merged" and not mc:
            raise bad("item %s is merged without a mergeCommit" % iid)
        for k in ("history", "rework", "deadBranches", "humanGate"):
            if not isinstance(r.get(k), list):
                raise bad("item %s %s is not a list" % (iid, k))
        if len(r["history"]) > MAX_HISTORY * 2:
            raise bad("item %s history is too long" % iid)
        for k in ("gates", "ac", "review"):
            if not isinstance(r.get(k), dict):
                raise bad("item %s %s is not an object" % (iid, k))
        br = r.get("branch")
        if br is not None and not (isinstance(br, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}\Z", br) and ".." not in br):
            raise bad("item %s branch is not a branch name" % iid)
    aps = d.get("approvals")
    if not isinstance(aps, list) or len(aps) > MAX_APPROVALS or not all(isinstance(a, dict) for a in aps):
        raise bad("approvals is malformed")
    return d


# -- the store --------------------------------------------------------------------------------------------------------

class StateStore(object):
    """`refs/loopctl/state/<plan>` read and written by compare-and-swap."""

    def __init__(self, repo: str, plan_name: str, remote: str = "") -> None:
        self.repo = repo
        self.plan = plan_name
        self.ref = ref_for(plan_name)
        self.remote = remote or ""
        self.max_tries = 8
        self._before_cas = None  # type: Optional[Callable[[], None]]   # test hook: race injection

    # -- git plumbing ----------------------------------------------------------------------------------------
    def _git(self, args: Sequence[str], input_text: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        return gitutil.run_git(args, cwd=self.repo, isolated=True, pin_hooks=True, input_text=input_text, env=env)

    def head(self, ref: Optional[str] = None) -> Optional[str]:
        proc = self._git(["rev-parse", "--verify", "-q", (ref or self.ref) + "^{commit}"])
        out = proc.stdout.strip() if proc.returncode == 0 else ""
        return out if gitutil.valid_oid(out) else None

    def _raw_ref_exists(self) -> bool:
        return self._git(["show-ref", "--verify", "-q", self.ref]).returncode == 0

    def _read_commit(self, sha: str) -> State:
        t = self._git(["cat-file", "-t", sha])
        if t.returncode != 0 or t.stdout.strip() != "commit":
            raise StateCorrupt("state ref %s does not point at a commit" % self.ref)
        tree = self._git(["ls-tree", "-z", sha])
        entries = [e for e in tree.stdout.split("\0") if e]
        if tree.returncode != 0 or len(entries) != 1 or not entries[0].endswith("\tstate.json") or not entries[0].startswith("100644 blob "):
            raise StateCorrupt("state ref %s is not a loopctl state commit (its tree is not exactly state.json)" % self.ref)
        blob = entries[0].split()[2]
        size = self._git(["cat-file", "-s", blob])
        if size.returncode != 0 or not size.stdout.strip().isdigit() or int(size.stdout.strip()) > MAX_STATE_BYTES:
            raise StateCorrupt("state.json is missing or larger than %d bytes" % MAX_STATE_BYTES)
        body = self._git(["cat-file", "blob", blob])
        try:
            d = common.strict_loads(body.stdout, max_depth=14, max_bytes=MAX_STATE_BYTES)
        except (ValueError, RecursionError) as exc:
            raise StateCorrupt("state.json is not usable JSON: %s" % common.clean_text(exc, 120))
        validate_state_doc(d, self.plan)
        st = State(d)
        # the sequence number must follow the parent's
        parents = self._git(["rev-list", "--parents", "-n", "1", sha]).stdout.split()
        if len(parents) > 2:
            raise StateCorrupt("state commit has more than one parent")
        if len(parents) == 2:
            pstate = self._parent_seq(parents[1])
            if pstate is not None and d["seq"] != pstate + 1:
                raise StateCorrupt("state sequence %d does not follow its parent's %d (a forged or rewritten ref)" % (d["seq"], pstate))
        elif d["seq"] != 1 and d["seq"] != 0:
            raise StateCorrupt("the first state commit has sequence %d" % d["seq"])
        return st

    def _parent_seq(self, sha: str) -> Optional[int]:
        body = self._git(["show", "%s:state.json" % sha])
        if body.returncode != 0:
            return None
        try:
            d = json.loads(body.stdout)
        except ValueError:
            return None
        return d.get("seq") if isinstance(d, dict) and _is_int(d.get("seq")) else None

    def read(self) -> Tuple[Optional[State], Optional[str]]:
        """(state, commit). (None, None) only when the ref does not exist. Raises StateCorrupt otherwise."""
        sha = self.head()
        if sha is None:
            if self._raw_ref_exists():
                raise StateCorrupt("state ref %s exists but does not resolve to a commit" % self.ref)
            return None, None
        return self._read_commit(sha), sha

    def verify_chain(self, limit: int = 500) -> List[str]:
        """Walk the history: every commit must be a valid state with sequence parent+1. Returns problems."""
        problems = []  # type: List[str]
        sha = self.head()
        n = 0
        while sha and n < limit:
            try:
                self._read_commit(sha)
            except StateCorrupt as exc:
                problems.append("%s: %s" % (sha[:12], exc))
                break
            parents = self._git(["rev-list", "--parents", "-n", "1", sha]).stdout.split()
            sha = parents[1] if len(parents) == 2 else None
            n += 1
        return problems

    # -- remote -----------------------------------------------------------------------------------------------
    def _check_remote(self) -> None:
        r = self.remote
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,50}\Z", r):
            raise Refused("state_remote %r is not a remote name (a URL is never accepted)" % common.clean_text(r, 40))
        names = self._git(["remote"]).stdout.split()
        if r not in names:
            raise Refused("state_remote %r is not a configured remote of this repository" % r)

    def _sync_remote(self, local: Optional[str]) -> Optional[str]:
        """Return the remote's value for the ref after making sure local and remote agree (or local is ahead)."""
        self._check_remote()
        remote = gitutil.ls_remote_ref(self.remote, self.ref, self.repo, isolated=True)
        if remote is None or remote == local:
            return remote
        if local is not None and self._is_ancestor(remote, local):
            return remote                       # remote is behind: our push will fast-forward it
        # remote is ahead or diverged: fetch it; fast-forward the local ref only if local is its ancestor
        f = self._git(["fetch", "--no-tags", "--quiet", "--", self.remote, self.ref])
        if f.returncode != 0:
            raise Refused("cannot fetch %s from %s: %s" % (self.ref, self.remote, common.clean_text(f.stderr, 120)))
        if local is None or self._is_ancestor(local, remote):
            self._read_commit(remote)           # must be a valid state before we adopt it
            res = gitutil.update_ref_cas(self.ref, remote, local, self.repo, isolated=True)
            if not res:
                raise Refused("state moved while syncing from the remote; retry")
            return remote
        raise Refused("the remote state for %s diverged from the local one; reconcile by hand (compare %s with %s)"
                      % (self.plan, local[:12], remote[:12]))

    def _is_ancestor(self, a: str, b: str) -> bool:
        return self._git(["merge-base", "--is-ancestor", a, b]).returncode == 0

    # -- writing ---------------------------------------------------------------------------------------------------
    def _write_commit(self, doc: Dict[str, Any], parent: Optional[str], message: str) -> str:
        text = json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n"
        if len(text) > MAX_STATE_BYTES:
            raise Refused("the state document would exceed %d bytes" % MAX_STATE_BYTES)
        blob = self._git(["hash-object", "-w", "--stdin"], input_text=text)
        if blob.returncode != 0:
            raise LoopctlError("git hash-object failed: %s" % common.clean_text(blob.stderr, 120))
        tree = self._git(["mktree"], input_text="100644 blob %s\tstate.json\n" % blob.stdout.strip())
        if tree.returncode != 0:
            raise LoopctlError("git mktree failed: %s" % common.clean_text(tree.stderr, 120))
        args = ["commit-tree", tree.stdout.strip()]
        if parent:
            args += ["-p", parent]
        args += ["-m", common.clean_text(message, 200) or "loopctl state"]
        c = self._git(args, env=dict(_IDENT))
        if c.returncode != 0 or not gitutil.valid_oid(c.stdout.strip()):
            raise LoopctlError("git commit-tree failed: %s" % common.clean_text(c.stderr, 120))
        return c.stdout.strip()

    def update(self, mutate: Callable[[State], Any], message: str, create: Optional[Callable[[], State]] = None) -> Tuple[State, Any]:
        """Apply `mutate` to the current state and publish it by compare-and-swap. Retries on a lost race, re-applying
        `mutate` to the fresh state. `create` builds the first state when the ref does not exist (otherwise a missing
        ref is an error). Returns (new state, mutate's return value)."""
        last = ""
        for attempt in range(self.max_tries):
            st, head = self.read()
            remote_old = None  # type: Optional[str]
            if self.remote:
                remote_old = self._sync_remote(head)
                if remote_old is not None and remote_old != head:
                    st, head = self.read()
            if st is None:
                if create is None:
                    raise Refused("there is no state for plan %s yet: `loopctl approve plan` creates it" % self.plan)
                st = create()
            work = State(copy.deepcopy(st.d))
            result = mutate(work)
            work.d["seq"] = st.d["seq"] + 1
            work.d["updated"] = common.iso()
            validate_state_doc(work.d, self.plan)
            new = self._write_commit(work.d, head, "%s [seq %d]" % (message, work.d["seq"]))
            if self._before_cas is not None:
                self._before_cas()
            if self.remote:
                res = gitutil.push_cas(self.remote, self.ref, new, remote_old, self.repo, isolated=True)
                if not res:
                    last = "push lost: " + common.clean_text(res.stderr, 100)
                    time.sleep(0.01 * (attempt + 1))
                    continue
            res = gitutil.update_ref_cas(self.ref, new, head, self.repo, isolated=True)
            if res:
                return work, result
            last = "update-ref lost: " + common.clean_text(res.stderr, 100)
            time.sleep(0.01 * (attempt + 1))
        raise Refused("the state moved under this command %d times in a row (%s); nothing was changed, try again" % (self.max_tries, last))


# -- the loop facade ----------------------------------------------------------------------------------------------------------

class Loop(object):
    """All state transitions. Built from a `context.Context`; the driver and the CLI call these methods.

    Every method is a single compare-and-swap update. Preconditions are checked inside the update, against the state
    that is about to be replaced.
    """

    def __init__(self, ctx: Any, require_tier2: bool = True) -> None:
        self.ctx = ctx
        if require_tier2 and not ctx.tier2:
            raise Refused("loopctl state writes (Tier 2) are off: set loopctl_tier2: true in .claude/loop.config.md (H6, a human edits it)")
        self.store = ctx.store()
        self.plan = ctx.plan
        self.cfg = ctx.cfg

    # -- helpers ------------------------------------------------------------------------------------------------
    def _order(self) -> List[str]:
        return plan_mod.topo_order(self.plan.items, [m for m in (self.plan.plan or {}).get("milestones", []) if isinstance(m, str)])

    def _deps(self) -> Dict[str, List[str]]:
        return {i: [d for d in it.get("dependsOn", []) if isinstance(d, str)] for i, it in self.plan.items.items()}

    def _need_plan_item(self, iid: str) -> Dict[str, Any]:
        if iid not in self.plan.items:
            raise UsageError("item %r is not in the plan" % common.clean_text(iid, 30))
        return self.plan.items[iid]

    def _check_lock(self, session_id: Optional[str], need: bool) -> None:
        rs = self.ctx.rs
        if not rs.lock_held():
            if need:
                raise Refused("the run lock is not held: `loopctl run` (or `lock acquire`) first. Nothing is written before the lock is held (L-9)")
            return
        try:
            rec = rs.read_lock()
        except (ValueError, OSError):
            raise Refused("the run lock is unreadable and counts as held; inspect it with `loopctl lock status`")
        if rec is None:
            return
        if session_id is None or rec.get("sessionId") != session_id:
            raise Refused("the run lock belongs to session %s; this command was given %s"
                          % (common.clean_text(rec.get("sessionId"), 40), common.clean_text(session_id, 40) if session_id else "no --session"))
        rs.heartbeat(session_id)

    def _halted(self) -> None:
        if self.ctx.rs.is_halted():
            h = self.ctx.rs.read_halt() or {}
            raise Refused("HALT is set (%s): nothing moves forward. A human clears it with `loopctl resume`"
                          % common.clean_text(h.get("reason", "no reason recorded"), 80))

    def _event(self, event: str, **kw: Any) -> None:
        try:
            self.ctx.rs.append_event("loopctl", dict(kw, event=event))
        except (OSError, ValueError):
            pass

    # -- plan approval (H1, H2) ---------------------------------------------------------------------------------------
    def approve_plan(self, by: str, note: str = "", seed: Optional[Dict[str, Dict[str, Any]]] = None) -> State:
        """Record the human's approval of the plan with its frozen ACs and create or sync the state. Refuses an invalid plan."""
        rep = plan_mod.validate(self.plan, max_item_loc=self.cfg.get_int("max_item_loc"), roster=self.ctx.roster(),
                                always_human_paths=self.cfg.get_list("always_human_paths"))
        if not rep.ok:
            raise Refused("the plan does not validate (%d error(s)); fix it before approving: %s"
                          % (len(rep.errors), "; ".join(f.message for f in rep.errors[:2])))
        name = self.plan.name
        if name != self.store.plan:
            raise UsageError("plan name mismatch")
        by = common.clean_text(by, 60)
        if not by.strip():
            raise UsageError("--by <name> is required: an approval is a human's, and it is recorded")
        digests = rep.ac_digests
        pdigest = rep.digest
        gates_by_item = {i: plan_mod.item_human_gate(self.plan.items[i], self.cfg.get_list("always_human_paths")) for i in self.plan.items}

        def create() -> State:
            return State.new(name)

        def mutate(st: State) -> None:
            prev = st.header.get("approvedPlan")
            prev_ac = (prev or {}).get("acDigests", {}) if isinstance(prev, dict) else {}
            for iid in self.plan.ids():
                if iid not in st.items:
                    st.items[iid] = new_item_record()
                    st.items[iid]["updated"] = common.iso()
                    if seed and iid in seed:
                        self._apply_seed(st, iid, seed[iid], by)
                r = st.items[iid]
                r["humanGate"] = gates_by_item.get(iid, [])
                old = prev_ac.get(iid)
                if old is not None and old != digests[iid] and r["status"] not in ("pending", "abandoned"):
                    # work already exists against criteria that changed: a human decides (CO-5)
                    if r["status"] != "needs_human":
                        _force_status(st, iid, "needs_human", by, "acceptance criteria changed after approval", "ac_changed")
                r["approvedAcDigest"] = digests[iid]
            for iid in list(st.items):
                if iid not in self.plan.items:
                    r = st.items[iid]
                    if r["status"] == "pending":
                        _force_status(st, iid, "abandoned", by, "removed from the plan")
                    elif r["status"] not in TERMINAL and r["status"] != "needs_human":
                        _force_status(st, iid, "needs_human", by, "removed from the plan while in progress", "plan_changed")
            st.header["approvedPlan"] = {"digest": pdigest, "acDigests": digests, "by": by, "at": common.iso(), "note": common.clean_text(note, 200)}
            _add_approval(st, "plan", name, by, "approve", note, plan_digest=pdigest)

        st, _ = self.store.update(mutate, "approve plan %s by %s" % (name, by), create=create)
        self._event("plan_approved", plan=name, by=by, digest=pdigest[:12])
        return st

    def _apply_seed(self, st: State, iid: str, sd: Dict[str, Any], by: str) -> None:
        """A v1 status seed (migrate-plan): done -> merged only if the merge commit exists; never trusted blindly."""
        r = st.items[iid]
        want = sd.get("status")
        if want == "merged":
            mc = sd.get("mergeCommit")
            if isinstance(mc, str) and gitutil.valid_oid(mc) and gitutil.rev_parse(mc + "^{commit}", self.ctx.repo, isolated=True) == mc:
                r["mergeCommit"] = mc
                _force_status(st, iid, "merged", by, "seeded from v1 (done)")
            else:
                _force_status(st, iid, "needs_human", by, "v1 says done but the merge commit is not in this repository", "merge_missing")
        elif want in RESTING:
            _force_status(st, iid, want, by, "seeded from v1", sd.get("blockReason") or "ESCALATION_NEEDED")
        if sd.get("v1Evidence"):
            r["review"]["v1Evidence"] = common.clean_text(sd["v1Evidence"], 200)

    def _check_approved(self, st: State, iid: str) -> None:
        ap = st.header.get("approvedPlan")
        if not ap:
            raise Refused("the plan is not approved (H1): `loopctl approve plan --by <you>`")
        cur = plan_mod.plan_digest(self.plan)
        if ap["digest"] != cur:
            raise Refused("the plan changed since it was approved (approved %s, now %s): a revised plan needs `loopctl approve plan` (H2)"
                          % (ap["digest"][:12], cur[:12]))
        if ap["acDigests"].get(iid) != plan_mod.ac_digest(self.plan.items[iid]):
            raise Refused("item %s: its acceptance criteria differ from the approved ones (ACs are frozen at H1)" % iid)

    # -- begin ------------------------------------------------------------------------------------------------------------
    def begin(self, iid: str, session_id: str, by: str = "conductor", branch: Optional[str] = None) -> Dict[str, Any]:
        item = self._need_plan_item(iid)
        self._halted()
        self._check_lock(session_id, need=True)
        deps = self._deps()

        def mutate(st: State) -> Dict[str, Any]:
            if st.header.get("runStatus") in ("stopped",):
                raise Refused("the run is stopped (%s); a human clears it with `loopctl approve breaker`" % (st.header.get("parked") or {}).get("reason"))
            if st.header.get("levelCap"):
                raise Refused("the effective level is capped after an environment failure (%s): fix it, run `loopctl doctor`, then "
                              "`loopctl approve env --by <you>`" % st.header["levelCap"].get("reason"))
            r = st.item(iid)
            if r["status"] != "pending":
                raise Refused("item %s is %s, not pending" % (iid, r["status"]))
            self._check_approved(st, iid)
            busy = [i for i in st.by_status(*ACTIVE) if i != iid]
            if busy:
                raise Refused("item %s is still in progress; the loop runs one item at a time" % busy[0])
            for d in deps.get(iid, []):
                if st.items.get(d, {}).get("status") != "merged":
                    raise Refused("item %s depends on %s, which is %s" % (iid, d, st.items.get(d, {}).get("status", "missing")))
            nb = r.get("notBefore")
            if nb and runstate.parse_iso(nb) and runstate.parse_iso(nb) > common.now():
                raise Refused("item %s is backing off until %s" % (iid, nb))
            slug = item["slug"]
            r["branch"] = branch or "feat/%s-a%d" % (slug, r["attempt"])
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$", r["branch"]) or ".." in r["branch"] or r["branch"].startswith("-"):
                raise UsageError("unsafe branch name")
            r["headSha"] = None
            r["notBefore"] = None
            st.set_status(iid, "designing", by, "begin attempt %d" % r["attempt"])
            st.header["runStatus"] = "running"
            return copy.deepcopy(r)

        st, r = self.store.update(mutate, "begin %s" % iid)
        self._after_begin(st, iid)
        self._event("begin", item=iid, attempt=r["attempt"], branch=r["branch"])
        return r

    def _after_begin(self, st: State, iid: str) -> None:
        from . import locked
        try:
            locked.refresh(self.ctx, st, self.plan, active=iid)
        except OSError:
            pass
        rec = self.ctx.rs.read_run() or {}
        rec["currentItem"] = iid
        self.ctx.rs.write_run(rec)

    # -- advance ------------------------------------------------------------------------------------------------------------
    def advance(self, iid: str, to: str, session_id: Optional[str], by: str = "conductor", note: str = "",
                merge_commit: Optional[str] = None, head: Optional[str] = None, pr_url: Optional[str] = None) -> Dict[str, Any]:
        item = self._need_plan_item(iid)
        if to not in STATUSES:
            raise UsageError("unknown status %r" % common.clean_text(to, 30))
        if to in ("blocked", "needs_human", "abandoned", "pending"):
            raise UsageError("advance moves forward only; use fail/park/abandon/reset for %s" % to)
        self._halted()
        self._check_lock(session_id, need=True)
        if merge_commit is not None and not gitutil.valid_oid(merge_commit):
            raise UsageError("--merge-commit must be a full object id")
        if head is not None and not gitutil.valid_oid(head):
            raise UsageError("--head must be a full object id")
        if pr_url is not None and not re.match(r"^https://[A-Za-z0-9./_-]{1,200}\Z", pr_url):
            raise UsageError("--pr-url must be an https URL")
        merge_checked = False
        if to == "merged":
            self._check_merge_commit(iid, merge_commit)
            merge_checked = True

        def mutate(st: State) -> Dict[str, Any]:
            r = st.item(iid)
            frm = r["status"]
            if to not in FORWARD.get(frm, ()):
                raise Refused("item %s: %s -> %s is not a forward step (allowed from %s: %s)"
                              % (iid, frm, to, frm, ", ".join(FORWARD.get(frm, ())) or "none"))
            if frm == "design_review" and to == "stubbing" and self.cfg.gate("g2") == "off":
                raise Refused("gates.g2 is off: go from design_review to building")
            for g in self.ctx.required_gates_for(frm, to):
                res = r["gates"].get(g)
                if not res or res.get("status") != "pass":
                    raise Refused("item %s: gate %s must pass before %s -> %s (gates.%s is on); run `loopctl gate %s %s`"
                                  % (iid, g, frm, to, g, g, iid))
                if head and res.get("head") and res["head"] != head:
                    raise Refused("item %s: gate %s passed for %s, not for %s: the evidence is stale" % (iid, g, res["head"][:12], head[:12]))
            if to == "merged":
                gate = r.get("humanGate") or plan_mod.item_human_gate(item, self.cfg.get_list("always_human_paths"))
                if frm == "awaiting_human" or gate:
                    ap = _find_approval(st, iid, r["attempt"])
                    if ap is None:
                        raise Refused("item %s needs a human approval before it merges%s: `loopctl approve item %s --by <you>`"
                                      % (iid, " (" + "; ".join(gate[:2]) + ")" if gate else "", iid))
                    ap["used"] = True
                r["mergeCommit"] = merge_commit
                st.header["breaker"] = 0
                if st.header.get("runStatus") == "parked":
                    st.header["runStatus"] = "running"
            if head:
                r["headSha"] = head
            if pr_url:
                r["prUrl"] = pr_url
            st.set_status(iid, to, by, note)
            return copy.deepcopy(r)

        st, r = self.store.update(mutate, "advance %s -> %s" % (iid, to))
        if to == "merged":
            self._after_merge(st, iid)
        self._event("advance", item=iid, to=to)
        return r

    def _check_merge_commit(self, iid: str, sha: Optional[str]) -> None:
        if not sha:
            raise UsageError("advancing to merged needs --merge-commit <sha>")
        repo = self.ctx.repo
        if gitutil.rev_parse(sha + "^{commit}", repo) != sha:
            raise Refused("merge commit %s does not exist in this repository" % sha[:12])
        branch = self.ctx.integration_branch()
        if branch:
            if not gitutil.is_ancestor(sha, "refs/heads/" + branch, repo):
                raise Refused("merge commit %s is not on the integration branch %s" % (sha[:12], branch))
        if self.cfg.get("delivery") == "local":
            msg = gitutil.run_git(["log", "-1", "--format=%B", sha], cwd=repo, isolated=True, pin_hooks=True).stdout
            try:
                import pr_prepare
                tr = pr_prepare.parse_trailers(msg)
            except Exception:  # noqa: BLE001
                tr = {}
            ids = tr.get("loop-item") or tr.get("Loop-Item") or []
            if iid not in ids:
                raise Refused("merge commit %s carries no 'Loop-Item: %s' trailer (required under delivery: local, L-4)" % (sha[:12], iid))

    def _after_merge(self, st: State, iid: str) -> None:
        from . import locked
        try:
            locked.refresh(self.ctx, st, self.plan, active=None)
        except OSError:
            pass

    # -- failure ----------------------------------------------------------------------------------------------------------
    def fail(self, iid: str, fclass: str, reason: str, session_id: Optional[str], by: str = "conductor",
             failed_acs: Sequence[str] = (), stage: Optional[str] = None, detail: str = "") -> failure.Decision:
        self._need_plan_item(iid)
        failure.check_reason(fclass, reason)
        self._check_lock(session_id, need=True)
        for a in failed_acs:
            if not isinstance(a, str) or not common.AC_RE.match(a):
                raise UsageError("failed AC ids must look like AC-<item>-<n>")
        decisions = []  # type: List[failure.Decision]

        def mutate(st: State) -> failure.Decision:
            r = st.item(iid)
            if r["status"] not in IN_FLIGHT:
                raise Refused("item %s is %s: only an item in progress can fail" % (iid, r["status"]))
            d = failure.decide(r, r["status"], self.cfg, fclass, reason, failed_acs, stage, seed="%s:%d" % (iid, r["attempt"]))
            r["counters"].update(d.counters)
            r["lastFailure"] = {"class": fclass, "reason": reason, "failedAcs": sorted(set(failed_acs)), "stage": stage or r["status"],
                                "at": common.iso(), "detail": common.clean_text(detail, 200), "action": d.action}
            frm = r["status"]
            if d.action == "retry":
                if d.backoff_s:
                    r["notBefore"] = common.iso(common.now() + d.backoff_s)
                if d.to_status and d.to_status != frm:
                    st.set_status(iid, d.to_status, by, "retry: %s/%s" % (fclass, reason))
                else:
                    st.log(iid, frm, frm, by, "retry: %s/%s %s" % (fclass, reason, d.note))
            elif d.action == "discard":
                d.branch = r["branch"]
                r["counters"] = _reset_counters(r["counters"], keep=("platform", "discards"))
                r["counters"]["discards"] += 1
                dead = "dead/%s-a%d" % (self.plan.items[iid]["slug"], r["attempt"])
                r["deadBranches"] = (r["deadBranches"] + [dead])[-10:]
                r["attempt"] += 1
                r["branch"] = None
                r["headSha"] = None
                r["gates"] = {}
                r["ac"] = {}
                st.set_status(iid, "pending", by, "discard: %s" % d.note)
            else:  # park or hard_stop
                kind = d.status_kind or "blocked"
                d.branch = r["branch"]
                r["notBefore"] = None
                st.set_status(iid, kind, by, d.note, block_reason=d.block_reason)
                st.header["parked"] = {"item": iid, "reason": d.block_reason, "at": common.iso()}
                st.header["runStatus"] = "parked"
                if d.breaker:
                    st.header["breaker"] += 1
                    if st.header["breaker"] >= self.cfg.get_int("max_consecutive_failures"):
                        st.header["runStatus"] = "stopped"
                        st.header["parked"] = {"item": iid, "reason": "breaker_tripped", "at": common.iso()}
                if d.demote:
                    st.header["levelCap"] = {"level": "L1", "reason": "environment failure: %s" % reason, "at": common.iso(), "item": iid}
            decisions.append(d)
            return d

        st, d = self.store.update(mutate, "fail %s %s/%s" % (iid, fclass, reason))
        self._event("fail", item=iid, failure_class=fclass, reason=reason, action=d.action)
        if d.action in ("discard", "hard_stop", "park"):
            from . import locked
            try:
                locked.refresh(self.ctx, st, self.plan, active=None)
            except OSError:
                pass
            self._git_effects(iid, d)
        return d

    def _git_effects(self, iid: str, d: failure.Decision) -> None:
        """The branch side of a discard or a park: executed by loopctl's own git, only in the registered loop worktree."""
        br = getattr(d, "branch", None)
        d.git = None
        if not br or not branches.parse(br) or not self.ctx.worktree_is_loop_worktree():
            return
        try:
            if d.action == "discard":
                d.git = branches.discard(self.ctx, br, self.ctx.integration_branch())
            else:
                new = branches.park(self.ctx, br)
                d.git = {"branch": br, "blocked": new}
                if new:
                    def mark(st: State) -> None:
                        st.item(iid)["parkedBranch"] = new
                    self.store.update(mark, "parked branch %s" % iid)
        except (Refused, UsageError) as exc:
            d.git = {"error": str(exc)}

    def park(self, iid: str, code: str, detail: str, session_id: Optional[str], by: str = "conductor",
             kind: str = "needs_human", breaker: bool = False) -> Dict[str, Any]:
        """A hard stop that is not a failure of the item: budget reached, unknown cost, ESCALATION_NEEDED, security block..."""
        self._need_plan_item(iid)
        if kind not in RESTING:
            raise UsageError("kind must be blocked or needs_human")
        if not code or len(code) > 60:
            raise UsageError("a park code is required")
        self._check_lock(session_id, need=False)

        def mutate(st: State) -> Dict[str, Any]:
            r = st.item(iid)
            if r["status"] in TERMINAL or r["status"] in RESTING:
                raise Refused("item %s is %s" % (iid, r["status"]))
            st.set_status(iid, kind, by, detail, block_reason=code)
            st.header["parked"] = {"item": iid, "reason": common.clean_text(code, 60), "at": common.iso()}
            st.header["runStatus"] = "parked"
            if breaker:
                st.header["breaker"] += 1
            return copy.deepcopy(r)

        st, r = self.store.update(mutate, "park %s %s" % (iid, code))
        self._event("park", item=iid, code=code)
        return r

    def clarify(self, iid: str, session_id: Optional[str], by: str = "conductor") -> int:
        """An engineer asked for clarification (NEEDS_CLARIFICATION). At most two rounds per attempt, then a human."""
        self._need_plan_item(iid)
        self._check_lock(session_id, need=True)

        def mutate(st: State) -> int:
            r = st.item(iid)
            if r["status"] not in ACTIVE:
                raise Refused("item %s is %s" % (iid, r["status"]))
            r["counters"]["clarify"] = r["counters"].get("clarify", 0) + 1
            n = r["counters"]["clarify"]
            if n > 2:
                st.set_status(iid, "blocked", by, "more than two clarification rounds", block_reason="NEEDS_CLARIFICATION")
                st.header["parked"] = {"item": iid, "reason": "NEEDS_CLARIFICATION", "at": common.iso()}
                st.header["runStatus"] = "parked"
            else:
                st.log(iid, r["status"], r["status"], by, "clarification round %d" % n)
            return n

        _, n = self.store.update(mutate, "clarify %s" % iid)
        return n

    # -- gate and review results ------------------------------------------------------------------------------------------------------
    def record_gate(self, iid: str, name: str, result: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        """Store a gate result on the item and fold its per-AC evidence into the AC x evidence matrix (G1, G8 view)."""
        self._need_plan_item(iid)
        if not re.match(r"^[a-z][a-z0-9_]{0,15}\Z", name):
            raise UsageError("bad gate name")
        self._check_lock(session_id, need=False)
        acs = set(plan_mod.ac_ids(self.plan.items[iid]))

        def mutate(st: State) -> Dict[str, Any]:
            r = st.item(iid)
            ev = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
            slim = {"status": result.get("status"), "mode": result.get("mode"), "blocking": bool(result.get("blocking")),
                    "at": common.iso(), "head": result.get("head"), "failure_class": result.get("failure_class"),
                    "findings": [{"id": common.clean_text(f.get("id"), 40), "severity": f.get("severity"),
                                  "message": common.clean_text(f.get("message"), 160)}
                                 for f in (result.get("findings") or [])[:20] if isinstance(f, dict)],
                    "summary": common.clean_text(ev.get("summary", ""), 160)}
            r["gates"][name] = slim
            for acid, cell in (ev.get("ac_results") or {}).items() if isinstance(ev.get("ac_results"), dict) else []:
                if acid not in acs or not isinstance(cell, dict) or cell.get("result") not in ("pass", "fail"):
                    continue
                r["ac"].setdefault(acid, {})[name] = {"result": cell["result"], "evidence": common.clean_text(cell.get("evidence", ""), 120),
                                                      "at": common.iso()}
            rv = ev.get("review")
            if isinstance(rv, dict):
                for k in ("code", "security", "design"):
                    if isinstance(rv.get(k), str):
                        r["review"][k] = common.clean_text(rv[k], 40)
            st.log(iid, r["status"], r["status"], "gate:" + name, "%s %s" % (name, slim["status"]))
            return slim

        _, slim = self.store.update(mutate, "gate %s %s" % (name, iid))
        return slim

    # -- approvals (L-10) ---------------------------------------------------------------------------------------------------------------
    def approve(self, scope: str, target: str, by: str, choice: str = "approve", note: str = "") -> Dict[str, Any]:
        """Write an approval record. scope: item | checkpoint | unblock | env | breaker | cost. A record is made by a named human
        (`by`); it cannot be backdated and is consumed by the transition it allows."""
        by = common.clean_text(by, 60)
        if not by.strip():
            raise UsageError("--by <name> is required")
        if scope not in ("item", "checkpoint", "unblock", "env", "breaker", "cost"):
            raise UsageError("unknown approval scope %r" % common.clean_text(scope, 20))
        if choice not in ("approve", "reject", "stop", "reset", "keep", "replan"):
            raise UsageError("unknown choice %r" % common.clean_text(choice, 20))
        if scope in ("item", "checkpoint", "unblock"):
            self._need_plan_item(target)

        def mutate(st: State) -> Dict[str, Any]:
            if scope in ("item", "checkpoint", "unblock"):
                r = st.item(target)
            if scope in ("item", "checkpoint"):
                if r["status"] not in ("awaiting_human", "reviewing", "pr_open", "needs_human", "merged"):
                    raise Refused("item %s is %s: nothing to approve there" % (target, r["status"]))
                if choice == "reject":
                    if r["status"] == "merged":
                        raise Refused("item %s already merged: a reject needs the revert plan (`loopctl abort`), not an approval" % target)
                    r["rework"].append({"at": common.iso(), "by": by, "note": common.clean_text(note, 300)})
                    del r["rework"][:-20]
                    r["branch"] = None
                    r["gates"] = {}
                    r["ac"] = {}
                    r["counters"] = _reset_counters(r["counters"], keep=("platform", "discards"))
                    st.set_status(target, "pending", by, "rejected at checkpoint: " + note)
                elif choice == "stop":
                    pass
                elif r["status"] == "needs_human":
                    st.set_status(target, "pending", by, "human resolved: " + note)
            elif scope == "unblock":
                if r["status"] not in RESTING:
                    raise Refused("item %s is %s, not blocked" % (target, r["status"]))
                if choice == "reset":
                    r["counters"] = _reset_counters(r["counters"], keep=("platform", "discards"))
                    r["lastFailure"] = None
                    st.set_status(target, "pending", by, "unblocked: " + note)
                    if st.header.get("runStatus") in ("parked",) and not st.by_status(*RESTING):
                        st.header["runStatus"] = "idle"
                elif choice not in ("keep", "replan"):
                    raise UsageError("unblock choices: reset, keep, replan")
            elif scope == "env":
                st.header["levelCap"] = None
            elif scope == "breaker":
                st.header["breaker"] = 0
                if st.header.get("runStatus") == "stopped":
                    st.header["runStatus"] = "idle"
                st.header["parked"] = None
            elif scope == "cost":
                pass
            return _add_approval(st, scope, target, by, choice, note, attempt=st.items[target]["attempt"] if target in st.items else None)

        st, ap = self.store.update(mutate, "approve %s %s (%s) by %s" % (scope, target, choice, by))
        if scope == "cost":
            from . import ledger
            run = self.ctx.rs.read_run() or {}
            if isinstance(run.get("runId"), str):
                ledger.Ledger(self.ctx.rs).acknowledge(run["runId"], by, note)
        self._event("approve", scope=scope, target=target, choice=choice, by=by)
        return ap

    def reset_inflight(self, iid: str, by: str, note: str) -> None:
        """Crash recovery (reconcile --apply, abort): an in-flight item with no live owner goes back to pending."""
        def mutate(st: State) -> None:
            r = st.item(iid)
            if r["status"] in ACTIVE:
                st.set_status(iid, "pending", by, note)

        self.store.update(mutate, "reset %s" % iid)


def _reset_counters(c: Dict[str, int], keep: Iterable[str] = ()) -> Dict[str, int]:
    out = {k: 0 for k in c}
    for k in keep:
        if k in c:
            out[k] = c[k]
    return out


def _force_status(st: State, iid: str, to: str, by: str, note: str, block_reason: Optional[str] = None) -> None:
    """A status change driven by the plan itself (approve plan): bypasses the edge table because the plan moved, not the item."""
    r = st.items[iid]
    frm = r["status"]
    r["status"] = to
    r["blockReason"] = block_reason if to in RESTING else None
    if to in RESTING and not block_reason:
        r["blockReason"] = "plan_changed"
    st.log(iid, frm, to, by, note)


def _add_approval(st: State, scope: str, target: str, by: str, choice: str, note: str, attempt: Optional[int] = None,
                  plan_digest: Optional[str] = None) -> Dict[str, Any]:
    ap = {"id": "ap-%d" % (len(st.d["approvals"]) + 1), "scope": scope, "target": target, "by": by, "choice": choice,
          "at": common.iso(), "note": common.clean_text(note, 300), "attempt": attempt, "used": False}
    if plan_digest:
        ap["planDigest"] = plan_digest
    st.d["approvals"].append(ap)
    del st.d["approvals"][:-MAX_APPROVALS]
    return ap


def _find_approval(st: State, iid: str, attempt: int) -> Optional[Dict[str, Any]]:
    for ap in reversed(st.d["approvals"]):
        if ap.get("target") == iid and ap.get("scope") in ("item", "checkpoint") and ap.get("choice") == "approve" \
                and not ap.get("used") and ap.get("attempt") == attempt:
            return ap
    return None
