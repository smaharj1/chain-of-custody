#!/usr/bin/env python3
"""repo_script: print the GitHub-side setup of a bootstrapped project as a script for a HUMAN to run (Phase 11a; DEC-13, IMP-15, H4).

This program only WRITES TEXT. It never runs `gh`, a kit script or anything it prints; its own source has no way to start a program
(the one exception is `git rev-parse --show-toplevel`, run through kitlib.gitutil to find the checkout when --out is used), so it is
safe for an agent to run. The script it prints is the human's: it refuses to run inside a Claude Code session, runs nothing but its own printed
list, and prints that list instead of running it unless the human adds --apply.

    repo_script.py --repo OWNER/NAME [options] [--out .claude/bootstrap/setup.sh] [--json]

      --create-repo                 greenfield: `gh repo create` and the first push (the repository does not exist on GitHub yet)
      --visibility private|public|internal   (default private)            --description TEXT   --default-branch NAME (default main)
      --variant solo|team           apply-boundary.sh variant (default solo)      --release-team-id N   --environment NAME   --reviewer-id N
      --approvers login1,login2     sets the repository variable FACTORY_APPROVERS (label-guard.yml and intake.yml read it)
      --labels                      run apply-labels.sh
      --boundary                    run apply-boundary.sh (rulesets, tag protection, squash setting, workflow token permissions)
      --workflows a,b               copy these templates into .github/workflows/:
                                    intake, dependency-check, smoke, verify, claude-review, label-guard
      --config-plan PATH            run bootstrap_config.py apply on this plan (an H6 change; the human reads the diff)

The printed script has two phases, because a ruleset must come after the first push and a config or workflow edit before it:

    sh .claude/bootstrap/setup.sh local            [--apply]   edits files in this checkout (config plan, workflow copies); you commit them
    sh .claude/bootstrap/setup.sh remote           [--apply]   talks to GitHub (create and push, rulesets, labels, variables)

Every value is checked against a strict pattern here and then shell-quoted; a value never becomes shell syntax. The script's SHA-256 is
printed so that what the human reads is what the receipt recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import labels  # noqa: E402
from kitlib import gitutil, jsonio  # noqa: E402

WORKFLOWS = ("intake", "dependency-check", "smoke", "verify", "claude-review", "label-guard")
VISIBILITY = ("private", "public", "internal")
VARIANTS = ("solo", "team")
OUT_DIR = os.path.join(".claude", "bootstrap")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_ENV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
_OUT = re.compile(r"^\.claude/bootstrap/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_PLAN = re.compile(r"^\.claude/bootstrap/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.json\Z")


class ScriptError(ValueError):
    pass


def q(value: Any) -> str:
    return shlex.quote(str(value))


class Params(object):
    def __init__(self, repo: str, create_repo: bool = False, visibility: str = "private", description: str = "", default_branch: str = "main",
                 variant: str = "solo", release_team_id: Optional[int] = None, environment: str = "", reviewer_ids: Sequence[int] = (),
                 approvers: Sequence[str] = (), labels_: bool = False, boundary: bool = False, workflows: Sequence[str] = (),
                 config_plan: str = "") -> None:
        try:
            self.repo = labels.check_repo(repo)
        except labels.LabelError as exc:
            raise ScriptError(str(exc))
        if visibility not in VISIBILITY:
            raise ScriptError("--visibility must be one of %s" % ", ".join(VISIBILITY))
        if variant not in VARIANTS:
            raise ScriptError("--variant must be solo or team")
        if not isinstance(description, str) or len(description) > 200 or any(unicodedata.category(c)[0] == "C" for c in description):
            raise ScriptError("--description must be plain text of at most 200 characters")
        if not isinstance(default_branch, str) or not _BRANCH.match(default_branch) or ".." in default_branch or default_branch.endswith(("/", ".lock", ".")) \
                or "//" in default_branch or "/." in default_branch:
            raise ScriptError("--default-branch is not a safe branch name")
        for n in ([release_team_id] if release_team_id is not None else []) + list(reviewer_ids):
            if isinstance(n, bool) or not isinstance(n, int) or not 0 < n < 10 ** 12:
                raise ScriptError("team and reviewer ids must be positive integers")
        if environment and not _ENV.match(environment):
            raise ScriptError("--environment must be letters, digits, '_' and '-'")
        logins = list(approvers)
        if len(logins) > 20 or len(set(l.lower() for l in logins)) != len(logins) or not all(isinstance(l, str) and _LOGIN.match(l) for l in logins):
            raise ScriptError("--approvers must be up to 20 distinct GitHub logins")
        wf = list(workflows)
        bad = [w for w in wf if w not in WORKFLOWS]
        if bad or len(set(wf)) != len(wf):
            raise ScriptError("--workflows takes distinct names from: %s" % ", ".join(WORKFLOWS))
        if config_plan and not _PLAN.match(config_plan):
            raise ScriptError("--config-plan must be .claude/bootstrap/<name>.json")
        if (release_team_id or environment or reviewer_ids) and not boundary:
            raise ScriptError("--release-team-id, --environment and --reviewer-id belong to --boundary")
        self.create_repo, self.visibility, self.description, self.default_branch = create_repo, visibility, description, default_branch
        self.variant, self.release_team_id, self.environment, self.reviewer_ids = variant, release_team_id, environment, list(reviewer_ids)
        self.approvers, self.labels, self.boundary, self.workflows, self.config_plan = logins, labels_, boundary, wf, config_plan


HEADER = r'''#!/bin/sh
# Generated by .claude/scripts/github/repo_script.py (Phase 11a, DEC-13). HUMAN-RUN ONLY, from a plain terminal.
# It prints every command and runs none of them unless you add --apply. Read it first: it is short on purpose.
#
#   sh .claude/bootstrap/setup.sh local  [--apply]    edits files in this checkout; you commit the result
#   sh .claude/bootstrap/setup.sh remote [--apply]    talks to GitHub: needs `gh auth login` done by you
#
# Parameters (checked when this file was generated):
%(params)s
set -eu
if [ -n "${CLAUDECODE+x}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT+x}" ]; then
  echo "setup: refusing to run inside a Claude Code session (CLAUDECODE is set). Run it from a plain terminal." >&2
  exit 2
fi
PHASE="${1:-}"
APPLY=no
case "$PHASE" in local|remote) ;; *) echo "usage: sh $0 local|remote [--apply]" >&2; exit 2 ;; esac
shift
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=yes ;;
    *) echo "setup: unknown argument: $arg" >&2; exit 2 ;;
  esac
done
cd "$(dirname "$0")/../.."
[ -f .claude/scripts/github/labels.py ] || { echo "setup: run this from a repository that has the kit installed (.claude/scripts/github/)" >&2; exit 2; }

show() {
  printf '+'
  for a in "$@"; do
    case "$a" in
      ''|*[!A-Za-z0-9_./:=@%%+,-]*) printf " '%%s'" "$a" ;;
      *) printf ' %%s' "$a" ;;
    esac
  done
  printf '\n'
}
step() { show "$@"; if [ "$APPLY" = yes ]; then "$@"; fi; }
# kit scripts are dry runs on their own; the flag that makes them act is added only with --apply
kit_apply() { show "$@"; if [ "$APPLY" = yes ]; then "$@" --apply; else "$@"; fi; }
kit_write() { show "$@"; if [ "$APPLY" = yes ]; then "$@" --write; else "$@"; fi; }
copy_new() { if [ -e "$2" ]; then echo "exists, left alone: $2"; else step cp "$1" "$2"; fi; }
say() { printf '\n== %%s\n' "$1"; }
if [ "$APPLY" = yes ]; then echo "setup $PHASE: APPLY"; else echo "setup $PHASE: DRY RUN (nothing runs; add --apply)"; fi
'''


def generate(p: Params) -> str:
    kv = [("repo", p.repo), ("create-repo", "yes" if p.create_repo else "no"), ("visibility", p.visibility), ("default-branch", p.default_branch),
          ("variant", p.variant), ("approvers", ",".join(p.approvers) or "(none)"), ("labels", "yes" if p.labels else "no"),
          ("boundary", "yes" if p.boundary else "no"), ("workflows", ",".join(p.workflows) or "(none)"), ("config-plan", p.config_plan or "(none)")]
    params = "\n".join("#   %-15s %s" % (k, v) for k, v in kv)
    out = [HEADER % {"params": params}]
    # ---- local phase
    out.append('if [ "$PHASE" = local ]; then')
    n = 0
    if p.config_plan:
        n += 1
        out += ['  say "%d. loop.config.md: apply the planned keys (an H6 change; read the diff it prints)"' % n,
                "  kit_write python3 .claude/scripts/bootstrap_config.py apply %s" % q(p.config_plan)]
    for w in p.workflows:
        n += 1
        out += ['  say "%d. workflow %s"' % (n, w),
                "  step mkdir -p .github/workflows",
                "  copy_new %s %s" % (q(".claude/templates/github/workflows/%s.yml" % w), q(".github/workflows/%s.yml" % w))]
    if p.workflows:
        out.append('  echo "Pin every uses: in .github/workflows/ to a commit SHA, run python3 .claude/scripts/ci_lint.py, and read each file once."')
    if n == 0:
        out.append('  echo "Nothing to do in the local phase."')
    out.append('  echo "Now commit what changed (an H6 change goes through your normal review), then run the remote phase."')
    out.append("  exit 0")
    out.append("fi")
    # ---- remote phase
    n = 0
    out.append("")
    if p.create_repo:
        n += 1
        out += ['say "%d. create the repository and make the first push"' % n,
                "git rev-parse --verify HEAD >/dev/null 2>&1 || { echo 'setup: there is no commit yet. Commit the bootstrap files first.' >&2; exit 1; }",
                '[ "$(git symbolic-ref --short HEAD)" = %s ] || { echo %s >&2; exit 1; }' % (q(p.default_branch), q("setup: check out %s first" % p.default_branch)),
                "step gh repo create %s --%s --source . --remote origin%s" % (q(p.repo), p.visibility, (" --description " + q(p.description)) if p.description else ""),
                "step git push -u origin %s" % q(p.default_branch)]
    if p.boundary:
        n += 1
        extra = ""
        if p.release_team_id:
            extra += " --release-team-id %d" % p.release_team_id
        if p.environment:
            extra += " --environment %s" % q(p.environment)
        for r in p.reviewer_ids:
            extra += " --reviewer-id %d" % r
        out += ['say "%d. rulesets, tag protection, squash setting, workflow token permissions (apply-boundary.sh)"' % n,
                "kit_apply sh .claude/scripts/github/apply-boundary.sh --repo %s --variant %s%s" % (q(p.repo), p.variant, extra)]
    if p.labels:
        n += 1
        out += ['say "%d. labels (apply-labels.sh)"' % n, "kit_apply sh .claude/scripts/github/apply-labels.sh --repo %s" % q(p.repo)]
    if p.approvers:
        n += 1
        out += ['say "%d. repository variable FACTORY_APPROVERS (the same logins as factory_approvers in loop.config)"' % n,
                "step gh variable set FACTORY_APPROVERS --repo %s --body %s" % (q(p.repo), q(",".join(p.approvers)))]
    out += ["", 'say "Still yours to do"']
    todo = ["create the bot identity: docs/security/bot-identity.md (this script never creates or scopes it)",
            "CODEOWNERS: copy .claude/templates/security/CODEOWNERS.template to .github/CODEOWNERS, fill the placeholders, commit it through a pull request",
            "secrets: if you use intake.yml or claude-review.yml, run `gh secret set ANTHROPIC_API_KEY --repo %s` yourself (it prompts; the key is never in a file)" % p.repo,
            "variables: CLAUDE_CODE_VERSION (the version your canary passed), SMOKE_URL if you use smoke.yml",
            "make the job `verify` a required status check in the ruleset (docs/security/rulesets.md)",
            "then check it: .claude/scripts/github/verify-boundary.sh --repo %s --run" % p.repo]
    for t in todo:
        out.append("echo %s" % q("  - " + t))
    return "\n".join(out) + "\n"


def safe_out_path(root: str, rel: str) -> str:
    if not isinstance(rel, str) or not _OUT.match(rel) or ".." in rel:
        raise ScriptError("--out must be .claude/bootstrap/<name> (letters, digits, '.', '_', '-')")
    claude = os.path.join(root, ".claude")
    if os.path.islink(claude) or not os.path.isdir(claude):
        raise ScriptError(".claude is missing or a symlink")
    d = os.path.join(root, OUT_DIR)
    if os.path.islink(d):
        raise ScriptError(".claude/bootstrap is a symlink: refusing to write through it")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(root, rel)
    if os.path.islink(path) or (os.path.exists(path) and not os.path.isfile(path)):
        raise ScriptError("%s is a symlink or not a regular file" % rel)
    return path


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="repo_script.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--create-repo", action="store_true")
    ap.add_argument("--visibility", default="private")
    ap.add_argument("--description", default="")
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--variant", default="solo")
    ap.add_argument("--release-team-id", type=int, default=None)
    ap.add_argument("--environment", default="")
    ap.add_argument("--reviewer-id", type=int, action="append", default=[])
    ap.add_argument("--approvers", default="")
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--boundary", action="store_true")
    ap.add_argument("--workflows", default="")
    ap.add_argument("--config-plan", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--repo-dir", default=".", help="the checkout to write --out into (default: current directory)")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        p = Params(args.repo, args.create_repo, args.visibility, args.description, args.default_branch, args.variant, args.release_team_id,
                   args.environment, args.reviewer_id, [a for a in args.approvers.split(",") if a] if args.approvers else [], args.labels,
                   args.boundary, [w for w in args.workflows.split(",") if w] if args.workflows else [], args.config_plan)
        text = generate(p)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        path = None  # type: Optional[str]
        if args.out:
            top = gitutil.toplevel(os.path.abspath(args.repo_dir), isolated=True)
            root = os.path.realpath(top or os.path.abspath(args.repo_dir))
            path = safe_out_path(root, args.out)
            jsonio.atomic_write_text(path, text, 0o644)
    except ScriptError as exc:
        sys.stderr.write("repo_script: %s\n" % exc)
        return 2
    if args.json:
        print(json.dumps({"schema": "repo_script.v1", "path": args.out or None, "sha256": digest, "bytes": len(text.encode("utf-8"))}, sort_keys=True))
    elif path:
        print("repo_script: wrote %s (sha256 %s). Read it, then run it yourself from a plain terminal." % (args.out, digest))
    else:
        sys.stdout.write(text)
        sys.stderr.write("repo_script: sha256 %s\n" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
