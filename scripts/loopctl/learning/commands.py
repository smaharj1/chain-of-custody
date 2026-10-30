"""The command line of the learning loop: `retro`, `gaps`, `calibration`, `harness` and `report scan` (Phase 10). `loopctl/cli.py` routes to these.

Classification (the exec guard reads the same table; see `loopctl/cli.py` and `guards/g_exec.py`):

    retro                 READ    milestone numbers
    harness               READ    gate owner metrics, plus an ablation file if given
    gaps                  READ    detect and show; `gaps --write` writes the drafts under the git directory (a write flag)
    calibration report    READ    each critic's precision
    calibration scan      READ    shows the blockers a scan would log; `scan --write` appends them to the calibration log
    calibration record    TIER 1 WRITE   a person's outcome for a logged blocker
    report scan           READ    how well reports conform, and whether `report_trailers: block` is ready

None of them commits, pushes, switches branches, opens a pull request, edits a rule, primer, agent, hook or setting, or starts a process (H6).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from .. import common
from ..common import EXIT_FINDINGS, EXIT_OK, UsageError

__all__ = ["cmd_retro", "cmd_gaps", "cmd_calibration", "cmd_harness", "report_scan", "structural_items"]


def _cli() -> Any:
    from .. import cli
    return cli


def _regular_file_in_repo(repo: str, rel: str) -> str:
    full = os.path.realpath(os.path.join(repo, rel))
    if not (full + os.sep).startswith(os.path.realpath(repo) + os.sep) or os.path.islink(os.path.join(repo, rel)) or not os.path.isfile(full):
        raise UsageError("%s must be a regular file inside the repository" % common.clean_text(rel, 60))
    return full


def structural_items(ctx: Any, data: Any, limit: int = 12) -> List[Dict[str, Any]]:
    """The last merged items, oldest first, each marked structural (a new top-level directory or a dependency manifest changed) and whether its reports
    carried an empty Primer Delta. One diff and one tree listing per item, from the merge commit's first parent."""
    from .. import lgit
    from . import trailers
    rows = []  # type: List[Dict[str, Any]]
    merged = [(r.get("updated") or "", iid, r) for iid, r in data.items.items() if isinstance(r, dict) and r.get("status") == "merged" and r.get("mergeCommit")]
    for _t, iid, r in sorted(merged)[-limit:]:
        mc = str(r["mergeCommit"])
        reps = [x for x in data.reports if x.get("item") == iid]
        if not reps or len(mc) < 7 or not all(c in "0123456789abcdef" for c in mc):
            continue
        diff = lgit.run_git(["diff", "--name-only", "-z", "--no-renames", mc + "^1", mc, "--"], cwd=ctx.repo)
        tree = lgit.run_git(["ls-tree", "--name-only", mc + "^1"], cwd=ctx.repo)
        if diff.returncode != 0 or tree.returncode != 0:
            continue
        why = trailers.structural_change([p for p in diff.stdout.split("\0") if p], tree.stdout.split("\n"))
        rows.append({"id": iid, "structural": why is not None, "why": why, "delta_empty": not any(x.get("primer_delta_text") for x in reps)})
    return rows


def cmd_retro(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("retro", "retro [--milestone M]: the milestone's numbers (flow, failures, rescues, gates, spend, reports, critics, unattended, parallel). Reads only.")
    ap.add_argument("--milestone", default=None, help="a milestone id of the plan (default: every item)")
    ap.add_argument("--no-git", action="store_true", help="skip the per-item diff behind the primer-staleness streak")
    a = ap.parse_args(args)
    from . import data as data_mod, retro
    ctx = cli._ctx(a, env, plan="base")
    d = data_mod.load(ctx)
    if a.milestone and a.milestone not in d.milestones():
        raise UsageError("the plan has no milestone %s (it has: %s)" % (common.clean_text(a.milestone, 30), ", ".join(d.milestones()) or "none"))
    doc = retro.build(d, a.milestone, None if a.no_git else structural_items(ctx, d))
    out.json(doc) if a.json else out.text(retro.render(doc))
    return EXIT_OK


def cmd_gaps(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("gaps", "gaps [--write]: gaps that repeat in the run data, drafted as rule or primer changes. Without --write it only shows them. "
                             "Drafts go under <git-common-dir>/loopctl/gaps/<id>/ and nowhere else; the git and gh commands are printed for YOU to run (H6).")
    ap.add_argument("--milestone", default=None)
    ap.add_argument("--write", action="store_true", help="write the drafts")
    ap.add_argument("--min", type=int, default=None, help="repeat threshold (default: gaps_min_occurrences)")
    a = ap.parse_args(args)
    from . import data as data_mod, gaps
    ctx = cli._ctx(a, env, plan="base")
    d = data_mod.load(ctx, a.milestone)
    n = a.min if a.min is not None else (ctx.cfg.get_int("gaps_min_occurrences") or 2)
    if n < 1:
        raise UsageError("--min must be at least 1")
    found = gaps.detect(d, n)
    doc = {"schema": gaps.SCHEMA, "milestone": a.milestone or "all", "threshold": n, "written": False,
           "gaps": [{k: g[k] for k in ("kind", "key", "count", "items", "domain", "globs")} for g in found]}
    if not found:
        doc["message"] = "nothing repeats at least %d time(s) in the run data" % n
        out.json(doc) if a.json else out.text(doc["message"])
        return EXIT_OK
    integration = ctx.integration_branch() or "main"
    if a.write:
        res = gaps.write_drafts(ctx.rs, found, ctx.repo, integration, n)
        doc.update(written=res["created"], id=res["id"], dir=res["dir"], files=res["files"], rules=res["rules"], commands=res["commands"])
    else:
        did = gaps.draft_id(found)
        drafts = gaps.plan_drafts(found, did)
        doc.update(id=did, would_draft=[x["rel"] for x in drafts])
    if a.json:
        out.json(doc)
        return EXIT_OK
    out.text("%d gap(s) repeat at least %d time(s):" % (len(found), n))
    for g in found:
        out.text("  %-16s %-26s seen %d, items %s" % (g["kind"], common.clean_text(g["key"], 26), g["count"], ", ".join(g["items"][:6]) or "-"))
    if not a.write:
        out.text("would draft (nothing written; add --write): %s" % ", ".join(doc["would_draft"]))
        return EXIT_OK
    out.text(("drafted %s" if doc["written"] else "already drafted (nothing rewritten): %s") % doc["dir"])
    out.text("loopctl has not edited, committed, pushed or opened anything. Read README.md in that directory, fill the <fill> marks, then (you run these):")
    for ln in doc["commands"]:
        out.text("  " + ln)
    return EXIT_OK


def cmd_calibration(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("calibration", "calibration scan|record|report: the critic calibration log (blockers and their outcomes).")
    ap.add_argument("action", choices=("scan", "record", "report"))
    ap.add_argument("--write", action="store_true", help="scan: append the new blockers and derived outcomes to the log")
    ap.add_argument("--blocker", default=None, help="record: the blocker id (bk-...) from `calibration report --json` or the scan")
    ap.add_argument("--outcome", default=None, help="record: fixed | overruled | false_positive")
    ap.add_argument("--engineer-hit", default="unknown", help="record: did an engineer hit the problem? yes | no | unknown")
    ap.add_argument("--by", default=None, help="record: the person who judged it (recorded)")
    ap.add_argument("--note", default="")
    a = ap.parse_args(args)
    from . import calibration, data as data_mod
    ctx = cli._ctx(a, env, plan="base")
    if a.action == "record":
        if a.write:
            raise UsageError("record takes no --write")
        ent = calibration.record(ctx.rs, a.blocker or "", a.outcome or "", a.engineer_hit, a.by or "", a.note)
        out.json(ent) if a.json else out.text("recorded %s for %s by %s" % (ent["outcome"], ent["blocker"], ent["by"]))
        return EXIT_OK
    d = data_mod.load(ctx)
    if a.action == "scan":
        res = calibration.derive(d.reviews, d.calibration)
        doc = {"schema": "loopctl.calibration.scan.v1", "reviews": len(d.reviews), "new_blockers": res["blockers"], "derived_outcomes": res["derived"],
               "open": res["open"], "written": False}
        if a.write:
            calibration.append(ctx.rs, res["entries"])
            doc["written"] = True
        out.json(doc) if a.json else out.text("%d review file(s): %d new blocker(s), %d outcome(s) derived (a later round no longer lists them), %d still open%s" % (
            doc["reviews"], doc["new_blockers"], doc["derived_outcomes"], doc["open"], "" if a.write else " (nothing written; add --write)"))
        return EXIT_OK
    n = d.cfg.get("calibration_min_samples")
    rep = calibration.report(d.calibration, n if isinstance(n, int) and not isinstance(n, bool) else 5)
    if a.json:
        out.json(rep)
        return EXIT_OK
    if not rep["critics"]:
        out.text("no blocker is logged yet: run `loopctl calibration scan --write` after a review")
    for c in rep["critics"]:
        out.text("%-17s %d logged: %d fixed, %d overruled, %d false positive, %d open; precision %s; engineer hit %s -> %s" % (
            c["critic"], c["logged"], c["fixed"], c["overruled"], c["false_positive"], c["open"], c["precision"] if c["precision"] is not None else "n/a",
            c["engineer_hit_rate"] if c["engineer_hit_rate"] is not None else "n/a", c["verdict"]))
    return EXIT_OK


def cmd_harness(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("harness", "harness [--ablation FILE] [--model ID]: the harness review. Each gate's owner metric from real runs and, with an ablation file "
                                "(python3 tools/evals/ablate.py), what switching each part off does to the eval cases. Advice only (docs/harness-review.md).")
    ap.add_argument("--ablation", default=None, help="a kit.ablation.v1 file inside the repository, made by tools/evals/ablate.py")
    ap.add_argument("--model", default="", help="the model release being reviewed (recorded)")
    a = ap.parse_args(args)
    from .. import gates
    from . import data as data_mod, harness
    ctx = cli._ctx(a, env, plan="base")
    d = data_mod.load(ctx)
    metrics, modes = {}, {}  # type: Dict[str, str], Dict[str, str]
    for name in gates.installed():
        mod = gates.get(name)
        metrics[name] = str(getattr(mod, "OWNER_METRIC", ""))
        modes[name] = gates.mode_for(ctx.cfg, name, mod)
    n = ctx.cfg.get_int("harness_min_item_runs") or 10
    rows = harness.gate_rows(d.events, metrics, modes, n)
    ab = None
    if a.ablation:
        full = _regular_file_in_repo(ctx.repo, a.ablation)
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            try:
                ab = common.strict_loads(fh.read(4 << 20), max_depth=8, max_bytes=4 << 20)
            except (ValueError, RecursionError) as exc:
                raise UsageError("the ablation file is not valid JSON: %s" % common.clean_text(exc, 80))
        if not isinstance(ab, dict) or ab.get("schema") != harness.ABLATION_SCHEMA:
            raise UsageError("the ablation file is not %s" % harness.ABLATION_SCHEMA)
    doc = harness.review(rows, ab, a.model)
    if a.json:
        out.json(doc)
        return EXIT_OK
    out.text("harness review%s (advice only; a person changes the harness in a reviewed commit)" % (" for " + doc["model"] if doc["model"] else ""))
    for r in doc["gates"]:
        out.text("  %-8s %-6s %3d run(s) on %2d item(s), %d failed, %d blocked -> %s" % (r["gate"], r["mode"], r["runs"], r["items"], r["failed"], r["blocked"], r["recommendation"]))
        if r["ablation"]:
            out.text("           ablation: %s (%s)" % (r["ablation"]["verdict"], common.clean_text(r["ablation"]["reason"], 100)))
    for p in doc["other_parts"]:
        out.text("  %-26s ablation: %s (%s)" % (p["id"], p["verdict"], common.clean_text(p["reason"], 100)))
    if not doc["ablation_present"]:
        out.text("no ablation file given: run `python3 tools/evals/ablate.py` for each part, then pass its output with --ablation")
    out.text("remove candidates: %s" % (", ".join(doc["candidates"]) or "none"))
    return EXIT_OK


def report_scan(a: Any, env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    from . import data as data_mod, trailers
    ctx = cli._ctx(a, env, plan="base")
    d = data_mod.load(ctx)
    mode = ctx.cfg.get("report_trailers")
    doc = trailers.summarize(d.events, d.reports, mode if mode in ("warn", "block") else "warn")
    if a.json:
        out.json(doc)
    else:
        e, r = doc["events"], doc["reports_on_disk"]
        out.text("report_trailers: %s" % doc["mode"])
        out.text("  driver read %d report(s): %d clean, %d warned, %d failed; clean streak %d of %d" % (
            e["read"], e["by_outcome"]["clean"], e["by_outcome"]["warned"], e["by_outcome"]["failed"], doc["clean_streak"], doc["needed"]))
        out.text("  on disk: %d checked, %d conforming, %d with a form problem, %d with a decision problem" % (r["checked"], r["conforming"], r["soft"], r["hard"]))
        for k, n in sorted(r["kinds"].items()):
            out.text("    %-22s %d" % (k, n))
        out.text("  " + doc["advice"])
    return EXIT_OK if not doc["reports_on_disk"]["hard"] else EXIT_FINDINGS
