"""`reconcile`: put the state back in line with git and with the PR facts (report section 10.2; CS-20; L-3, L-4, L-5).

    reconcile --dry-run [--prs FILE] [--rule-suites FILE]     report what would change; writes nothing
    reconcile --apply   [--prs FILE] [--rule-suites FILE]     do it, as one compare-and-swap of the state plus the branch renames

What it looks at:

1. MERGED items. The recorded merge commit must exist and be an ancestor of the integration branch. Otherwise a human moved
   or rewrote history under the loop: the item goes to `needs_human` (`merge_missing`). This is also the check that exposes a
   forged `merged` entry in the state ref.
2. Crash recovery (L-4). An item the state does not call merged, but whose merge commit is on the integration branch with the
   exact trailer `Loop-Item: <id>` and the branch name `feat/<slug>-a<n>` in its subject, is recovered as merged. A trailer
   with no matching branch name is reported as a possible forgery and NOT applied.
3. Human reverts (L-5). A commit saying "This reverts commit <merge commit>" on the integration branch sends the item to
   `needs_human` (`human_revert`). It is never silently re-run.
4. Orphans (L-3). In-flight items with no live lock holder go back to `pending` (V1 discards partial work). `feat/*` branches no
   live item owns are renamed `dead/*`; `dead/*` older than `orphan_retention_days` are pruned; `blocked/*` is never touched.
   Worktrees with no live state are reported, not removed.
5. PR and check overlay (L3+, RC-15), from recorded `gh` JSON (`--prs`; `--live` runs `gh` for a human): authoritative for
   in_review, merged and ci_failed. A merged PR merges the item (the merge commit must exist locally, else the overlay is stored
   and a fetch is requested); a PR closed unmerged is `needs_human` (`pr_closed`).
6. The bypass log (RC-9), from recorded ruleset rule-suite JSON (`--rule-suites`): every evaluation whose result is `bypass`.
7. State items that are not in the plan.

The merge commits it trusts are the ones git shows on the integration branch; nothing in a PR or a commit message can make an
item merged unless the commit is really there.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from kitlib import gitutil

from . import lgit, branches, common, state as state_mod
from .common import Finding

__all__ = ["Report", "scan", "apply", "normalize_prs", "live_prs_argv"]

MAX_SCAN_COMMITS = 3000


class Report(object):
    def __init__(self) -> None:
        self.findings = []  # type: List[Finding]
        self.actions = []  # type: List[Dict[str, Any]]
        self.applied = []  # type: List[str]

    def find(self, sev: str, fid: str, msg: str, item: Optional[str] = None) -> None:
        self.findings.append(Finding(sev, fid, msg, item))

    @property
    def drift(self) -> bool:
        return bool(self.actions)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "loopctl.reconcile.v1", "drift": self.drift, "actions": self.actions, "applied": self.applied,
                "findings": [f.to_dict() for f in self.findings]}


def live_prs_argv() -> List[str]:
    """The `gh` command `--live` runs (human only). Fixed argv; nothing from the plan is interpolated."""
    return ["gh", "pr", "list", "--state", "all", "--limit", "200", "--json",
            "number,state,isDraft,headRefName,url,mergeCommit,statusCheckRollup,mergedAt,title"]


def normalize_prs(raw: Any) -> List[Dict[str, Any]]:
    """Recorded `gh pr list --json ...` output -> [{number, state, draft, head, url, merge_commit, checks}]."""
    out = []  # type: List[Dict[str, Any]]
    if not isinstance(raw, list):
        return out
    for p in raw[:500]:
        if not isinstance(p, dict) or not isinstance(p.get("number"), int):
            continue
        st = str(p.get("state", "")).upper()
        if st not in ("OPEN", "MERGED", "CLOSED"):
            continue
        mc = p.get("mergeCommit")
        sha = mc.get("oid") if isinstance(mc, dict) else (mc if isinstance(mc, str) else None)
        rollup = p.get("statusCheckRollup") if isinstance(p.get("statusCheckRollup"), list) else []
        results = [str((c or {}).get("conclusion") or (c or {}).get("state") or "").upper() for c in rollup if isinstance(c, dict)]
        if any(r in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for r in results):
            checks = "fail"
        elif results and all(r in ("SUCCESS", "NEUTRAL", "SKIPPED") for r in results):
            checks = "pass"
        else:
            checks = "pending"
        url = p.get("url")
        out.append({"number": p["number"], "state": st, "draft": bool(p.get("isDraft")), "head": str(p.get("headRefName") or ""),
                    "url": url if isinstance(url, str) and url.startswith("https://") else None,
                    "merge_commit": sha if isinstance(sha, str) and gitutil.valid_oid(sha) else None, "checks": checks})
    return out


def _git(ctx: Any, args: Sequence[str]):
    return lgit.run_git(list(args), cwd=ctx.repo, isolated=True, pin_hooks=True)


def _merge_commits(ctx: Any, ib: str) -> List[Dict[str, Any]]:
    import pr_prepare
    proc = _git(ctx, ["log", "--merges", "-n", str(MAX_SCAN_COMMITS), "--format=%H%x1f%P%x1f%s%x1f%B%x1e", "refs/heads/" + ib])
    out = []  # type: List[Dict[str, Any]]
    for rec in proc.stdout.split("\x1e"):
        rec = rec.strip("\n")
        f = rec.split("\x1f", 3)
        if len(f) < 4:
            continue
        out.append({"sha": f[0].strip(), "parents": f[1].split(), "subject": f[2], "trailers": pr_prepare.parse_trailers(f[3])})
    return out


def scan(ctx: Any, st: Any, lp: Any, prs: Optional[List[Dict[str, Any]]] = None, rule_suites: Optional[Any] = None) -> Report:
    import pr_prepare
    rep = Report()
    ib = ctx.integration_branch()
    items = lp.items
    lk = ctx.lock().status()
    # 7. state items not in the plan
    for iid in sorted(st.items, key=common.natural_key):
        if iid not in items and st.items[iid]["status"] not in state_mod.TERMINAL:
            rep.find("warn", "reconcile.not-in-plan", "state item %s is not in the plan" % iid, iid)
    if not ib:
        rep.find("warn", "reconcile.no-integration-branch", "no integration branch (main/master or integration_branch) exists: git facts were not checked")
    else:
        tip = gitutil.rev_parse("refs/heads/" + ib, ctx.repo, isolated=True)
        # 1. merged items must really be there
        merged = {}
        for iid in st.by_status("merged"):
            mc = st.items[iid].get("mergeCommit")
            real = bool(mc and gitutil.rev_parse(mc + "^{commit}", ctx.repo, isolated=True) == mc)
            if not real or not gitutil.is_ancestor(mc, "refs/heads/" + ib, ctx.repo, isolated=True):
                rep.find("error", "reconcile.merge-missing", "item %s is merged in the state but %s is not on %s: history was rewritten, or the state was forged"
                         % (iid, (mc or "?")[:12], ib), iid)
                rep.actions.append({"op": "merge-missing", "item": iid, "commit": mc})
            else:
                merged[iid] = mc
        # 3. human reverts
        if merged:
            for mc, revs in pr_prepare.find_reverts(ctx.repo, list(merged.values()), "refs/heads/" + ib).items():
                for iid, c in merged.items():
                    if c == mc:
                        rep.find("error", "reconcile.human-revert", "item %s: %s reverts its merge commit %s" % (iid, revs[0][:12], mc[:12]), iid)
                        rep.actions.append({"op": "human-revert", "item": iid, "commit": mc, "revert": revs[0]})
        # 2. crash recovery by trailer
        unmerged = {i for i in st.items if st.items[i]["status"] not in ("merged", "abandoned")}
        for mcm in _merge_commits(ctx, ib):
            ids = mcm["trailers"].get("loop-item") or []
            for iid in ids:
                if iid not in unmerged or iid not in items:
                    continue
                slug = items[iid]["slug"]
                if re.search(r"\bfeat/%s-a\d+\b" % re.escape(slug), mcm["subject"]) and len(mcm["parents"]) == 2:
                    rep.find("warn", "reconcile.recover-merged", "item %s is %s in the state but %s on %s carries 'Loop-Item: %s' for its branch: recovered as merged"
                             % (iid, st.items[iid]["status"], mcm["sha"][:12], ib, iid), iid)
                    rep.actions.append({"op": "merged-recovered", "item": iid, "commit": mcm["sha"]})
                    unmerged.discard(iid)
                else:
                    rep.find("warn", "reconcile.trailer-unverified", "commit %s names 'Loop-Item: %s' but is not a merge of feat/%s-a<n>: not trusted"
                             % (mcm["sha"][:12], iid, slug), iid)
    # 4. orphans
    if not (lk["held"] and not lk["stale"]):
        for iid in st.by_status(*state_mod.ACTIVE):
            rep.find("warn", "reconcile.stale-inflight", "item %s is %s but no run holds the lock: back to pending" % (iid, st.items[iid]["status"]), iid)
            rep.actions.append({"op": "reset-inflight", "item": iid})
    finishing = {a["item"] for a in rep.actions if a["op"] in ("merged-recovered",)}
    live_branches = [r["branch"] for i, r in st.items.items() if r.get("branch") and r["status"] in state_mod.IN_FLIGHT and i not in finishing]
    for a in branches.sweep(ctx, live_branches):
        if a["action"] == "delete-merged":
            rep.find("info", "reconcile.merged-branch", "branch %s is fully merged: deleted (merged branches are deleted after pass)" % a["branch"])
            rep.actions.append({"op": "delete-merged", "branch": a["branch"]})
        else:
            rep.find("info", "reconcile.orphan-branch", "branch %s has no live item: renamed to %s" % (a["branch"], a["to"]))
            rep.actions.append({"op": "orphan-branch", "branch": a["branch"], "to": a["to"]})
    days = ctx.cfg.get_int("orphan_retention_days")
    deadlog = branches._deadlog(ctx.repo)
    old = [b["name"] for b in branches.list_branches(ctx.repo, ("dead",))
           if (deadlog.get(b["name"]) or b["committed"]) and common.now() - (deadlog.get(b["name"]) or b["committed"]) > days * 86400]
    for n in old:
        rep.find("info", "reconcile.prune-dead", "dead branch %s is older than %d days: pruned" % (n, days))
        rep.actions.append({"op": "prune-dead", "branch": n})
    # worktrees with no live state: report only
    wt = _git(ctx, ["worktree", "list", "--porcelain"]).stdout
    for blk in wt.split("\n\n"):
        m = re.search(r"^branch refs/heads/(feat/\S+)$", blk, re.M)
        if m and m.group(1) not in live_branches and ".claude/worktrees" in blk:
            rep.find("info", "reconcile.orphan-worktree", "a worktree holds %s, which no live item owns: remove it by hand when you are done with it" % m.group(1))
    # 5. PR overlay
    for pr in prs or []:
        iid = _item_for_pr(st, items, pr)
        if iid is None:
            continue
        r = st.items[iid]
        overlay = {"pr": pr["number"], "state": pr["state"], "draft": pr["draft"], "checks": pr["checks"], "url": pr["url"]}
        if r.get("overlay") != overlay:
            rep.actions.append({"op": "overlay", "item": iid, "overlay": overlay})
        if pr["state"] == "MERGED" and r["status"] != "merged":
            mc = pr["merge_commit"]
            if mc and gitutil.rev_parse(mc + "^{commit}", ctx.repo, isolated=True) == mc and ib and gitutil.is_ancestor(mc, "refs/heads/" + ib, ctx.repo, isolated=True):
                rep.find("warn", "reconcile.pr-merged", "PR #%d is merged: item %s merged at %s" % (pr["number"], iid, mc[:12]), iid)
                rep.actions.append({"op": "pr-merged", "item": iid, "commit": mc})
            else:
                rep.find("warn", "reconcile.pr-merged-unfetched", "PR #%d is merged but its merge commit is not on %s here: fetch, then reconcile again" % (pr["number"], ib or "the integration branch"), iid)
        elif pr["state"] == "CLOSED" and r["status"] in state_mod.IN_FLIGHT:
            rep.find("error", "reconcile.pr-closed", "PR #%d was closed without merging while item %s is %s" % (pr["number"], iid, r["status"]), iid)
            rep.actions.append({"op": "pr-closed", "item": iid})
        elif pr["state"] == "OPEN" and r["status"] == "reviewing":
            rep.actions.append({"op": "pr-open", "item": iid})
        if pr["state"] == "OPEN" and pr["checks"] == "fail":
            rep.find("warn", "reconcile.ci-failed", "CI is red on PR #%d (item %s): a CI-fix attempt is due" % (pr["number"], iid), iid)
    # 6. bypass log
    known = {(e.get("at"), e.get("actor"), e.get("ref")) for e in st.header.get("bypass", []) if isinstance(e, dict)}
    for e in _bypass_entries(rule_suites):
        if (e["at"], e["actor"], e["ref"]) not in known:
            rep.find("warn", "reconcile.bypass", "ruleset bypass by %s on %s at %s" % (e["actor"], e["ref"], e["at"]))
            rep.actions.append({"op": "bypass", "entry": e})
    return rep


def _item_for_pr(st: Any, items: Dict[str, Any], pr: Dict[str, Any]) -> Optional[str]:
    for iid, r in st.items.items():
        if (r.get("prUrl") and pr.get("url") and r["prUrl"] == pr["url"]) or (r.get("branch") and r["branch"] == pr.get("head")):
            return iid
    p = branches.parse(pr.get("head", ""))
    if p and p[0] == "feat":
        for iid, it in items.items():
            if it.get("slug") == p[1] and iid in st.items and st.items[iid].get("attempt") == p[2]:
                return iid
    return None


def _bypass_entries(raw: Any) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    rows = raw if isinstance(raw, list) else (raw.get("rule_suites") if isinstance(raw, dict) else None)
    for r in (rows or [])[:500]:
        if not isinstance(r, dict) or str(r.get("evaluation_result", r.get("result", ""))).lower() != "bypass":
            continue
        out.append({"at": common.clean_text(r.get("pushed_at") or r.get("evaluated_at") or "", 30),
                    "actor": common.clean_text(r.get("actor_name") or r.get("actor") or "unknown", 40),
                    "ref": common.clean_text(r.get("ref") or "", 60), "note": common.clean_text(r.get("note") or "ruleset bypass (RC-9)", 100),
                    "after_sha": common.clean_text(r.get("after_sha") or "", 40)})
    return out


def apply(loop: Any, rep: Report, by: str = "reconcile") -> Report:
    """Apply the actions of `scan` as ONE compare-and-swap of the state, then the branch operations."""
    ctx = loop.ctx
    acts = list(rep.actions)
    state_ops = [a for a in acts if a["op"] in ("merge-missing", "human-revert", "merged-recovered", "reset-inflight", "overlay", "pr-merged",
                                                "pr-closed", "pr-open", "bypass")]
    if state_ops:
        def mutate(st: Any) -> None:
            for a in state_ops:
                op = a["op"]
                if op == "bypass":
                    st.header.setdefault("bypass", []).append(a["entry"])
                    del st.header["bypass"][:-200]
                    continue
                r = st.items.get(a["item"])
                if r is None:
                    continue
                if op == "merge-missing" and r["status"] == "merged":
                    _park(st, a["item"], "needs_human", by, "merge commit missing from the integration branch", "merge_missing", force=True)
                elif op == "human-revert" and r["status"] == "merged":
                    _park(st, a["item"], "needs_human", by, "a human reverted the merge commit", "human_revert", force=True)
                elif op in ("merged-recovered", "pr-merged") and r["status"] not in ("merged", "abandoned"):
                    r["mergeCommit"] = a["commit"]
                    st.header["breaker"] = 0
                    state_mod._force_status(st, a["item"], "merged", by, "recovered from git (%s)" % op)
                elif op == "reset-inflight" and r["status"] in state_mod.ACTIVE:
                    st.set_status(a["item"], "pending", by, "no run holds the lock: partial work discarded")
                elif op == "pr-closed" and r["status"] in state_mod.IN_FLIGHT:
                    _park(st, a["item"], "needs_human", by, "the PR was closed without merging", "pr_closed", force=True)
                elif op == "pr-open" and r["status"] == "reviewing":
                    st.set_status(a["item"], "pr_open", by, "PR open")
                elif op == "overlay":
                    r["overlay"] = a["overlay"]
                    if a["overlay"].get("url") and not r.get("prUrl"):
                        r["prUrl"] = a["overlay"]["url"]
        loop.store.update(mutate, "reconcile --apply (%d state change(s))" % len(state_ops))
        rep.applied += ["%s %s" % (a["op"], a.get("item") or a.get("branch", "")) for a in state_ops]
    sweep = [{"action": "rename-dead", "branch": a["branch"], "to": a["to"]} for a in acts if a["op"] == "orphan-branch"]
    sweep += [{"action": "delete-merged", "branch": a["branch"]} for a in acts if a["op"] == "delete-merged"]
    if sweep:
        rep.applied += branches.apply_sweep(ctx, sweep)
    if any(a["op"] == "prune-dead" for a in acts):
        rep.applied += ["prune %s" % n for n in branches.prune_dead(ctx, ctx.cfg.get_int("orphan_retention_days"))]
    return rep


def _park(st: Any, iid: str, kind: str, by: str, note: str, code: str, force: bool = False) -> None:
    state_mod._force_status(st, iid, kind, by, note, code)
    st.header["parked"] = {"item": iid, "reason": code, "at": common.iso()}
