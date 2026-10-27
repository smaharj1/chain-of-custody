"""The CI halves of G3 (held-out) and G4 (mutation): what `heldout.yml` and `mutation.yml` run (report section 8.4, section 9, RC-6).

    PYTHONPATH=<kit>/.claude/scripts python3 -m loopctl.gates._ci heldout  --repo DIR --base SHA --dir HELDOUT_DIR
    PYTHONPATH=<kit>/.claude/scripts python3 -m loopctl.gates._ci mutation --repo DIR --base SHA

`<kit>` is the kit EXTRACTED FROM THE BASE SHA by the workflow, never the pull request's copy. Config and plan are read at `--base`, so a pull
request cannot change what is run or what is required (CO-11). The branch name (`HEAD_REF` in the environment, untrusted text, matched
against a strict pattern) picks the plan item; a pull request that is not an item branch has no held-out tests to run.

Output is GitHub workflow annotations. For the held-out job they carry **only criterion ids and counts**: the bot that wrote the code has no
`actions: read`, sees the check result and the failing ids, and must never learn what the tests assert (RC-6). Test names, assertion text and
the harness's own output are never printed; the harness's output is parsed here and dropped.

Exit codes: 0 passed (or nothing to run), 1 the gate failed, 2 it could not run (missing held-out tests for an item that declares them is a failure, 1).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from typing import Any, Optional, Sequence

from kitlib import evidence, gitutil

from .. import branches, common
from ..context import Context
from . import g3_acceptance, g4_mutation, _lib


def _annotate(level: str, title: str, message: str) -> None:
    msg = common.clean_text(message, 300).replace("%", "%25").replace("\r", "").replace("\n", " ")
    sys.stdout.write("::%s title=%s::%s\n" % (level, title, msg))


def _item_for(ctx: Any, head_ref: Optional[str]) -> Optional[str]:
    parsed = branches.parse(head_ref or "")
    if not parsed or parsed[0] != "feat":
        return None
    for iid, it in ctx.plan.items.items():
        if it.get("slug") == parsed[1]:
            return iid
    return None


def _base_evidence(repo: str, base: str) -> bool:
    """Whether docs/baseline/evidence.json is complete AT THE BASE (a pull request cannot supply its own evidence)."""
    text = gitutil.show_file(base, evidence.EVIDENCE_PATH, repo, isolated=True)
    if text is None:
        return False
    tmp = tempfile.mkdtemp(prefix="loopctl-ci-evidence-")
    try:
        path = os.path.join(tmp, evidence.EVIDENCE_PATH)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return bool(evidence.check(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _context(args: argparse.Namespace) -> Any:
    ctx = Context.build(args.repo, dict(os.environ), base=args.base, plan="base")
    if not ctx.cfg_trusted or ctx.plan is None or ctx.plan.fatal or not ctx.plan_trusted:
        raise common.Refused("no usable config and plan at the base %s" % args.base[:12])
    return ctx


def cmd_heldout(args: argparse.Namespace) -> int:
    ctx = _context(args)
    iid = _item_for(ctx, args.head_ref or os.environ.get("HEAD_REF"))
    if iid is None:
        _annotate("notice", "heldout", "not an item branch (feat/<slug>-a<n>): no held-out tests to run")
        return 0
    gctx = ctx.gate_context(iid, worktree=ctx.repo)
    spec, _p = _lib.g3_spec(gctx.item)
    root = os.path.realpath(args.dir)
    hdir = os.path.join(root, iid)
    if not spec["heldout"]:
        _annotate("notice", "heldout", "item %s declares no held-out tests (ext.g3.heldout)" % iid)
        return 0
    if os.path.islink(hdir) or not os.path.isdir(hdir) or not os.path.realpath(hdir).startswith(root + os.sep):
        _annotate("error", "heldout", "item %s declares held-out tests but the restored store has none for it: the sync job has not run, or the store lacks the item" % iid)
        return 1
    findings, classes, results, rate = g3_acceptance.run_heldout(gctx, hdir, True)
    failed = sorted(a for a, r in (results or {}).items() if r["verdict"] != "pass")
    for a in failed:
        _annotate("error", "heldout", "%s fails the held-out check" % a)
    for f in findings:
        if f["id"] not in ("g3.heldout-failed",):          # ids and counts only: the harness's own words are not repeated
            _annotate("error", "heldout", "%s (see the gate id; the log is not shown to the bot)" % f["id"])
    if findings:
        _annotate("error", "heldout", "item %s: held-out %s" % (iid, rate or "did not run"))
        return 1
    _annotate("notice", "heldout", "item %s: held-out %s pass" % (iid, rate))
    return 0


def cmd_mutation(args: argparse.Namespace) -> int:
    ctx = _context(args)
    iid = _item_for(ctx, args.head_ref or os.environ.get("HEAD_REF"))
    if iid is None:
        _annotate("notice", "mutation", "not an item branch (feat/<slug>-a<n>): nothing to mutate")
        return 0
    gctx = ctx.gate_context(iid, worktree=ctx.repo)
    from . import mode_for
    mode = mode_for(ctx.cfg, "g4", g4_mutation)
    if mode == "off":
        _annotate("notice", "mutation", "gates.g4 is off at the base: nothing to run")
        return 0
    if not _base_evidence(ctx.repo, ctx.base_sha):
        _annotate("error", "mutation", "G4 is OPT-IN and evidence-gated: docs/baseline/evidence.json is not complete at the base (IMP-1)")
        return 1
    gctx.evidence_passed = True
    res = g4_mutation.run(gctx)
    for f in res.findings:
        _annotate("error" if f["severity"] == "error" else "notice", "mutation", f["message"])
    if res.status == "pass":
        _annotate("notice", "mutation", res.evidence.get("summary", "passed"))
        return 0
    if res.status == "skipped":
        return 0
    if mode == "report":
        _annotate("warning", "mutation", "gates.g4 is report: the result is recorded, not blocking")
        return 0
    return 2 if res.status == "error" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="loopctl.gates._ci", description="CI halves of G3 (held-out) and G4 (mutation).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("heldout", cmd_heldout), ("mutation", cmd_mutation)):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--base", required=True)
        p.add_argument("--head-ref", default=None)
        if name == "heldout":
            p.add_argument("--dir", required=True, help="the restored held-out store (one directory per item id)")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    if args.base.startswith("-") or not gitutil.valid_oid(args.base):
        sys.stderr.write("--base must be a full commit id\n")
        return 2
    try:
        return int(args.fn(args))
    except common.LoopctlError as exc:
        _annotate("error", args.cmd, str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - a CI check that cannot run must not pass
        _annotate("error", args.cmd, "internal error: %s: %s" % (type(exc).__name__, common.clean_text(exc, 120)))
        return 2


if __name__ == "__main__":
    sys.exit(main())
