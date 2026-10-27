"""`abort [--rollback-to <sha>]`: stop the run and, before merge to main, roll it back (report section 10.7; CO-9, L-6, F-27, RC-14).

    abort --dry-run [--rollback-to SHA]       show exactly what would happen; writes nothing
    abort --rollback-to SHA --by NAME         do it

Steps, in order:
1. HALT is set: all dispatch stops (guard-halt denies every mutating tool).
2. The items merged after `<sha>` are listed in REVERSE merge order.
3. For each item that is NOT yet on main -- `delivery: local`, merge commit in no remote protected branch, and loopctl is in the
   registered loop worktree on the integration branch with a clean tree -- loopctl runs its down-migration (if
   `db_migrate_down` is set and the class is not `lossy`) against LOCAL or ephemeral databases only, then reverts the merge
   on the integration branch (`git revert -m 1`). A revert that conflicts is aborted, and every remaining item becomes plan-only.
4. Open PRs are closed (a caller-supplied closer; with none, they are listed as manual actions: loopctl has no network).
5. Dependents of the rolled-back items that were in flight are marked `abandoned` (the reject cascade); the rolled-back items go
   back to `pending`, or `needs_human` when they could not be rolled back.
6. An abort record is written to the state (`header.abort`) and a human-readable plan to `<git-common-dir>/loopctl/abort/`.

AFTER MERGE TO MAIN loopctl executes NOTHING. A revert pushed to main can deploy (B5); a down-migration may reach a shared
database (H4). Those items get a written REVERT PLAN -- the reverts in reverse merge order, each down-migration with its data-loss
class and where it must run -- for a human to put into an issue or a draft PR. A `lossy` down-migration is never run by the loop
at all, anywhere.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kitlib import gitutil, jsonio

from . import lgit, branches, common, plan as plan_mod, state as state_mod
from .common import Refused, UsageError

__all__ = ["plan_abort", "execute", "AbortPlan", "revert_plan_text"]

_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TZ", "VIRTUAL_ENV", "PYENV_ROOT", "NVM_DIR")


class AbortPlan(object):
    def __init__(self) -> None:
        self.rollback_to = None  # type: Optional[str]
        self.integration = None  # type: Optional[str]
        self.items = []  # type: List[Dict[str, Any]]        # merged after the sha, reverse merge order
        self.in_flight = []  # type: List[str]
        self.open_prs = []  # type: List[Dict[str, Any]]
        self.cascade = []  # type: List[str]
        self.problems = []  # type: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "loopctl.abort.v1", "rollback_to": self.rollback_to, "integration": self.integration, "items": self.items,
                "in_flight": self.in_flight, "open_prs": self.open_prs, "cascade_abandon": self.cascade, "problems": self.problems}


def _git(ctx: Any, args: Sequence[str], env: Optional[Dict[str, str]] = None):
    return lgit.run_git(list(args), cwd=ctx.repo, isolated=True, pin_hooks=True, env=env)


def _published(ctx: Any, sha: str) -> List[str]:
    """Remote-tracking protected branches that contain `sha` (the work has left this machine)."""
    proc = _git(ctx, ["for-each-ref", "--contains", sha, "--format=%(refname)", "refs/remotes/"])
    prot = set(ctx.protected_branches())
    out = []  # type: List[str]
    for ref in proc.stdout.split():
        parts = ref.split("/", 3)       # refs/remotes/<remote>/<branch...>
        if len(parts) == 4 and parts[3] in prot:
            out.append(ref)
    return out


def plan_abort(ctx: Any, st: Any, lp: Any, rollback_to: Optional[str] = None) -> AbortPlan:
    p = AbortPlan()
    ib = ctx.integration_branch()
    p.integration = ib
    p.in_flight = st.by_status(*state_mod.IN_FLIGHT)
    for iid in st.by_status("pr_open", "awaiting_human"):
        r = st.items[iid]
        if r.get("prUrl"):
            p.open_prs.append({"item": iid, "url": r["prUrl"]})
    if rollback_to is None:
        return p
    if not gitutil.valid_oid(rollback_to) or gitutil.rev_parse(rollback_to + "^{commit}", ctx.repo, isolated=True) != rollback_to:
        raise UsageError("--rollback-to must be a full commit sha that exists in this repository")
    if not ib:
        raise Refused("no integration branch to roll back")
    if not gitutil.is_ancestor(rollback_to, "refs/heads/" + ib, ctx.repo, isolated=True):
        raise Refused("%s is not an ancestor of %s: nothing can be rolled back to it" % (rollback_to[:12], ib))
    p.rollback_to = rollback_to
    order = _git(ctx, ["rev-list", "--topo-order", "--reverse", "%s..refs/heads/%s" % (rollback_to, ib)]).stdout.split()
    pos = {sha: i for i, sha in enumerate(order)}
    hits = []  # type: List[Tuple[int, str]]
    for iid in st.by_status("merged"):
        mc = st.items[iid].get("mergeCommit")
        if mc in pos:
            hits.append((pos[mc], iid))
    delivery = ctx.cfg.get("delivery")
    for _i, iid in sorted(hits, reverse=True):
        r = st.items[iid]
        mc = r["mergeCommit"]
        item = lp.items.get(iid, {})
        pub = _published(ctx, mc)
        reason = None
        if delivery != "local":
            reason = "delivery is %s: the merge is on main by pull request" % delivery
        elif pub:
            reason = "the merge commit is already on %s" % pub[0].split("/", 2)[2]
        migs = []
        for m in item.get("migrations") or []:
            if isinstance(m, dict):
                migs.append({"name": m.get("name"), "down": m.get("down"), "class": m.get("dataLossClass"),
                             "executable": (m.get("down") is not None and m.get("dataLossClass") != "lossy" and reason is None
                                            and bool(str(ctx.cfg.get("db_migrate_down") or "").strip()))})
        parents = _git(ctx, ["rev-list", "--parents", "-n", "1", mc]).stdout.split()
        p.items.append({"item": iid, "commit": mc, "merge": len(parents) > 2, "executable": reason is None, "plan_only_reason": reason,
                        "migrations": migs, "title": common.clean_text(item.get("title"), 80)})
    rolled = {x["item"] for x in p.items}
    for iid in p.in_flight:
        if iid in rolled:
            continue
        if any(a in rolled for a in plan_mod.ancestors(lp.items, iid)):
            p.cascade.append(iid)
    return p


def _db_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    return env


def revert_plan_text(ctx: Any, ap: AbortPlan, plan_only: Sequence[Dict[str, Any]], manual: Sequence[str], run_id: str) -> str:
    L = ["# Revert plan (written by `loopctl abort`; NOT executed)", "",
         "Run id: %s. Rolled back to: %s on %s." % (run_id, (ap.rollback_to or "-")[:12], ap.integration),
         "These items already reached main or a shared database. loopctl does not touch them: a revert pushed to main can deploy",
         "(B5), and a down-migration against a shared database is H4. Put this plan in an issue or a draft PR and run it by hand.", ""]
    for n, it in enumerate(plan_only, 1):
        L.append("%d. Item %s (%s)" % (n, it["item"], it["title"]))
        L.append("   revert: git revert %s%s" % ("-m 1 " if it["merge"] else "", it["commit"]))
        L.append("   why it was not executed: %s" % it["plan_only_reason"])
        for m in it["migrations"]:
            L.append("   down-migration %s (%s): data-loss class %s%s. Environments it touches: every database that ran the up-migration, "
                     "including shared ones." % (m["name"], m["down"] or "none recorded", m["class"], " -- LOSSY: take a snapshot first, a human decides" if m["class"] == "lossy" else ""))
    if manual:
        L += ["", "Manual actions:"] + ["- " + m for m in manual]
    L.append("")
    return "\n".join(L)


def execute(loop: Any, ap: AbortPlan, by: str, reason: str, session_id: Optional[str], pr_closer: Optional[Callable[[str], bool]] = None,
            runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    ctx = loop.ctx
    by = common.clean_text(by, 60)
    if not by.strip():
        raise UsageError("--by <name> is required: an abort is a human's decision and it is recorded")
    run_rec = None
    try:
        run_rec = ctx.rs.read_run()
    except common.READ_ERRORS:
        pass
    run_id = (run_rec or {}).get("runId") or "run-%d" % int(common.now())
    ctx.rs.set_halt("abort: %s" % common.clean_text(reason or "no reason given", 120), by)       # 1. halt all dispatch
    record = {"at": common.iso(), "by": by, "reason": common.clean_text(reason, 200), "rollbackTo": ap.rollback_to, "reverted": [],
              "revertPlan": [], "downMigrations": [], "manualActions": [], "abandoned": [], "problems": list(ap.problems)}  # type: Dict[str, Any]
    executed = []  # type: List[str]
    plan_only = []  # type: List[Dict[str, Any]]
    stop_exec = False
    ib = ap.integration
    can_run_git = bool(ib and ctx.worktree_is_loop_worktree())
    if ap.items and can_run_git:
        if branches.current(ctx.repo) != ib:
            record["problems"].append("the loop worktree is not on %s: nothing was reverted" % ib)
            stop_exec = True
        elif not lgit.is_clean(ctx.repo):
            record["problems"].append("the loop worktree has uncommitted changes: nothing was reverted")
            stop_exec = True
    elif ap.items:
        record["problems"].append("loopctl is not in the registered loop worktree (run.json): nothing was reverted")
        stop_exec = True
    for it in ap.items:                                                                            # 2-3. reverse merge order
        if stop_exec or not it["executable"]:
            it = dict(it, plan_only_reason=it["plan_only_reason"] or "an earlier step could not be done")
            plan_only.append(it)
            continue
        ok = True
        for m in it["migrations"]:                                                                  # down first: the files still exist
            if m["executable"]:
                cmd = shlex.split(str(ctx.cfg.get("db_migrate_down")))
                env = dict(_db_env(), LOOPCTL_ITEM=it["item"], LOOPCTL_MIGRATION=str(m["name"]), LOOPCTL_MIGRATION_DOWN=str(m["down"]))
                try:
                    proc = runner(cmd, cwd=ctx.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=600)
                    good = proc.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    good = False
                record["downMigrations"].append({"item": it["item"], "migration": m["name"], "ok": good})
                if not good:
                    ok = False
                    record["problems"].append("down-migration %s of %s failed: the item was not reverted" % (m["name"], it["item"]))
                    break
            elif m["down"] is not None or m["class"] == "lossy":
                it.setdefault("manual", []).append(m)
        if not ok:
            stop_exec = True
            plan_only.append(dict(it, plan_only_reason="its down-migration failed"))
            continue
        args = ["revert", "--no-edit"] + (["-m", "1"] if it["merge"] else []) + [it["commit"]]
        proc = lgit.run_git(args, cwd=ctx.repo, isolated=True, pin_hooks=True,
                               env={"GIT_AUTHOR_NAME": "loopctl", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
                                    "GIT_COMMITTER_NAME": "loopctl", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"})
        if proc.returncode != 0:
            lgit.run_git(["revert", "--abort"], cwd=ctx.repo, isolated=True, pin_hooks=True)
            record["problems"].append("reverting %s (%s) conflicted and was aborted" % (it["item"], it["commit"][:12]))
            stop_exec = True
            plan_only.append(dict(it, plan_only_reason="the revert conflicts; resolve it by hand"))
            continue
        executed.append(it["item"])
        record["reverted"].append({"item": it["item"], "commit": it["commit"]})
    manual = []  # type: List[str]
    for pr in ap.open_prs:                                                                          # 4. close open PRs
        closed = False
        if pr_closer is not None:
            try:
                closed = bool(pr_closer(pr["url"]))
            except Exception:  # noqa: BLE001
                closed = False
        if not closed:
            manual.append("close the pull request %s (item %s); loopctl has no network" % (common.clean_text(pr["url"], 100), pr["item"]))
    record["revertPlan"] = [{"item": x["item"], "commit": x["commit"], "reason": x["plan_only_reason"],
                            "migrations": x["migrations"]} for x in plan_only]
    record["manualActions"] = manual
    plan_file = None
    if plan_only or manual:
        d = os.path.join(ctx.rs.root, "abort")
        jsonio.ensure_dir(d)
        plan_file = os.path.join(d, "revert-plan-%s.md" % re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id)))
        jsonio.atomic_write_text(plan_file, revert_plan_text(ctx, ap, plan_only, manual, str(run_id)), 0o600)
        record["planFile"] = plan_file
    cascade = set(ap.cascade)
    branch_of = {}  # type: Dict[str, Optional[str]]
    try:
        cur_state, _ = loop.store.read()
        branch_of = {i: cur_state.items[i].get("branch") for i in ap.in_flight if cur_state and i in cur_state.items}
    except common.LoopctlError:
        pass
    plan_only_ids = {x["item"] for x in plan_only}

    def mutate(st: Any) -> None:                                                                    # 5-6. state and record
        for iid in executed:
            r = st.items.get(iid)
            if r and r["status"] == "merged":
                r["mergeCommit"] = None
                r["branch"] = None
                r["gates"], r["ac"] = {}, {}
                state_mod._force_status(st, iid, "pending", by, "rolled back by abort (reverted on %s)" % ib)
        for iid in sorted(plan_only_ids):
            r = st.items.get(iid)
            if r and r["status"] == "merged":
                state_mod._force_status(st, iid, "needs_human", by, "abort could not roll this back; see the revert plan", "aborted")
        for iid in st.by_status(*state_mod.IN_FLIGHT):
            r = st.items[iid]
            if iid in cascade:
                state_mod._force_status(st, iid, "abandoned", by, "abort: it depends on a rolled-back item")
                record["abandoned"].append(iid)
            else:
                state_mod._force_status(st, iid, "blocked", by, "abort: " + common.clean_text(reason, 80), "aborted")
        st.header["abort"] = record
        st.header["runStatus"] = "aborted"
        st.header["parked"] = {"item": None, "reason": "aborted", "at": common.iso()}

    loop.store.update(mutate, "abort by %s" % by)
    from . import locked
    try:
        st2, _ = loop.store.read()
        locked.refresh(ctx, st2, loop.plan, active=None)
    except (OSError, common.LoopctlError):
        pass
    if session_id:
        try:
            ctx.lock().release(session_id)
        except Exception:  # noqa: BLE001
            pass
    # branches of stopped work are preserved, never deleted: blocked/* for parked items, dead/* for abandoned ones
    if ctx.worktree_is_loop_worktree():
        for iid in ap.in_flight:
            br = branch_of.get(iid)
            if not br or not branches.parse(br):
                continue
            try:
                if iid in cascade:
                    branches.rename(ctx.repo, br, branches._unique(ctx.repo, "dead", branches.parse(br)[1], branches.parse(br)[2])) \
                        if branches.exists(ctx.repo, br) else None
                else:
                    branches.park(ctx, br)
            except (Refused, UsageError) as exc:
                record["problems"].append("branch %s: %s" % (br, common.clean_text(exc, 80)))
    return record
