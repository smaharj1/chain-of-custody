"""Infrastructure rules for ci_lint.py (report §8.2, §8.4, RC-2, RC-3, RC-4; IMP-25).

`ci_lint.py` calls `check(wf, name, ctx)` for every workflow. The rules apply only to workflows that
run infrastructure tooling (terraform, tofu, terragrunt, pulumi, cdk, cloudformation, kubectl, helm),
so an ordinary application workflow is not touched. They are structural: they check that the gates
are present in the workflow text, in the order that makes them mean something. They cannot prove an
Environment has reviewers (the doctor reads that from GitHub) or that a repository variable is set.

  infra.static-credentials        a long-lived cloud key from `secrets` instead of OIDC
  infra.apply-trigger             an apply that a schedule, a pull request or a comment can start
  infra.apply-ungated             an apply with neither an Environment nor a dispatch-only actor check
  infra.apply-same-job-as-plan    plan and apply in one job: the approval would come before the plan
  infra.apply-without-saved-plan  `terraform apply` without a plan file argument (it would plan again)
  infra.apply-plan-hash-unverified the saved plan is not hash-checked before apply
  infra.apply-main-sha-unchecked  nothing compares the planned commit with the tip of main
  infra.apply-digest-unchecked    a dispatch apply that does not compare the plan with the one a human approved
  infra.dispatch-ref-unchecked    a dispatch apply that does not require refs/heads/main
  infra.apply-ref-input           an apply (or its plan) that checks out a ref chosen at dispatch
  infra.apply-flags               -lock=false or -refresh=false on an apply, or on the plan beside one
  infra.apply-tool-unsupported    an apply tool with no saved-plan-by-hash flow here (cdk, pulumi, kubectl, helm)
  infra.environment-approval-unverified an Environment apply that never checks the run's approval history (an Environment created without reviewers
                                  gates nothing, and these rules cannot read GitHub)
  infra.apply-concurrency         no concurrency group: two applies could overlap (warning)
  infra.plan-ungated              a credentialed plan on a pull request event with no human gate
  infra.policy-after-init         init, validate or plan on pull-request source before source_policy ran
  infra.policy-from-pr            source_policy.py run from the pull request's own checkout
  infra.policy-bypassable         source_policy.py with continue-on-error or `|| true`
  infra.input-in-run              a dispatch input interpolated into a run: script
  infra.gate-bypassable           a gate step that can fail without failing the job or its dependents (continue-on-error, `|| true`,
                                  a pipe), or an apply job that runs after a failed dependency (`if: always()`)
  infra.apply-environment-dynamic an `environment:` chosen at run time (`${{ }}`): the dispatcher could name one with no reviewers
  infra.apply-indirect            `$VAR apply`, `$VAR destroy`, `$VAR deploy`, `$VAR up`: the command word is hidden from these rules (error in a
                                  credentialed job, warning elsewhere)

These rules read the commands in the workflow's own `run:` text (and the args of a `docker://` step), after `workflows.normalize_shell` has
removed the spellings that hide a command word: a path (`/usr/local/bin/terraform`), quotes (`terraform 'apply'`), `$(which terraform)`,
`${TF:-terraform}` and a `TF=terraform` assignment. A command hidden in a script, an alias or `$VAR` is outside them (the
doctor's deploy-surface scan follows one level of scripts); `.github/**` is a CODEOWNERS path so a person reads what is installed.
"""
from __future__ import annotations

import functools
import posixpath
import re
import shlex
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from doctorlib import workflows

Finding = Tuple[str, str, str, str]   # (rule, severity, job id, message)

ERROR, WARN = "error", "warn"

_PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "merge_group",
              "issue_comment", "issues", "workflow_run", "repository_dispatch", "discussion", "discussion_comment", "release", "create")

def bot_reachable(events: Dict[str, Any]) -> List[str]:
    """The triggers a bot (or anyone with a pull request) can start: the `_PR_EVENTS` plus a `push` that is not main-only (any other branch,
    `branches-ignore`, `**`, tags). A credentialed job on one of these needs a human gate (IN-1)."""
    out = [e for e in events if e in _PR_EVENTS]
    if "push" in events and not workflows.push_main_only(events["push"]):
        out.append("push")
    return out


_CMD_RE = re.compile(r"(?<![\w./-])(terraform|tofu|terragrunt|pulumi|cdktf|cdk8s|cdk|kubectl|helm|sam)\b")
_INFRA_USES = ("hashicorp/setup-terraform", "opentofu/setup-opentofu", "pulumi/actions", "aws-actions/configure-aws-credentials",
               "hashicorp/terraform-github-actions", "dflook/terraform-", "aws-actions/aws-cloudformation-github-deploy")
_APPLY_USES = ("hashicorp/terraform-github-actions", "dflook/terraform-apply", "dflook/terraform-destroy", "dflook/tofu-apply", "pulumi/actions",
               "gruntwork-io/terragrunt-action", "aws-actions/aws-cloudformation-github-deploy", "azure/arm-deploy", "azure/k8s-deploy",
               "serverless/github-action", "dawidd6/action-ansible-playbook")
_MODE_USES = ("hashicorp/terraform-github-actions", "pulumi/actions", "gruntwork-io/terragrunt-action")   # one action, several commands: read the command
_CRED_SECRETS = workflows.CRED_SECRETS
_CRED_ACTIONS = workflows.CRED_ACTIONS
_VALUE_FLAGS = {"-var", "-var-file", "-target", "-replace", "-parallelism", "-lock-timeout", "-state", "-state-out", "-backup", "-out", "-chdir"}
_BEHIND = workflows.PROG_BEHIND    # what may not stand in front of a program name (shared with the doctor): the start of every pattern below, and the flag run's stop rule


def _fl(prog: str, words: str = "") -> str:
    """The flags between a program and its verb (linear on hostile text; see `workflows.flags_re`), with this module's rule for where a call may start."""
    return workflows.flags_re(prog, behind=_BEHIND, words=words)


# `terraform [flags] SUBCOMMAND args`: the flags are read as the doctor reads them (any flag, a bare variable word, a quoted value with blanks). One line: a
# verb on the next line is not this command's (`[^\S\n]` is any blank but a newline).
_TF_SUBCMD = re.compile(_BEHIND + r"(terraform|tofu)\b(" + _fl("terraform|tofu") + r")[^\S\n]+([a-z][a-z-]*)\b([^\n;&|]*)")
_CFN_RE = re.compile(_BEHIND + r"aws\b" + _fl("aws") + r"\s+cloudformation\b")      # `aws --region x cloudformation deploy`: the global flags come first (D4)
_APPLY_OTHER = [
    ("cdk deploy", re.compile(r"(?<![\w./-])cdk\b" + _fl("cdk") + r"\s+(?:deploy|destroy)\b")),
    ("cdktf deploy", re.compile(r"(?<![\w./-])cdktf\b" + _fl("cdktf") + r"\s+(?:deploy|destroy)\b")),
    ("sam deploy", re.compile(r"(?<![\w./-])sam\b" + _fl("sam") + r"\s+(?:deploy|sync|delete)\b")),
    ("copilot deploy", re.compile(r"(?<![\w./-])copilot\b" + _fl("copilot") + r"\s+(?:(?:svc|env|job|pipeline)\s+)?deploy\b")),
    ("eb deploy", re.compile(r"(?<![\w./-])eb\b" + _fl("eb") + r"\s+(?:deploy|create|terminate|upgrade|swap)\b")),
    ("serverless deploy", re.compile(r"(?<![\w./-])(?:serverless|sls)\b" + _fl("serverless|sls") + r"\s+(?:deploy|remove)\b")),
    ("ansible-playbook", re.compile(r"(?<![\w./-])ansible-playbook\b")),
    ("pulumi up", re.compile(r"(?<![\w./-])pulumi\b" + _fl("pulumi") + r"\s+(?:up|update|destroy)\b")),
    ("cloudformation", re.compile(_BEHIND + r"aws\b" + _fl("aws") + r"\s+cloudformation\s+(?:deploy|execute-change-set|create-stack|update-stack|delete-stack)\b")),
    ("kubectl apply", re.compile(r"(?<![\w./-])kubectl\b" + _fl("kubectl") + r"\s+(?:apply|replace|delete|patch|create)\b")),
    ("helm upgrade", re.compile(r"(?<![\w./-])helm\b" + _fl("helm") + r"\s+(?:upgrade|install|uninstall|rollback)\b")),
    ("terragrunt apply", re.compile(r"(?<![\w./-])terragrunt\b" + _fl("terragrunt", workflows.TERRAGRUNT_WORDS) + r"\s+(?:apply|destroy)\b")),
]  # type: List[Tuple[str, Any]]
# A command that is a variable, a substitution or a backtick pair, in front of a deploy verb. The same bounded text as the doctor's (`workflows.var_command_re`):
# the old `(?:\w+=\S+[ \t]+)*` and `(?:-\S+[ \t]+)*` ran over `;` to the end of the text from every start, and `;A=x` repeated 50,000 times took 44 s (D1).
_INDIRECT_RE = re.compile(workflows.var_command_re(workflows.VAR_WORD + r"|\$\([^)\n]{0,200}\)|`[^`\n]{0,200}`", r"[;&|({]|\bthen\b|\bdo\b|\belse\b",
                                                   "apply|destroy|deploy|up"), re.M)
_GATE_CMDS = ("authorize", "verify-hash", "base-is-main", "require-main-ref", "require-run-approval", "plan-digest")
_GATE_ANY = re.compile(r"gate\.py\s+(?:%s)\b" % "|".join(_GATE_CMDS))
_GATE_TAIL = re.compile(r"\|\|\s*\S|(?<!\|)\|(?!\|)|;\s*(?:true|:|exit\s+0)\b|&\s*$")
_INIT_LIKE = ("init", "validate", "plan", "apply", "destroy", "providers", "get", "test", "console", "refresh", "import", "show")
_REF_OK = re.compile(r"^\s*(?:main|master|refs/heads/main|\$\{\{\s*(?:github\.sha|github\.ref|github\.event\.repository\.default_branch|steps\.[\w.-]+\.outputs\.[\w.-]+|needs\.[\w.-]+\.outputs\.[\w.-]+)\s*\}\})\s*$")


class Ctx(object):
    """What ci_lint hands over: its own PR-checkout test and permission lookup."""
    def __init__(self, step_pr: Callable[[Any], bool], job_perm: Callable[[Any, Any, str], Optional[str]]) -> None:
        self.step_pr = step_pr
        self.job_perm = job_perm


@functools.lru_cache(maxsize=128)
def _norm_run(text: str) -> str:
    """A run: script with comments stripped (`workflows.strip_comments`: quote-aware, and a line whose quoting is unclear keeps its `#`) and the
    spellings that hide a command word normalised (`workflows.normalize_shell`). Comments go first, one physical line at a time, and the continuations
    are joined after: a comment ends at its line, so the `\\` that ends `echo a # c \\` continues nothing and the next line is a command of its own (D3)."""
    return workflows.normalize_shell(workflows.strip_comments(text).replace("\\\r\n", " ").replace("\\\n", " "))


def _cmd(step: Any) -> str:
    """The command text of a step: its run: script, or for a `uses: docker://IMAGE` step the image's command and args."""
    return workflows.step_command_text(step)


def _policy_prefixes(text: str) -> List[str]:
    """For each `source_policy.py` in the text, the non-space characters in front of it (the path it is run from; at most 400 of them). `(\\S*)` in
    front of a literal costs a scan to the start of the word per start position, so a long word is quadratic (R-INFRA-3)."""
    out = []  # type: List[str]
    i = 0
    while True:
        j = text.find("source_policy.py", i)
        if j < 0:
            return out
        k = j
        while k > 0 and j - k < 400 and not text[k - 1].isspace():
            k -= 1
        out.append(text[k:j])
        i = j + len("source_policy.py")


def _gate_tail(run: str) -> bool:
    """A gate.py call followed on its line by `|| x`, a pipe, `; true` or a trailing `&`. Per line, from the first gate call only: a later call on the
    same line has a subset of its tail, and a scan per call is quadratic on a line of repeated `gate.py authorize` words (R-INFRA-3)."""
    for line in run.split("\n"):
        m = _GATE_ANY.search(line)
        if m and _GATE_TAIL.search(line, m.end()):
            return True
    return False


def _tf_calls(run: str) -> List[Tuple[str, str, List[str]]]:
    """(tool, subcommand, args) for every terraform/tofu call in a run: script. A call is listed once however often it is written: the normalised text
    also holds a copy of the lines with a quoted string or an expression in them (`workflows.normalize_shell`), where `-chdir=${{ x }}` is one word."""
    calls = []  # type: List[Tuple[str, str, List[str]]]
    seen = set()  # type: Set[Tuple[str, str, Tuple[str, ...]]]
    for m in _TF_SUBCMD.finditer(_norm_run(run)):
        try:
            args = shlex.split(m.group(4))
        except ValueError:
            args = m.group(4).split()
        args = [a.replace("\x01", " ") for a in args]       # the blanks `normalize_shell` made one word in the copy
        key = (m.group(1), m.group(3), tuple(args))
        if key not in seen:
            seen.add(key)
            calls.append((m.group(1), m.group(3), args))
    return calls


def _has_plan_arg(args: Sequence[str]) -> bool:
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _VALUE_FLAGS:
                skip = True
            continue
        return True
    return False


def _kinds(step: Any) -> Set[str]:
    out = set()  # type: Set[str]
    run = _norm_run(_cmd(step))
    for m in _CMD_RE.finditer(run):
        out.add(m.group(1))
    if _CFN_RE.search(run):
        out.add("cloudformation")
    return out


def is_infra_workflow(wf: Any) -> bool:
    for job in wf.jobs.values():
        for s in job.steps:
            if _kinds(s) or any(s.uses.lower().startswith(u) for u in _INFRA_USES) or apply_calls(s):
                return True
        # `$TOOL apply` in a job that holds cloud credentials: the tool name is hidden, so the workflow is infrastructure until shown otherwise
        if workflows.job_credentialed(wf, job) and any(_INDIRECT_RE.search(_norm_run(s.run)) for s in job.steps):
            return True
    return False


def apply_calls(step: Any) -> List[str]:
    """Labels of apply-class commands in a step."""
    out = []  # type: List[str]
    text = _cmd(step)
    for tool, sub, _ in _tf_calls(text):
        if sub in ("apply", "destroy"):
            out.append("%s %s" % (tool, sub))
    norm = _norm_run(text)
    for label, rx in _APPLY_OTHER:
        if rx.search(norm):
            out.append(label)
    u = step.uses.lower().split("@", 1)[0]
    if any(u == a or u.startswith(a) for a in _APPLY_USES):
        mode = " ".join(str(v) for v in step.with_.values()).lower()
        if u not in _MODE_USES or re.search(r"\b(?:apply|up|update|deploy|destroy)\b", mode):
            out.append("uses " + u)
    if workflows.docker_opaque_iac(step):
        out.append("uses " + step.uses.split("@", 1)[0].lower())      # a known IaC image whose command is not visible: assume it can apply
    return out


def _ancestors(wf: Any, jid: str) -> List[Any]:
    seen = []  # type: List[str]
    todo = [jid]
    while todo:
        cur = todo.pop()
        job = wf.jobs.get(cur)
        if job is None:
            continue
        needs = job.raw.get("needs")
        for n in ([needs] if isinstance(needs, str) else (needs or [])):
            n = str(n)
            if n not in seen and n != jid:
                seen.append(n)
                todo.append(n)
    return [wf.jobs[n] for n in seen if n in wf.jobs]


def _chain_runs(wf: Any, job: Any, before: Optional[int] = None, own_only: bool = False) -> str:
    """The run: scripts (comments stripped) of the job's earlier steps and, unless own_only, of every job it needs. Gate checks are
    read from here, so an env: entry or a comment that merely mentions a check does not count."""
    jobs = ([] if own_only else _ancestors(wf, job.id)) + [job]
    parts = []  # type: List[str]
    for j in jobs:
        for st in j.steps:
            if j is job and before is not None and st.index >= before:
                continue
            parts.append(_norm_run(st.run))
    return "\n".join(parts)


def _chain_text(wf: Any, job: Any, before: Optional[int] = None) -> str:
    parts = [a.text for a in _ancestors(wf, job.id)] + [job.if_]
    parts += [s.text for s in job.steps if before is None or s.index < before]
    return "\n".join(parts)


_ACTOR_ARG_RE = re.compile(r"--actor[ \t=]+\S*\$")
_OPENS_BLOCK_RE = re.compile(r"^[ \t]*(?:if|elif|else|case|while|until|for|select|exit|return)\b|(?:\bthen|\bdo|\belse|&&|\|\||\||\{|\()[ \t]*$")
_GATE_SHELLS = ("", "bash", "sh")   # a `shell:` that runs the script as given; anything else (`true {0}`, `python {0}`) can turn the whole step into a no-op


def _gate_rx(cmd: str) -> "re.Pattern":
    """`[python3] [path/]gate.py CMD ARGS` as a command on its own line."""
    return re.compile(r"^[ \t]*(?:(?:\S*/)?python3?[ \t]+)?(?:\S*/)?gate\.py[ \t]+" + re.escape(cmd) + r"\b([^\n]*)")


def _step_plain(st: Any) -> bool:
    """A step whose commands really run and whose failure fails the job: no `if:` of any kind (a literal `if: false` is falsy in the parsed
    value, so the key is tested, not its text), `continue-on-error` absent or the literal `false`, and no custom `shell:`."""
    if "if" in st.raw:
        return False
    if str(st.raw.get("continue-on-error", "")).strip().lower() not in ("", "false"):
        return False
    return str(st.raw.get("shell", "") or "").strip().lower() in _GATE_SHELLS


def _gate_args(st: Any, cmd: str) -> Optional[str]:
    """The argument text of `gate.py CMD` when this step runs it for real: as the command itself (not an argument of `echo`, not in a comment),
    in a plain step (`_step_plain`), with no `if`/`case`/loop around it, no `&&` or `||` in front of it and no `exit` before it. None otherwise.
    Every gate command is read this way (R-INFRA-2), not only `authorize`."""
    if not _step_plain(st):
        return None
    lines = _norm_run(st.run).split("\n")
    rx = _gate_rx(cmd)
    for i, line in enumerate(lines):
        m = rx.match(line)
        if m:
            if any(_OPENS_BLOCK_RE.search(prev) for prev in lines[:i] if prev.strip()):
                return None
            return m.group(1)
    return None


def _step_authorizes(st: Any) -> bool:
    """One step that runs `gate.py authorize` for real (`_gate_args`), with the actor taken from the run (`--actor "$ACTOR"`,
    `--actor "${{ github.actor }}"`): a constant name makes the check pass for ever."""
    args = _gate_args(st, "authorize")
    return args is not None and bool(_ACTOR_ARG_RE.search(args))


def _chain_steps(wf: Any, job: Any, before: Optional[int] = None, own_only: bool = False) -> List[Any]:
    """The steps of the job's ancestors (unless own_only) and of the job itself (those before step index `before`, when given)."""
    out = []  # type: List[Any]
    for j in ([] if own_only else _ancestors(wf, job.id)) + [job]:
        for st in j.steps:
            if j is job and before is not None and st.index >= before:
                continue
            out.append(st)
    return out


def _has_gate(wf: Any, job: Any, cmd: str, before: Optional[int] = None, own_only: bool = False) -> bool:
    return any(_gate_args(st, cmd) is not None for st in _chain_steps(wf, job, before, own_only))


def _has_actor_check(wf: Any, job: Any, before: Optional[int] = None) -> bool:
    """Only the kit's gate.py authorize counts: a hand-written comparison cannot be checked structurally, so it is reported and a human reads it.
    The step must be in the job (before step `before`, when given) or in a job it needs: a job-level `if:` that skips an earlier job skips
    this one too, and a step in a job this one does not need gates nothing."""
    return any(_step_authorizes(st) for st in _chain_steps(wf, job, before))


_MAIN_REF_EXPR = re.compile(r"github\.ref\s*==\s*['\"]refs/heads/main['\"]|github\.ref_name\s*==\s*['\"]main['\"]")


def _job_if_chain(wf: Any, job: Any) -> List[str]:
    return [j.if_ for j in _ancestors(wf, job.id) + [job]]


def _has_main_ref_check(wf: Any, job: Any, before: Optional[int] = None) -> bool:
    """`gate.py require-main-ref` as a real command, or a job-level `if:` (this job's or an ancestor's, which skips this job too) that compares the
    ref with main and is not widened by `||` or a status function. A step `if:` or an `echo` of the words gates nothing."""
    if _has_gate(wf, job, "require-main-ref", before):
        return True
    return any(_MAIN_REF_EXPR.search(i) and "||" not in i and not re.search(r"\b(?:always|failure|cancelled)\s*\(", i) for i in _job_if_chain(wf, job))


_HASH_RX = re.compile(r"^[ \t]*(?:sha256sum|shasum[ \t]+-a[ \t]+256)[ \t]+(?:-c|--check)\b|\|[ \t]*(?:sha256sum|shasum[ \t]+-a[ \t]+256)[ \t]+(?:-c|--check)\b", re.M)


def _has_hash_check(wf: Any, job: Any, before: Optional[int] = None, own_only: bool = False) -> bool:
    for st in _chain_steps(wf, job, before, own_only):
        if _gate_args(st, "verify-hash") is not None:
            return True
        if _step_plain(st) and _HASH_RX.search(_norm_run(st.run)):
            return True
    return False


_LS_REMOTE_RX = re.compile(r"^[ \t]*(?:[\w]+=\S*[ \t]+)*git[ \t]+ls-remote\b|\$\([ \t]*git[ \t]+ls-remote\b", re.M)


def _has_sha_check(wf: Any, job: Any, before: Optional[int] = None, own_only: bool = False) -> bool:
    for st in _chain_steps(wf, job, before, own_only):
        if _gate_args(st, "base-is-main") is not None:
            return True
        if _step_plain(st):
            run = _norm_run(st.run)
            if _LS_REMOTE_RX.search(run) and re.search(r"GITHUB_SHA|github\.sha|PLANNED_SHA|planned[_-]sha", run + " " + st.text):
                return True
    return False


_DIGEST_RX = re.compile(r"(?:^[ \t]*|\$\([ \t]*|`[ \t]*)(?:(?:\S*/)?python3?[ \t]+)?(?:\S*/)?gate\.py[ \t]+plan-digest\b", re.M)


def _has_digest_check(wf: Any, job: Any, before: Optional[int] = None, own_only: bool = False) -> bool:
    """`gate.py plan-digest` run for real (as a command or in `$(...)`) in a plain step."""
    return any(_step_plain(st) and _DIGEST_RX.search(_norm_run(st.run)) for st in _chain_steps(wf, job, before, own_only))


def _events_only_dispatch(events: Dict[str, Any]) -> bool:
    return bool(events) and set(events) == {"workflow_dispatch"}


def _push_main_only(cfg: Any) -> bool:
    return workflows.push_main_only(cfg)


def _credentialed(wf: Any, job: Any, ctx: Ctx) -> bool:
    if ctx.job_perm(wf, job, "id-token") == "write":
        return True
    if any(s.uses.lower().startswith(a) for s in job.steps for a in _CRED_ACTIONS):
        return True
    return bool(_CRED_SECRETS.search(job.text))


def check(wf: Any, name: str, ctx: Ctx) -> List[Finding]:
    out = []  # type: List[Finding]
    if not is_infra_workflow(wf):
        return out
    events = wf.events
    pr_like = bot_reachable(events)
    has_apply_anywhere = False
    wf_text = "\n".join(j.text for j in wf.jobs.values())

    # a default shell that is not a real shell turns every `run:` step, the gate commands included, into a no-op
    for where, d in [("the workflow", wf.raw.get("defaults"))] + [("job %s" % j.id, j.raw.get("defaults")) for j in wf.jobs.values()]:
        run_d = d.get("run") if isinstance(d, dict) else None
        sh = str(run_d.get("shell", "") or "").strip().lower() if isinstance(run_d, dict) else ""
        if sh not in _GATE_SHELLS:
            out.append(("infra.gate-bypassable", ERROR, "", "%s sets defaults.run.shell to %r: a shell that does not run the script as written makes every gate command a no-op" % (where, sh)))

    # static credentials
    for jid, job in wf.jobs.items():
        m = _CRED_SECRETS.search(job.text)
        if m:
            out.append(("infra.static-credentials", ERROR, jid, "a long-lived cloud credential (secrets.%s): use an OIDC role whose trust is pinned to an environment or workflow (docs/security/infra-human-gate.md)" % (m.group(1) or "[...]")))
        for s in job.steps:
            for key in ("aws-access-key-id", "aws-secret-access-key", "credentials_json"):
                if key in s.with_ and "oidc" not in str(s.with_.get(key)).lower():
                    out.append(("infra.static-credentials", ERROR, jid, "step passes %s to %s: use OIDC (role-to-assume) instead of a stored key" % (key, s.uses.split("@")[0] or "an action")))
                    break
        for st in job.steps:
            run = _norm_run(st.run)
            if not _GATE_ANY.search(run):
                continue
            quiet = str(st.raw.get("continue-on-error", "")).lower() == "true"
            if quiet or _gate_tail(run) or re.search(r"\bset\s+\+e\b", run):
                out.append(("infra.gate-bypassable", ERROR, jid, "a gate.py check can fail without failing the step (continue-on-error, `||`, a pipe, `set +e`)"))
                break
        for st in job.steps:
            if _INDIRECT_RE.search(_norm_run(st.run)):
                out.append(("infra.apply-indirect", ERROR if _credentialed(wf, job, ctx) else WARN, jid,
                            "`$VAR apply` (or destroy, deploy, up) hides the command word from these rules; write the tool name so the gates can be checked"))
                break
        # dispatch inputs in run scripts
        for s in job.steps:
            if re.search(r"\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)", s.run):
                out.append(("infra.input-in-run", ERROR, jid, "a workflow input is interpolated into a run: script; pass it through env: and quote it"))
                break

    for jid, job in wf.jobs.items():
        applies = [(s, lab) for s in job.steps for lab in apply_calls(s)]
        if not applies:
            continue
        has_apply_anywhere = True
        labels = sorted({lab for _, lab in applies})
        # trigger
        bad = []  # type: List[str]
        for e, cfg in events.items():
            if e == "workflow_dispatch":
                continue
            if e == "push" and _push_main_only(cfg):
                continue
            bad.append(e)
        if bad:
            sched = " (a schedule is for drift detection: it plans, it never applies)" if "schedule" in bad else ""
            out.append(("infra.apply-trigger", ERROR, jid, "an apply (%s) can be started by %s%s; only a push to main or a human dispatch may" % (", ".join(labels), ", ".join(bad), sched)))
        if job.environment and "${{" in job.environment:
            out.append(("infra.apply-environment-dynamic", ERROR, jid, "environment %r is chosen at run time: whoever dispatches could name one with no required reviewers" % job.environment))
        for j in [job] + _ancestors(wf, jid):
            if str(j.raw.get("continue-on-error", "")).lower() == "true":
                out.append(("infra.gate-bypassable", ERROR, j.id, "continue-on-error: true on a job in an apply path: a failed gate would not stop the apply"))
        bad_if = re.search(r"\b(?:always|failure|cancelled)\s*\(", job.if_)
        if bad_if:
            out.append(("infra.gate-bypassable", ERROR, jid, "the apply job's if: contains %s(): it would run after a failed gate" % bad_if.group(0).rstrip("( ")))
        # gate
        first_apply = min(s.index for s, _ in applies)
        dispatch_only = _events_only_dispatch(events)
        if not job.environment:
            if not (dispatch_only and _has_actor_check(wf, job, first_apply)):
                out.append(("infra.apply-ungated", ERROR, jid,
                            "an apply (%s) with no `environment:` (required reviewers) and no dispatch-only actor check against factory_approvers: nothing needs a human" % ", ".join(labels)))
        if "workflow_dispatch" in events and not _has_main_ref_check(wf, job, first_apply):
            out.append(("infra.dispatch-ref-unchecked", ERROR, jid, "a dispatch can run on any branch; require refs/heads/main (gate.py require-main-ref) before planning or applying"))
        # one job for plan and apply
        plans_here = [s for s in job.steps for tool, sub, args in _tf_calls(_cmd(s)) if sub == "plan"]
        if plans_here:
            out.append(("infra.apply-same-job-as-plan", ERROR, jid, "plan and apply are in one job: an Environment approval comes before the job starts, so nobody approves the plan. Plan in an earlier job"))
        # per-apply-step checks
        for s, lab in applies:
            for tool, sub, args in _tf_calls(_cmd(s)):
                if sub in ("apply", "destroy"):
                    if not _has_plan_arg(args):
                        out.append(("infra.apply-without-saved-plan", ERROR, jid, "`%s %s` without a saved plan file plans again at apply time: apply the plan that was approved (apply tfplan)" % (tool, sub)))
                    flags = " ".join(args)
                    if re.search(r"-(?:lock|refresh)=false", flags):
                        out.append(("infra.apply-flags", ERROR, jid, "`%s %s` with -lock=false or -refresh=false" % (tool, sub)))
            if lab.split()[0] not in ("terraform", "tofu"):
                out.append(("infra.apply-tool-unsupported", WARN, jid, "%s: this kit's apply flow (plan, hash, base-is-main, approve) is written for terraform and tofu; keep the same human gate with a change set or equivalent" % lab))
                continue
            if not _has_hash_check(wf, job, s.index, own_only=True):
                out.append(("infra.apply-plan-hash-unverified", ERROR, jid, "the saved plan is not hash-checked before apply (gate.py verify-hash or sha256sum -c): it could have changed since it was approved"))
            if not _has_sha_check(wf, job, s.index, own_only=True):
                out.append(("infra.apply-main-sha-unchecked", ERROR, jid, "nothing compares the planned commit with the tip of main before apply (gate.py base-is-main): a plan of an older main could be applied"))
            if not job.environment and not _has_digest_check(wf, job, s.index, own_only=True):
                out.append(("infra.apply-digest-unchecked", ERROR, jid, "a dispatch apply must compare the fresh plan's digest (gate.py plan-digest) with the one a human approved"))
        # checkout refs in the apply job and its plan job
        for j in [job] + _ancestors(wf, jid):
            for s in j.steps:
                if s.uses.lower().startswith("actions/checkout"):
                    ref = str(s.with_.get("ref", "") or "")
                    if ref and not _REF_OK.match(ref):
                        out.append(("infra.apply-ref-input", ERROR, j.id, "checkout ref %r in an apply path: apply only main (or github.sha on a push to main)" % ref))
                    if ctx.step_pr(s):
                        out.append(("infra.apply-ref-input", ERROR, j.id, "an apply path checks out pull request code"))
        if job.environment and not _has_gate(wf, job, "require-run-approval", first_apply):
            out.append(("infra.environment-approval-unverified", ERROR, jid, "an Environment created without reviewers gates nothing, and this lint cannot see GitHub: add gate.py require-run-approval (in this job or one it needs) so the apply refuses unless a listed approver approved this run"))
        if not (wf.raw.get("concurrency") or job.raw.get("concurrency")):
            out.append(("infra.apply-concurrency", WARN, jid, "no concurrency group: two applies of the same state could overlap"))

    if has_apply_anywhere:
        for jid, job in wf.jobs.items():
            for s in job.steps:
                for tool, sub, args in _tf_calls(_cmd(s)):
                    if sub == "plan" and re.search(r"-(?:lock|refresh)=false", " ".join(args)):
                        out.append(("infra.apply-flags", ERROR, jid, "the plan that gets applied must run with the state lock and refresh on (found -lock=false or -refresh=false in a workflow that applies)"))

    # credentialed plan on an untrusted trigger
    if pr_like or events:
        for jid, job in wf.jobs.items():
            if not _credentialed(wf, job, ctx):
                continue
            if not any(_kinds(s) for s in job.steps):
                continue
            if any(apply_calls(s) for s in job.steps):
                continue
            untrusted = [e for e in pr_like]
            if not untrusted:
                continue
            ok_label = "pull_request_target" in events and set(untrusted) <= {"pull_request_target"} and any("plan-ok" in i for i in _job_if_chain(wf, job)) and _has_actor_check(wf, job)
            if not ok_label:
                out.append(("infra.plan-ungated", ERROR, jid,
                            "a plan with cloud credentials on %s needs a human gate: pull_request_target with the plan-ok label and an approver check (gate.py authorize), or workflow_dispatch (a push to any branch but main is reachable by the bot)" % "/".join(untrusted)))

    # policy before init, from the base ref, not bypassable
    untrusted_source = [e for e in events if e in ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "merge_group", "issue_comment")]
    for jid, job in wf.jobs.items():
        pr_paths = set()  # type: Set[str]
        other_paths = set()  # type: Set[str]
        for s in job.steps:
            if s.uses.lower().startswith("actions/checkout"):
                p = posixpath.normpath(str(s.with_.get("path", ".") or "."))
                (pr_paths if ctx.step_pr(s) else other_paths).add(p)
        for s in job.steps:
            for raw_prefix in _policy_prefixes(_norm_run(s.run)):
                prefix = raw_prefix.strip("\"'")
                if not prefix or "$" in prefix:
                    continue
                d = posixpath.normpath(posixpath.dirname(prefix) or ".")
                if d.startswith("./"):
                    d = d[2:]
                from_pr = False
                for p in pr_paths:
                    if p == ".":
                        if not any(d == o or d.startswith(o + "/") for o in other_paths if o != "."):
                            from_pr = True
                    elif d == p or d.startswith(p + "/"):
                        from_pr = True
                if from_pr and untrusted_source:
                    restored = any(re.search(r"\bgit\s+(?:checkout|restore)\b[^\n]*\.claude\b", x.run) for x in job.steps if x.index < s.index)
                    if not restored:
                        out.append(("infra.policy-from-pr", ERROR, jid, "source_policy.py runs from the pull request's own checkout (%s): a PR can edit the policy; run the base ref's copy" % (prefix or ".")))
            if "source_policy.py" in s.run:
                coe = str(s.raw.get("continue-on-error", "")).lower() == "true"
                if coe or re.search(r"source_policy\.py[^\n]*(?:\|\|\s*(?:true|:)\b|;\s*(?:true|:)\b)", _norm_run(s.run)):
                    out.append(("infra.policy-bypassable", ERROR, jid, "source_policy.py can fail without failing the job (continue-on-error or `|| true`)"))
        if untrusted_source:
            for s in job.steps:
                init_like = [sub for tool, sub, args in _tf_calls(_cmd(s)) if sub in _INIT_LIKE and not (sub == "init" and "-backend=false" in " ".join(args) and False)]
                if not init_like:
                    continue
                before = _chain_runs(wf, job, before=s.index)
                if "source_policy.py" not in before:
                    out.append(("infra.policy-after-init", ERROR, jid,
                                "terraform %s runs on pull-request source before source_policy.py: init downloads modules and providers the PR names, plan executes them" % init_like[0]))
                    break
    return out
