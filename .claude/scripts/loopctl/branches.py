"""Branch lifecycle and the orphan sweep (report section 19.7: Discard and retry L-2, Branch lifecycle L-3).

    feat/<slug>-a<n>      the item's working branch, attempt n
    dead/<slug>-a<n>      a discarded attempt (pruned after `orphan_retention_days`)
    blocked/<slug>-a<n>   a parked item's branch, kept for resume; NEVER auto-deleted

loopctl runs these git commands itself, outside the guards (RC-13), so they are narrow: every name is validated against the
three patterns above, argument lists are fixed, and the destructive step (`discard`) runs only inside the registered loop
worktree, only on the item's own branch, and never on a protected branch.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kitlib import gitutil, jsonio, runstate

from . import common
from .common import Refused, UsageError

__all__ = ["delete_merged", "BRANCH_RE", "feat_name", "dead_name", "blocked_name", "parse", "exists", "list_branches", "create", "rename",
           "discard", "park", "sweep", "prune_dead"]

BRANCH_RE = re.compile(r"^(feat|dead|blocked)/([a-z0-9][a-z0-9-]{0,40})-a([0-9]{1,4})(?:-([0-9]{1,3}))?\Z")


def feat_name(slug: str, attempt: int) -> str:
    return _name("feat", slug, attempt)


def dead_name(slug: str, attempt: int) -> str:
    return _name("dead", slug, attempt)


def blocked_name(slug: str, attempt: int) -> str:
    return _name("blocked", slug, attempt)


def _name(kind: str, slug: str, attempt: int) -> str:
    if not common.valid_slug(slug) or not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 9999:
        raise UsageError("bad slug or attempt for a branch name")
    return "%s/%s-a%d" % (kind, slug, attempt)


def parse(name: str) -> Optional[Tuple[str, str, int]]:
    m = BRANCH_RE.match(name or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _git(repo: str, args: Sequence[str]):
    return gitutil.run_git(list(args), cwd=repo, isolated=True, pin_hooks=True)


def exists(repo: str, name: str) -> bool:
    return parse(name) is not None and _git(repo, ["show-ref", "--verify", "-q", "refs/heads/" + name]).returncode == 0


def list_branches(repo: str, kinds: Sequence[str] = ("feat", "dead", "blocked")) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    proc = _git(repo, ["for-each-ref", "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)", "refs/heads/"])
    for ln in proc.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        p = parse(parts[0])
        if p and p[0] in kinds:
            out.append({"name": parts[0], "kind": p[0], "slug": p[1], "attempt": p[2], "sha": parts[1],
                        "committed": int(parts[2]) if parts[2].isdigit() else 0})
    return sorted(out, key=lambda b: b["name"])


def current(repo: str) -> Optional[str]:
    return gitutil.current_branch(repo, isolated=True)


def create(repo: str, name: str, base_sha: str) -> None:
    """Create the branch at `base_sha` without switching to it."""
    p = parse(name)
    if not p or p[0] != "feat":
        raise UsageError("only feat/<slug>-a<n> branches are created")
    if not gitutil.valid_oid(base_sha):
        raise UsageError("base must be a full object id")
    if exists(repo, name):
        raise Refused("branch %s already exists" % name)
    r = _git(repo, ["branch", name, base_sha])
    if r.returncode != 0:
        raise Refused("git branch failed: %s" % common.clean_text(r.stderr, 120))


def _deadlog_path(repo: str) -> Optional[str]:
    rs = runstate.RunState.for_path(repo)
    return os.path.join(rs.root, "dead-branches.json") if rs is not None else None


def _deadlog(repo: str) -> Dict[str, float]:
    p = _deadlog_path(repo)
    try:
        d = jsonio.read_json_opt(p) if p else None
    except common.READ_ERRORS:
        d = None
    return {k: float(v) for k, v in d.items() if isinstance(k, str) and isinstance(v, (int, float))} if isinstance(d, dict) else {}


def rename(repo: str, old: str, new: str) -> None:
    po, pn = parse(old), parse(new)
    if not po or not pn:
        raise UsageError("branch names must be feat|dead|blocked/<slug>-a<n>")
    if not exists(repo, old):
        raise Refused("branch %s does not exist" % old)
    if exists(repo, new):
        raise Refused("branch %s already exists" % new)
    r = _git(repo, ["branch", "-m", old, new])
    if r.returncode != 0:
        raise Refused("git branch -m failed: %s" % common.clean_text(r.stderr, 120))
    if pn[0] == "dead":                     # retention counts from the day it became dead, not from its last commit
        p = _deadlog_path(repo)
        if p:
            log = _deadlog(repo)
            log[new] = common.now()
            for k in [k for k in log if not exists(repo, k)]:
                del log[k]
            jsonio.ensure_dir(os.path.dirname(p))
            jsonio.atomic_write_json(p, log, 0o600)


def delete_merged(ctx: Any, branch: str, integration: Optional[str]) -> bool:
    """Delete a feat/* branch that is fully merged into the integration branch (`git branch -d` refuses anything else).
    "Merged branches are deleted after pass" (L-3)."""
    p = parse(branch)
    if not p or p[0] != "feat" or not integration or not exists(ctx.repo, branch) or current(ctx.repo) == branch:
        return False
    if not gitutil.is_ancestor("refs/heads/" + branch, "refs/heads/" + integration, ctx.repo, isolated=True):
        return False
    return _git(ctx.repo, ["branch", "-d", branch]).returncode == 0


def _unique(repo: str, kind: str, slug: str, attempt: int) -> str:
    base = _name(kind, slug, attempt)
    if not exists(repo, base):
        return base
    for k in range(2, 100):
        cand = "%s-%d" % (base, k)
        if not exists(repo, cand):
            return cand
    raise Refused("too many %s branches for %s" % (kind, slug))


def discard(ctx: Any, branch: str, integration: Optional[str]) -> Dict[str, Any]:
    """The only destructive step in the loop (L-2): inside the registered loop worktree, on the item's own branch:
    `git reset --hard && git clean -fd`, back to the integration branch, rename the branch to dead/<slug>-a<n>."""
    repo = ctx.repo
    p = parse(branch)
    if not p or p[0] != "feat":
        raise Refused("discard works on feat/<slug>-a<n> branches only")
    if not ctx.worktree_is_loop_worktree():
        raise Refused("discard runs only inside the loop's registered worktree (run.json), never in someone else's checkout (CO-15)")
    if branch in ctx.protected_branches() or integration == branch:
        raise Refused("refusing to discard a protected or integration branch")
    result = {"branch": branch, "dead": None, "reset": False}  # type: Dict[str, Any]
    if not exists(repo, branch):
        return result                       # nothing was ever created; the state change is all that is needed
    if current(repo) == branch:
        r = _git(repo, ["reset", "--hard"])
        c = _git(repo, ["clean", "-fd"])
        if r.returncode != 0 or c.returncode != 0:
            raise Refused("could not reset the item branch: %s" % common.clean_text(r.stderr + c.stderr, 120))
        result["reset"] = True
        if not integration:
            raise Refused("no integration branch to return to")
        s = _git(repo, ["switch", "--quiet", integration])
        if s.returncode != 0:
            raise Refused("could not switch back to %s: %s" % (integration, common.clean_text(s.stderr, 120)))
    dead = _unique(repo, "dead", p[1], p[2])
    rename(repo, branch, dead)
    result["dead"] = dead
    return result


def park(ctx: Any, branch: str) -> Optional[str]:
    """Rename a parked item's branch feat/ -> blocked/ (preserved for resume). Never deletes."""
    p = parse(branch)
    if not p or p[0] != "feat" or not exists(ctx.repo, branch):
        return None
    if current(ctx.repo) == branch:
        integration = ctx.integration_branch()
        if integration and ctx.worktree_is_loop_worktree() and _git(ctx.repo, ["status", "--porcelain"]).stdout.strip() == "":
            _git(ctx.repo, ["switch", "--quiet", integration])
    new = _unique(ctx.repo, "blocked", p[1], p[2])
    rename(ctx.repo, branch, new)
    return new


def sweep(ctx: Any, live_branches: Sequence[str]) -> List[Dict[str, Any]]:
    """Orphan sweep (L-3): `feat/*` branches that no live item owns. Returns the actions (rename to dead/); the caller applies
    them with `apply_sweep`. Worktrees are reported, not removed."""
    actions = []  # type: List[Dict[str, Any]]
    live = set(live_branches)
    cur = current(ctx.repo)
    ib = ctx.integration_branch()
    for b in list_branches(ctx.repo, ("feat",)):
        if b["name"] in live or b["name"] == cur:
            continue
        if ib and gitutil.is_ancestor("refs/heads/" + b["name"], "refs/heads/" + ib, ctx.repo, isolated=True):
            actions.append({"action": "delete-merged", "branch": b["name"]})          # all of it is already on the integration branch
        else:
            actions.append({"action": "rename-dead", "branch": b["name"], "to": _unique(ctx.repo, "dead", b["slug"], b["attempt"])})
    return actions


def apply_sweep(ctx: Any, actions: Sequence[Dict[str, Any]]) -> List[str]:
    done = []  # type: List[str]
    for a in actions:
        if a.get("action") == "rename-dead" and exists(ctx.repo, a["branch"]):
            rename(ctx.repo, a["branch"], a["to"])
            done.append("%s -> %s" % (a["branch"], a["to"]))
        elif a.get("action") == "delete-merged" and delete_merged(ctx, a["branch"], ctx.integration_branch()):
            done.append("deleted merged %s" % a["branch"])
    return done


def prune_dead(ctx: Any, retention_days: int, now: Optional[float] = None) -> List[str]:
    """Delete `dead/*` branches whose tip is older than `retention_days`. `blocked/*` and `feat/*` are never touched."""
    now = common.now() if now is None else now
    pruned = []  # type: List[str]
    cur = current(ctx.repo)
    log = _deadlog(ctx.repo)
    for b in list_branches(ctx.repo, ("dead",)):
        since = log.get(b["name"], b["committed"])
        if b["name"] == cur or not since or now - since < retention_days * 86400:
            continue
        r = _git(ctx.repo, ["branch", "-D", b["name"]])
        if r.returncode == 0:
            pruned.append(b["name"])
    return pruned
