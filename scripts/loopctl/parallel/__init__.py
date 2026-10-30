"""Parallel items (Phase 8; report 11.5 item 3, 10.3, 16.1 row 8, 19.7 L-35 / L-6; DEC-9, DEC-34, D-3, D-6, D-22, D-29). OPT-IN and evidence-gated.

`parallel_mode: worktree` (with `runner: driver`) lets the driver build up to `max_parallel` plan items at the same time, each in its own git
worktree under `.claude/worktrees/`, and merge them one at a time. The serial driver (`loopctl/driver.py`) is untouched in behaviour: a run
with `parallel_mode: off` never imports this package.

    footprint   which items may run together (disjoint `touches.paths`, no shared lane) and whether a finished item stayed in its lane
    mutex       leases: one claim per item, the schema and dependency mutexes, the integration slot; expiry, renewal, reclaiming after a crash
    ports       a block of ports per worker (an offset from `verify_ports`), checked free, never given to two workers
    worktrees   creating and removing a worker's worktree safely (only under `.claude/worktrees/`), `.worktreeinclude`, orphan cleanup
    scheduler   `ParallelDriver`: admits items, runs a `Worker` thread per item, hands finished items to the integrator, drains on a stop
    integrate   the single writer: lease check, footprint check, rebase onto the moved integration branch, re-verify, merge, record
    cascade     the reject cascade (L-6): revert the merged dependents of a rejected item, reset them, invalidate their in-flight descendants

Everything here is called from the driver's process: there is no second program. The lock, the state, the ledger and the guards are the ones
the serial driver uses, so a parallel run is a serial run with more than one worker and one extra rule: only the integrator merges.
"""
from __future__ import annotations

from typing import Any, Dict, List

from kitlib import kitpaths

from .. import common


def enabled(cfg: Any) -> bool:
    """True when the base sha's loop.config asks for parallel items."""
    return str(cfg.get("parallel_mode") or "off") == "worktree"


def max_parallel(cfg: Any) -> int:
    """`max_parallel` clamped to 1..4 (a hostile or mistyped value cannot start fifty workers)."""
    try:
        n = int(cfg.get("max_parallel"))
    except (TypeError, ValueError):
        n = 2
    return max(1, min(4, n))


def rows(ctx: Any) -> List[Dict[str, Any]]:
    """The checks a parallel start makes, as `[{id, ok, message}]` (`loopctl run`'s preflight and `loopctl parallel check` print them). Never writes.
    Nothing here is asked of a run that does not ask for parallel items."""
    res = []  # type: List[Dict[str, Any]]

    def row(rid: str, ok: bool, msg: str) -> None:
        res.append({"id": rid, "ok": bool(ok), "message": msg})

    if str(ctx.cfg.get("pipeline_host") or "conductor") == "workflow":
        row("parallel.pipeline-host", False, "pipeline_host: workflow is design-only (%s; probes V3, V23): use conductor" % kitpaths.kit("spec", "pipeline-host-workflow.md"))
    if not enabled(ctx.cfg):
        row("parallel.mode", True, "parallel_mode is off: one item at a time")
        return res
    row("parallel.mode", True, "parallel_mode is worktree: up to %d item(s) at once" % max_parallel(ctx.cfg))
    row("parallel.runner", str(ctx.cfg.get("runner")) == "driver",
        "runner is driver" if str(ctx.cfg.get("runner")) == "driver" else "parallel_mode: worktree is the driver's scheduler: it needs runner: driver")
    ev = ctx.evidence_passed()
    row("parallel.evidence", ev, "the evidence gate passed" if ev else "the evidence gate has not passed (%s): OPT-IN features refuse to enable (IMP-1)" % common.clean_text(ctx.evidence().reason, 100))
    from . import worktrees
    why = worktrees._escapes(ctx.repo)
    row("parallel.worktrees", why is None, "worktrees go under %s" % worktrees.ROOT_REL if why is None else "worktrees cannot go under %s: %s" % (worktrees.ROOT_REL, why))
    n = int(ctx.cfg.get_int("max_parallel") or 0)
    row("parallel.max", 1 <= n <= 4, "max_parallel is %d" % n if 1 <= n <= 4 else "max_parallel is %d: it must be 1 to 4" % n)
    return res
