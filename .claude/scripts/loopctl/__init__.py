"""loopctl: the deterministic half of the loop (report section 10, IMP-25).

Run it as `python3 .claude/scripts/loopctl <command>`. Python 3.9, standard library only. Git runs as a
subprocess with a fixed argument list, `shell=False`, global config ignored and hooks pinned off
(`kitlib.gitutil`); nothing from a plan, a report or an issue is ever placed on a command line unvalidated.

Modules (the ones other phases import are marked):
    common     errors, exit codes, text hygiene, strict JSON, the clock                       (4b, 4c)
    plan       plan/ schema, DAG, critical path, width, slicing, AC ids, data-loss class       (4b, 4c)
    state      item state machine, refs/loopctl/state/<plan> by compare-and-swap               (4b, 4c)
    lock       the O_EXCL run lock                                                              (4c)
    failure    failure taxonomy and retry caps                                                  (4c)
    ledger     the spend ledger                                                                 (4c)
    gates      the gate plug-in interface and runner (`gates/__init__.py`)                      (4b)
    context    the per-invocation context: repo, run state, base sha, config at the base sha
    branches   feat/dead/blocked branch lifecycle and the orphan sweep
    report     engineer report and JSON trailer parsing
    audit      the PR audit block, built from state and validated with pr_prepare
    render     BUILD_PLAN.md and diagrams
    status     status, --pending-human, the AC x evidence matrix, the bypass log
    reconcile  state against git and PR facts
    abort      abort and rollback before merge to main
    migrate    v1 build-plan.json -> plan/items/*.json
    cli        the command line
    driver     the serial driver, `runner: driver` (Phase 4c; OPT-IN)
    launch     the one module that starts a child session: launch line, profile, environment, process group, orphans
    heartbeat  the driver's lock heartbeat, a child's liveness, the canary
    compact_hook  the SessionStart(compact) hook (a script, not imported)
"""
from __future__ import annotations

VERSION = "0.4.0"
