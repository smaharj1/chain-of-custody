#!/usr/bin/env python3
"""ci_lint: a structural linter for GitHub workflow YAML (Phase 1, IMP-23, report §5.3, §8.4).

It enforces the split-CI rules that make a pull request safe to run:

  ci.oidc-with-pr-code        a job with `id-token: write` must not check out PR code        (RC-3, DEC-39)
  ci.secrets-in-pr-job        no secrets in a `pull_request` job                             (RC-3)
  ci.pr-target-head-checkout  `pull_request_target` / `workflow_run` must not check out head  (RC-3)
  ci.skills-not-restored      a `claude -p` step runs the base ref's .claude/, .mcp.json,
                              .claude-plugin/ and skill dirs, never the PR's                (RC-18, §5.3 rule 5)
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
  ci.unreadable               the lint itself failed on the file (an exception, a recursion limit, more quotes and expressions than it reads): an error, never "passed"
  ci.kit-placeholder          a file in .github/workflows still holds a literal `{{KIT_` placeholder (the templates' `{{KIT_REF}}` and
                              `{{KIT_SHA}}`, which the bootstrap setup script fills): the kit step would fetch nothing (an error)
  infra.*                     rules for workflows that run terraform, tofu, pulumi, cdk, kubectl or helm: the
                              apply gate, the saved plan, policy before init (infra/ci_rules.py, Phase 6, §8.4)

Exit 0: no errors and nothing unparsed (warnings do not fail). Exit 1: an error or an unparsed
file. Exit 2: usage. `--json` prints machine-readable findings.

Usage: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/ci_lint.py [PATH ...] [--repo DIR] [--json] [--restore-paths a,b,c]
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
DEFAULT_RESTORE = (".claude", ".mcp.json", ".claude-plugin")

_PR_HEAD_RE = re.compile(r"github\.event\.pull_request\.head|github\.head_ref|refs/pull/|github\.event\.workflow_run\.head|"
                         r"github\.event\.pull_request\.merge_commit_sha|github\.event\.number|github\.event\.issue\.number|pull/\$\{\{", re.I)
# On a pull_request (and merge_group) the workflow's own commit is the PR merge commit: `github.sha` and `github.ref` name PR code (R-INFRA-1)
_MERGE_REF_RE = re.compile(r"github\.sha\b|github\.ref(?![\w])|github\s*\[\s*['\"](?:sha|ref)['\"]\s*\]|\bGITHUB_(?:SHA|REF)\b", re.I)
# the refs a privileged-trigger job may check out: its own default branch. A `steps.*` or `needs.*` output is whatever an earlier step computed, which
# is how a job fetches the PR head sha (R-INFRA-1), so unlike ci_rules._REF_OK these are not allowed here.
_REF_OK_PRIV = re.compile(r"^\s*(?:main|master|refs/heads/main|\$\{\{\s*(?:github\.sha|github\.ref|github\.event\.repository\.default_branch)\s*\}\})\s*$")
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
_EVENT_REF = re.compile(r"github\.event(?![\w])(?:\.[\w-]+|\[[^\]\[]*\])*", re.I)    # no `[` inside an index: an unclosed run of them would rescan to the end each
# expression property names are case-insensitive and `github['event']` is the same as `github.event`
_UNSAFE_REF = re.compile(r"(?<![\w.])inputs\b|github\.head_ref|github\.ref_name|github\.ref(?![\w])|client_payload|"
                         r"github\s*\[\s*['\"](?:event|head_ref|ref_name|ref)['\"]\s*\]", re.I)


def _outside_strings(p: str) -> List[Tuple[int, str, int]]:
    """(index, char, nesting depth) of every character of an expression that is outside a single-quoted string (`''` is an escaped quote)."""
    out = []  # type: List[Tuple[int, str, int]]
    depth, i, n = 0, 0, len(p)
    while i < n:
        c = p[i]
        if c == "'":
            i += 1
            while i < n:
                if p[i] == "'":
                    if i + 1 < n and p[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if c in ")]":
            depth = max(depth - 1, 0)
        out.append((i, c, depth))
        if c in "([":
            depth += 1
        i += 1
    return out


def _split_top(inner: str) -> List[str]:
    """The operands of `&&` and `||` at the top level of an expression (not inside a call, an index or a string)."""
    pieces, last = [], 0
    chars = _outside_strings(inner)
    for k, (i, c, d) in enumerate(chars):
        if d == 0 and c in "&|" and k + 1 < len(chars) and chars[k + 1][1] == c and chars[k + 1][0] == i + 1:
            pieces.append(inner[last:i])
            last = i + 2
    pieces.append(inner[last:])
    return pieces


def _unwrap(p: str) -> str:
    """`p` without the parentheses that enclose all of it (`((a == b))` -> `a == b`; `(a) == (b)` is left alone). One pass, however deep they nest."""
    match, stack = {}, []  # type: Dict[int, int], List[int]
    for i, c, _ in _outside_strings(p):
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            match[stack.pop()] = i
    lo, hi = 0, len(p)
    while True:
        while lo < hi and p[lo].isspace():
            lo += 1
        while hi > lo and p[hi - 1].isspace():
            hi -= 1
        if hi - lo >= 2 and p[lo] == "(" and match.get(lo) == hi - 1:
            lo, hi = lo + 1, hi - 1
        else:
            return p[lo:hi]


def _yields_boolean(piece: str) -> bool:
    """The value of this operand is true or false, which cannot carry text into a script: its top-level operator is a comparison or `!`, or it is one
    whole `contains()`/`startsWith()`/`endsWith()` call. A comparison inside a call (`format('{0}', title, 1 == 1)`) is not the top-level operator
    (R-INFRA-4)."""
    p = _unwrap(piece)
    if p.startswith("!") and not p.startswith("!="):
        return True
    chars = _outside_strings(p)
    for k, (i, c, d) in enumerate(chars):
        if d != 0:
            continue
        nxt = chars[k + 1][1] if k + 1 < len(chars) and chars[k + 1][0] == i + 1 else ""
        if (c in "=!<>" and nxt == "=") or (c in "<>" and nxt != "="):
            return True
    m = re.match(r"(?:contains|startsWith|endsWith)\s*\(", p)
    if m:
        # the call's own closing parenthesis must be the last character
        for i, c, d in chars:
            if c == ")" and d == 0:
                return i == len(p) - 1
    return False


_ENV_REF = re.compile(r"(?<![\w.])env\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*'([A-Za-z_]\w*)'\s*\])", re.I)


def injected(text: str, tainted: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """(errors, warnings): the `${{ }}` expressions in a script whose value a stranger can choose (IN-8). The fix is always `env:`.
    `&&` and `||` pass their operands' values through, so each operand is judged on its own. `tainted` names `env:` entries whose value is itself
    untrusted text (`TITLE: ${{ github.event.issue.title }}`): `${{ env.TITLE }}` in a script is the same injection (R-INFRA-4)."""
    bad, warn = [], []  # type: List[str], List[str]
    taint = {t.upper() for t in tainted}
    for whole, inner in _expressions(text):
        flagged = actor = False
        for piece in _split_top(inner):
            if _yields_boolean(piece):
                continue
            if [t for t in _EVENT_REF.findall(piece) if not _EVENT_SAFE.fullmatch(t)] or _UNSAFE_REF.search(piece) \
                    or any((m.group(1) or m.group(2)).upper() in taint for m in _ENV_REF.finditer(piece)):
                flagged = True
            elif re.search(r"github\.actor\b", piece):
                actor = True
        if flagged:
            bad.append(whole.strip()[:70])
        elif actor:
            warn.append(whole.strip()[:70])
    return bad, warn


def tainted_env(maps: Iterable[Any], seed: Iterable[str] = ()) -> List[str]:
    """The names, among the given `env:` mappings, whose value interpolates untrusted text, directly or through another tainted entry (`seed` names
    are tainted already: the workflow's and job's, when the maps are one step's). One pass over the entries and a work list, so a long chain of
    entries costs no more than a short one."""
    taint = {t.upper() for t in seed}
    uses = {}      # type: Dict[str, List[str]]    # name -> names of the entries whose value reads it
    for m in maps:
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            name, val = str(k).upper(), str(v)
            if injected(val)[0]:
                taint.add(name)
            for _, inner in _expressions(val):
                for r in _ENV_REF.finditer(inner):
                    uses.setdefault((r.group(1) or r.group(2)).upper(), []).append(name)
    work = list(taint)
    while work:
        for user in uses.get(work.pop(), []):
            if user not in taint:
                taint.add(user)
                work.append(user)
    return sorted(taint)


_FETCHER_RE = re.compile(r"\b(?:curl|wget)\b", re.I)
_GH_API_RE = re.compile(r"\bgh\s+api\b", re.I)
_ARCHIVE_WORD_RE = re.compile(r"tarball|zipball|/archive/|\.tar(?:\.gz)?\b|\.tgz\b|\.zip\b", re.I)
_EXTRACT_RE = re.compile(r"\b(?:tar|unzip|gunzip|bsdtar|7z)\b")
_REPO_OK = re.compile(r"^\s*\$\{\{\s*github\.repository\s*\}\}\s*$")
# `secrets.NAME`, `secrets['NAME']`, `toJSON(secrets)`, and a computed index (`secrets[format(...)]`: the name is not visible, so it may be any secret)
_SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)|secrets\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]|(?i:tojson)\(\s*(secrets)\s*\)|secrets\s*\[(?!\s*['\"][A-Za-z0-9_]+['\"]\s*\])")
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
    return m.group(1) or m.group(2) or ("(all secrets)" if m.group(3) else "(a computed secret name)")


_perm = workflows.perm_level


def _job_perm(wf: workflows.Workflow, job: workflows.Job, name: str) -> Optional[str]:
    """The effective level of one permission: the job's own `permissions:` block replaces the workflow's."""
    return _perm(job.permissions if job.permissions is not None else wf.permissions, name)


def _is_checkout(step: workflows.Step) -> bool:
    return step.uses.lower().startswith("actions/checkout@") or step.uses.lower() == "actions/checkout"


# `A ... B` on one line without the quadratic scan of `A[^\n]*B` (a line of ten thousand `git fetch` words took seconds)
_GIT_FETCH_PR = workflows._Near(r"\bgit\s+(?:fetch|pull)\b", r"pull/|refs/pull/|head_ref|pull_request\.head")
_GIT_CHECKOUT_PR = workflows._Near(r"\bgit\s+(?:clone|checkout|switch|reset|merge|cherry-pick|rebase)\b", r"pull_request\.head|head_ref|refs/pull/|FETCH_HEAD|workflow_run\.head")


def _step_checks_out_pr_code(step: workflows.Step, events: Dict[str, Any]) -> bool:
    pr_event = any(e in events for e in ("pull_request", "pull_request_review", "pull_request_review_comment", "merge_group"))
    if _is_checkout(step):
        ref = str(step.with_.get("ref", "") or "")
        repo = str(step.with_.get("repository", "") or "")
        if ref and (_PR_HEAD_RE.search(ref) or (pr_event and _MERGE_REF_RE.search(ref))):
            return True
        if repo and re.search(r"head\.repo|pull_request\.head", repo):
            return True
        if not ref and not repo and pr_event:
            return True  # default checkout on pull_request is the PR merge commit
        return False
    text = step.run
    if re.search(r"\bgh\s+pr\s+checkout\b", text) or _GIT_FETCH_PR.search(text) or _GIT_CHECKOUT_PR.search(text):
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


_GRANT_NO, _GRANT_UNKNOWN, _GRANT_YES = 0, 1, 2     # what a caller passes down, in the order a later caller can only raise it


class Callers(object):
    """What is known about the workflows that call a reusable workflow (`on: workflow_call`): `events` is the union of their triggers (a callee runs
    on its callers' events, and its default checkout is the caller's commit, the PR merge commit for a pull_request caller), `id_token` that a
    caller grants `id-token: write` (directly, or down a chain of reusable workflows that set no permissions; None when the only thing above is a
    reusable workflow nothing here calls, so whether a token comes down is not known), `known` that at least one caller was found."""

    def __init__(self, events: Optional[Dict[str, Any]] = None, id_token: Optional[bool] = False, known: bool = False) -> None:
        self.events = events or {}
        self.id_token = id_token
        self.known = known


def _merge_events(into: Dict[str, Any], other: Dict[str, Any]) -> None:
    for e, cfg in other.items():
        if e == "workflow_call":
            continue
        if e not in into or (e == "push" and workflows.push_main_only(into[e]) and not workflows.push_main_only(cfg)):
            into[e] = cfg


def lint_workflow(wf: workflows.Workflow, name: str, opts: Options, repo_has_skills_dir: bool = False,
                  callers: Optional[Callers] = None) -> List[Finding]:
    out = []  # type: List[Finding]
    # A reusable workflow runs on its callers' events (R-INFRA-1). Callers found in the lint set are merged in; with none found it may be called from
    # elsewhere, a pull request included, so it is read as if a pull_request called it. The infra rules read `wf.events`, so it is set here too.
    events = dict(wf.events)
    inherits_caller = "workflow_call" in events
    if inherits_caller:
        if callers is not None and callers.known:
            _merge_events(events, callers.events)
        else:
            _merge_events(events, {"pull_request": {}})
        wf.events = events
    pr_events = [e for e in events if e in ("pull_request", "pull_request_review", "pull_request_review_comment", "merge_group")]
    restore = list(opts.restore_paths)
    if repo_has_skills_dir and "skills" not in restore:
        restore.append("skills")
    jp = _job_perm
    if inherits_caller:
        grants = callers.id_token if (callers is not None and callers.known) else None

        def jp(w: workflows.Workflow, j: workflows.Job, n: str) -> Optional[str]:
            """A reusable workflow with no `permissions:` of its own gets its caller's: id-token when a caller grants it (callers unknown: when the job
            uses a credential action, which needs one)."""
            v = _job_perm(w, j, n)
            if n == "id-token" and v is None and j.permissions is None and w.permissions is None:
                g = grants if grants is not None else any(st.uses.lower().startswith(a) for st in j.steps for a in workflows.CRED_ACTIONS)
                return "write" if g else None
            return v
    ctx = ci_rules.Ctx(lambda step: _step_checks_out_pr_code(step, events), jp)
    bot_events = ci_rules.bot_reachable(events)
    any_perm = wf.permissions is not None or any(j.permissions is not None for j in wf.jobs.values())
    if not any_perm:
        out.append(Finding("ci.missing-permissions", WARN, name, "", "no permissions: block anywhere; the token may default to write"))
    wf_secrets = [_secret_name(m) for v in wf.env.values() for m in _SECRET_RE.finditer(str(v)) if _secret_name(m) != "GITHUB_TOKEN"]
    for jid, job in wf.jobs.items():
        pr_code = checks_out_pr_code(job, events)
        job_text = job.text
        id_token = jp(wf, job, "id-token") == "write"
        job_taint = tainted_env([wf.env, job.env])
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
            taint = tainted_env([s.env], job_taint) if s.env else job_taint
            bad, warn = injected(s.run, taint)
            if bad:
                out.append(Finding("ci.script-injection", ERROR, name, jid, "untrusted text %s interpolated into a run: script; pass it through env: instead" % bad[0]))
            elif warn:
                out.append(Finding("ci.script-injection", WARN, name, jid, "%s interpolated into a run: script: an actor name is a login, but pass it through env: anyway" % warn[0]))
            script = str(s.with_.get("script", "")) if "github-script" in s.uses else ""
            sbad, swarn = injected(script, taint) if script else ([], [])
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
        priv_trigger = [e for e in ("pull_request_target", "workflow_run", "issue_comment", "issues") if e in events]
        holds_priv = bool(secrets_used or id_token or inherits)
        if priv_trigger and holds_priv:
            if any(_downloads_artifact(st, "workflow_run" in events) for st in job.steps):
                out.append(Finding("ci.artifact-from-pr-run", ERROR, name, jid,
                                   "a %s job with secrets or OIDC downloads artifacts built by another run: a pull request can poison them" % "/".join(priv_trigger)))
            for st in job.steps:
                if not _is_checkout(st):
                    continue
                for key, ok in (("ref", _REF_OK_PRIV), ("repository", _REPO_OK)):
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


def _kit_placeholder(text: str, name: str) -> List[Finding]:
    """`ci.kit-placeholder` for a file GitHub runs (one in a `.github/workflows` directory) that still holds `{{KIT_`. The kit's own templates
    (templates/github/workflows/, templates/infra/workflows/) hold the placeholders on purpose and are not in such a directory."""
    if "/.github/workflows/" not in "/" + name.replace(os.sep, "/"):
        return []
    lines = [i + 1 for i, line in enumerate(text.splitlines()) if "{{KIT_" in line]
    if not lines:
        return []
    return [Finding("ci.kit-placeholder", ERROR, name, "",
                    "a kit placeholder `{{KIT_...}}` is still in the file (line %s): the setup script fills KIT_REF and KIT_SHA when it writes the "
                    "workflow; by hand, KIT_REF is v<KIT_VERSION> and KIT_SHA the commit that tag points at" % ", ".join(str(n) for n in lines[:5]))]


def lint_text(text: str, name: str = "<workflow>", opts: Optional[Options] = None, repo_has_skills_dir: bool = False,
              callers: Optional[Callers] = None) -> List[Finding]:
    """The findings for one workflow. Whatever goes wrong while reading it (an exception, `RecursionError`, `workflows.ScanLimit` when the script has
    more quotes and expressions than the span pass reads) is one `ci.unreadable` error: the file is never read as clean (round 4, structural)."""
    out = _kit_placeholder(text, name)
    try:
        return out + _lint_text(text, name, opts, repo_has_skills_dir, callers)
    except Exception as exc:       # RecursionError and MemoryError are Exceptions too
        return out + [Finding("ci.unreadable", ERROR, name, "", "unreadable: the lint stopped on this file (%s: %s); manual review, it is not read as clean"
                              % (type(exc).__name__, str(exc)[:120]))]


def _lint_text(text: str, name: str, opts: Optional[Options], repo_has_skills_dir: bool, callers: Optional[Callers]) -> List[Finding]:
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
    return lint_workflow(wf, name, opts, repo_has_skills_dir, callers)


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


def _callers_of(parsed: Dict[str, workflows.Workflow]) -> Dict[str, Callers]:
    """For each workflow file (by base name) that another file in the set calls with `uses: ./.github/workflows/NAME`: the triggers of its callers
    (a caller that is itself reusable counts as the pull_request it might be called from, when nothing calls it) and whether one grants id-token."""
    calls = {}  # type: Dict[str, List[Tuple[str, workflows.Workflow, workflows.Job]]]
    for base, wf in parsed.items():
        for job in wf.jobs.values():
            if job.uses.startswith("./.github/workflows/"):
                calls.setdefault(os.path.basename(job.uses.split("@", 1)[0]), []).append((base, wf, job))

    # What each callee inherits, found by passes over the call graph until nothing changes: a chain of K files is K passes of work, where following
    # every caller of every caller one by one costs 2**K when each file has two jobs that call the next. A pass only adds events and raises a
    # grant, so it ends (a hop of the chain per pass at most, and a cycle adds nothing the second time round).
    inherited = {callee: {} for callee in calls}    # type: Dict[str, Dict[str, Any]]
    granted = {callee: _GRANT_NO for callee in calls}

    def events_of(base: str) -> Dict[str, Any]:
        ev = dict(parsed[base].events)
        if "workflow_call" in ev:
            _merge_events(ev, inherited[base] if base in calls else {"pull_request": {}})   # nothing calls it here: it might be a pull request
        return ev

    def grant_via(callee: str, cb: str, cwf: workflows.Workflow, cjob: workflows.Job) -> int:
        """What the job `cjob` of `cb` passes down to `callee`: its own `id-token: write`, or, when it and its workflow set no permissions and the
        workflow is reusable itself, whatever its own callers pass down (GitHub hands the caller's token permissions down the chain)."""
        level = _job_perm(cwf, cjob, "id-token")
        if level == "write":
            return _GRANT_YES
        if level is None and cjob.permissions is None and cwf.permissions is None and "workflow_call" in cwf.events and cb != callee:
            return granted[cb] if cb in calls else _GRANT_UNKNOWN
        return _GRANT_NO

    def signature(ev: Dict[str, Any]) -> Any:
        return sorted((e, e == "push" and workflows.push_main_only(cfg)) for e, cfg in ev.items())

    for _ in range(len(parsed) + 2):
        changed = False
        for callee, cs in calls.items():
            events = {}  # type: Dict[str, Any]
            grant = _GRANT_NO
            for cb in dict.fromkeys(c[0] for c in cs):
                if cb != callee:
                    _merge_events(events, events_of(cb))
            for cb, cwf, cjob in cs:
                grant = max(grant, grant_via(callee, cb, cwf, cjob))
            if signature(events) != signature(inherited[callee]) or grant != granted[callee]:
                changed = True
            inherited[callee], granted[callee] = events, grant
        if not changed:
            break
    return {callee: Callers(inherited[callee], {_GRANT_YES: True, _GRANT_UNKNOWN: None}.get(granted[callee], False), True) for callee in calls}


def lint_paths(paths: Sequence[str], opts: Optional[Options] = None, repo: Optional[str] = None) -> List[Finding]:
    opts = opts or Options()
    has_skills = bool(repo) and os.path.isdir(os.path.join(repo, "skills"))
    out = []  # type: List[Finding]
    texts = []  # type: List[Tuple[str, str]]
    for p in paths:
        for f in workflow_files(p):
            text, problem = safeio.read_text_status(f, 1 << 20)
            if text is None:
                out.append(Finding("ci.unparsed", ERROR, f, "", "unreadable (%s): manual review" % problem))
                continue
            texts.append((os.path.relpath(f, repo) if repo else f, text))
    parsed = {}  # type: Dict[str, workflows.Workflow]
    for name, text in texts:
        try:
            parsed[os.path.basename(name)] = workflows.parse(text, name)
        except Exception:          # Unparsed, or the parser gave out: `lint_text` reports the file below
            pass
    try:
        callers = _callers_of(parsed)
    except Exception as exc:       # the call graph could not be read: every callee is judged as if a pull request called it
        callers = {}
        out.append(Finding("ci.unreadable", ERROR, "", "", "unreadable: the callers of the reusable workflows could not be followed (%s: %s); manual review"
                           % (type(exc).__name__, str(exc)[:120])))
    for name, text in texts:
        out.extend(lint_text(text, name, opts, has_skills, callers.get(os.path.basename(name))))
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
