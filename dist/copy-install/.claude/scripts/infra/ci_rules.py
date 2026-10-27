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
  infra.environment-approval-unverified an Environment apply that never checks the run's approval history (warning)
  infra.apply-concurrency         no concurrency group: two applies could overlap (warning)
  infra.plan-ungated              a credentialed plan on a pull request event with no human gate
  infra.policy-after-init         init, validate or plan on pull-request source before source_policy ran
  infra.policy-from-pr            source_policy.py run from the pull request's own checkout
  infra.policy-bypassable         source_policy.py with continue-on-error or `|| true`
  infra.input-in-run              a dispatch input interpolated into a run: script
  infra.gate-bypassable           a gate step that can fail without failing the job or its dependents (continue-on-error, `|| true`,
                                  a pipe), or an apply job that runs after a failed dependency (`if: always()`)
  infra.apply-environment-dynamic an `environment:` chosen at run time (`${{ }}`): the dispatcher could name one with no reviewers
  infra.apply-indirect            `$VAR apply` or `$VAR destroy`: the command is hidden from these rules (warning)

These rules read the commands in the workflow's own `run:` text. A command hidden in a script, an alias or `$VAR` is outside them (the
doctor's deploy-surface scan follows one level of scripts); `.github/**` is a CODEOWNERS path so a person reads what is installed.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

Finding = Tuple[str, str, str, str]   # (rule, severity, job id, message)

ERROR, WARN = "error", "warn"

_PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "merge_group",
              "issue_comment", "issues", "workflow_run", "repository_dispatch", "discussion", "discussion_comment", "release", "create")

_CMD_RE = re.compile(r"(?<![\w./-])(terraform|tofu|terragrunt|pulumi|cdk|kubectl|helm)\b")
_CFN_RE = re.compile(r"\baws\s+cloudformation\b")
_INFRA_USES = ("hashicorp/setup-terraform", "opentofu/setup-opentofu", "pulumi/actions", "aws-actions/configure-aws-credentials",
               "hashicorp/terraform-github-actions", "dflook/terraform-", "aws-actions/aws-cloudformation-github-deploy")
_APPLY_USES = ("hashicorp/terraform-github-actions", "dflook/terraform-apply", "dflook/terraform-destroy", "pulumi/actions",
               "aws-actions/aws-cloudformation-github-deploy", "azure/arm-deploy", "azure/k8s-deploy")
_CRED_SECRETS = re.compile(
    r"secrets\.(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|ARM_CLIENT_SECRET|ARM_ACCESS_KEY|ARM_CLIENT_CERTIFICATE\w*|"
    r"GOOGLE_CREDENTIALS|GOOGLE_APPLICATION_CREDENTIALS|GCP_SA_KEY\w*|TF_TOKEN_\w+|TF_API_TOKEN|TFE_TOKEN|PULUMI_ACCESS_TOKEN|"
    r"DIGITALOCEAN_TOKEN|AZURE_CREDENTIALS|KUBE_CONFIG\w*|KUBECONFIG\w*)", re.I)
_CRED_ACTIONS = ("aws-actions/configure-aws-credentials", "google-github-actions/auth", "azure/login")
_TF_SUBCMD = re.compile(r"(?<![\w./-])(terraform|tofu)\b((?:\s+-chdir=\S+)*)\s+([a-z][a-z-]*)\b([^\n;&|]*)")
_VALUE_FLAGS = {"-var", "-var-file", "-target", "-replace", "-parallelism", "-lock-timeout", "-state", "-state-out", "-backup", "-out", "-chdir"}
_APPLY_OTHER = [
    ("cdk deploy", re.compile(r"(?<![\w./-])cdk\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+(?:deploy|destroy)\b")),
    ("pulumi up", re.compile(r"(?<![\w./-])pulumi\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+(?:up|destroy)\b")),
    ("cloudformation", re.compile(r"\baws\s+cloudformation\s+(?:deploy|execute-change-set|create-stack|update-stack|delete-stack)\b")),
    ("kubectl apply", re.compile(r"(?<![\w./-])kubectl\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+(?:apply|replace|delete|patch|create)\b")),
    ("helm upgrade", re.compile(r"(?<![\w./-])helm\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+(?:upgrade|install|uninstall|rollback)\b")),
    ("terragrunt apply", re.compile(r"(?<![\w./-])terragrunt\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+(?:run-all\s+)?(?:apply|destroy)\b")),
]  # type: List[Tuple[str, Any]]
_GATE_CMDS = ("authorize", "verify-hash", "base-is-main", "require-main-ref", "require-run-approval")
_INIT_LIKE = ("init", "validate", "plan", "apply", "destroy", "providers", "get", "test", "console", "refresh", "import", "show")
_REF_OK = re.compile(r"^\s*(?:main|master|refs/heads/main|\$\{\{\s*(?:github\.sha|github\.ref|github\.event\.repository\.default_branch|steps\.[\w.-]+\.outputs\.[\w.-]+|needs\.[\w.-]+\.outputs\.[\w.-]+)\s*\}\})\s*$")


class Ctx(object):
    """What ci_lint hands over: its own PR-checkout test and permission lookup."""
    def __init__(self, step_pr: Callable[[Any], bool], job_perm: Callable[[Any, Any, str], Optional[str]]) -> None:
        self.step_pr = step_pr
        self.job_perm = job_perm


def _norm_run(text: str) -> str:
    out = []  # type: List[str]
    for line in text.replace("\\\n", " ").split("\n"):
        m = re.search(r"(?:^|\s)#", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)


def _tf_calls(run: str) -> List[Tuple[str, str, List[str]]]:
    """(tool, subcommand, args) for every terraform/tofu call in a run: script."""
    calls = []  # type: List[Tuple[str, str, List[str]]]
    for m in _TF_SUBCMD.finditer(_norm_run(run)):
        try:
            args = shlex.split(m.group(4))
        except ValueError:
            args = m.group(4).split()
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
    run = _norm_run(step.run)
    for m in _CMD_RE.finditer(run):
        out.add(m.group(1))
    if _CFN_RE.search(run):
        out.add("cloudformation")
    return out


def is_infra_workflow(wf: Any) -> bool:
    for job in wf.jobs.values():
        for s in job.steps:
            if _kinds(s) or any(s.uses.lower().startswith(u) for u in _INFRA_USES):
                return True
    return False


def apply_calls(step: Any) -> List[str]:
    """Labels of apply-class commands in a step."""
    out = []  # type: List[str]
    for tool, sub, _ in _tf_calls(step.run):
        if sub in ("apply", "destroy"):
            out.append("%s %s" % (tool, sub))
    for label, rx in _APPLY_OTHER:
        if rx.search(_norm_run(step.run)):
            out.append(label)
    u = step.uses.lower().split("@", 1)[0]
    if any(u == a or u.startswith(a) for a in _APPLY_USES):
        mode = " ".join(str(v) for v in step.with_.values()).lower()
        if u not in ("hashicorp/terraform-github-actions", "pulumi/actions") or re.search(r"apply|up\b|deploy|destroy", mode + " " + str(step.with_.get("command", "")).lower()):
            out.append("uses " + u)
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


def _has_actor_check(runs: str) -> bool:
    """Only the kit's gate.py authorize counts: a hand-written comparison cannot be checked structurally, so it is reported and a human reads it."""
    return bool(re.search(r"gate\.py\s+authorize\b", runs))


def _has_main_ref_check(text: str) -> bool:
    return bool(re.search(r"gate\.py\s+require-main-ref\b|github\.ref\s*==\s*['\"]refs/heads/main['\"]|github\.ref_name\s*==\s*['\"]main['\"]", text))


def _has_hash_check(text: str) -> bool:
    return bool(re.search(r"gate\.py\s+verify-hash\b|sha256sum\s+(?:-c|--check)\b|shasum\s+-a\s+256\s+(?:-c|--check)\b", text))


def _has_sha_check(text: str) -> bool:
    return bool(re.search(r"gate\.py\s+base-is-main\b", text)) or bool(
        re.search(r"ls-remote", text) and re.search(r"GITHUB_SHA|github\.sha|PLANNED_SHA|planned[_-]sha", text))


def _events_only_dispatch(events: Dict[str, Any]) -> bool:
    return bool(events) and set(events) == {"workflow_dispatch"}


def _push_main_only(cfg: Any) -> bool:
    from doctorlib import workflows
    cfg = cfg or {}
    br = [str(b) for b in workflows._as_list(cfg.get("branches"))]
    return bool(br) and all(b in ("main", "master") for b in br) and not workflows._as_list(cfg.get("tags")) \
        and not workflows._as_list(cfg.get("branches-ignore")) and not workflows._as_list(cfg.get("tags-ignore"))


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
    pr_like = [e for e in events if e in _PR_EVENTS]
    has_apply_anywhere = False
    wf_text = "\n".join(j.text for j in wf.jobs.values())

    # static credentials
    for jid, job in wf.jobs.items():
        m = _CRED_SECRETS.search(job.text)
        if m:
            out.append(("infra.static-credentials", ERROR, jid, "a long-lived cloud credential (secrets.%s): use an OIDC role whose trust is pinned to an environment or workflow (docs/security/infra-human-gate.md)" % m.group(1)))
        for s in job.steps:
            for key in ("aws-access-key-id", "aws-secret-access-key", "credentials_json"):
                if key in s.with_ and "oidc" not in str(s.with_.get(key)).lower():
                    out.append(("infra.static-credentials", ERROR, jid, "step passes %s to %s: use OIDC (role-to-assume) instead of a stored key" % (key, s.uses.split("@")[0] or "an action")))
                    break
        for st in job.steps:
            run = _norm_run(st.run)
            if not re.search(r"gate\.py\s+(?:%s)\b" % "|".join(_GATE_CMDS), run):
                continue
            quiet = str(st.raw.get("continue-on-error", "")).lower() == "true"
            tail = re.search(r"gate\.py\s+(?:%s)\b[^\n]*?(?:\|\|\s*\S|(?<!\|)\|(?!\|)|;\s*(?:true|:|exit\s+0)\b|&\s*$)" % "|".join(_GATE_CMDS), run, re.M)
            if quiet or tail or re.search(r"\bset\s+\+e\b", run):
                out.append(("infra.gate-bypassable", ERROR, jid, "a gate.py check can fail without failing the step (continue-on-error, `||`, a pipe, `set +e`)"))
                break
        for st in job.steps:
            if re.search(r"\$\{?[A-Za-z_]\w*\}?\s+(?:-\S+\s+)*(?:apply|destroy)\b", _norm_run(st.run)):
                out.append(("infra.apply-indirect", WARN, jid, "`$VAR apply` hides the command from these rules; write the tool name so the gates can be checked"))
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
        chain = _chain_text(wf, job)
        dispatch_only = _events_only_dispatch(events)
        if not job.environment:
            if not (dispatch_only and _has_actor_check(_chain_runs(wf, job))):
                out.append(("infra.apply-ungated", ERROR, jid,
                            "an apply (%s) with no `environment:` (required reviewers) and no dispatch-only actor check against factory_approvers: nothing needs a human" % ", ".join(labels)))
        if "workflow_dispatch" in events and not _has_main_ref_check(chain + "\n" + _chain_runs(wf, job)):
            out.append(("infra.dispatch-ref-unchecked", ERROR, jid, "a dispatch can run on any branch; require refs/heads/main (gate.py require-main-ref) before planning or applying"))
        # one job for plan and apply
        plans_here = [s for s in job.steps for tool, sub, args in _tf_calls(s.run) if sub == "plan"]
        if plans_here:
            out.append(("infra.apply-same-job-as-plan", ERROR, jid, "plan and apply are in one job: an Environment approval comes before the job starts, so nobody approves the plan. Plan in an earlier job"))
        # per-apply-step checks
        for s, lab in applies:
            before_text = _chain_runs(wf, job, before=s.index, own_only=True)
            for tool, sub, args in _tf_calls(s.run):
                if sub in ("apply", "destroy"):
                    if not _has_plan_arg(args):
                        out.append(("infra.apply-without-saved-plan", ERROR, jid, "`%s %s` without a saved plan file plans again at apply time: apply the plan that was approved (apply tfplan)" % (tool, sub)))
                    flags = " ".join(args)
                    if re.search(r"-(?:lock|refresh)=false", flags):
                        out.append(("infra.apply-flags", ERROR, jid, "`%s %s` with -lock=false or -refresh=false" % (tool, sub)))
            if lab.split()[0] not in ("terraform", "tofu"):
                out.append(("infra.apply-tool-unsupported", WARN, jid, "%s: this kit's apply flow (plan, hash, base-is-main, approve) is written for terraform and tofu; keep the same human gate with a change set or equivalent" % lab))
                continue
            if not _has_hash_check(before_text):
                out.append(("infra.apply-plan-hash-unverified", ERROR, jid, "the saved plan is not hash-checked before apply (gate.py verify-hash or sha256sum -c): it could have changed since it was approved"))
            if not _has_sha_check(before_text):
                out.append(("infra.apply-main-sha-unchecked", ERROR, jid, "nothing compares the planned commit with the tip of main before apply (gate.py base-is-main): a plan of an older main could be applied"))
            if not job.environment and "plan-digest" not in before_text:
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
        if job.environment and not re.search(r"gate\.py\s+require-run-approval\b", _chain_runs(wf, job)):
            out.append(("infra.environment-approval-unverified", WARN, jid, "an Environment created without reviewers gates nothing; add gate.py require-run-approval so the job refuses unless a listed approver approved this run"))
        if not (wf.raw.get("concurrency") or job.raw.get("concurrency")):
            out.append(("infra.apply-concurrency", WARN, jid, "no concurrency group: two applies of the same state could overlap"))

    if has_apply_anywhere:
        for jid, job in wf.jobs.items():
            for s in job.steps:
                for tool, sub, args in _tf_calls(s.run):
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
            chain = _chain_text(wf, job)
            untrusted = [e for e in pr_like]
            if not untrusted:
                continue
            ok_label = "pull_request_target" in events and set(untrusted) <= {"pull_request_target"} and "plan-ok" in chain and _has_actor_check(_chain_runs(wf, job))
            if not ok_label:
                out.append(("infra.plan-ungated", ERROR, jid,
                            "a plan with cloud credentials on %s needs a human gate: pull_request_target with the plan-ok label and an approver check (gate.py authorize), or workflow_dispatch" % "/".join(untrusted)))

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
            for m in re.finditer(r"(\S*)source_policy\.py", _norm_run(s.run)):
                prefix = m.group(1).strip("\"'")
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
                init_like = [sub for tool, sub, args in _tf_calls(s.run) if sub in _INIT_LIKE and not (sub == "init" and "-backend=false" in " ".join(args) and False)]
                if not init_like:
                    continue
                before = _chain_runs(wf, job, before=s.index)
                if "source_policy.py" not in before:
                    out.append(("infra.policy-after-init", ERROR, jid,
                                "terraform %s runs on pull-request source before source_policy.py: init downloads modules and providers the PR names, plan executes them" % init_like[0]))
                    break
    return out
