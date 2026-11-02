"""Serial integration: build in parallel, merge one at a time (report 11.5 item 3 "Serial integrate", D-6, F-7; DEC-33).

A worker ends with its item reviewed and its branch committed. It does not merge. The scheduler hands finished items to `integrate`, one at a time,
in plan order, and this is the ONLY code in a parallel run that writes the integration branch:

    1. the integration slot (`integrate` lease) and the item's own lease are held and renewed; either one lost means no merge;
    2. FOOTPRINT: the item's real diff must stay inside the globs it declared (plus its own `docs/features/<slug>/` trail). A file outside them is a
       footprint lie: the scheduler ran this item next to others because of what it promised, so the item is parked (`footprint_violation`),
       never merged;
    3. REBASE: if the integration branch moved since the item began, the item's branch is rebased onto the new tip, in the item's worktree. A conflict
       is aborted and parks the item (`merge_conflict`) for a human, exactly as the serial driver does;
    4. MIGRATION ORDER: a migration the item added must sort after every migration already on the integration branch (migrations are ordered by name).
       If one does not, the item goes back to its engineer (`item/migration_order`) to regenerate it: the tooling that numbers migrations is the
       engineer's, and the schema mutex means this only happens when someone committed a migration by hand;
    5. RE-VERIFY: a rebased head is code nobody has verified, and Evidence wants every gate that is `on` fresh for the head it judges, so ALL the
       deterministic gates run again on it, in the item's worktree: every `driver.VERIFY_GATES` gate (g5 g1 g3 g6 g9 g11 docs g10 g4) and `g7` (a read of
       the committed review files, fresh across a rebase), `off` gates skipped, a blocking g5 ending the round. The LLM reviews are not re-run (the
       rebase did not change the item's own diff; a conflicting rebase already parked). Then whatever the merge transition still requires (Evidence).
       A failure is an ordinary item failure (retry with the engineer, or discard). Only LINEAR history is rebased: an item branch that holds a merge
       commit is parked (`ESCALATION_NEEDED`) instead, because a rebase would silently drop the resolution inside it (GT-11);
    6. the lease check, then the merge itself (`Driver.merge_branch`: `--no-ff`, the Loop-Item trailer, the record), then the worktree and the branch go.

What a lease lost at step 6 means: before the merge, nothing was changed and the item stays `reviewing` (`lost`); the scheduler tries again with a new
lease. After the merge commit exists the merge is a fact, so the record is written whatever the lease says (and `recover()` would find it from the
trailer if the record failed). Two integrators cannot both get past git's own index lock in the loop worktree, so even a rival that reclaimed an
expired lease cannot interleave a second merge.

Returns one of: "merged", "retry" (the item went back to an engineer or was reset), "parked", "busy" (someone holds the integration slot), "lost" (a lease
was lost before the merge).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from kitlib import gitutil

from .. import common, failure, lgit
from ..common import LoopctlError, Refused
from . import footprint, mutex

__all__ = ["integrate", "changed_paths", "has_merge_commits", "rebase", "migration_order_problem", "HOOKS"]

# Test seams (the way driver.SLEEP is one): HOOKS["before_merge"](driver, slot) runs after every check and just before the merge.
HOOKS = {}  # type: Dict[str, Callable[..., None]]

_IDENT = {"GIT_AUTHOR_NAME": "loopctl driver", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl driver", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}


def changed_paths(repo: str, base: str, head: str) -> List[str]:
    """Every path the item's branch changed since `base`, renames as delete plus add (so a file moved OUT of the footprint is seen too)."""
    proc = lgit.run_git(["diff", "--name-only", "--no-renames", "-z", base, head, "--"], cwd=repo, isolated=True, pin_hooks=True)
    if proc.returncode != 0:
        raise Refused("cannot read the item's diff: %s" % common.clean_text(proc.stderr, 120))
    return sorted({p for p in proc.stdout.split("\0") if p})


def rebase(worktree: str, onto: str) -> bool:
    """`git rebase <onto>` in the item's worktree. False (and the rebase aborted, the branch as it was) on a conflict."""
    proc = lgit.run_git(["rebase", "--quiet", onto], cwd=worktree, isolated=True, pin_hooks=True, env=dict(_IDENT))
    if proc.returncode == 0:
        return True
    lgit.run_git(["rebase", "--abort"], cwd=worktree, isolated=True, pin_hooks=True)
    return False


def has_merge_commits(repo: str, base: str, head: str) -> bool:
    """Does `base..head` hold a merge commit? (isolated git; an unreadable range counts as yes: fail closed)"""
    proc = lgit.run_git(["rev-list", "--merges", "--max-count=1", "%s..%s" % (base, head)], cwd=repo, isolated=True, pin_hooks=True)
    return proc.returncode != 0 or bool(proc.stdout.strip())


def migration_order_problem(drv: Any, item: Dict[str, Any], head: str, tip: str) -> Optional[str]:
    """A sentence if an `up` migration of this item sorts before one already on the integration tip, else None."""
    for m in item.get("migrations") or []:
        up = m.get("up") if isinstance(m, dict) else None
        if not isinstance(up, str) or "/" not in up or not common.safe_rel_path(up):
            continue
        d, name = up.rsplit("/", 1)
        proc = lgit.run_git(["ls-tree", "--name-only", "-z", tip, d + "/"], cwd=drv.repo, isolated=True, pin_hooks=True)
        if proc.returncode != 0:
            continue
        later = sorted(n.rsplit("/", 1)[-1] for n in proc.stdout.split("\0") if n and n.rsplit("/", 1)[-1] > name)
        if later:
            return "migration %s sorts before %s, which is already on the integration branch: regenerate it so it comes after" % (
                common.clean_text(up, 90), common.clean_text(d + "/" + later[-1], 90))
    return None


def integrate(drv: Any, slot: Any) -> str:
    """Merge one finished item into the integration branch. `drv` is the ParallelDriver (it owns the loop worktree), `slot` the item's slot."""
    try:
        lease = drv.leases.acquire(mutex.INTEGRATE, "integrator:%s" % slot.iid, item=slot.iid)
    except mutex.LeaseHeld:
        return "busy"
    try:
        return _integrate(drv, slot, lease)
    finally:
        drv.leases.release(lease)


def _integrate(drv: Any, slot: Any, lease: Any) -> str:
    from ..driver import _StageDone
    iid = slot.iid
    w = slot.worker
    item = drv.item_def(iid)
    r = drv.rec(iid)
    if r["status"] != "reviewing":
        drv.ev("integrate_skipped", item=iid, status=r["status"])
        slot.ready = False                                       # the state moved under it (a human rejected it, say): the slot follows the state
        drv.settle_by_state(slot, "integrator")
        return "retry"
    drv.ev("integrate_start", item=iid)
    base = drv.item_base.get(iid) or ""
    head = w.head()
    # 2. the lane
    try:
        changed = changed_paths(slot.path, base, head) if base else []
    except Refused as exc:
        w.park(iid, "ESCALATION_NEEDED", str(exc))
        return "parked"
    lie = footprint.undeclared(changed, item)
    if lie:
        drv.ev("footprint_violation", item=iid, paths=lie[:10])
        w.park(iid, "footprint_violation", "the item changed %d file(s) outside the footprint it declared: %s" % (
            len(lie), ", ".join(common.clean_text(p, 60) for p in lie[:3])))
        return "parked"
    if not _holds(drv, slot, lease):
        return "lost"
    # 3. the integration branch moved?
    tip = gitutil.rev_parse("refs/heads/%s^{commit}" % drv.ib, drv.repo, isolated=True)
    if not tip:
        w.park(iid, "ESCALATION_NEEDED", "the integration branch %s has no commit" % drv.ib)
        return "parked"
    rebased = False
    if not gitutil.is_ancestor(tip, head, slot.path, isolated=True):
        if has_merge_commits(slot.path, base or tip, head):
            w.park(iid, "ESCALATION_NEEDED", "item branch contains merge commits; the parallel integrator rebases linear history only")
            return "parked"
        if not rebase(slot.path, tip):
            drv.ev("rebase_conflict", item=iid, onto=tip[:12])
            w.park(iid, "merge_conflict", "rebasing %s onto %s conflicts; a human resolves it" % (slot.branch, drv.ib))
            return "parked"
        head = w.head()
        drv.item_base[iid] = tip
        rebased = True
        drv.ev("rebased", item=iid, onto=tip[:12], head=head[:12])
    # 4. migration order
    problem = migration_order_problem(drv, item, head, tip)
    if problem:
        w.fail(iid, failure.ITEM, "migration_order", (), "verifying", problem)
        drv.settle_failed(slot)
        return "retry"
    # 5. re-verify what was rebased
    if rebased:
        from ..driver import REVIEW_GATES, VERIFY_GATES
        res = w.run_gates(iid, tuple(VERIFY_GATES) + tuple(REVIEW_GATES))
        if w.gate_failure(iid, res, "verifying"):
            drv.settle_failed(slot)
            return "retry"
        if not _holds(drv, slot, lease):
            return "lost"
    if not w.ensure_required(iid, "merged", head):
        drv.settle_failed(slot)
        return "retry"
    # 6. the merge
    hook = HOOKS.get("before_merge")
    if hook is not None:
        hook(drv, slot)
    if not _holds(drv, slot, lease):
        drv.ev("lease_lost", item=iid, where="before the merge")
        return "lost"
    if drv.rec(iid)["status"] != "reviewing":
        return "retry"
    try:
        drv.merge_branch(iid, head)
    except _StageDone:
        return "parked"
    except LoopctlError:
        if drv.rec(iid)["status"] == "merged":
            return "merged"
        raise
    return "merged" if drv.rec(iid)["status"] == "merged" else "retry"


def _holds(drv: Any, slot: Any, lease: Any) -> bool:
    """Renew both leases and say whether they are still this integrator's and this worker's. A lease that cannot be renewed was reclaimed."""
    ok = drv.leases.renew(lease) and drv.leases.holds(lease)
    if ok and slot.lease is not None:
        ok = drv.leases.renew(slot.lease)
    if not ok:
        drv.ev("lease_lost", item=slot.iid, where="integration")
    return ok
