"""B1 / B4: what GitHub itself will enforce (report §5.1, §8.4, RC-2, RC-9, RC-11, DEC-40, DEC-41).

The doctor reads GitHub through one seam, `ctx.ghapi(path) -> (status, json)`. Tests and
`--gh-fixture` give it recorded `gh api` JSON keyed by API path; `--live` gives it a thin
wrapper around `gh api` (GET only, human-run). With neither, every GitHub fact is unknown, and
unknown means unavailable unless the human attested the feature in `platform_attested` (V27).

Fixture file: one JSON object, API path -> response. A failed call is
`{"__status": 404, "message": "Not Found"}`. Paths read:

    repos/{o}/{r}                           visibility, owner type, default branch, merge settings
    repos/{o}/{r}/rulesets                  list; each item may carry its rules, else:
    repos/{o}/{r}/rulesets/{id}             full ruleset with rules and bypass_actors
    repos/{o}/{r}/branches/{b}/protection   classic branch protection
    repos/{o}/{r}/environments              Environments and their protection rules
    repos/{o}/{r}/pages                     GitHub Pages source (a deploy path with no workflow), optional
    repos/{o}/{r}/actions/permissions/workflow   default token permissions, can approve PRs
    repos/{o}/{r}/installation              the bot App's installation (permissions), optional
    orgs/{o}                                plan name, optional
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import workflows
from .model import (Context, Result, check, failed, gatherer, passed, skipped, unknown, ERROR, WARN)

GhApi = Callable[[str], Tuple[int, Any]]

ADMIN_ROLE_ID = 5
FORBIDDEN_BOT_PERMS = ("workflows", "actions", "administration", "environments", "secrets", "deployments", "pages", "repository_hooks",
                       "organization_administration", "organization_secrets", "members", "checks", "statuses")


def fixture_ghapi(data: Dict[str, Any]) -> GhApi:
    def call(path: str) -> Tuple[int, Any]:
        key = path.strip("/")
        if key in data:
            v = data[key]
            if isinstance(v, dict) and "__status" in v:
                return int(v["__status"]), {k: x for k, x in v.items() if k != "__status"}
            return 200, v
        return 404, {"message": "Not Found (no recorded response for %s)" % key}
    return call


def live_ghapi(run: Callable[..., Tuple[int, str, str]], env: Dict[str, str]) -> GhApi:
    """`gh api <path>` (GET). Only built for `--live`, which a human runs from a plain terminal."""
    def call(path: str) -> Tuple[int, Any]:
        rc, out, err = run(["gh", "api", path], 20.0, env, None)
        if rc == 0:
            try:
                return 200, json.loads(out) if out.strip() else {}
            except ValueError:
                return 200, {"raw": out[:200]}
        m = re.search(r"HTTP (\d{3})", err)
        return (int(m.group(1)) if m else 0), {"message": err.strip()[:200]}
    return call


# --- origin ------------------------------------------------------------------------------------------

_REMOTE_RES = [re.compile(r"^(?:https?|ssh|git)://(?:[^/@]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$"),
               re.compile(r"^(?:[^@/]+@)?([^:/]+):([^/]+)/([^/]+?)(?:\.git)?/?$")]


def parse_remote(url: str) -> Optional[Tuple[str, str, str]]:
    url = url.strip()
    for rx in _REMOTE_RES:
        m = rx.match(url)
        if m:
            return m.group(1).lower(), m.group(2), m.group(3)
    return None


@gatherer("origin")
def gather_origin(ctx: Context) -> Dict[str, Any]:
    override = str(ctx.cfg("github_repo", "") or "").strip()
    rc, out, err = ctx.git(["remote", "get-url", "origin"])
    url = out.strip() if rc == 0 else ""
    parsed = parse_remote(url) if url else None
    info = {"url": re.sub(r"//[^@/]*@", "//", url), "host": parsed[0] if parsed else "", "owner": parsed[1] if parsed else "",
            "repo": parsed[2] if parsed else "", "has_origin": bool(url)}
    if override and "/" in override:
        info["owner"], info["repo"] = override.split("/", 1)
        if not info["host"]:
            info["host"] = "github.com"
    info["is_github"] = info["host"] in ("github.com", "www.github.com")
    return info


@check("gh.forge", "github")
def check_forge(ctx: Context) -> Result:
    o = ctx.fact("origin")
    if o["is_github"]:
        return passed("gh.forge", "origin is on github.com (%s/%s)" % (o["owner"], o["repo"]))
    if not o["has_origin"]:
        return failed("gh.forge", "no origin remote: the server-side boundary cannot be checked", "L2 and above need a GitHub-hosted repo (report §8.4)",
                      severity=WARN)
    return failed("gh.forge", "origin host %r is not github.com" % o["host"], "every server-side control here is a GitHub feature; other forges stay at L1 (§8.4)",
                  severity=WARN)


# --- gatherer ----------------------------------------------------------------------------------------------

def _items(v: Any, key: str) -> Optional[List[Dict[str, Any]]]:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict) and isinstance(v.get(key), list):
        return [x for x in v[key] if isinstance(x, dict)]
    return None


@gatherer("gh")
def gather_gh(ctx: Context) -> Dict[str, Any]:
    o = ctx.fact("origin")
    base = {"available": False, "slug": "", "repo_info": None, "default_branch": "main", "rulesets": None, "rulesets_error": "",
            "protection": None, "protection_status": None, "pages": None, "environments": None, "actions_perms": None, "installation": None, "org": None,
            "errors": []}  # type: Dict[str, Any]
    if ctx.ghapi is None:
        base["errors"].append("no GitHub data (pass --gh-fixture FILE, or run --live from a plain terminal)")
        return base
    if not (o["owner"] and o["repo"]):
        base["errors"].append("cannot tell which GitHub repo this is (no origin; set github_repo)")
        return base
    slug = "%s/%s" % (o["owner"], o["repo"])
    api = ctx.ghapi
    base.update({"available": True, "slug": slug})
    st, info = api("repos/%s" % slug)
    if st == 200 and isinstance(info, dict):
        base["repo_info"] = info
        base["default_branch"] = str(info.get("default_branch") or "main")
    else:
        base["errors"].append("repos/%s: HTTP %s" % (slug, st))
    st, lst = api("repos/%s/rulesets" % slug)
    if st == 200:
        items = _items(lst, "rulesets") or []
        full = []  # type: List[Dict[str, Any]]
        for it in items:
            if "rules" in it and "bypass_actors" in it:
                full.append(it)
                continue
            st2, det = api("repos/%s/rulesets/%s" % (slug, it.get("id")))
            if st2 == 200 and isinstance(det, dict):
                full.append(det)
            else:
                base["errors"].append("ruleset %s: HTTP %s (details unreadable; treated as unknown)" % (it.get("id"), st2))
        base["rulesets"] = full
    else:
        msg = str(lst.get("message", "")) if isinstance(lst, dict) else ""
        base["rulesets_error"] = "HTTP %s %s" % (st, msg)
    st, prot = api("repos/%s/branches/%s/protection" % (slug, base["default_branch"]))
    base["protection_status"] = st
    if st == 200 and isinstance(prot, dict):
        base["protection"] = prot
    st, pages = api("repos/%s/pages" % slug)
    if st == 200 and isinstance(pages, dict):
        base["pages"] = pages
    st, envs = api("repos/%s/environments" % slug)
    if st == 200:
        base["environments"] = _items(envs, "environments") or []
    st, perms = api("repos/%s/actions/permissions/workflow" % slug)
    if st == 200 and isinstance(perms, dict):
        base["actions_perms"] = perms
    st, inst = api("repos/%s/installation" % slug)
    if st == 200 and isinstance(inst, dict):
        base["installation"] = inst
    if info and isinstance(info, dict) and (info.get("owner") or {}).get("type") == "Organization":
        st, org = api("orgs/%s" % o["owner"])
        if st == 200 and isinstance(org, dict):
            base["org"] = org
    return base


# --- ruleset analysis (pure) -----------------------------------------------------------------------------------

def ruleset_applies(rs: Dict[str, Any], branch: str) -> bool:
    if rs.get("target", "branch") != "branch" or rs.get("enforcement") != "active":
        return False
    ref = "refs/heads/" + branch
    cond = ((rs.get("conditions") or {}).get("ref_name") or {})
    inc = [str(x) for x in (cond.get("include") or [])]
    exc = [str(x) for x in (cond.get("exclude") or [])]

    def hit(patterns: List[str]) -> bool:
        for p in patterns:
            if p in ("~DEFAULT_BRANCH", "~ALL"):
                return True
            if workflows.branch_pattern_matches(p, ref) or workflows.branch_pattern_matches(p, branch):
                return True
        return False
    return hit(inc) and not hit(exc)


def judge_bypass(actors: List[Dict[str, Any]], solo: bool, bot_id: str) -> List[Tuple[str, str]]:
    """[(severity, message)] for ruleset bypass actors. RC-9: the bot never; a solo human admin only in
    pull_request mode; teams (two or more humans) none."""
    out = []  # type: List[Tuple[str, str]]
    for a in actors:
        typ = str(a.get("actor_type", ""))
        aid = str(a.get("actor_id", ""))
        mode = str(a.get("bypass_mode", "always"))
        who = "%s %s%s" % (typ or "actor", aid, " (%s)" % a["actor_name"] if a.get("actor_name") else "")
        if bot_id and aid == bot_id and typ in ("Integration", "User"):
            out.append((ERROR, "the bot (%s) is a bypass actor: it could push to the protected branch" % who))
        elif typ == "Integration":
            out.append((ERROR, "%s can bypass the ruleset: automation must never bypass" % who))
        elif typ == "DeployKey":
            out.append((ERROR, "a deploy key can bypass the ruleset"))
        elif typ == "Team":
            out.append((ERROR, "%s can bypass the ruleset: bypass is for one human admin on a solo install, nobody on a team" % who))
        elif typ in ("RepositoryRole", "OrganizationAdmin", "User"):
            human_admin = (typ == "OrganizationAdmin") or (typ == "RepositoryRole" and aid == str(ADMIN_ROLE_ID)) or typ == "User"
            if typ == "RepositoryRole" and not human_admin:
                out.append((ERROR, "%s can bypass the ruleset: the bot has write access and would inherit it" % who))
            elif not solo:
                out.append((ERROR, "%s can bypass the ruleset but solo_maintainer is false: teams use no bypass actors" % who))
            elif mode != "pull_request":
                out.append((ERROR, "%s bypasses in %r mode: a solo admin may bypass only in pull_request mode" % (who, mode)))
            else:
                out.append(("info", "%s may bypass in pull_request mode (solo human admin, logged)" % who))
        else:
            out.append((ERROR, "unrecognised bypass actor %s" % who))
    return out


def analyze(gh: Dict[str, Any], solo: bool, bot_id: str) -> Dict[str, Any]:
    """Normalise rulesets and classic protection of the default branch into one view."""
    branch = gh["default_branch"]
    view = {"known": gh["available"], "pr_required": None, "approvals": 0, "code_owner_review": None, "last_push_approval": None,
            "status_checks": None, "check_entries": [], "no_force_push": None, "no_delete": None, "bypass": [], "sources": [],
            "classic_admin_bypass": None, "classic_restricts_bot": False}  # type: Dict[str, Any]
    if not gh["available"]:
        return view
    applicable = [rs for rs in (gh["rulesets"] or []) if ruleset_applies(rs, branch)]
    if gh["rulesets"] is not None:
        view["pr_required"] = view["pr_required"] or False
        view["code_owner_review"] = False
        view["status_checks"] = []
        view["no_force_push"] = False
        view["no_delete"] = False
        view["last_push_approval"] = False
    for rs in applicable:
        view["sources"].append("ruleset %s" % rs.get("name", rs.get("id")))
        for rule in rs.get("rules") or []:
            typ = rule.get("type")
            par = rule.get("parameters") or {}
            if typ == "pull_request":
                view["pr_required"] = True
                view["approvals"] = max(view["approvals"], int(par.get("required_approving_review_count") or 0))
                view["code_owner_review"] = view["code_owner_review"] or bool(par.get("require_code_owner_review"))
                view["last_push_approval"] = view["last_push_approval"] or bool(par.get("require_last_push_approval"))
            elif typ == "required_status_checks":
                for c in par.get("required_status_checks") or []:
                    view["check_entries"].append(c)
                    view["status_checks"].append(str(c.get("context", "")))
            elif typ == "non_fast_forward":
                view["no_force_push"] = True
            elif typ == "deletion":
                view["no_delete"] = True
        view["bypass"] += rs.get("bypass_actors") or []
    prot = gh["protection"]
    if prot:
        view["sources"].append("branch protection")
        rpr = prot.get("required_pull_request_reviews") or {}
        if rpr:
            view["pr_required"] = True
            view["approvals"] = max(view["approvals"], int(rpr.get("required_approving_review_count") or 0))
            view["code_owner_review"] = bool(view["code_owner_review"]) or bool(rpr.get("require_code_owner_reviews"))
            view["last_push_approval"] = bool(view["last_push_approval"]) or bool(rpr.get("require_last_push_approval"))
        elif view["pr_required"] is None:
            view["pr_required"] = False
        rsc = prot.get("required_status_checks") or {}
        ctxs = [str(c) for c in (rsc.get("contexts") or [])] + [str(c.get("context", "")) for c in (rsc.get("checks") or []) if isinstance(c, dict)]
        view["status_checks"] = (view["status_checks"] or []) + ctxs
        view["check_entries"] += [c for c in (rsc.get("checks") or []) if isinstance(c, dict)]
        view["no_force_push"] = bool(view["no_force_push"]) or not (prot.get("allow_force_pushes") or {}).get("enabled", False)
        view["no_delete"] = bool(view["no_delete"]) or not (prot.get("allow_deletions") or {}).get("enabled", False)
        view["classic_admin_bypass"] = not (prot.get("enforce_admins") or {}).get("enabled", False)
        r = prot.get("restrictions") or {}
        pushers = [str(x.get("id", x.get("login", ""))) for k in ("users", "apps", "teams") for x in (r.get(k) or []) if isinstance(x, dict)]
        view["classic_restricts_bot"] = bool(bot_id and bot_id in pushers)
    return view


# --- checks ---------------------------------------------------------------------------------------------------------

def _unavail(ctx: Context, rid: str, what: str, feature: str) -> Result:
    att = ctx.cfg("platform_attested", []) or []
    gh = ctx.fact("gh")
    if feature in att:
        return passed(rid, "%s: attested by the human in platform_attested (not verified here)" % what)
    reason = gh["errors"][0] if gh["errors"] else "no data"
    return unknown(rid, "%s: unknown (%s); unknown means unavailable" % (what, reason),
                   "run with --gh-fixture / --live, or attest %r in platform_attested after checking the repo settings yourself (V27)" % feature, severity=WARN)


@gatherer("gh_analysis")
def gather_analysis(ctx: Context) -> Dict[str, Any]:
    return analyze(ctx.fact("gh"), bool(ctx.cfg("solo_maintainer", False)), str(ctx.cfg("bot.id", "") or ""))


@check("gh.visibility", "github")
def check_visibility(ctx: Context) -> Result:
    gh = ctx.fact("gh")
    if not gh["available"] or not gh["repo_info"]:
        return unknown("gh.visibility", "repository visibility and plan: unknown (%s)" % (gh["errors"][0] if gh["errors"] else "no data"), "", severity=WARN)
    info = gh["repo_info"]
    vis = info.get("visibility") or ("private" if info.get("private") else "public")
    owner = (info.get("owner") or {}).get("type", "?")
    plan = ((gh["org"] or {}).get("plan") or {}).get("name") or ((info.get("owner") or {}).get("plan") or {}).get("name") or "unknown plan"
    return passed("gh.visibility", "%s repository owned by a %s on %s" % (vis, owner, plan),
                  detail={"visibility": vis, "owner_type": owner, "plan": plan})


@check("gh.main-protection", "github", emits=("gh.required-review", "gh.last-push-approval", "gh.force-push"))
def check_main_protection(ctx: Context) -> List[Result]:
    gh = ctx.fact("gh")
    v = ctx.fact("gh_analysis")
    if not gh["available"] or (gh["rulesets"] is None and gh["protection"] is None):
        if gh["available"] and gh["rulesets_error"] and gh["protection_status"] not in (200,):
            return [failed("gh.main-protection", "no ruleset or branch protection could be read on %s (%s; protection HTTP %s): the bot's contents: write can push straight to main"
                           % (gh["default_branch"], gh["rulesets_error"], gh["protection_status"]),
                           "enable rulesets or branch protection (a paid plan for private repos, V27) and require pull requests; without them the level is L1")]
        return [_unavail(ctx, "gh.main-protection", "branch protection or rulesets on the default branch", "rulesets")]
    out = []  # type: List[Result]
    if not v["pr_required"]:
        out.append(failed("gh.main-protection", "no ruleset or protection on %s requires a pull request" % gh["default_branch"],
                          "apply .claude/scripts/github/apply-boundary.sh (dry-run first); without it the bot's contents: write can push to main directly"))
    else:
        out.append(passed("gh.main-protection", "%s requires pull requests (%s)" % (gh["default_branch"], ", ".join(v["sources"]))))
    if v["pr_required"] and v["approvals"] < 1:
        out.append(failed("gh.required-review", "pull requests need no approving review", "set required_approving_review_count to 1 or more"))
    elif v["pr_required"]:
        out.append(passed("gh.required-review", "%d approving review(s) required" % v["approvals"]))
    if v["pr_required"] and not v["last_push_approval"]:
        out.append(failed("gh.last-push-approval", "the most recent push need not be approved by someone other than its author",
                          "enable require_last_push_approval so an agent cannot push after approval, or approve its own push", severity=WARN))
    elif v["pr_required"]:
        out.append(passed("gh.last-push-approval", "the most recent push needs approval from someone other than its author"))
    if v["no_force_push"] is False or v["no_delete"] is False:
        out.append(failed("gh.force-push", "force pushes or deletion of %s are not blocked" % gh["default_branch"],
                          "add the non_fast_forward and deletion rules", severity=WARN))
    else:
        out.append(passed("gh.force-push", "force pushes and deletion of %s are blocked" % gh["default_branch"]))
    return out


@check("gh.codeowner-review", "github")
def check_codeowner_review(ctx: Context) -> Result:
    gh = ctx.fact("gh")
    v = ctx.fact("gh_analysis")
    if not gh["available"] or v["code_owner_review"] is None:
        return _unavail(ctx, "gh.codeowner-review", "required review from code owners", "code_owner_review")
    if v["code_owner_review"]:
        return passed("gh.codeowner-review", "code-owner review is required on %s" % gh["default_branch"])
    return failed("gh.codeowner-review", "code-owner review is not required: CODEOWNERS on .claude/, .github/ and IaC is decoration",
                  "set require_code_owner_review in the pull_request rule (effective level is at most L2 without it)")


@check("gh.required-checks", "github", emits=("gh.required-checks-source",))
def check_required_checks(ctx: Context) -> List[Result]:
    gh = ctx.fact("gh")
    v = ctx.fact("gh_analysis")
    if not gh["available"] or v["status_checks"] is None:
        return [_unavail(ctx, "gh.required-checks", "required status checks (verify, evidence, secrets scan)", "required_status_checks")]
    names = [c.lower() for c in v["status_checks"]]
    need = [("verify", ("verify",)), ("evidence", ("evidence",)), ("secrets scan", ("gitleaks", "secret", "trufflehog"))]
    missing = [label for label, keys in need if not any(any(k in n for k in keys) for n in names)]
    out = []  # type: List[Result]
    if missing:
        out.append(failed("gh.required-checks", "required checks lack: %s (have: %s)" % (", ".join(missing), ", ".join(v["status_checks"]) or "none"),
                          "require the verify, evidence and secrets-scan checks (needed at L3)"))
    else:
        out.append(passed("gh.required-checks", "required checks include verify, evidence and a secrets scan"))
    unpinned = [str(e.get("context")) for e in v["check_entries"] if not e.get("integration_id")]
    if v["check_entries"] and not unpinned:
        out.append(passed("gh.required-checks-source", "every required check is pinned to an app (integration_id)"))
    if v["check_entries"] and unpinned:
        out.append(failed("gh.required-checks-source", "required check(s) %s accept a result from any app: an app with checks: write can post a passing one" % ", ".join(unpinned),
                          "set integration_id on each required status check (the Actions app)", severity=WARN))
    return out


@check("gh.bypass", "github")
def check_bypass(ctx: Context) -> List[Result]:
    gh = ctx.fact("gh")
    v = ctx.fact("gh_analysis")
    solo = bool(ctx.cfg("solo_maintainer", False))
    bot_id = str(ctx.cfg("bot.id", "") or "")
    if not gh["available"] or (gh["rulesets"] is None and gh["protection"] is None):
        return [_unavail(ctx, "gh.bypass", "ruleset bypass list (the bot must not be on it)", "bot_no_bypass")]
    out = []  # type: List[Result]
    findings = judge_bypass(v["bypass"], solo, bot_id)
    for sev, msg in findings:
        if sev == "info":
            out.append(passed("gh.bypass", msg))
        else:
            out.append(failed("gh.bypass", msg, "remove the actor; apply the %s ruleset from .claude/templates/security/rulesets/" % ("solo" if solo else "team")))
    if v["classic_restricts_bot"]:
        out.append(failed("gh.bypass", "classic branch protection lets the bot push to %s" % gh["default_branch"], "remove it from the push restrictions"))
    if v["classic_admin_bypass"]:
        out.append(failed("gh.bypass", "classic branch protection does not enforce for admins (enforce_admins off): an admin bypasses everything, not only in pull_request mode",
                          "use a ruleset with a pull_request-mode bypass, or enable enforce_admins", severity=WARN if solo else ERROR))
    if not findings and not v["classic_restricts_bot"] and not v["classic_admin_bypass"]:
        out.append(passed("gh.bypass", "no ruleset bypass actors on %s" % gh["default_branch"]))
    return out


@check("gh.environments", "github")
def check_environments(ctx: Context) -> List[Result]:
    gh = ctx.fact("gh")
    if not gh["available"] or gh["environments"] is None:
        att = "environment_reviewers" in (ctx.cfg("platform_attested", []) or [])
        return [passed("gh.environments", "Environment reviewers attested in platform_attested (not verified here)") if att else
                skipped("gh.environments", "no Environment data: apply gate for IaC is 'human dispatch' unless environment_reviewers is attested")]
    out = []  # type: List[Result]
    with_rev = 0
    for e in gh["environments"]:
        rules = [r for r in (e.get("protection_rules") or []) if isinstance(r, dict) and r.get("type") == "required_reviewers"]
        if not rules or not rules[0].get("reviewers"):
            continue
        with_rev += 1
        if not rules[0].get("prevent_self_review"):
            out.append(failed("gh.environments", "Environment %r has reviewers but allows self-review" % e.get("name"),
                              "enable \"Prevent self-review\"", severity=WARN))
    if with_rev:
        out.append(passed("gh.environments", "%d Environment(s) with required reviewers" % with_rev))
    else:
        out.append(failed("gh.environments", "no Environment has required reviewers (Enterprise only on private repos): apply gate = human dispatch",
                          "use a workflow_dispatch the bot cannot trigger; IaC projects stay at L3 at most (RC-2)", severity=WARN))
    return out


@check("gh.actions-permissions", "github")
def check_actions_permissions(ctx: Context) -> Result:
    gh = ctx.fact("gh")
    p = gh["actions_perms"]
    if p is None:
        return skipped("gh.actions-permissions", "no Actions permissions data")
    probs = []
    if p.get("default_workflow_permissions") != "read":
        probs.append("default workflow token permissions are not read-only")
    if p.get("can_approve_pull_request_reviews"):
        probs.append("workflows may approve pull requests (a workflow could approve the bot's PR)")
    if probs:
        return failed("gh.actions-permissions", "; ".join(probs), "PUT repos/{o}/{r}/actions/permissions/workflow with read and can_approve_pull_request_reviews false (apply-boundary.sh does this)")
    return passed("gh.actions-permissions", "workflow tokens are read-only by default and cannot approve pull requests")


@check("gh.squash-setting", "github")
def check_squash(ctx: Context) -> Result:
    gh = ctx.fact("gh")
    info = gh["repo_info"]
    if not info:
        return unknown("gh.squash-setting", "squash merge setting: unknown (attest required)", "set squash_merge_commit_title to PR_TITLE and squash_merge_commit_message to PR_BODY, "
                       "so the PR audit block survives a squash (CS-20)", severity=WARN)
    if info.get("allow_squash_merge") is False:
        return passed("gh.squash-setting", "squash merging is off; merge commits keep the PR body")
    if info.get("squash_merge_commit_title") == "PR_TITLE" and info.get("squash_merge_commit_message") == "PR_BODY":
        return passed("gh.squash-setting", "squash commits use the PR title and description")
    return failed("gh.squash-setting", "squash commits do not use \"PR title and description\" (title %s, message %s): the audit block would not survive a squash"
                  % (info.get("squash_merge_commit_title"), info.get("squash_merge_commit_message")),
                  "PATCH repos/{o}/{r} squash_merge_commit_title=PR_TITLE squash_merge_commit_message=PR_BODY", severity=WARN)


@check("gh.bot-permissions", "github")
def check_bot_permissions(ctx: Context) -> List[Result]:
    gh = ctx.fact("gh")
    inst = gh["installation"]
    kind = str(ctx.cfg("bot.kind", "none"))
    if inst is None:
        if kind == "github-app":
            return [unknown("gh.bot-permissions", "the App's installation permissions were not readable; check them by hand: contents and pull_requests write only",
                            "GET repos/{o}/{r}/installation as the App; no workflows, actions, administration, environments, secrets, checks, statuses or deployments (RC-11)",
                            severity=WARN)]
        return [skipped("gh.bot-permissions", "bot permissions not checked (bot.kind is %s)" % kind)]
    perms = inst.get("permissions") or {}
    out = []  # type: List[Result]
    for name in FORBIDDEN_BOT_PERMS:
        if perms.get(name):
            out.append(failed("gh.bot-permissions", "the bot has %s: %s (it must not)" % (name, perms[name]), "remove the permission from the App (RC-11, B1)"))
    for need in ("contents", "pull_requests"):
        if perms.get(need) != "write":
            out.append(failed("gh.bot-permissions", "the bot lacks %s: write" % need, "grant it; nothing wider", severity=WARN))
    return out or [passed("gh.bot-permissions", "the bot has contents and pull_requests write only")]


# --- bot identity (config attestation) ------------------------------------------------------------------------

def _parse_date(text: str) -> Optional[float]:
    import calendar
    import time
    try:
        return float(calendar.timegm(time.strptime(text.strip(), "%Y-%m-%d")))
    except (ValueError, AttributeError):
        return None


@check("bot.identity", "github")
def check_bot_identity(ctx: Context) -> List[Result]:
    kind = str(ctx.cfg("bot.kind", "none"))
    login = str(ctx.cfg("bot.login", "") or "").strip()
    out = []  # type: List[Result]
    if kind == "none":
        return [unknown("bot.identity", "bot.kind is none: no bot identity is configured; without one the agent would push as you",
                        "create a GitHub App (L3+) or a machine user with a fine-grained PAT (L2 only): docs/security/bot-identity.md", severity=WARN)]
    if not login:
        out.append(failed("bot.identity", "bot.login is empty", "set the bot's login", severity=WARN))
    if kind == "github-app":
        if login and not login.endswith("[bot]"):
            out.append(failed("bot.identity", "bot.login %r does not end in [bot]: an App acts as <slug>[bot]. Is this a human account?" % login,
                              "use the App's bot login", severity=WARN))
        out.append(passed("bot.identity", "bot.kind is github-app (acceptable at L3 and above)"))
        return out
    # machine-user-pat: L2 only, short-lived
    exp = str(ctx.cfg("bot.token_expires", "") or "")
    ts = _parse_date(exp) if exp else None
    day = 86400.0
    if ts is None:
        out.append(failed("bot.identity", "bot.token_expires is missing or not YYYY-MM-DD: a machine-user PAT must expire within 14 days", "set it from the token's settings page"))
    else:
        left = (ts + day - ctx.now) / day
        if left <= 0:
            out.append(failed("bot.identity", "the bot's PAT expired on %s" % exp, "mint a new fine-grained PAT that expires within 14 days"))
        elif left > 14.5:
            out.append(failed("bot.identity", "the bot's PAT expires %s, %.0f days away: the limit is 14 days (a long-lived token is a standing credential)" % (exp, left),
                              "mint a PAT with a shorter expiry"))
        elif left <= 2:
            out.append(failed("bot.identity", "the bot's PAT expires %s (in %.0f day(s)): rotate it" % (exp, max(left, 0)), "rotate the token", severity=WARN))
        else:
            out.append(passed("bot.identity", "machine-user PAT expires %s (%.0f days left); acceptable at L2 only" % (exp, left)))
    if login and login.endswith("[bot]"):
        out.append(failed("bot.identity", "bot.kind is machine-user-pat but the login looks like an App", "set bot.kind to github-app", severity=WARN))
    hosts = ctx.fact("gh_hosts")
    humans = sorted({u for h in hosts for u in h["users"] if login and u.lower() != login.lower()})
    if humans and not login:
        out.append(failed("bot.identity", "bot.login is unset but gh is logged in as %s on this machine" % ", ".join(humans), "set bot.login to the bot, and log the human out"))
    return out
