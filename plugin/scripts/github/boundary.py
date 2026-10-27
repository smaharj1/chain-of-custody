#!/usr/bin/env python3
"""boundary: apply and verify the GitHub-side half of the boundary (Phase 1; report §5.1 B1/B4, §8.4; IMP-15).

Run by a human, as a repo admin, from a plain terminal. It refuses to run when `CLAUDECODE` is set.
Never run it as the bot.

    boundary.py apply  --repo OWNER/NAME --variant solo|team          DRY RUN: prints every gh api call
    boundary.py apply  --repo OWNER/NAME --variant solo|team --apply  make the calls
    boundary.py verify --repo OWNER/NAME                              DRY RUN: prints the read-only calls
    boundary.py verify --repo OWNER/NAME --run [--out FILE]           read the settings, write a fixture, run the doctor on it

`apply` makes these calls, in order: the branch ruleset (no bot bypass; solo = the human admin in
pull_request mode only; team = no bypass), the tag ruleset (releases are H5), the squash-merge
setting ("PR title and description", so the audit block survives a squash), the Actions workflow
permissions (read-only token that cannot approve PRs), and optionally one Environment with required
reviewers and self-review prevented. CODEOWNERS is a file: copy `.claude/templates/security/CODEOWNERS.template`
into the repo yourself. Nothing here creates the bot, mints a token or changes anything outside the
repository settings listed.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
KIT_ROOT = os.path.dirname(SCRIPTS)  # .claude
TEMPLATES = os.path.join(KIT_ROOT, "templates", "security", "rulesets")
DOCTOR = os.path.join(SCRIPTS, "doctor.py")

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class Call(object):
    """One gh api call."""

    def __init__(self, method: str, path: str, body: Optional[Any] = None, note: str = "") -> None:
        self.method, self.path, self.body, self.note = method, path, body, note

    def argv(self) -> List[str]:
        a = ["gh", "api"]
        if self.method != "GET":
            a += ["-X", self.method]
        a.append(self.path)
        if self.body is not None:
            a += ["--input", "-"]
        return a

    def render(self) -> str:
        cmd = " ".join(shlex.quote(x) for x in self.argv())
        out = ["# " + self.note] if self.note else []
        if self.body is not None:
            out.append("%s <<'JSON'\n%s\nJSON" % (cmd, json.dumps(self.body, indent=2)))
        else:
            out.append(cmd)
        return "\n".join(out)


def load_template(name: str) -> Dict[str, Any]:
    with open(os.path.join(TEMPLATES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_apply_plan(repo: str, variant: str, release_team_id: Optional[int] = None, environment: Optional[str] = None,
                     reviewer_ids: Sequence[int] = ()) -> Tuple[List[Call], List[str]]:
    """The ordered calls and any steps the script cannot do. `repo` is OWNER/NAME."""
    if not _SLUG_RE.match(repo):
        raise ValueError("--repo must be OWNER/NAME")
    if variant not in ("solo", "team"):
        raise ValueError("--variant must be solo or team")
    calls = []  # type: List[Call]
    manual = []  # type: List[str]
    main = load_template("main-protection.%s.json" % variant)
    calls.append(Call("POST", "repos/%s/rulesets" % repo, main,
                      "branch ruleset %r (PUT repos/%s/rulesets/<id> instead if one with this name already exists)" % (main["name"], repo)))
    tags = load_template("tags-protection.%s.json" % variant)
    if variant == "team":
        if release_team_id:
            tags = copy.deepcopy(tags)
            tags["bypass_actors"][0]["actor_id"] = int(release_team_id)
            calls.append(Call("POST", "repos/%s/rulesets" % repo, tags, "tag ruleset %r" % tags["name"]))
        else:
            manual.append("tag ruleset skipped: pass --release-team-id N (the team allowed to cut releases), or create tags-protection.team.json yourself")
    else:
        calls.append(Call("POST", "repos/%s/rulesets" % repo, tags, "tag ruleset %r" % tags["name"]))
    calls.append(Call("PATCH", "repos/%s" % repo, {"squash_merge_commit_title": "PR_TITLE", "squash_merge_commit_message": "PR_BODY"},
                      "squash commits use the PR title and description, so the audit block survives a squash (CS-20)"))
    calls.append(Call("PUT", "repos/%s/actions/permissions/workflow" % repo,
                      {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
                      "workflow tokens are read-only and workflows cannot approve pull requests"))
    if environment:
        body = {"prevent_self_review": True, "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}}
        if reviewer_ids:
            body["reviewers"] = [{"type": "User", "id": int(i)} for i in reviewer_ids]
        else:
            manual.append("environment %r created without reviewers: pass --reviewer-id N (repeatable), or add them in Settings > Environments" % environment)
        calls.append(Call("PUT", "repos/%s/environments/%s" % (repo, environment), body,
                          "Environment %r: required reviewers and self-review prevention (Enterprise only on private repos, RC-2)" % environment))
    manual.append("CODEOWNERS: copy .claude/templates/security/CODEOWNERS.template to .github/CODEOWNERS, replace the placeholders, commit it through a PR")
    manual.append("bot identity: create the GitHub App (docs/security/bot-identity.md); this script never creates or scopes it")
    return calls, manual


def build_verify_plan(repo: str, default_branch: str = "main") -> List[Call]:
    if not _SLUG_RE.match(repo):
        raise ValueError("--repo must be OWNER/NAME")
    return [Call("GET", "repos/%s" % repo, None, "visibility, owner type, default branch, merge settings"),
            Call("GET", "repos/%s/rulesets" % repo, None, "rulesets (the doctor fetches each one's detail)"),
            Call("GET", "repos/%s/branches/%s/protection" % (repo, default_branch), None, "classic protection (404 is fine if you use rulesets)"),
            Call("GET", "repos/%s/environments" % repo, None, "Environments and their reviewers"),
            Call("GET", "repos/%s/actions/permissions/workflow" % repo, None, "default workflow token permissions"),
            Call("GET", "repos/%s/installation" % repo, None, "the bot App's installation permissions (needs App credentials; 404 otherwise)")]


def run_gh(call: Call, runner: Callable = subprocess.run, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    data = json.dumps(call.body).encode("utf-8") if call.body is not None else None
    try:
        p = runner(call.argv(), input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, shell=False, env=env)
    except FileNotFoundError:
        return 127, "", "gh is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def refuse_in_session(env: Dict[str, str]) -> Optional[str]:
    if "CLAUDECODE" in env:
        return "boundary: refusing to run inside a Claude Code session (CLAUDECODE is set). This is a human-run script: run it from a plain terminal."
    return None


def cmd_apply(args: argparse.Namespace, env: Dict[str, str], runner: Callable) -> int:
    calls, manual = build_apply_plan(args.repo, args.variant, args.release_team_id, args.environment, args.reviewer_id)
    mode = "APPLY" if args.apply else "DRY RUN (nothing is sent; add --apply to make these calls)"
    print("boundary apply: %s, repo %s, variant %s\n" % (mode, args.repo, args.variant))
    for i, c in enumerate(calls, 1):
        print("[%d/%d] %s\n" % (i, len(calls), c.render()))
    if args.apply:
        for i, c in enumerate(calls, 1):
            rc, out, err = run_gh(c, runner, env)
            if rc != 0:
                sys.stderr.write("boundary: call %d failed (exit %d): %s\nStopped; nothing after it was sent. Fix the error and run again (rulesets that already exist need PUT).\n"
                                 % (i, rc, err.strip()[:300]))
                return 1
            print("ok: " + " ".join(c.argv()[2:4]))
    print("\nStill yours to do:")
    for m in manual:
        print("  - " + m)
    print("  - then verify: .claude/scripts/github/verify-boundary.sh --repo %s --run" % args.repo)
    return 0


def cmd_verify(args: argparse.Namespace, env: Dict[str, str], runner: Callable, doctor: Callable = subprocess.run) -> int:
    calls = build_verify_plan(args.repo, args.default_branch)
    print("boundary verify: %s, repo %s\n" % ("RUN (read-only GET calls)" if args.run else "DRY RUN (add --run to read the settings)", args.repo))
    for c in calls:
        print(c.render() + "\n")
    if not args.run:
        return 0
    fixture = {}  # type: Dict[str, Any]
    for c in calls:
        rc, out, err = run_gh(c, runner, env)
        if rc == 0:
            try:
                fixture[c.path] = json.loads(out) if out.strip() else {}
            except ValueError:
                fixture[c.path] = {"__status": 0, "message": "unparsable response"}
        else:
            m = re.search(r"HTTP (\d{3})", err)
            fixture[c.path] = {"__status": int(m.group(1)) if m else 0, "message": err.strip()[:200]}
    rs = fixture.get("repos/%s/rulesets" % args.repo)
    if isinstance(rs, list):
        for item in rs:
            if isinstance(item, dict) and "id" in item:
                p = "repos/%s/rulesets/%s" % (args.repo, item["id"])
                rc, out, err = run_gh(Call("GET", p), runner, env)
                if rc == 0:
                    try:
                        fixture[p] = json.loads(out)
                    except ValueError:
                        pass
    out_path = args.out or "boundary-fixture.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, indent=2)
    print("wrote %s (recorded gh api responses; contains no secrets, but review it before committing)\n" % out_path)
    rc = doctor([sys.executable, DOCTOR, "--gh-fixture", out_path, "--level"], env=env, shell=False).returncode
    print("\nLive halves the doctor cannot see (do these by hand):")
    print("  - as the bot, try `git push origin HEAD:main` with a throwaway commit: GitHub must reject it (GH013).")
    print("  - as the bot, approve and merge a PR it opened: it must be impossible (no review rights, no bypass).")
    print("  - open a PR that adds a skill whose frontmatter hook writes a marker file; the claude -p CI job must not create it (RC-18).")
    return rc


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="boundary.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="create the rulesets and settings (dry run by default)")
    a.add_argument("--repo", required=True, help="OWNER/NAME")
    a.add_argument("--variant", required=True, choices=("solo", "team"), help="solo: one human admin may bypass in pull_request mode; team: no bypass")
    a.add_argument("--apply", action="store_true", help="actually make the calls (without this, nothing is sent)")
    a.add_argument("--release-team-id", type=int, default=None, help="team allowed to create tags (team variant)")
    a.add_argument("--environment", default=None, help="also create this Environment with required reviewers (e.g. production)")
    a.add_argument("--reviewer-id", type=int, action="append", default=[], help="user id of an Environment reviewer; repeatable")
    v = sub.add_parser("verify", help="read the settings and run the doctor on them (dry run by default)")
    v.add_argument("--repo", required=True, help="OWNER/NAME")
    v.add_argument("--run", action="store_true", help="actually make the read-only calls")
    v.add_argument("--out", default=None, help="where to write the recorded JSON (default boundary-fixture.json)")
    v.add_argument("--default-branch", default="main")
    return ap


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None, runner: Callable = subprocess.run, doctor: Callable = subprocess.run) -> int:
    env = dict(os.environ if env is None else env)
    msg = refuse_in_session(env)
    if msg:
        sys.stderr.write(msg + "\n")
        return 2
    args = parser().parse_args(argv)
    try:
        return cmd_apply(args, env, runner) if args.cmd == "apply" else cmd_verify(args, env, runner, doctor)
    except ValueError as exc:
        sys.stderr.write("boundary: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
