"""Recorded-shape `gh api` responses, keyed by API path, for the doctor's `--gh-fixture` (report §8.4).

These mirror the GitHub REST shapes the doctor reads. `build()` assembles a full fixture; tests
override one piece at a time.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence

SLUG = "acme/widgets"


def pr_rule(approvals: int = 1, code_owner: bool = True, last_push: bool = True) -> Dict[str, Any]:
    return {"type": "pull_request", "parameters": {"required_approving_review_count": approvals, "dismiss_stale_reviews_on_push": True,
                                                     "require_code_owner_review": code_owner, "require_last_push_approval": last_push,
                                                     "required_review_thread_resolution": True}}


def checks_rule(contexts: Sequence[str] = ("verify", "evidence", "secrets-scan"), integration_id: Optional[int] = 15368) -> Dict[str, Any]:
    items = []
    for c in contexts:
        e = {"context": c}  # type: Dict[str, Any]
        if integration_id:
            e["integration_id"] = integration_id
        items.append(e)
    return {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True, "required_status_checks": items}}


def ruleset(rid: int = 1, name: str = "main", rules: Optional[List[Dict[str, Any]]] = None, bypass: Sequence[Dict[str, Any]] = (),
            include: Sequence[str] = ("~DEFAULT_BRANCH",), exclude: Sequence[str] = (), enforcement: str = "active", target: str = "branch") -> Dict[str, Any]:
    if rules is None:
        rules = [{"type": "deletion"}, {"type": "non_fast_forward"}, pr_rule(), checks_rule()]
    return {"id": rid, "name": name, "target": target, "enforcement": enforcement, "source_type": "Repository", "source": SLUG,
            "conditions": {"ref_name": {"include": list(include), "exclude": list(exclude)}}, "bypass_actors": list(bypass), "rules": rules}


def repo_info(private: bool = True, owner_type: str = "User", default_branch: str = "main", squash_ok: bool = True) -> Dict[str, Any]:
    d = {"full_name": SLUG, "private": private, "visibility": "private" if private else "public", "default_branch": default_branch,
         "owner": {"login": "acme", "type": owner_type}, "allow_squash_merge": True, "allow_auto_merge": False}  # type: Dict[str, Any]
    if squash_ok:
        d["squash_merge_commit_title"] = "PR_TITLE"
        d["squash_merge_commit_message"] = "PR_BODY"
    else:
        d["squash_merge_commit_title"] = "COMMIT_OR_PR_TITLE"
        d["squash_merge_commit_message"] = "COMMIT_MESSAGES"
    return d


def build(rulesets: Optional[Sequence[Dict[str, Any]]] = None, protection: Any = None, environments: Any = None, actions_perms: Any = "default",
          installation: Any = None, info: Optional[Dict[str, Any]] = None, rulesets_status: Optional[Dict[str, Any]] = None,
          list_summaries_only: bool = False) -> Dict[str, Any]:
    fx = {}  # type: Dict[str, Any]
    fx["repos/%s" % SLUG] = info or repo_info()
    if rulesets_status is not None:
        fx["repos/%s/rulesets" % SLUG] = rulesets_status
    else:
        rs = list(rulesets) if rulesets is not None else [ruleset()]
        if list_summaries_only:
            fx["repos/%s/rulesets" % SLUG] = [{"id": r["id"], "name": r["name"], "target": r["target"], "enforcement": r["enforcement"]} for r in rs]
            for r in rs:
                fx["repos/%s/rulesets/%s" % (SLUG, r["id"])] = r
        else:
            fx["repos/%s/rulesets" % SLUG] = copy.deepcopy(rs)
    fx["repos/%s/branches/main/protection" % SLUG] = protection if protection is not None else {"__status": 404, "message": "Branch not protected"}
    fx["repos/%s/environments" % SLUG] = environments if environments is not None else {"total_count": 0, "environments": []}
    if actions_perms == "default":
        actions_perms = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
    if actions_perms is not None:
        fx["repos/%s/actions/permissions/workflow" % SLUG] = actions_perms
    if installation is not None:
        fx["repos/%s/installation" % SLUG] = installation
    return fx


ADMIN_PR_MODE = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
ADMIN_ALWAYS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}


def env_with_reviewers(name: str = "production", self_review_blocked: bool = True) -> Dict[str, Any]:
    return {"name": name, "protection_rules": [{"type": "required_reviewers", "prevent_self_review": self_review_blocked,
                                                 "reviewers": [{"type": "User", "reviewer": {"login": "owner", "id": 1}}]}]}
