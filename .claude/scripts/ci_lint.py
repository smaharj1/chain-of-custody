#!/usr/bin/env python3
"""ci_lint: a structural linter for GitHub workflow YAML (Phase 1, IMP-23, report §5.3, §8.4).

It enforces the split-CI rules that make a pull request safe to run:

  ci.oidc-with-pr-code        a job with `id-token: write` must not check out PR code        (RC-3, DEC-39)
  ci.secrets-in-pr-job        no secrets in a `pull_request` job                             (RC-3)
  ci.pr-target-head-checkout  `pull_request_target` / `workflow_run` must not check out head  (RC-3)
  ci.skills-not-restored      a `claude -p` step runs the base ref's .claude/, .mcp.json,
                              plugin/ and skill dirs, never the PR's                         (RC-18, §5.3 rule 5)
  ci.claude-settings-sources  `claude -p` passes --setting-sources user or --settings        (§5.3 rule 5)
  ci.script-injection         untrusted `${{ }}` text interpolated into `run:`: any github.event.* (but a
                              number, action or repository field), inputs.*, head_ref, ref_name,
                              ref, client_payload, toJSON(github.event) (actor: warning)     (CS-5, IN-8)
  ci.deploy-in-pr-job         a PR job that runs a deploy-class command                      (RC-1)
  ci.floating-action-tag      `uses:` pinned to a tag or branch, not a SHA (warning)         (IMP-23)
  ci.docker-uses-unpinned     `uses: docker://IMAGE` without @sha256: (error with credentials) (IN-3)
  ci.credentials-on-bot-push  cloud credentials or secrets in a job a push to any branch
                              but main (or a tag) starts, with no Environment                (IN-1)
  ci.remote-workflow-secrets  a remote reusable workflow that gets secrets or id-token       (IN-7)
  ci.pr-target-checkout-expression, ci.pr-code-fetch
                              a privileged trigger that checks out a computed ref or fetches
                              and unpacks an archive while it holds secrets or id-token      (IN-9)
  ci.missing-permissions      no `permissions:` anywhere (warning)
  ci.write-permissions-pr-job write token alongside PR code (warning)
  ci.untrusted-comment-trigger comment/issue trigger with no author_association check (warning)
  ci.persist-credentials      checkout leaves the token in .git/config in a PR job (warning)
  ci.unparsed                 the restricted YAML subset could not read the file: manual review, never "passed"
  infra.*                     rules for workflows that run terraform, tofu, pulumi, cdk, kubectl or helm: the
                              apply gate, the saved plan, policy before init (infra/ci_rules.py, Phase 6, §8.4)

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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio, workflows  # noqa: E402
from infra import ci_rules  # noqa: E402

ERROR, WARN = "error", "warn"
DEFAULT_RESTORE = (".claude", ".mcp.json", "plugin", ".claude-plugin")

_PR_HEAD_RE = re.compile(r"github\.event\.pull_request\.head|github\.head_ref|refs/pull/|github\.event\.workflow_run\.head|"
                         r"github\.event\.pull_request\.merge_commit_sha|github\.event\.number|github\.event\.issue\.number|pull/\$\{\{", re.I)
_BASE_REF_RE = re.compile(r"github\.event\.pull_request\.base|github\.base_ref|github\.event\.repository\.default_branch|github\.ref_name|^\s*(?:main|master)\s*$", re.I)
_CLAUDE_P_RE = re.compile(r"(?:^|[\s;&|(/])(?:npx\s+(?:-y\s+)?(?:@anthropic-ai/claude-code\s+)?)?claude\b[^\n]*?\s(?:-p|--print)\b|\bclaude\s+(?:-p|--print)\b")
_CLAUDE_ACTIONS = ("anthropics/claude-code-action", "anthropics/claude-code-base-action", "anthropics/claude-code-security-review")


def _expressions(text: str) -> List[Tuple[str, str]]:
    """(whole `${{ ... }}`, inner text) for each expression, found with str.find so a long run of unclosed openers costs one scan, not one per opener."""
    out = []  # type: List[Tuple[str, str]]
    i = 0
    while True:
        a = text.find("${{", i)
        if a < 0:
            return out
        b = text.find("}}", a + 3)
        if b < 0:
            return out
        out.append((text[a:b + 2], text[a + 3:b]))
        i = b + 2


# github.event.* fields that are numbers or fixed words, or the repository's own: the only ones that may be interpolated into a script
_EVENT_SAFE = re.compile(r"github\.event\.(?:number|pull_request\.number|issue\.number|action|repository(?:\.[\w-]+)+)", re.I)
_EVENT_REF = re.compile(r"github\.event(?![\w])(?:\.[\w-]+|\[[^\]]*\])*", re.I)
# expression property names are case-insensitive and `github['event']` is the same as `github.event`
_UNSAFE_REF = re.compile(r"(?<![\w.])inputs\b|github\.head_ref|github\.ref_name|github\.ref(?![\w])|client_payload|"
                         r"github\s*\[\s*['\"](?:event|head_ref|ref_name|ref)['\"]\s*\]", re.I)


def _yields_boolean(piece: str) -> bool:
    """A comparison, a negation or contains()/startsWith()/endsWith() yields true or false, which cannot carry text into a script."""
    p = piece.strip().lstrip("(").strip()
    return p.startswith("!") or bool(re.match(r"(?:contains|startsWith|endsWith)\(", p)) or bool(re.search(r"==|!=|<=|>=|(?<![-=])[<>]", p))


def injected(text: str) -> Tuple[List[str], List[str]]:
    """(errors, warnings): the `${{ }}` expressions in a script whose value a stranger can choose (IN-8). The fix is always `env:`.
    `&&` and `||` pass their operands' values through, so each operand is judged on its own."""
    bad, warn = [], []  # type: List[str], List[str]
    for whole, inner in _expressions(text):
        flagged = actor = False
        for piece in re.split(r"&&|\|\|", inner):
            if _yields_boolean(piece):
                continue
            if [t for t in _EVENT_REF.findall(piece) if not _EVENT_SAFE.fullmatch(t)] or _UNSAFE_REF.search(piece):
                flagged = True
            elif re.search(r"github\.actor\b", piece):
                actor = True
        if flagged:
            bad.append(whole.strip()[:70])
        elif actor:
            warn.append(whole.strip()[:70])
    return bad, warn


_FETCHER_RE = re.compile(r"\b(?:curl|wget)\b", re.I)
_GH_API_RE = re.compile(r"\bgh\s+api\b", re.I)
_ARCHIVE_WORD_RE = re.compile(r"tarball|zipball|/archive/|\.tar(?:\.gz)?\b|\.tgz\b|\.zip\b", re.I)
_EXTRACT_RE = re.compile(r"\b(?:tar|unzip|gunzip|bsdtar|7z)\b")
_REPO_OK = re.compile(r"^\s*\$\{\{\s*github\.repository\s*\}\}\s*$")
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


_perm = workflows.perm_level


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


def _fetches_archive(run: str) -> bool:
    """A line that downloads an archive (curl/wget of a tarball or zip, `gh api .../tarball`). Two linear scans per line, not one scan per `curl`."""
    for line in run.split("\n"):
        if _ARCHIVE_WORD_RE.search(line) and (_FETCHER_RE.search(line) or (_GH_API_RE.search(line) and re.search(r"tarball|zipball", line, re.I))):
            return True
    return False


def _downloads_artifact(st: workflows.Step, workflow_run: bool) -> bool:
    """A step that fetches artifacts another run produced. Any action with `download-artifact` in its name counts (the official one only when it
    names a run, a repository or a token, unless the trigger is workflow_run, whose artifacts are the triggering run's)."""
    if re.search(r"\bgh\s+run\s+download\b", st.run):
        return True
    u = st.uses.lower().split("@", 1)[0]
    if "download-artifact" not in u:
        return False
    if u == "actions/download-artifact" and not workflow_run:
        return any(k in st.with_ for k in ("run-id", "repository", "github-token"))
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
        if e == "push" and workflows.push_main_only(cfg):
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
    ctx = ci_rules.Ctx(lambda step: _step_checks_out_pr_code(step, events), _job_perm)
    bot_events = ci_rules.bot_reachable(events)
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
            bad, warn = injected(s.run)
            if bad:
                out.append(Finding("ci.script-injection", ERROR, name, jid, "untrusted text %s interpolated into a run: script; pass it through env: instead" % bad[0]))
            elif warn:
                out.append(Finding("ci.script-injection", WARN, name, jid, "%s interpolated into a run: script: an actor name is a login, but pass it through env: anyway" % warn[0]))
            script = str(s.with_.get("script", "")) if "github-script" in s.uses else ""
            sbad, swarn = injected(script) if script else ([], [])
            if sbad:
                out.append(Finding("ci.script-injection", ERROR, name, jid, "untrusted text %s interpolated into github-script; pass it through env: instead" % sbad[0]))
            elif swarn:
                out.append(Finding("ci.script-injection", WARN, name, jid, "%s interpolated into github-script: pass it through env: instead" % swarn[0]))
        # R7
        if pr_events:
            labels = workflows.deploy_labels("\n".join(workflows.step_command_text(s) for s in job.steps))
            for s in job.steps:
                lab = workflows.deploy_uses_label(s)
                if lab:
                    labels.append(lab)
            if labels:
                out.append(Finding("ci.deploy-in-pr-job", ERROR, name, jid, "a pull-request job runs deploy-class commands (%s)" % ", ".join(labels)))
        # R8
        cred_job = ci_rules._credentialed(wf, job, ctx)
        for s in job.steps + ([workflows.Step({"uses": job.uses}, -1)] if job.uses else []):
            u = s.uses
            if not u or u.startswith("./"):
                continue
            if u.startswith("docker://"):
                # IN-3: a mutable image tag runs whatever its owner pushes next, with the job's credentials
                if not workflows.docker_pinned(u):
                    out.append(Finding("ci.docker-uses-unpinned", ERROR if cred_job else WARN, name, jid,
                                       "%s is not pinned by digest (@sha256:...)%s" % (u, ": the job holds cloud credentials or secrets" if cred_job else "")))
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
        # R13 / IN-9: a privileged-trigger job that holds secrets or OIDC and takes in what a pull request produced or names
        priv_trigger = [e for e in ("pull_request_target", "workflow_run") if e in events]
        holds_priv = bool(secrets_used or id_token or inherits)
        if priv_trigger and holds_priv:
            if any(_downloads_artifact(st, "workflow_run" in events) for st in job.steps):
                out.append(Finding("ci.artifact-from-pr-run", ERROR, name, jid,
                                   "a %s job with secrets or OIDC downloads artifacts built by another run: a pull request can poison them" % "/".join(priv_trigger)))
            for st in job.steps:
                if not _is_checkout(st):
                    continue
                for key, ok in (("ref", ci_rules._REF_OK), ("repository", _REPO_OK)):
                    val = str(st.with_.get(key, "") or "")
                    if "${{" in val and not ok.match(val):
                        out.append(Finding("ci.pr-target-checkout-expression", ERROR, name, jid,
                                           "a %s job with secrets or OIDC checks out a computed %s (%s): it can name a pull request's code; use the base ref" % ("/".join(priv_trigger), key, val.strip()[:60])))
            fetched = [st.index for st in job.steps if _fetches_archive(st.run)]
            if fetched and any(_EXTRACT_RE.search(st.run) for st in job.steps if st.index >= min(fetched)):
                out.append(Finding("ci.pr-code-fetch", ERROR, name, jid,
                                   "a %s job with secrets or OIDC downloads an archive and unpacks it: that is a checkout of whatever the archive holds" % "/".join(priv_trigger)))
        # IN-1: a push to any branch but main (or a tag) is something the bot can do; a job it starts must not hold credentials with no human gate
        if "push" in bot_events and (not job.environment or "${{" in job.environment) and (holds_priv or ci_rules._credentialed(wf, job, ctx)):
            out.append(Finding("ci.credentials-on-bot-push", ERROR, name, jid,
                               "a job with cloud credentials or secrets starts on a push to a branch other than main (or a tag), which the bot can make: "
                               "restrict the trigger to main, or run the job in an Environment with required reviewers"))
        # IN-7: a remote reusable workflow is code nobody here can read; with secrets or an OIDC token it is a deploy surface
        if job.uses and not job.uses.startswith("./"):
            if job.secrets is not None or id_token:
                ref = job.uses.split("@", 1)[1] if "@" in job.uses else ""
                pinned = bool(_SHA_RE.match(ref))
                sev = ERROR if (bot_events or not pinned) else WARN
                out.append(Finding("ci.remote-workflow-secrets", sev, name, jid,
                                   "%s is a remote reusable workflow that receives %s%s%s" % (
                                       job.uses.split("@", 1)[0], "secrets" if job.secrets is not None else "an OIDC token",
                                       "" if pinned else " and is not pinned to a 40-character commit SHA",
                                       " on a trigger the bot or a pull request can start" if bot_events else "; a person must read it before this is trusted")))
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
    # infrastructure rules live in infra/ci_rules.py (Phase 6); they only fire on workflows that run infrastructure tooling
    for rule, severity, jid, message in ci_rules.check(wf, name, ctx):
        out.append(Finding(rule, severity, name, jid, message))
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
