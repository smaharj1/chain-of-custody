#!/usr/bin/env python3
"""ci_lint: a structural linter for GitHub workflow YAML (Phase 1, IMP-23, report §5.3, §8.4).

It enforces the split-CI rules that make a pull request safe to run:

  ci.oidc-with-pr-code        a job with `id-token: write` must not check out PR code        (RC-3, DEC-39)
  ci.secrets-in-pr-job        no secrets in a `pull_request` job                             (RC-3)
  ci.pr-target-head-checkout  `pull_request_target` / `workflow_run` must not check out head  (RC-3)
  ci.skills-not-restored      a `claude -p` step runs the base ref's .claude/, .mcp.json,
                              plugin/ and skill dirs, never the PR's                         (RC-18, §5.3 rule 5)
  ci.claude-settings-sources  `claude -p` passes --setting-sources user or --settings        (§5.3 rule 5)
  ci.script-injection         untrusted `${{ }}` text interpolated into `run:`               (CS-5)
  ci.deploy-in-pr-job         a PR job that runs a deploy-class command                      (RC-1)
  ci.floating-action-tag      `uses:` pinned to a tag or branch, not a SHA (warning)         (IMP-23)
  ci.missing-permissions      no `permissions:` anywhere (warning)
  ci.write-permissions-pr-job write token alongside PR code (warning)
  ci.untrusted-comment-trigger comment/issue trigger with no author_association check (warning)
  ci.persist-credentials      checkout leaves the token in .git/config in a PR job (warning)
  ci.unparsed                 the restricted YAML subset could not read the file: manual review, never "passed"

Exit 0: no errors and nothing unparsed (warnings do not fail). Exit 1: an error or an unparsed
file. Exit 2: usage. `--json` prints machine-readable findings.

Usage: python3 .claude/scripts/ci_lint.py [PATH ...] [--repo DIR] [--json] [--restore-paths a,b,c]
With no PATH it lints DIR/.github/workflows (DIR defaults to the current directory).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio, workflows  # noqa: E402

ERROR, WARN = "error", "warn"
DEFAULT_RESTORE = (".claude", ".mcp.json", "plugin", ".claude-plugin")

_PR_HEAD_RE = re.compile(r"github\.event\.pull_request\.head|github\.head_ref|refs/pull/|github\.event\.workflow_run\.head|"
                         r"github\.event\.pull_request\.merge_commit_sha|github\.event\.number|github\.event\.issue\.number|pull/\$\{\{", re.I)
_BASE_REF_RE = re.compile(r"github\.event\.pull_request\.base|github\.base_ref|github\.event\.repository\.default_branch|github\.ref_name|^\s*(?:main|master)\s*$", re.I)
_CLAUDE_P_RE = re.compile(r"(?:^|[\s;&|(/])(?:npx\s+(?:-y\s+)?(?:@anthropic-ai/claude-code\s+)?)?claude\b[^\n]*?\s(?:-p|--print)\b|\bclaude\s+(?:-p|--print)\b")
_CLAUDE_ACTIONS = ("anthropics/claude-code-action", "anthropics/claude-code-base-action", "anthropics/claude-code-security-review")
_INJECT_RE = re.compile(
    r"\$\{\{[^}]*github\.(?:head_ref|event\.(?:issue|pull_request|comment|review|review_comment|head_commit|commits|pages|discussion|workflow_run|label)"
    r"[^}]*\.(?:title|body|message|name|email|label|ref|head_branch|page_name))[^}]*\}\}", re.I)
_SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)|secrets\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]|(?i:tojson)\(\s*(secrets)\s*\)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)


class Finding(object):
    __slots__ = ("rule", "severity", "file", "job", "message")

    def __init__(self, rule: str, severity: str, file: str, job: str, message: str) -> None:
        self.rule, self.severity, self.file, self.job, self.message = rule, severity, file, job, message

    def to_dict(self) -> Dict[str, str]:
        return {"rule": self.rule, "severity": self.severity, "file": self.file, "job": self.job, "message": self.message}

    def __str__(self) -> str:
        where = self.file + (" [job %s]" % self.job if self.job else "")
        return "%s: %s: %s: %s" % (self.severity.upper(), self.rule, where, self.message)


class Options(object):
    def __init__(self, restore_paths: Sequence[str] = DEFAULT_RESTORE) -> None:
        self.restore_paths = tuple(restore_paths)


def _secret_name(m: "re.Match") -> str:
    return m.group(1) or m.group(2) or "(all secrets)"


def _perm(permissions: Any, name: str) -> Optional[str]:
    if isinstance(permissions, str):
        return "write" if permissions == "write-all" else ("read" if permissions == "read-all" else None)
    if isinstance(permissions, dict):
        v = permissions.get(name)
        return str(v) if v is not None else None
    return None


def _job_perm(wf: workflows.Workflow, job: workflows.Job, name: str) -> Optional[str]:
    """The effective level of one permission: the job's own `permissions:` block replaces the workflow's."""
    return _perm(job.permissions if job.permissions is not None else wf.permissions, name)


def _is_checkout(step: workflows.Step) -> bool:
    return step.uses.lower().startswith("actions/checkout@") or step.uses.lower() == "actions/checkout"


def _step_checks_out_pr_code(step: workflows.Step, events: Dict[str, Any]) -> bool:
    pr_event = any(e in events for e in ("pull_request", "pull_request_review", "pull_request_review_comment", "merge_group"))
    if _is_checkout(step):
        ref = str(step.with_.get("ref", "") or "")
        repo = str(step.with_.get("repository", "") or "")
        if ref and _PR_HEAD_RE.search(ref):
            return True
        if repo and re.search(r"head\.repo|pull_request\.head", repo):
            return True
        if not ref and not repo and pr_event:
            return True  # default checkout on pull_request is the PR merge commit
        return False
    text = step.run
    if re.search(r"\bgh\s+pr\s+checkout\b", text) or re.search(r"\bgit\s+(?:fetch|pull)\b[^\n]*(?:pull/|refs/pull/|head_ref|pull_request\.head)", text):
        return True
    if re.search(r"\bgit\s+(?:clone|checkout|switch|reset|merge|cherry-pick|rebase)\b[^\n]*(?:pull_request\.head|head_ref|refs/pull/|FETCH_HEAD|workflow_run\.head)", text):
        return True
    return False


def checks_out_pr_code(job: workflows.Job, events: Dict[str, Any]) -> bool:
    return any(_step_checks_out_pr_code(s, events) for s in job.steps)


# Events whose default checkout is the default branch, not the pushed or merged commit.
_DEFAULT_BRANCH_EVENTS = ("pull_request_target", "issue_comment", "issues", "schedule", "workflow_run", "discussion", "discussion_comment", "repository_dispatch")


def _checks_out_base_only(job: workflows.Job, events: Dict[str, Any]) -> bool:
    cos = [s for s in job.steps if _is_checkout(s)]
    if not cos or checks_out_pr_code(job, events):
        return False
    default_is_base = all(e in _DEFAULT_BRANCH_EVENTS for e in events)
    for s in cos:
        ref = str(s.with_.get("ref", "") or "")
        if ref:
            if not _BASE_REF_RE.search(ref):
                return False
        elif not default_is_base:
            return False
    return True


def _claude_steps(job: workflows.Job) -> List[workflows.Step]:
    out = []  # type: List[workflows.Step]
    for s in job.steps:
        if any(s.uses.lower().startswith(a) for a in _CLAUDE_ACTIONS) or _CLAUDE_P_RE.search(s.run):
            out.append(s)
    return out


def _restores(steps: Iterable[workflows.Step], paths: Sequence[str]) -> bool:
    for s in steps:
        t = s.run
        if not re.search(r"\bgit\s+(?:checkout|restore)\b", t):
            continue
        ok = True
        for p in paths:
            if not re.search(r"(?<![\w.-])" + re.escape(p) + r"(?![\w-])", t):
                ok = False
                break
        if ok:
            return True
    return False


def _trusted_only_events(events: Dict[str, Any]) -> bool:
    for e, cfg in events.items():
        if e in ("schedule", "workflow_dispatch"):
            continue
        if e == "push":
            br = [str(b) for b in workflows._as_list((cfg or {}).get("branches"))]
            if br and all(b in ("main", "master") for b in br) and not workflows._as_list((cfg or {}).get("tags")):
                continue
        return False
    return True


def lint_workflow(wf: workflows.Workflow, name: str, opts: Options, repo_has_skills_dir: bool = False) -> List[Finding]:
    out = []  # type: List[Finding]
    events = wf.events
    pr_events = [e for e in events if e in ("pull_request", "pull_request_review", "pull_request_review_comment", "merge_group")]
    restore = list(opts.restore_paths)
    if repo_has_skills_dir and "skills" not in restore:
        restore.append("skills")
    any_perm = wf.permissions is not None or any(j.permissions is not None for j in wf.jobs.values())
    if not any_perm:
        out.append(Finding("ci.missing-permissions", WARN, name, "", "no permissions: block anywhere; the token may default to write"))
    wf_secrets = [_secret_name(m) for v in wf.env.values() for m in _SECRET_RE.finditer(str(v)) if _secret_name(m) != "GITHUB_TOKEN"]
    for jid, job in wf.jobs.items():
        pr_code = checks_out_pr_code(job, events)
        job_text = job.text
        id_token = _job_perm(wf, job, "id-token") == "write"
        # R1
        if id_token and pr_code:
            out.append(Finding("ci.oidc-with-pr-code", ERROR, name, jid,
                               "id-token: write together with a checkout of PR code: the PR could mint cloud credentials"))
        # R2
        secrets_used = sorted({_secret_name(m) for m in _SECRET_RE.finditer(job_text) if _secret_name(m) != "GITHUB_TOKEN"} | set(wf_secrets))
        inherits = isinstance(job.secrets, str) and job.secrets.strip() == "inherit"
        if pr_events and (secrets_used or inherits):
            out.append(Finding("ci.secrets-in-pr-job", ERROR, name, jid,
                               "secrets (%s) in a %s job: same-repo PRs, including the bot's, receive them"
                               % (", ".join(secrets_used) or "inherit", "/".join(pr_events))))
        elif ("pull_request_target" in events or "workflow_run" in events or "issue_comment" in events) and pr_code and (secrets_used or inherits):
            out.append(Finding("ci.secrets-in-pr-job", ERROR, name, jid, "secrets (%s) alongside a checkout of PR code" % (", ".join(secrets_used) or "inherit")))
        # R3
        if pr_code and any(e in events for e in ("pull_request_target", "workflow_run", "issue_comment", "issues")):
            out.append(Finding("ci.pr-target-head-checkout", ERROR, name, jid,
                               "a privileged trigger (%s) checks out the PR's code" % "/".join(e for e in events if e in ("pull_request_target", "workflow_run", "issue_comment", "issues"))))
        # R4/R5
        claude = _claude_steps(job)
        if claude and not _trusted_only_events(events):
            first = min(s.index for s in claude)
            before = [s for s in job.steps if s.index < first]
            base_only = _checks_out_base_only(job, events)
            if not base_only and not _restores(before, restore):
                out.append(Finding("ci.skills-not-restored", ERROR, name, jid,
                                   "a claude step runs without first restoring %s from the base ref (git checkout <base> -- ...): the PR's skill hooks would run (RC-18)"
                                   % ", ".join(restore)))
            for s in claude:
                text = s.text
                if "--setting-sources" not in text and "--settings" not in text and not s.uses.lower().startswith("anthropics/"):
                    out.append(Finding("ci.claude-settings-sources", ERROR if pr_events else WARN, name, jid,
                                       "claude -p without --setting-sources user or --settings: project settings from the checkout would apply"))
        # R6
        for s in job.steps:
            for m in _INJECT_RE.finditer(s.run):
                out.append(Finding("ci.script-injection", ERROR, name, jid, "untrusted text %s interpolated into a run: script; pass it through env: instead" % m.group(0)[:70]))
                break
            script = str(s.with_.get("script", "")) if "github-script" in s.uses else ""
            if script and _INJECT_RE.search(script):
                out.append(Finding("ci.script-injection", ERROR, name, jid, "untrusted text interpolated into github-script"))
        # R7
        if pr_events:
            labels = workflows.deploy_labels("\n".join(s.run for s in job.steps))
            for s in job.steps:
                lab = workflows.deploy_uses_label(s)
                if lab:
                    labels.append(lab)
            if labels:
                out.append(Finding("ci.deploy-in-pr-job", ERROR, name, jid, "a pull-request job runs deploy-class commands (%s)" % ", ".join(labels)))
        # R8
        for s in job.steps + ([workflows.Step({"uses": job.uses}, -1)] if job.uses else []):
            u = s.uses
            if not u or u.startswith("./") or u.startswith("docker://"):
                continue
            ref = u.split("@", 1)[1] if "@" in u else ""
            if not _SHA_RE.match(ref):
                out.append(Finding("ci.floating-action-tag", WARN, name, jid, "%s is pinned to %s, not a commit SHA" % (u.split("@", 1)[0], ref or "nothing")))
        # R10 / R12
        if pr_code:
            writes = [p for p in ("contents", "pull-requests", "actions", "packages", "id-token", "deployments") if _job_perm(wf, job, p) == "write"]
            if writes and not id_token:
                out.append(Finding("ci.write-permissions-pr-job", WARN, name, jid, "write permissions (%s) alongside a checkout of PR code" % ", ".join(writes)))
            for s in job.steps:
                if _is_checkout(s) and str(s.with_.get("persist-credentials", "")).lower() != "false":
                    out.append(Finding("ci.persist-credentials", WARN, name, jid, "checkout without persist-credentials: false leaves the token in .git/config"))
                    break
        # R13: a workflow_run job that downloads what a pull request run produced and holds privilege
        if "workflow_run" in events and (secrets_used or id_token or inherits) and \
                any(st.uses.lower().startswith("actions/download-artifact") or re.search(r"\bgh\s+run\s+download\b", st.run) for st in job.steps):
            out.append(Finding("ci.artifact-from-pr-run", ERROR, name, jid,
                               "a workflow_run job with secrets or OIDC downloads artifacts built by another run: a pull request can poison them"))
        # R11
        stranger_text = [e for e in events if e in ("issue_comment", "pull_request_review_comment", "discussion_comment", "discussion")]
        for e, cfg in events.items():
            if e == "issues":
                types = [str(t) for t in workflows._as_list((cfg or {}).get("types"))]
                if not types or any(t in ("opened", "edited", "reopened", "transferred") for t in types):
                    stranger_text.append(e)
        if stranger_text:
            all_ifs = job.if_ + " ".join(s.if_ for s in job.steps)
            if "author_association" not in all_ifs and job.steps:
                out.append(Finding("ci.untrusted-comment-trigger", WARN, name, jid,
                                   "%s trigger with no author_association check: any commenter starts this job" % "/".join(stranger_text)))
    return out


def lint_text(text: str, name: str = "<workflow>", opts: Optional[Options] = None, repo_has_skills_dir: bool = False) -> List[Finding]:
    opts = opts or Options()
    try:
        wf = workflows.parse(text, name)
    except workflows.Unparsed as exc:
        out = [Finding("ci.unparsed", ERROR, name, "", "unparsed: manual review (%s)" % exc)]
        labels = workflows.scan_text(text)
        if labels:
            out.append(Finding("ci.unparsed", ERROR, name, "", "the raw text mentions deploy-class commands: %s" % ", ".join(labels)))
        if re.search(r"id-token:\s*write", text) and re.search(r"pull_request", text):
            out.append(Finding("ci.unparsed", ERROR, name, "", "the raw text has both id-token: write and a pull_request trigger"))
        return out
    return lint_workflow(wf, name, opts, repo_has_skills_dir)


def workflow_files(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path]
    out = []  # type: List[str]
    try:
        for n in sorted(os.listdir(path)):
            if n.endswith((".yml", ".yaml")):
                out.append(os.path.join(path, n))
    except OSError:
        pass
    return out


def lint_paths(paths: Sequence[str], opts: Optional[Options] = None, repo: Optional[str] = None) -> List[Finding]:
    opts = opts or Options()
    has_skills = bool(repo) and os.path.isdir(os.path.join(repo, "skills"))
    out = []  # type: List[Finding]
    for p in paths:
        for f in workflow_files(p):
            text, problem = safeio.read_text_status(f, 1 << 20)
            if text is None:
                out.append(Finding("ci.unparsed", ERROR, f, "", "unreadable (%s): manual review" % problem))
                continue
            name = os.path.relpath(f, repo) if repo else f
            out.extend(lint_text(text, name, opts, has_skills))
    return out


def lint_repo(repo: str, opts: Optional[Options] = None) -> List[Finding]:
    return lint_paths([os.path.join(repo, ".github", "workflows")], opts, repo)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Lint GitHub workflows for split-CI rules (report §8.4, IMP-23).")
    ap.add_argument("paths", nargs="*", help="workflow files or directories (default: <repo>/.github/workflows)")
    ap.add_argument("--repo", default=".", help="repository root (default: current directory)")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    ap.add_argument("--restore-paths", default=",".join(DEFAULT_RESTORE), help="comma-separated paths a claude -p job must restore from the base ref")
    args = ap.parse_args(argv)
    repo = os.path.abspath(args.repo)
    opts = Options([p.strip() for p in args.restore_paths.split(",") if p.strip()])
    paths = args.paths or [os.path.join(repo, ".github", "workflows")]
    findings = lint_paths(paths, opts, repo)
    errors = [f for f in findings if f.severity == ERROR]
    if args.json:
        print(json.dumps({"schema": "ci_lint.v1", "ok": not errors, "findings": [f.to_dict() for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f)
        n_files = sum(len(workflow_files(p)) for p in paths)
        print("ci_lint: %d file(s), %d error(s), %d warning(s)" % (n_files, len(errors), len(findings) - len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - an internal error must not read as "clean"
        sys.stderr.write("ci_lint: internal error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
