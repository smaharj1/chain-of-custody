"""The reject cascade (report 19.7 "Reject cascade (L-6)", 10.7, 11.5 item 3; RC-14, CO-18; DEC-2).

Rejecting an item that is not on `main` invalidates what was built on it:

    1. the items that depend on it, directly or not, and were MERGED after it, are reverted in REVERSE merge order on the local integration branch;
    2. their down-migrations run in reverse order on local or ephemeral databases only (`db_migrate_down`; a `lossy` one never runs, CO-18);
    3. every item the rejection touched goes back to `pending`, with a fresh attempt (a<n+1>), its counters reset (the cap is per attempt, L-8);
    4. the items that depend on it and are IN FLIGHT (a worker is building one, or one waits for a human) are invalidated: the worker is
       stopped at its next safe point, its work is kept on a `dead/*` branch, and the item goes back to `pending` (parallel mode: they can exist,
       because the rejected item had merged on the integration branch before they began).

Once ANY affected item has reached `main`, or `delivery` is not `local` (the merge is on `main` by pull request), loopctl reverts and migrates NOTHING.
A revert pushed to `main` can deploy (B5); a down-migration may reach a shared database (H4). It writes a human revert plan instead
(`abort/revert-plan-<run>.md`, the reverts in reverse order, each down-migration with its data-loss class) and moves those items to `needs_human`.

IT CANNOT LOOP. The set of items is computed once from the plan with a visited set (a cyclic `dependsOn`, which `validate` refuses but a hostile
plan file might carry, still terminates). A cascade never starts another cascade: resetting a dependent is not a rejection. And each root may be
cascaded at most `MAX_CASCADES` times in a run (the count is kept in the state header): the next time the item is parked for a human with
`cascade_loop` instead, because an item that keeps being rejected is a problem a person has to look at.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from kitlib import gitutil, jsonio

from .. import abort as abort_mod
from .. import branches, common, lgit, plan as plan_mod, state as state_mod
from ..common import Refused, UsageError

__all__ = ["CascadePlan", "plan_cascade", "execute", "reset_item", "invalid_inflight", "MAX_CASCADES"]

MAX_CASCADES = 3
_IDENT = {"GIT_AUTHOR_NAME": "loopctl", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}


class CascadePlan(object):
    def __init__(self, root: str) -> None:
        self.root = root
        self.root_status = ""
        self.integration = None  # type: Optional[str]
        self.merged = []  # type: List[Dict[str, Any]]         # merged items, REVERSE merge order (the root included when it merged)
        self.in_flight = []  # type: List[str]                 # dependents being worked on (or waiting for a human)
        self.untouched = []  # type: List[str]                 # dependents that have not started
        self.plan_only = False                                 # an affected item is on main, or delivery is not local: nothing is executed
        self.plan_only_reason = ""
        self.loops = 0                                         # how many times this root was cascaded in this run
        self.problems = []  # type: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "loopctl.cascade.v1", "root": self.root, "root_status": self.root_status, "integration": self.integration, "merged": self.merged,
                "in_flight": self.in_flight, "untouched": self.untouched, "plan_only": self.plan_only, "plan_only_reason": self.plan_only_reason,
                "cascades_so_far": self.loops, "problems": self.problems}


def _git(ctx: Any, args: Sequence[str], env: Optional[Dict[str, str]] = None):
    return lgit.run_git(list(args), cwd=ctx.repo, isolated=True, pin_hooks=True, env=env)


def plan_cascade(ctx: Any, st: Any, lp: Any, root: str) -> CascadePlan:
    """What rejecting `root` would do. Reads only."""
    if not common.valid_item_id(root) or root not in lp.items or root not in st.items:
        raise UsageError("item %r is not in the plan or the state" % common.clean_text(root, 30))
    p = CascadePlan(root)
    p.root_status = st.items[root]["status"]
    ib = ctx.integration_branch()
    p.integration = ib
    p.loops = int((st.header.get("cascades") or {}).get(root, 0)) if isinstance(st.header.get("cascades"), dict) else 0
    affected = {root} | set(plan_mod.descendants(lp.items, root))          # a visited set inside: a cyclic plan terminates
    merged = [i for i in affected if st.items.get(i, {}).get("status") == "merged" and st.items[i].get("mergeCommit")]
    p.in_flight = sorted((i for i in affected if i != root and st.items.get(i, {}).get("status") in state_mod.IN_FLIGHT), key=common.natural_key)
    p.untouched = sorted((i for i in affected if i != root and st.items.get(i, {}).get("status") == "pending"), key=common.natural_key)
    if root not in merged and p.root_status in state_mod.IN_FLIGHT:
        p.in_flight = sorted(set(p.in_flight) | {root}, key=common.natural_key)       # the root itself is being worked on: it is rejected like any in-flight item
    if not merged:
        return p
    if not ib:
        p.problems.append("there is no integration branch to revert on")
        p.plan_only, p.plan_only_reason = True, "there is no integration branch"
    order = _git(ctx, ["rev-list", "--topo-order", "--reverse", "--max-count=20000", "refs/heads/%s" % ib]).stdout.split() if ib else []
    pos = {sha: k for k, sha in enumerate(order)}
    rows = []  # type: List[Tuple[int, str]]
    for iid in merged:
        mc = st.items[iid]["mergeCommit"]
        if mc not in pos:
            p.problems.append("item %s: its merge commit %s is not on %s" % (iid, mc[:12], ib))
            p.plan_only, p.plan_only_reason = True, p.plan_only_reason or "a merge commit is not on the integration branch"
            continue
        rows.append((pos[mc], iid))
    delivery = ctx.cfg.get("delivery")
    for _k, iid in sorted(rows, reverse=True):                                                # reverse merge order
        mc = st.items[iid]["mergeCommit"]
        item = lp.items.get(iid, {})
        published = abort_mod._published(ctx, mc)
        why = None
        if delivery != "local":
            why = "delivery is %s: the merge is on main by pull request" % delivery
        elif published:
            why = "the merge commit is already on %s" % published[0].split("/", 2)[2]
        if why:
            p.plan_only, p.plan_only_reason = True, p.plan_only_reason or why
        migs = []
        for m in item.get("migrations") or []:
            if isinstance(m, dict):
                migs.append({"name": m.get("name"), "down": m.get("down"), "class": m.get("dataLossClass"),
                             "executable": bool(m.get("down") is not None and m.get("dataLossClass") != "lossy" and why is None
                                                and str(ctx.cfg.get("db_migrate_down") or "").strip())})
        parents = _git(ctx, ["rev-list", "--parents", "-n", "1", mc]).stdout.split()
        p.merged.append({"item": iid, "commit": mc, "merge": len(parents) > 2, "migrations": migs, "title": common.clean_text(item.get("title"), 80),
                         "plan_only_reason": why})
    return p


# -- state helpers (used inside a StateStore.update) --------------------------------------------------------------------------------

def reset_item(st: Any, iid: str, by: str, note: str) -> None:
    """Back to `pending` with a fresh attempt: no branch, no gates, counters reset except `platform` and `discards` (as a discard does). The
    caller renames or removes the old branch and worktree."""
    r = st.items[iid]
    r["mergeCommit"] = None
    r["branch"] = None
    r["headSha"] = None
    r["prUrl"] = None
    r["gates"], r["ac"], r["review"] = {}, {}, {}
    r["lastFailure"] = None
    r["notBefore"] = None
    r["counters"] = state_mod._reset_counters(r["counters"], keep=("platform", "discards"))
    r["attempt"] = int(r["attempt"]) + 1
    state_mod._force_status(st, iid, "pending", by, note)


def invalid_inflight(st: Any, items: Dict[str, Any]) -> List[str]:
    """Items in progress (or waiting for a human) whose ancestors are no longer all merged: something they were built on was rejected or reverted."""
    out = []  # type: List[str]
    for iid in sorted(st.items, key=common.natural_key):
        if st.items[iid]["status"] not in state_mod.IN_FLIGHT or iid not in items:
            continue
        if any(st.items.get(a, {}).get("status") != "merged" for a in plan_mod.ancestors(items, iid)):
            out.append(iid)
    return out


def _dead_branch(ctx: Any, branch: Optional[str]) -> Optional[str]:
    """Rename a feat/* or blocked/* branch to dead/*; None if there was nothing to rename."""
    p = branches.parse(branch or "")
    if not p or p[0] == "dead" or not branches.exists(ctx.repo, branch or ""):
        return None
    new = branches._unique(ctx.repo, "dead", p[1], p[2])
    branches.rename(ctx.repo, branch or "", new)
    return new


# -- doing it ----------------------------------------------------------------------------------------------------------------------------

def execute(loop: Any, cp: CascadePlan, by: str, reason: str, runner: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Carry the cascade out (the caller has shown the plan and been told to). Returns the record also written to `header.cascade`."""
    ctx = loop.ctx
    by = common.clean_text(by, 60)
    if not by.strip():
        raise UsageError("--by <name> is required: a rejection is a human's decision and it is recorded")
    run_id = ((ctx.rs.read_run() or {}).get("runId")) or "run-%d" % int(common.now())
    record = {"at": common.iso(), "by": by, "root": cp.root, "reason": common.clean_text(reason, 200), "reverted": [], "downMigrations": [],
              "reset": [], "invalidated": [], "revertPlan": [], "problems": list(cp.problems)}  # type: Dict[str, Any]
    if cp.loops >= MAX_CASCADES:
        def stop(st: Any) -> None:
            r = st.items[cp.root]
            if r["status"] not in state_mod.TERMINAL and r["status"] not in state_mod.RESTING:
                st.set_status(cp.root, "needs_human", by, "rejected %d times in this run" % cp.loops, block_reason="cascade_loop")
            st.header["cascade"] = dict(record, refused="cascade_loop")
        loop.store.update(stop, "cascade of %s refused (loop)" % cp.root)
        record["refused"] = "cascade_loop"
        return record
    executed, plan_only = [], []  # type: List[str], List[Dict[str, Any]]
    ib = cp.integration
    can_git = bool(ib and not cp.plan_only and ctx.worktree_is_loop_worktree() and branches.current(ctx.repo) == ib and lgit.is_clean(ctx.repo))
    if cp.merged and not cp.plan_only and not can_git:
        record["problems"].append("loopctl is not in the registered loop worktree on %s with a clean tree: nothing was reverted" % ib)
    stop_exec = not can_git
    extra = {"runner": runner} if runner is not None else {}
    for it in cp.merged:                                                          # reverse merge order
        if stop_exec:
            plan_only.append(dict(it, plan_only_reason=it.get("plan_only_reason") or cp.plan_only_reason or "an earlier step could not be done"))
            continue
        ok = True
        for m in it["migrations"]:
            if m["executable"]:
                good = abort_mod.run_down(ctx, it["item"], m, **extra)
                record["downMigrations"].append({"item": it["item"], "migration": m["name"], "ok": good})
                if not good:
                    ok = False
                    record["problems"].append("down-migration %s of %s failed: the item was not reverted" % (m["name"], it["item"]))
                    break
        if not ok:
            stop_exec = True
            plan_only.append(dict(it, plan_only_reason="its down-migration failed"))
            continue
        proc = _git(ctx, ["revert", "--no-edit"] + (["-m", "1"] if it["merge"] else []) + [it["commit"]], env=dict(_IDENT))
        if proc.returncode != 0:
            _git(ctx, ["revert", "--abort"])
            record["problems"].append("reverting %s (%s) conflicted and was aborted" % (it["item"], it["commit"][:12]))
            stop_exec = True
            plan_only.append(dict(it, plan_only_reason="the revert conflicts; resolve it by hand"))
            continue
        executed.append(it["item"])
        record["reverted"].append({"item": it["item"], "commit": it["commit"]})
    plan_only_ids = {x["item"] for x in plan_only}
    cur, _sha = loop.store.read()
    old_branch = {i: (cur.items[i].get("branch") if cur and i in cur.items else None) for i in cp.in_flight}
    plan_file = None
    if plan_only:
        d = os.path.join(ctx.rs.root, "abort")
        jsonio.ensure_dir(d)
        plan_file = os.path.join(d, "revert-plan-%s.md" % re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id)))
        ap = abort_mod.AbortPlan()
        ap.rollback_to, ap.integration = None, ib
        jsonio.atomic_write_text(plan_file, abort_mod.revert_plan_text(ctx, ap, plan_only, [], str(run_id)), 0o600)
        record["planFile"] = plan_file
    record["revertPlan"] = [{"item": x["item"], "commit": x["commit"], "reason": x["plan_only_reason"], "migrations": x["migrations"]} for x in plan_only]

    def mutate(st: Any) -> None:
        for iid in executed:
            r = st.items.get(iid)
            if r and r["status"] == "merged":
                reset_item(st, iid, by, "rejected: reverted on %s (%s)" % (ib, common.clean_text(reason, 60)))
                record["reset"].append(iid)
        for iid in sorted(plan_only_ids):
            r = st.items.get(iid)
            if r and r["status"] == "merged":
                state_mod._force_status(st, iid, "needs_human", by, "rejected, but loopctl does not revert what has reached main; see the revert plan", "human_revert")
        for iid in cp.in_flight:
            r = st.items.get(iid)
            if r and r["status"] in state_mod.IN_FLIGHT:
                reset_item(st, iid, by, "invalidated: it depends on %s, which was rejected" % cp.root)
                record["invalidated"].append(iid)
        root = st.items[cp.root]
        if root["status"] in state_mod.RESTING:
            reset_item(st, cp.root, by, "rejected by %s" % by)
        if reason:
            root["rework"].append({"at": common.iso(), "by": by, "note": common.clean_text(reason, 300)})
            del root["rework"][:-20]
        counts = st.header.get("cascades") if isinstance(st.header.get("cascades"), dict) else {}
        counts[cp.root] = int(counts.get(cp.root, 0)) + 1
        st.header["cascades"] = counts
        st.header["cascade"] = record

    loop.store.update(mutate, "cascade of %s by %s" % (cp.root, by))
    for iid, br in old_branch.items():                                   # the work of an invalidated item is kept: dead/* (pruned after orphan_retention_days)
        try:
            _dead_branch(ctx, br)
        except (Refused, UsageError) as exc:
            record["problems"].append("branch %s: %s" % (br, common.clean_text(exc, 80)))
    try:
        from .. import locked
        st2, _ = loop.store.read()
        locked.refresh(ctx, st2, loop.plan, active=None)
    except (OSError, common.LoopctlError):
        pass
    return record
