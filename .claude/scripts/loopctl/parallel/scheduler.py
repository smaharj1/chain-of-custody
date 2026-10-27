"""The parallel scheduler: `parallel_mode: worktree` (report 11.5 item 3, 10.3, 16.1 row 8; DEC-9, DEC-34; OPT-IN, evidence-gated).

`ParallelDriver` IS the serial driver (`driver.Driver`: the same lock, state, ledger, gates, child launch line, crash recovery, checkpoints and
unattended hooks) with a different `run_items`. The serial driver walks one item through its stages in the loop worktree. This one runs up to
`max_parallel` of them at the same time:

    main thread (the only writer of the loop worktree and the integration branch)
        admit      pick the next eligible items (dependencies merged, footprints disjoint, mutexes free, a port slot free), take their leases,
                   `Loop.begin`, cut `feat/<slug>-a<n>` off the integration tip, `git worktree add`, start a worker thread
        harvest    a worker that ended: ready for the integrator, waiting for a human, parked, discarded, drained or crashed
        integrate  one finished item at a time, in plan order (integrate.py): footprint, rebase, re-verify, merge
        invalidate an item whose ancestor was rejected or reverted (cascade.py)
    one `Worker` thread per item, in that item's worktree: the serial driver's stages, unchanged, for ONE item

A `Worker` is a `Driver` that does not run its own loop: it borrows everything shared from the parent (`__getattr__`) and owns only what is per item
(its worktree as `repo`, a `Context` whose repo is that worktree, its own `Loop`, its environment with the port offset). It never merges, never switches
the loop worktree, never renames a branch: those are the main thread's, because git does not like two writers in one worktree and the report says there is
one writer (D-6).

WHAT A STOP DOES. HALT, a lost run lock and a signal reach every worker through the same `tick` the serial driver uses, so a running child is ended
within one poll and the run exits as the serial driver would. A park or a hard stop of ONE item (it is the item's, not the run's) makes the scheduler DRAIN:
no new item starts, the running workers finish the child they are in and stop, items already finished are still merged, and the run exits 1.
A worker that hits a run-wide stop (the budget, an unattended demotion) hands the stop to the main thread, which drains and raises it.
A crash in a worker is an internal error: the others drain, every worktree goes, and the exception reaches the caller.

MONEY. Every child gets the remaining budget divided by the workers running (DEC-34), and never more than what is not already promised to another child
(`reserved`): the allowances of the children alive at once always add up to no more than what the ledger says is left, so parallel workers cannot spend
past the run budget by launching at the same moment. A worker that finds nothing unreserved waits for another child to end.

EXIT CODES are the serial driver's: 0 complete or stopped on purpose (a checkpoint, an item waiting for a human), 1 parked or stopped, 2 internal error,
3 refused.
"""
from __future__ import annotations

import copy
import math
import os
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kitlib import gitutil, runstate

from .. import branches, common, failure, lgit, plan as plan_mod, reconcile, state as state_mod
from ..common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, LoopctlError, Refused
from .. import driver as driver_mod
from ..driver import MAX_ITERATIONS, Driver, _StageDone, _Stop
from . import cascade, footprint, integrate, max_parallel, mutex, ports, worktrees

__all__ = ["ParallelDriver", "Worker", "Slot"]

MIN_ALLOWANCE = 0.0001


class _Cancelled(Exception):
    """The scheduler cancelled this worker (an ancestor of its item was rejected, or its lease was lost): stop at the next safe point."""


class _Drain(Exception):
    """The run is draining: stop at the next safe point (between children), leave the item as it is."""


def _floor4(x: float) -> float:
    return math.floor(x * 10000) / 10000.0


class Slot(object):
    """One item that holds a worktree: running, or finished and waiting for the integrator."""

    def __init__(self, iid: str, attempt: int, branch: str, path: str, leases: List[mutex.Lease], block: ports.Block, mutexes: List[mutex.Lease]) -> None:
        self.iid, self.attempt, self.branch, self.path = iid, attempt, branch, path
        self.lease = leases[0]                       # the item's own lease
        self.leases = leases + mutexes + [block.lease]
        self.mutexes = mutexes
        self.block = block
        self.worker = None  # type: Any
        self.thread = None  # type: Optional[threading.Thread]
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.result = None  # type: Optional[Dict[str, Any]]
        self.harvested = False
        self.ready = False
        self.lost = 0
        self.started = time.time()


class Worker(Driver):
    """The serial driver's stages for one item, in that item's worktree. Not started with `Driver.__init__`: it borrows the run's shared state."""

    def __init__(self, parent: "ParallelDriver", slot: Slot) -> None:  # noqa: D107 - deliberately not calling Driver.__init__
        self.parent = parent
        self.slot = slot
        self.only_item = slot.iid
        self.repo = slot.path
        ctx = copy.copy(parent.ctx)                  # the same run, base sha, config and plan; its repo is this worktree
        ctx.repo = slot.path
        env = dict(parent.env)
        env.update(slot.block.env())
        env["KIT_ITEM"] = slot.iid
        ctx.env = env
        self.ctx = ctx
        self.env = env
        self.loop = state_mod.Loop(ctx, require_tier2=False)         # its own store; no branch side effects (the scheduler owns branches)
        self._cache = None
        self.ready_head = None  # type: Optional[str]

    def __getattr__(self, name: str) -> Any:
        if name in ("parent", "__setstate__", "__getstate__"):
            raise AttributeError(name)
        return getattr(self.parent, name)

    # -- stopping --------------------------------------------------------------------------------------------------------------------------
    def check_stop(self) -> None:
        Driver.check_stop(self)                      # HALT, the lock, a signal, the supervisor: these win
        if self.slot.cancel.is_set():
            raise _Cancelled()
        if self.parent.drain_event.is_set():
            raise _Drain()

    def sleep(self, seconds: float) -> None:
        """The backoff, in slices, ended early by a drain or a cancel as well as by HALT, a lost lock or a signal."""
        def tick() -> Optional[str]:
            return self.parent.tick() or ("drain" if self.parent.drain_event.is_set() or self.slot.cancel.is_set() else None)
        (driver_mod.SLEEP or driver_mod._default_sleep)(seconds, tick)
        self.check_stop()

    # -- the branch and the worktree belong to the scheduler -------------------------------------------------------------------------
    def shelve_branch(self, iid: str) -> None:
        return None

    def shelve_branch_if_current(self, iid: str) -> None:
        return None

    def begin(self, iid: str) -> None:
        raise Refused("a worker never begins an item: the scheduler does, before it starts the worker")

    # -- money --------------------------------------------------------------------------------------------------------------------------
    def child_allowance(self, led: Any, iid: str) -> float:
        return self.parent.allowance_for(self, led, iid)

    def child_ended(self, iid: str) -> None:
        self.parent.release_allowance(iid)

    def count_child(self) -> None:
        self.parent.count_child()

    # -- the item ----------------------------------------------------------------------------------------------------------------------
    def process_item(self, iid: str) -> str:
        handlers = {"designing": self.stage_designing, "design_review": self.stage_design_review, "stubbing": self.stage_stubbing,
                    "building": self.stage_building, "verifying": self.stage_verifying, "reviewing": self.stage_reviewing}
        for _ in range(MAX_ITERATIONS):
            self.check_stop()
            status = self.rec(iid)["status"]
            if status in handlers:
                try:
                    handlers[status](iid)
                except _StageDone:
                    pass
                if self.ready_head:
                    return "ready"
            elif status == "merged":
                return "merged"
            elif status == "pending":
                return "retry"
            elif status in ("awaiting_human", "pr_open"):
                return "awaiting"
            else:
                return "parked"
        raise _Stop("runaway", "item %s went round its stages %d times: stopping" % (iid, MAX_ITERATIONS), EXIT_USAGE)

    def finish_item(self, iid: str, head: str) -> None:
        """Reviewed and green. An item that needs a human waits for one, as in the serial driver; any other is READY: the integrator merges it."""
        r = self.rec(iid)
        item = self.item_def(iid)
        gate = list(r.get("humanGate") or []) or plan_mod.item_human_gate(item, self.cfg.get_list("always_human_paths"))
        infra = "infra" in ((item.get("touches") or {}).get("domains") or [])
        if gate or infra or str(self.cfg.get("delivery") or "local") != "local":
            Driver.finish_item(self, iid, head)
            return
        if not self.ensure_required(iid, "merged", head):
            return
        self.ready_head = head


class ParallelDriver(Driver):
    def __init__(self, ctx: Any, loop: Any, run_record: Dict[str, Any], as_json: bool, out: Any = None) -> None:
        Driver.__init__(self, ctx, loop, run_record, as_json, out)
        self.max_parallel = max_parallel(self.cfg)
        self.leases = mutex.LeaseStore(self.rs, ttl_s=float(self.cfg.get_int("lease_ttl_s") or 300))
        self.ports = ports.PortAllocator(self.leases, [int(p) for p in self.cfg.get_list("verify_ports") if isinstance(p, int) and not isinstance(p, bool)])
        self.slots = {}  # type: Dict[str, Slot]
        self.pending_spawn = []  # type: List[Slot]
        self.wake = threading.Condition()
        self.rlock = threading.RLock()
        self.reserved = {}  # type: Dict[str, float]
        self.drain_event = threading.Event()
        self.draining = False
        self.drain_reason = ""
        self.cp_pending = False
        self.stop_exc = None  # type: Optional[_Stop]
        self.crash = None  # type: Optional[BaseException]
        self.invalidating = set()  # type: set
        self.cascades = {}  # type: Dict[str, int]
        self.waits = {}  # type: Dict[str, str]
        self.index = {}  # type: Dict[str, int]
        self._last_renew = 0.0
        self.include = []  # type: List[str]

    # -- what the rest of the system reads --------------------------------------------------------------------------------------------
    def current_items(self) -> List[str]:
        """The items in progress, for `unattended.json` (`items`; the old `item` key still names the newest)."""
        return sorted(self.slots, key=common.natural_key)

    def running_workers(self) -> int:
        return sum(1 for s in list(self.slots.values()) if not s.done.is_set())

    # -- setup and recovery -------------------------------------------------------------------------------------------------------------
    def setup(self) -> None:
        worktrees.ensure_excluded(self.repo)
        if not self.ctx.evidence_passed():
            raise Refused("parallel_mode: worktree needs the evidence gate (%s): OPT-IN features refuse to enable (IMP-1)" % common.clean_text(self.ctx.evidence().reason, 100))
        Driver.setup(self)
        names, bad = worktrees.parse_include(worktrees.read_include(self.repo, self.anchor) or "")
        self.include = names
        for b in bad:
            self.say("warning: .worktreeinclude line %r is not honoured (only %s are copied)" % (b, ", ".join(worktrees.ALLOWED_INCLUDE)))
        order = self.loop._order()
        self.index = {i: k for k, i in enumerate(order)}
        self.ev("parallel", max_parallel=self.max_parallel, ports=self.ports.base, include=names)
        self.say("parallel: up to %d item(s) at once, worktrees under %s" % (self.max_parallel, worktrees.ROOT_REL))

    def recover(self) -> None:
        """A crash leaves leases, worktrees and branches. The leases of dead owners are reclaimed (a live owner means another driver is working in this
        repository: refuse), the worktrees are removed with their work saved on the item's branch, and then the serial recovery runs: a merge that never
        reached the state, items in flight back to pending, orphan branches renamed to dead/*."""
        gone = self.leases.reap()
        for n in gone:
            self.ev("lease_reaped", lease=n)
        left = [r for r in self.leases.list() if not isinstance(r.get("meta"), dict) or r["meta"].get("kind") != "hook"]      # a WorktreeCreate hook's port slot is not a driver's
        if left:
            raise Refused("lease(s) %s are held by a live owner or cannot be read: another driver may be working in this repository (`loopctl parallel leases`; "
                          "`loopctl parallel cleanup --apply` removes unreadable ones when no driver runs)" % ", ".join(sorted(str(r.get("name")) for r in left)[:4]))
        worktrees.ensure_excluded(self.repo)
        for p in worktrees.cleanup_orphans(self.repo):
            self.say("recovery: removed the worktree %s a previous run left (its work is on the item's branch)" % os.path.basename(p))
            self.ev("worktree_removed", worktree=os.path.basename(p), why="orphan")
        Driver.recover(self)

    def recover_merge(self) -> None:
        """Only the merge part of recovery: other workers are alive, so nothing in flight is reset and no worktree or lease is touched."""
        rep = reconcile.scan(self.ctx, self.state(), self.plan)
        reconcile.apply(self.loop, rep, by="driver")

    # -- money (DEC-34) -------------------------------------------------------------------------------------------------------------------
    def allowance_for(self, worker: Worker, led: Any, iid: str) -> float:
        """The `--max-budget-usd` for a worker's next child: the remaining budget divided by the workers running, and no more than what no other live child
        has been promised. Waits (checking for a stop) while that is nothing and another child is running."""
        while True:
            with self.rlock:
                v = led.verdict(self.cfg, self.run_id, iid)
                if not v.ok:
                    raise Refused("cannot launch: " + "; ".join(v.reasons))
                if v.remaining is None:
                    raise Refused("cannot launch: run_budget_usd is not set (0). Set a ceiling in loop.config; there is no projection before a baseline")
                share = _floor4(v.remaining / max(1, self.running_workers()))
                others = [a for k, a in self.reserved.items() if k != iid]
                a = min(share, _floor4(max(0.0, v.remaining - sum(others))))
                if a >= MIN_ALLOWANCE or not others:
                    self.reserved[iid] = a
                    return a
            with self.wake:
                self.wake.wait(timeout=max(0.02, self.timing.poll_s))
            worker.check_stop()

    def release_allowance(self, iid: str) -> None:
        with self.rlock:
            self.reserved.pop(iid, None)
        with self.wake:
            self.wake.notify_all()

    def count_child(self) -> None:
        with self.rlock:
            self.child_n += 1

    def budget_stop(self, v: Any, iid: Optional[str] = None) -> None:
        """A run-wide ceiling reached by the main thread: the items belong to their workers, so nothing is parked here; the run parks and drains."""
        code = v.codes[0] if v.codes else "budget_run"
        why = "; ".join(v.reasons)[:200]
        self.set_header("run parked: %s" % code, runStatus="parked", parked={"item": iid, "reason": code, "at": common.iso()})
        self.ev("budget_stop", code=code, reasons=v.reasons[:2], spent=v.spent)
        raise _Stop("budget", why, EXIT_FINDINGS)

    # -- workers ------------------------------------------------------------------------------------------------------------------------------
    def _run_worker(self, slot: Slot) -> None:
        res = {"item": slot.iid, "outcome": None}  # type: Dict[str, Any]
        try:
            res["outcome"] = slot.worker.process_item(slot.iid)
        except _Cancelled:
            res["outcome"] = "cancelled"
        except _Drain:
            res["outcome"] = "drained"
        except _Stop as s:
            res["outcome"], res["stop"] = "stop", s
        except BaseException as exc:  # noqa: BLE001 - a worker that dies must still be counted, or the run waits for it for ever
            res["outcome"], res["exc"] = "crash", exc
            res["trace"] = traceback.format_exc(limit=6)
        finally:
            slot.result = res
            slot.done.set()
            with self.wake:
                self.wake.notify_all()

    def spawn(self, slot: Slot) -> None:
        slot.done.clear()
        slot.result = None
        slot.harvested = False
        t = threading.Thread(target=self._run_worker, args=(slot,), name="worker-%s" % slot.iid, daemon=True)
        slot.thread = t
        t.start()

    def respawn(self, slot: Slot) -> None:
        """Back to an engineer: the same worktree, a fresh `Worker` (its pass counters live in the parent)."""
        slot.ready = False
        slot.worker = Worker(self, slot)
        self.spawn(slot)

    def settle_failed(self, slot: Slot) -> None:
        """The integrator recorded a failure for this item: it is back with an engineer, or discarded, or parked."""
        status = self.rec(slot.iid)["status"]
        if status in state_mod.ACTIVE:
            self.respawn(slot)
        else:
            self.settle_by_state(slot, "integrator")

    # -- admission ---------------------------------------------------------------------------------------------------------------------------
    def occupying(self, st: Any) -> List[str]:
        """Items whose footprint is spoken for: the ones holding a worktree, and the ones waiting for a human (their branch will still be merged)."""
        return sorted(set(self.slots) | set(st.by_status("awaiting_human", "pr_open")), key=common.natural_key)

    def admit(self, st: Any, order: Sequence[str], deps: Dict[str, List[str]]) -> int:
        """Start every eligible item that fits, in plan order. An item skipped for a conflict also holds back later items that conflict with it, so a
        stream of small items cannot starve a large one."""
        started = 0
        occ = self.occupying(st)
        skipped = []  # type: List[str]
        now = common.now()
        for iid in st.eligible(order, deps):
            if len(self.slots) >= self.max_parallel:
                break
            if iid in self.slots:
                continue                                    # its worker has just ended (a discard): harvest settles it before it can start again
            item = self.item_def(iid)
            why = None  # type: Optional[str]
            for other in occ + skipped:
                why = footprint.conflict(item, self.item_def(other))
                if why:
                    why = "%s (%s %s)" % (why, "running" if other in occ else "waiting first", other)
                    break
            nb = runstate.parse_iso(str(st.items[iid].get("notBefore") or ""))
            if why is None and nb is not None and nb > now:
                continue                                    # a platform backoff: it does not hold anything back
            if why is not None:
                skipped.append(iid)
                if self.waits.get(iid) != why:
                    self.waits[iid] = why
                    self.ev("admit_wait", item=iid, why=common.clean_text(why, 160))
                continue
            missing = [n for n in item.get("requiresEnv") or [] if isinstance(n, str) and not self.env.get(n)]
            if missing:
                self.loop.park(iid, "needs_provisioning", "missing environment: %s" % ", ".join(missing[:5]), self.sid, "driver", "blocked")
                self.say("item %s needs %s before it can start" % (iid, ", ".join(missing[:5])))
                self.note_park(iid)
                continue
            if self.start_item(iid):
                occ.append(iid)
                started += 1
            else:
                skipped.append(iid)
        for slot in self.pending_spawn:
            self.spawn(slot)
        self.pending_spawn = []
        return started

    def start_item(self, iid: str) -> bool:
        """Claim the item (lease, mutexes, a port slot), begin it in the state, cut its branch, make its worktree, start its worker. All or nothing."""
        item = self.item_def(iid)
        owner = "worker:%s" % iid
        got = []  # type: List[mutex.Lease]
        block = None  # type: Optional[ports.Block]
        try:
            ilease = self.leases.acquire(mutex.item_lease(iid), owner, item=iid)
            got.append(ilease)
            mus = mutex.acquire_mutexes(self.leases, owner, sorted(footprint.lanes(item)), item=iid)
            got += mus
            block = self.ports.allocate(owner, item=iid)
            if block is None:
                raise mutex.LeaseHeld("no free port slot (%s)" % ("; ".join("slot %d: port(s) %s busy" % (s, ",".join(str(p) for p in ps)) for s, ps in self.ports.skipped) or "all taken"))
        except mutex.LeaseHeld as exc:
            mutex.release_all(self.leases, got)
            if self.waits.get(iid) != str(exc):
                self.waits[iid] = str(exc)
                self.ev("admit_wait", item=iid, why=common.clean_text(exc, 160))
            return False
        try:
            rec = self.loop.begin(iid, self.sid, "driver", max_active=self.max_parallel)
        except LoopctlError:
            self.ports.release(block)
            mutex.release_all(self.leases, got)
            raise
        tip = gitutil.rev_parse("refs/heads/%s^{commit}" % self.ib, self.repo, isolated=True) or ""
        self.item_base[iid] = tip
        branch = rec["branch"]
        path = ""
        try:
            if not tip:
                raise Refused("the integration branch %s has no commit" % self.ib)
            branches.create(self.repo, branch, tip)
            path = worktrees.create(self.repo, worktrees.loop_name(item["slug"], rec["attempt"]), branch)
            worktrees.apply_include(self.repo, path, self.include)
        except LoopctlError as exc:
            self.loop.park(iid, "ESCALATION_NEEDED", "could not create the item's worktree: %s" % common.clean_text(exc, 120), self.sid, "driver")
            if path:
                try:
                    worktrees.remove(self.repo, path, save=False)
                except LoopctlError:
                    pass
            self.ports.release(block)
            mutex.release_all(self.leases, got)
            self.shelve_branch(iid)
            self.note_park(iid)
            return False
        slot = Slot(iid, rec["attempt"], branch, path, got[:1], block, got[1:])
        self.slots[iid] = slot
        slot.worker = Worker(self, slot)
        self.waits.pop(iid, None)
        self.ev("begin", item=iid, attempt=rec["attempt"], branch=branch)
        self.ev("admit", item=iid, worktree=os.path.basename(path), slot=block.slot, offset=block.offset, workers=len(self.slots))
        self.say("item %s: attempt %d on %s in %s (port slot %d, %d at once)" % (iid, rec["attempt"], branch, os.path.relpath(path, self.repo), block.slot, len(self.slots)))
        self.pending_spawn.append(slot)                # started by `admit` once every item of this round has its slot: the allowance divides by all of them
        return True

    # -- ending a slot -------------------------------------------------------------------------------------------------------------------------
    def release_slot(self, slot: Slot) -> None:
        self.ports.release(slot.block)
        mutex.release_all(self.leases, slot.mutexes)
        self.leases.release(slot.lease)
        self.slots.pop(slot.iid, None)
        self.release_allowance(slot.iid)

    def retire(self, slot: Slot, mode: str) -> None:
        """Take a slot out of service. `mode`: park (the branch becomes blocked/*), dead (dead/*), merged (the branch is deleted), keep, drain (the
        branch stays feat/* for the next start's recovery). The worktree goes first (its uncommitted work is saved on the branch); a problem is an event, not a stop."""
        iid = slot.iid
        if self.slots.get(iid) is not slot:
            return
        try:
            worktrees.remove(self.repo, slot.path, save=mode != "merged")
        except LoopctlError as exc:
            self.ev("worktree_remove_failed", item=iid, message=common.clean_text(exc, 160))
        try:
            if mode == "park":
                Driver.shelve_branch(self, iid)
            elif mode == "dead":
                cascade._dead_branch(self.ctx, slot.branch)
            elif mode == "merged":
                branches.delete_merged(self.ctx, slot.branch, self.ib)
        except LoopctlError as exc:
            self.ev("branch_settle_failed", item=iid, mode=mode, message=common.clean_text(exc, 160))
        self.release_slot(slot)

    def note_park(self, iid: str) -> None:
        if not self.draining:
            self.draining, self.drain_reason = True, "parked"
            self.ev("drain", why="parked", item=iid)
            self.say("item %s is parked: no new item starts; the others finish and are merged" % iid)

    def settle_by_state(self, slot: Slot, outcome: str) -> None:
        """What the state says about an item decides what happens to its slot."""
        iid = slot.iid
        try:
            status = self.state().items[iid]["status"]
        except (LoopctlError, KeyError):
            self.retire(slot, "drain")
            return
        if status == "pending":
            self.retire(slot, "dead")
        elif status in state_mod.RESTING:
            self.retire(slot, "park")
            self.note_park(iid)
        elif status in ("awaiting_human", "pr_open"):
            self.retire(slot, "keep")
        elif status == "merged":
            self.retire(slot, "merged")
        elif outcome == "ready" and status == "reviewing":
            slot.ready = True
        elif outcome == "cancelled":
            self.reset_invalid(iid, slot)
        else:
            self.retire(slot, "drain")

    def harvest(self) -> None:
        for slot in list(self.slots.values()):
            if not slot.done.is_set() or slot.harvested:
                continue
            slot.harvested = True
            res = slot.result or {}
            outcome = str(res.get("outcome"))
            if outcome == "stop" and self.stop_exc is None:
                self.stop_exc = res["stop"]
                self.start_drain("stop")
            elif outcome == "crash" and self.crash is None:
                self.crash = res["exc"]
                self.ev("worker_crash", item=slot.iid, error=common.clean_text(repr(res["exc"]), 160))
                self.start_drain("crash")
            self.settle_by_state(slot, outcome)

    def start_drain(self, why: str) -> None:
        if not self.drain_event.is_set():
            self.drain_event.set()
            self.draining = True
            self.drain_reason = self.drain_reason or why
            with self.wake:
                self.wake.notify_all()

    # -- the reject cascade ------------------------------------------------------------------------------------------------------------------------
    def invalidate(self, st: Any) -> None:
        """An item in progress whose ancestor is no longer merged was built on something that is gone: stop its worker and put it back."""
        for iid in cascade.invalid_inflight(st, self.plan.items):
            if iid in self.invalidating:
                continue
            self.invalidating.add(iid)
            self.cascades[iid] = self.cascades.get(iid, 0) + 1
            slot = self.slots.get(iid)
            self.ev("invalidate", item=iid, count=self.cascades[iid])
            if self.cascades[iid] > cascade.MAX_CASCADES:
                self.say("item %s was invalidated %d times: it is parked for a human" % (iid, self.cascades[iid]))
                try:
                    self.loop.park(iid, "cascade_loop", "invalidated %d times in this run" % self.cascades[iid], self.sid, "driver")
                except LoopctlError:
                    pass
                if slot is not None and not slot.done.is_set():
                    slot.cancel.set()
                elif slot is not None:
                    self.settle_by_state(slot, "cancelled")
                self.note_park(iid)
                continue
            self.say("item %s was built on an item that is no longer merged: it goes back to pending" % iid)
            if slot is not None and not slot.done.is_set():
                slot.cancel.set()                              # the worker stops at its next safe point; harvest resets the item
            else:
                self.reset_invalid(iid, slot)

    def reset_invalid(self, iid: str, slot: Optional[Slot]) -> None:
        old = self.rec(iid).get("branch")

        def mutate(st: Any) -> None:
            if st.items[iid]["status"] in state_mod.IN_FLIGHT:
                cascade.reset_item(st, iid, "driver", "invalidated: an ancestor is no longer merged")
        self.loop.store.update(mutate, "invalidate %s" % iid)
        self.ev("invalidated", item=iid)
        if slot is not None:
            self.retire(slot, "dead")
        else:
            try:
                cascade._dead_branch(self.ctx, old)
            except LoopctlError as exc:
                self.ev("branch_settle_failed", item=iid, mode="dead", message=common.clean_text(exc, 160))
        self.invalidating.discard(iid)

    # -- leases ------------------------------------------------------------------------------------------------------------------------------------
    def renew_leases(self) -> None:
        now = common.now()
        if now - self._last_renew < max(1.0, self.leases.ttl_s / 5.0):
            return
        self._last_renew = now
        for slot in list(self.slots.values()):
            lost = self.leases.renew_all(slot.leases)
            if lost:
                self.ev("lease_lost", item=slot.iid, leases=lost)
                self.invalidating.add(slot.iid)
                slot.cancel.set()

    # -- the loop ------------------------------------------------------------------------------------------------------------------------------------
    def integrate_one(self) -> bool:
        ready = sorted((s for s in self.slots.values() if s.ready and s.done.is_set()), key=lambda s: self.index.get(s.iid, 10 ** 6))
        if not ready:
            return False
        slot = ready[0]
        out = integrate.integrate(self, slot)
        if out == "merged":
            self.merged_now.append(slot.iid)
            self.retire(slot, "merged")
            self.say("item %s merged" % slot.iid)
            if not self.cp_pending and self.checkpoint_due(slot.iid):
                self.cp_pending = True
                self.draining, self.drain_reason = True, "checkpoint"
            return True
        if out == "parked":
            self.settle_by_state(slot, "parked")
            return True
        if out == "retry":
            return True
        if out == "lost":
            slot.lost += 1
            if slot.lost > 3:
                try:
                    self.loop.park(slot.iid, "lease_lost", "the integration lease was lost %d times" % slot.lost, self.sid, "driver")
                except LoopctlError:
                    pass
                self.settle_by_state(slot, "parked")
                return True
        return False

    def drain_code(self) -> int:
        if self.drain_reason == "checkpoint":
            return EXIT_OK
        self.outcome = "parked"
        return EXIT_FINDINGS

    def run_items(self) -> int:
        order = self.loop._order()
        deps = self.loop._deps()
        while True:
            self.check_stop()
            self.renew_leases()
            st = self.state()
            hdr = st.header
            if hdr.get("runStatus") in ("stopped", "aborted"):
                self.start_drain("stopped")
                self.join_workers()
                self.harvest()
                raise _Stop("stopped", "the run is %s (%s): a human clears it with `loopctl approve breaker`" % (
                    hdr.get("runStatus"), (hdr.get("parked") or {}).get("reason")), EXIT_FINDINGS)
            if not self.cp_pending:
                gate = self.pending_checkpoint(st)
                if gate == "wait":
                    p = hdr.get("parked") or {}
                    raise _Stop("checkpoint", "waiting for the checkpoint after %s: `loopctl approve checkpoint %s --by <you>`" % (p.get("item"), p.get("item")), EXIT_REFUSED)
                if gate == "stop":
                    self.outcome = "stopped_by_human"
                    self.say("a human chose to stop at the checkpoint")
                    return EXIT_OK
            v = self.ctx.ledger().verdict(self.cfg, self.run_id)
            if not v.ok:
                self.start_drain("budget")
                self.join_workers()
                self.harvest()
                self.budget_stop(v)
            self.harvest()
            if self.stop_exc is not None or self.crash is not None:
                self.join_workers()
                self.harvest()
                if self.crash is not None:
                    raise self.crash
                raise self.stop_exc  # type: ignore[misc]
            st = self.state()
            self.invalidate(st)
            ready_approved = self.awaiting_with_approval(st)
            if ready_approved:
                try:
                    self.merge_approved(ready_approved)
                except _StageDone:
                    self.start_drain("parked")
                    self.outcome = "parked"
                    continue
                if not self.cp_pending and self.checkpoint_due(ready_approved):
                    self.cp_pending = True
                    self.draining, self.drain_reason = True, "checkpoint"
                continue
            if self.integrate_one():
                continue
            if not self.draining:
                self.admit(self.state(), order, deps)
                st = self.state()
            running, ready = self.running_workers(), [s for s in self.slots.values() if s.ready]
            if running == 0 and not ready:
                if self.draining:
                    return self.drain_code()
                waiting = st.by_status("awaiting_human", "pr_open")
                eligible = [i for i in st.eligible(order, deps)]
                if not eligible:
                    if waiting:
                        self.outcome = "awaiting_human"
                        self.say("item %s is waiting for a human (`loopctl status --pending-human`); approve it and run again" % waiting[0])
                        return EXIT_OK
                    return self.end_of_plan(st)
                backoff = self.earliest_backoff(st, eligible)
                if backoff is not None:
                    self.say("every eligible item is backing off; waiting %ds" % (int(backoff) + 1))
                    self.sleep(backoff + 0.5)
                    continue
                if waiting:
                    self.outcome = "awaiting_human"
                    self.say("item %s is waiting for a human and the rest conflict with it: approve it and run again" % waiting[0])
                    return EXIT_OK
                raise _Stop("stuck", "eligible item(s) %s cannot start: %s" % (", ".join(eligible[:4]), common.clean_text("; ".join(sorted(set(self.waits.values()))[:2]) or "nothing says why", 200)), EXIT_FINDINGS)
            with self.wake:
                self.wake.wait(timeout=max(0.01, self.timing.poll_s))

    def earliest_backoff(self, st: Any, eligible: Sequence[str]) -> Optional[float]:
        now = common.now()
        waits = []  # type: List[float]
        for i in eligible:
            nb = runstate.parse_iso(str(st.items[i].get("notBefore") or ""))
            if nb is not None and nb > now:
                waits.append(nb - now)
        return min(waits) if waits and len(waits) == len(eligible) else None

    # -- the end ----------------------------------------------------------------------------------------------------------------------------------------
    def join_workers(self, deadline_s: Optional[float] = None) -> None:
        """Wait for the workers to end. They stop at their next safe point (`drain_event`), or at once when a child is ended by HALT, a lost lock or a signal."""
        end = time.time() + (deadline_s if deadline_s is not None else max(30.0, self.timing.term_grace_s + 20.0))
        while time.time() < end:
            alive = [s for s in list(self.slots.values()) if s.thread is not None and s.thread.is_alive()]
            if not alive:
                return
            for s in alive:
                s.thread.join(timeout=0.05)

    def settle_worktree(self) -> None:
        """However the run ended: the workers stop, every worktree goes (its work is saved on the item's branch), every lease is given back; then the
        loop worktree is put right as the serial driver does."""
        try:
            self.start_drain("end")
            self.join_workers()
            for slot in list(self.slots.values()):
                if slot.thread is not None and slot.thread.is_alive():
                    continue                                   # still running after the wait: leave it; the next start's recovery cleans up
                slot.ready = False
                try:
                    self.settle_by_state(slot, "drained")
                except Exception:  # noqa: BLE001 - best effort on the way out; the next start's recovery handles whatever is left
                    pass
        except Exception:  # noqa: BLE001
            pass
        Driver.settle_worktree(self)
