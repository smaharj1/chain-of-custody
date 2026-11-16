"""DEC-1: the doctor suggests `delivery: draft-pr` once the ruleset checks pass, and reports the push tier the guard reads.

    delivery.suggest-draft-pr   `delivery` is `local` or `push-branch` and the checks that prove a pull request cannot be bypassed all pass
                                (main requires a pull request and a review, code-owner review is on, the required checks are pinned, nobody
                                bypasses). An information row only: it changes nothing. It is never a failure.
    delivery.push-tier          `git_autonomy` and `delivery` in loop.config at the base sha (the merge-base of HEAD with origin/HEAD,
                                origin/main, origin/master, main or master: the values the guard reads). Information, and a WARN when the
                                push tier is on there (branch+commit+push with push-branch or draft-pr) but `bot.kind` is `none`: the
                                agent's push then uses whatever credential the session has, which is the person's.

Registered by `levelcalc` importing this module (a check module needs no edit to `doctor.py`). Nothing here pushes or opens a pull request:
`pr_prepare.py` prints the commands, and who runs them follows `git_autonomy` and `delivery` at the base sha.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from . import github
from .model import Context, Result, check, failed, passed, skipped, FAIL, UNKNOWN, WARN

PUSH_TIER = "branch+commit+push"
PUSHING = ("push-branch", "draft-pr")
# the default-branch candidates the guard (guards/lib.py) and verify.py use for the merge-base
BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


@check("delivery.suggest-draft-pr", "config", doc="suggests delivery: draft-pr once the ruleset checks pass (DEC-1); information only")
def check_suggest_draft_pr(ctx: Context) -> Result:
    if ctx.config is None:
        return skipped("delivery.suggest-draft-pr", "no loop.config")
    current = str(ctx.cfg("delivery", "local") or "local")
    if current not in ("local", "push-branch"):
        return skipped("delivery.suggest-draft-pr", "delivery is %s: nothing to suggest" % current)
    rows = []  # type: List[Result]
    for fn in (github.check_main_protection, github.check_codeowner_review, github.check_required_checks, github.check_bypass):
        res = fn(ctx)
        rows.extend(res if isinstance(res, list) else [res])
    bad = [r for r in rows if r.status in (FAIL, UNKNOWN)]
    if bad or not rows:
        why = "%s: %s" % (bad[0].id, bad[0].message) if bad else "no ruleset data"
        return skipped("delivery.suggest-draft-pr", "delivery stays %s: the ruleset checks do not all pass yet (%s)" % (current, why))
    return passed("delivery.suggest-draft-pr",
                  "the ruleset checks pass (pull request and review required, code owners, pinned required checks, no bypass): you can set delivery: draft-pr "
                  "in loop.config (it is %s now). With git_autonomy: branch+commit+push the conductor pushes the feature branch and opens the draft; below it, "
                  "it prepares both for you. It never pushes a protected branch or merges. Nothing changes until a person commits the new value to the "
                  "base branch." % current)


def base_config(ctx: Context) -> Tuple[Optional[str], Any]:
    """(base sha, Config or None): loop.config committed at the merge-base of HEAD with the default branch. (None, None) with no base."""
    for cand in BASE_CANDIDATES:
        rc, out, _ = ctx.git(["rev-parse", "--verify", "-q", cand + "^{commit}"])
        tip = out.strip().split("\n")[0] if rc == 0 else ""
        if not tip:
            continue
        rc, out, _ = ctx.git(["merge-base", "HEAD", tip])
        sha = out.strip().split("\n")[0] if rc == 0 else ""
        if not sha:
            continue
        try:
            from kitlib import config as config_mod
            return sha, config_mod.load_at_sha(ctx.repo, sha)
        except Exception:  # absent or unreadable at the base: the guard falls back to the defaults
            return sha, None
    return None, None


@check("delivery.push-tier", "config", doc="git_autonomy and delivery at the base sha (what the guard reads); warns when the push tier has no bot identity")
def check_push_tier(ctx: Context) -> Result:
    rid = "delivery.push-tier"
    sha, cfg = base_config(ctx)
    if sha is None:
        return skipped(rid, "no base sha (no commit shared with origin/HEAD, main or master): the guard reads git_autonomy none and delivery local, "
                            "so no agent pushes")
    tier = str(cfg.get("git_autonomy", "none") or "none").strip().lower() if cfg is not None else "none"
    delivery = str(cfg.get("delivery", "local") or "local") if cfg is not None else "local"
    where = "at the base sha %s%s: git_autonomy %s, delivery %s" % (sha[:10], "" if cfg is not None else " (no loop.config there: the defaults)",
                                                                    tier, delivery)
    tree = ""
    if ctx.config is not None:
        t_tier = str(ctx.cfg("git_autonomy", "none") or "none").strip().lower()
        t_delivery = str(ctx.cfg("delivery", "local") or "local")
        if (t_tier, t_delivery) != (tier, delivery):
            tree = "; the working tree says git_autonomy %s, delivery %s, which counts once a person merges it" % (t_tier, t_delivery)
    if tier != PUSH_TIER or delivery not in PUSHING:
        return passed(rid, "%s: no agent pushes; pr_prepare.py prints the push and PR commands as a HUMAN STEP%s" % (where, tree))
    opens = "and opens the draft PR" if delivery == "draft-pr" else "(you open the PR)"
    kind = str(ctx.cfg("bot.kind", "none") or "none")
    if kind == "none":
        return failed(rid, "%s: the conductor pushes its own feature branch %s, but bot.kind is none, so the push uses whatever credential the "
                           "session has, which is yours%s" % (where, opens, tree),
                      "set up a bot identity (%s), or set git_autonomy: branch+commit to run the push yourself"
                      % ctx.kit_path("docs", "security", "bot-identity.md"), severity=WARN)
    return passed(rid, "%s: the conductor pushes its own feature branch %s with the credential the session has (bot.kind %s; the bot.identity "
                       "and cred.* rows say whether that is the bot's alone)%s" % (where, opens, kind, tree))
