"""Parallel items (Phase 8; report 11.2, 11.5, 16.1 row 8; IMP-1): `parallel_mode` and `pipeline_host`.

  * `pipeline_host: workflow` is DESIGN-ONLY (the kit's `spec/pipeline-host-workflow.md`; probes V3 and V23). It is an ERROR whether or not the
    evidence gate has passed: there is nothing behind the value to enable.
  * `parallel_mode: worktree` is the driver's scheduler. The evidence gate itself is `config.evidence-gate`; these rows add what only parallel mode
    needs: the driver as runner, a sane `max_parallel`, and a worktrees directory that really is a directory inside the repository.
"""
from __future__ import annotations

import os
from typing import List

from .model import Context, Result, check, failed, passed, skipped, ERROR, WARN


@check("parallel.pipeline-host", "config")
def check_pipeline_host(ctx: Context) -> Result:
    v = str(ctx.cfg("pipeline_host", "conductor") or "conductor")
    if v == "workflow":
        return failed("parallel.pipeline-host", "pipeline_host: workflow is design-only and cannot run (a saved Workflow cannot be launched from the headless profile or from a "
                      "subagent: probes V3 and V23 are unverified)", "set pipeline_host: conductor (the default); see the kit's spec/pipeline-host-workflow.md", severity=ERROR)
    return passed("parallel.pipeline-host", "pipeline_host is %s" % v)


@check("parallel.mode", "config")
def check_parallel_mode(ctx: Context) -> List[Result]:
    if str(ctx.cfg("parallel_mode", "off") or "off") != "worktree":
        return [skipped("parallel.mode", "parallel_mode is off")]
    out = []  # type: List[Result]
    if str(ctx.cfg("runner", "sequential")) != "driver":
        out.append(failed("parallel.mode", "parallel_mode: worktree is the driver's scheduler and needs runner: driver (runner is %s)" % ctx.cfg("runner", "sequential"),
                          "set runner: driver, or parallel_mode: off"))
    n = ctx.cfg("max_parallel", 2)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 4:
        out.append(failed("parallel.mode", "max_parallel is %r: it must be 1 to 4" % (n,), "set max_parallel: 2"))
    wt = os.path.join(ctx.repo, ".claude", "worktrees")
    if os.path.islink(os.path.join(ctx.repo, ".claude")) or os.path.islink(wt):
        out.append(failed("parallel.mode", ".claude or .claude/worktrees is a symbolic link: worktrees would be created somewhere else", "make it a plain directory"))
    rc, so, _ = ctx.git(["check-ignore", "-q", ".claude/worktrees/x"], 10.0)
    if rc != 0:
        out.append(failed("parallel.mode", ".claude/worktrees/ is not in .gitignore (loopctl also excludes it in this clone, but a fresh clone shows the worktrees as untracked)",
                          "add .claude/worktrees/ to .gitignore", severity=WARN))
    return out or [passed("parallel.mode", "parallel_mode: worktree is set up for the driver (max_parallel %s)" % n)]
