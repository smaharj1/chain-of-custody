"""A small structural reader for GitHub workflow YAML, shared by the doctor and `ci_lint.py`.

It parses with `kitlib.miniyaml` (restricted subset, IMP-11). A workflow the subset cannot read
raises `Unparsed`; callers must report "unparsed: manual review" and never treat it as clean
(IMP-23). `scan_text` is the fallback: it looks for deploy commands in the raw text so an
unparsed workflow is still examined.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from kitlib import miniyaml

__all__ = ["Unparsed", "Step", "Job", "Workflow", "parse", "deploy_labels", "deploy_uses_label", "scan_text",
           "branch_pattern_matches", "PR_EVENTS", "DEPLOY_COMMANDS", "normalize_shell", "push_main_only", "docker_image",
           "docker_command", "docker_is_iac", "docker_opaque_iac", "docker_pinned", "step_command_text", "perm_level", "job_credentialed",
           "CRED_SECRETS", "CRED_ACTIONS"]

PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "merge_group")


class Unparsed(ValueError):
    pass


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class Step(object):
    def __init__(self, raw: Any, index: int) -> None:
        d = _as_dict(raw)
        self.index = index
        self.name = str(d.get("name", "") or "")
        self.uses = str(d.get("uses", "") or "")
        self.run = str(d.get("run", "") or "")
        self.with_ = _as_dict(d.get("with"))
        self.env = _as_dict(d.get("env"))
        self.if_ = str(d.get("if", "") or "")
        self.raw = d

    @property
    def text(self) -> str:
        parts = [self.run, self.uses, self.if_]
        parts += [str(v) for v in self.with_.values()]
        parts += [str(v) for v in self.env.values()]
        return "\n".join(parts)


class Job(object):
    def __init__(self, job_id: str, raw: Any) -> None:
        d = _as_dict(raw)
        self.id = job_id
        env = d.get("environment")
        self.environment = (str(env.get("name", "")) if isinstance(env, dict) else str(env or "")) or None
        self.permissions = d.get("permissions")
        self.uses = str(d.get("uses", "") or "")
        self.if_ = str(d.get("if", "") or "")
        self.secrets = d.get("secrets")
        self.env = _as_dict(d.get("env"))
        self.steps = [Step(s, i) for i, s in enumerate(_as_list(d.get("steps")))]
        self.raw = d

    @property
    def text(self) -> str:
        parts = [self.uses, self.if_] + [s.text for s in self.steps] + [str(v) for v in self.env.values()]
        if self.secrets is not None:
            parts.append(str(self.secrets))
        return "\n".join(parts)


class Workflow(object):
    def __init__(self, path: str, data: Dict[str, Any]) -> None:
        self.path = path
        self.name = str(data.get("name", "") or "")
        self.events = self._events(data.get("on", data.get(True)))
        self.permissions = data.get("permissions")
        self.env = _as_dict(data.get("env"))
        self.jobs = {str(k): Job(str(k), v) for k, v in _as_dict(data.get("jobs")).items()}
        self.raw = data

    @staticmethod
    def _events(on: Any) -> Dict[str, Dict[str, Any]]:
        ev = {}  # type: Dict[str, Dict[str, Any]]
        if isinstance(on, str):
            ev[on] = {}
        elif isinstance(on, list):
            for e in on:
                ev[str(e)] = {}
        elif isinstance(on, dict):
            for k, v in on.items():
                ev[str(k)] = v if isinstance(v, dict) else {}
        return ev

    @property
    def text(self) -> str:
        return "\n".join(j.text for j in self.jobs.values())


def parse(text: str, path: str = "<workflow>") -> Workflow:
    try:
        data = miniyaml.parse(text)
    except miniyaml.YamlError as exc:
        raise Unparsed(str(exc))
    if not isinstance(data, dict):
        raise Unparsed("top level is not a mapping")
    if "jobs" not in data:
        raise Unparsed("no jobs: key")
    if "on" not in data and True not in data:
        raise Unparsed("no on: key")
    return Workflow(path, data)


# --- deploy-class commands (report §8.4) ----------------------------------------------------------------

_W = r"(?:\s+-{1,2}[A-Za-z][\w-]*(?:[= ](?!apply\b|deploy\b)\S+)?)*"   # tolerate flags between program and verb

class _Near(object):
    """`A ... B` on one line, as a matcher with `.search`. The regex `A[^\\n]*B` costs a scan to the end of the line for every `A`, so a line of
    ten thousand `curl` words (a workflow comes from a stranger) takes minutes. This takes the first `A` and looks for one `B` after it: same
    answer, two linear scans."""

    def __init__(self, a: str, b: str) -> None:
        self.a, self.b = re.compile(a), re.compile(b)
        self.pattern = a + " ... " + b

    def search(self, text: str) -> Optional[bool]:
        for line in text.split("\n"):
            m = self.a.search(line)
            if m and self.b.search(line, m.end()):
                return True
        return None


class _Any(object):
    def __init__(self, *parts: Any) -> None:
        self.parts = parts
        self.pattern = " | ".join(getattr(p, "pattern", "") for p in parts)

    def search(self, text: str) -> Optional[bool]:
        for p in self.parts:
            if p.search(text):
                return True
        return None


DEPLOY_COMMANDS = [
    ("cdk deploy", re.compile(r"\bcdk" + _W + r"\s+(?:deploy|destroy|bootstrap)\b")),
    ("cdktf deploy", re.compile(r"\bcdktf" + _W + r"\s+(?:deploy|destroy)\b")),
    ("sam deploy", re.compile(r"\bsam" + _W + r"\s+(?:deploy|sync|delete)\b")),
    ("terraform apply", re.compile(r"\b(?:terraform|tofu)" + _W + r"\s+(?:apply|destroy|import|taint)\b")),
    ("terragrunt apply", re.compile(r"\bterragrunt" + _W + r"\s+(?:(?:run-all|run" + _W + r"\s+(?:--all|-a))" + _W + r"\s+)?(?:apply|destroy)\b")),
    ("copilot deploy", re.compile(r"\bcopilot" + _W + r"\s+(?:(?:svc|env|job|pipeline)\s+)?deploy\b")),
    ("eb deploy", re.compile(r"(?<![\w./-])eb" + _W + r"\s+(?:deploy|create|terminate|upgrade|swap)\b")),
    ("ansible-playbook", re.compile(r"(?<![\w./-])ansible-playbook\b")),
    ("sst deploy", re.compile(r"\bsst" + _W + r"\s+(?:deploy|remove)\b")),
    ("serverless deploy", re.compile(r"\b(?:serverless|sls)" + _W + r"\s+(?:deploy|remove)\b")),
    ("amplify publish", re.compile(r"\bamplify" + _W + r"\s+(?:publish|push)\b")),
    ("fly deploy", re.compile(r"\b(?:flyctl|fly)" + _W + r"\s+(?:deploy|launch)\b")),
    ("wrangler deploy", re.compile(r"\bwrangler" + _W + r"\s+(?:deploy|publish)\b")),
    ("vercel deploy", _Any(_Near(r"\bvercel\b", r"\s--prod\b"), re.compile(r"\bvercel\s+deploy\b|\bvercel\s+--prebuilt\b"))),
    ("netlify deploy", re.compile(r"\bnetlify" + _W + r"\s+deploy\b")),
    ("kubectl apply", re.compile(r"\bkubectl" + _W + r"\s+(?:apply|rollout|replace|patch|set|scale|delete|create)\b")),
    ("helm upgrade", re.compile(r"\bhelm" + _W + r"\s+(?:upgrade|install|rollback|uninstall)\b")),
    ("pulumi up", re.compile(r"\bpulumi" + _W + r"\s+(?:up|update|destroy|refresh)\b")),
    ("cloudformation deploy", re.compile(r"\baws\s+cloudformation\s+(?:deploy|create-stack|update-stack|execute-change-set|delete-stack)\b")),
    ("aws deploy", re.compile(r"\baws\s+(?:ecs\s+(?:update-service|run-task)|lambda\s+(?:update-function-code|publish-version|update-alias)|s3\s+(?:sync|cp|mv)\s+\S+\s+s3://|deploy\s+create-deployment|elasticbeanstalk\s+update-environment|apprunner\s+start-deployment)")),
    ("gcloud deploy", re.compile(r"\bgcloud\s+(?:run\s+deploy|app\s+deploy|functions\s+deploy|builds\s+submit|compute\s+instances\s+create)")),
    ("az deploy", re.compile(r"\baz\s+(?:webapp\s+(?:deploy|up)|functionapp\s+deploy|containerapp\s+(?:up|update|create)|deployment\s+\S+\s+create)")),
    ("firebase deploy", re.compile(r"\bfirebase" + _W + r"\s+deploy\b")),
    ("railway up", re.compile(r"\brailway\s+(?:up|deploy)\b")),
    ("heroku deploy", re.compile(r"\bgit\s+push\s+heroku\b|\bheroku\s+container:release\b")),
    ("docker push", _Any(re.compile(r"\bdocker" + _W + r"\s+push\b"), _Near(r"\bdocker\s+buildx\s+build\b", r"--push\b"))),
    ("package publish", re.compile(r"\b(?:npm|pnpm|yarn|bun)" + _W + r"\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b|\bpoetry\s+publish\b|\buv\s+publish\b|\bgradle\w*\s+publish\b")),
    ("gh release", re.compile(r"\bgh\s+release\s+(?:create|upload|edit)\b")),
    ("release automation", re.compile(r"\bsemantic-release\b|\bchangesets?\s+publish\b|\bchangeset\s+version\b|\brelease-please\b")),
    ("deploy script", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:deploy|release|publish|ship)(?::\S+)?\b|\bmake\s+(?:-\S+\s+)*(?:deploy|release|publish|apply)\b|\bjust\s+(?:deploy|release|publish|apply)\b|(?:^|[\s;&|])\./(?:scripts/|bin/)?(?:deploy|release|publish)[\w.-]*")),
    ("remote copy", _Any(_Near(r"\brsync\b", r"\S+@\S+:"), _Near(r"\bscp\b", r"\S+@\S+:"))),
    ("deploy hook", _Near(r"\bcurl\b", r"deploy[-_ ]?hook|api\.vercel\.com/v1/integrations/deploy|api\.netlify\.com/build_hooks|api\.render\.com/deploy")),
]  # type: List[Tuple[str, Any]]

# --- shell normalisation and docker:// steps (IN-2, IN-3, IN-6) ------------------------------------------------

# Programs whose invocation form is normalised before matching: a path (`/usr/local/bin/terraform`, `./bin/tofu`), a quoted word
# (`terraform 'apply'`), `$(which terraform)` and `${TF:-terraform}` all read as the bare word.
_CMD_TOOLS = ("terraform", "tofu", "terragrunt", "pulumi", "cdktf", "cdk8s", "cdk", "kubectl", "helm", "sam", "eb", "copilot", "ansible-playbook",
              "serverless", "sls", "sst", "aws", "gcloud", "az", "wrangler", "vercel", "netlify", "firebase", "flyctl")
_TOOL_ALT = "|".join(sorted(_CMD_TOOLS + ("opentofu",), key=len, reverse=True))
_WHICH_RE = re.compile(r"\$\(\s*(?:which|command\s+-v|type\s+-p|realpath|readlink\s+-f)\s+[\"']?(?:%s)[\"']?\s*\)|"
                       r"`\s*(?:which|command\s+-v|type\s+-p)\s+[\"']?(?:%s)[\"']?\s*`" % (_TOOL_ALT, _TOOL_ALT))
_WHICH_TOOL_RE = re.compile(r"(%s)" % _TOOL_ALT)
_DEFAULT_RE = re.compile(r"\$\{\w+:?-[\"']?(%s)[\"']?\}" % _TOOL_ALT)
# A path before a tool (`/usr/local/bin/terraform`, `hashicorp/terraform`), with the `:tag` or `@sha256:` of a container image dropped, so that
# `docker run hashicorp/terraform:1.9 apply` reads `terraform apply`.
_PATH_CMD_RE = re.compile(r"(?<![\w.@+~/-])(?:~|\.{1,2})?/?(?:[\w.@+~-]+/)+(%s)(?::[\w.-]+)?(?:@sha256:[0-9a-f]{16,})?(?=[\s;|&)\"']|$)" % _TOOL_ALT)
_UNQUOTE_RE = re.compile(r"(?<![\w$\\])([\"'])([A-Za-z][\w.-]*)\1(?![\w$])")
_ASSIGN_RE = re.compile(r"(?:^|[;&|(\s])([A-Za-z_]\w*)=[\"']?(%s)[\"']?(?=\s|;|$)" % _TOOL_ALT, re.M)


def normalize_shell(text: str) -> str:
    """Shell text with the spellings that hide a command word removed: line continuations, `$(which terraform)`, `${X:-terraform}`, empty
    quote pairs, a backslash inside a word, a path before a deploy tool, quotes around a single word, and `TF=terraform` assignments
    (so `$TF apply` reads `terraform apply`). Matching aid only: the result is never executed or shown as the user's text."""
    text = text.replace("\\\n", " ")
    text = _WHICH_RE.sub(lambda m: _WHICH_TOOL_RE.search(m.group(0)).group(1), text)
    text = _DEFAULT_RE.sub(lambda m: m.group(1), text)
    text = text.replace('""', "").replace("''", "")
    text = re.sub(r"(?<=\w)\\(?=\w)", "", text)
    text = _PATH_CMD_RE.sub(lambda m: "tofu" if m.group(1) == "opentofu" else m.group(1), text)
    text = _UNQUOTE_RE.sub(lambda m: m.group(2), text)
    assigns = {}  # type: Dict[str, str]
    for m in _ASSIGN_RE.finditer(text):
        if len(assigns) >= 16:        # bounded: each name costs a pass over the text
            break
        assigns.setdefault(m.group(1), m.group(2))
    for name, val in assigns.items():
        text = re.sub(r"\$\{%s\}|\$%s\b" % (re.escape(name), re.escape(name)), val, text)
    return text


def push_main_only(cfg: Any) -> bool:
    """True when a `push:` trigger can only be a push to main or master (no other branch, no tag, no ignore list)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    br = [str(b) for b in _as_list(cfg.get("branches"))]
    return bool(br) and all(b in ("main", "master") for b in br) and not _as_list(cfg.get("tags")) \
        and not _as_list(cfg.get("branches-ignore")) and not _as_list(cfg.get("tags-ignore"))


_IAC_IMAGE_REPOS = ("hashicorp/terraform", "hashicorp/tfc-agent", "amazon/aws-cli", "bitnami/kubectl", "alpine/helm", "dtzar/helm-kubectl",
                    "gruntwork/terragrunt", "alpine/terragrunt")
_IAC_IMAGE_WORD = re.compile(r"terraform|tofu|terragrunt|pulumi|kubectl|helm")


def docker_image(uses: str) -> Tuple[str, str, str]:
    """(repository, tag, digest) of a `docker://IMAGE[:tag][@sha256:...]` reference; empty strings for what is absent."""
    ref = uses[len("docker://"):] if uses.startswith("docker://") else uses
    name, _, digest = ref.partition("@")
    tag = ""
    if ":" in name.rsplit("/", 1)[-1]:
        name, _, tag = name.rpartition(":")
    return name, tag, digest


def docker_pinned(uses: str) -> bool:
    return re.match(r"^sha256:[0-9a-f]{64}$", docker_image(uses)[2], re.I) is not None


def docker_is_iac(uses: str) -> bool:
    repo = docker_image(uses)[0].lower()
    if any(repo == r or repo.endswith("/" + r) for r in _IAC_IMAGE_REPOS):
        return True
    return bool(_IAC_IMAGE_WORD.search(repo.rsplit("/", 1)[-1])) or repo.startswith("opentofu/") or "/opentofu/" in repo \
        or repo.startswith("pulumi/") or "/pulumi/" in repo


def _docker_tool(repo: str) -> str:
    last = repo.lower().rsplit("/", 1)[-1]
    for t in ("terraform", "tofu", "terragrunt", "pulumi", "kubectl", "helm"):
        if t in last:
            return t
    return "aws" if "aws-cli" in last else last


def docker_command(step: "Step") -> str:
    """The command a `uses: docker://IMAGE` step runs, as shell text: the entrypoint (or the tool the image is known for) followed by
    `with.args`. Empty for a step that is not a docker:// step."""
    if not step.uses.startswith("docker://"):
        return ""
    repo = docker_image(step.uses)[0]
    args = step.with_.get("args", "")
    args = " ".join(str(a) for a in args) if isinstance(args, list) else str(args or "")
    entry = str(step.with_.get("entrypoint", "") or "").strip()
    head = entry or (_docker_tool(repo) if docker_is_iac(step.uses) else "")
    return (head + " " + args).strip()


def docker_opaque_iac(step: "Step") -> bool:
    """A known IaC image with no args and no entrypoint: whatever it runs is invisible here, so it counts as deploy-capable."""
    if not (step.uses.startswith("docker://") and docker_is_iac(step.uses)):
        return False
    cmd = docker_command(step).strip()
    # no args at all, or args computed at run time (`${{ inputs.cmd }}`, `$ARGS`): the verb is not visible here
    return not cmd.replace(_docker_tool(docker_image(step.uses)[0]), "").strip() or "${{" in cmd or bool(re.search(r"\$\{?\w", cmd.split(None, 1)[-1] if " " in cmd else ""))


def step_command_text(step: "Step") -> str:
    """The shell text of a step as the infra rules read it: its `run:` plus the command of a docker:// step."""
    cmd = docker_command(step)
    return step.run + ("\n" + cmd if cmd else "")


# --- cloud credentials in a job (shared by ci_lint's infra rules and the doctor's deploy scan) ---------------------

CRED_SECRETS = re.compile(
    r"secrets\.(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|ARM_CLIENT_SECRET|ARM_ACCESS_KEY|ARM_CLIENT_CERTIFICATE\w*|"
    r"GOOGLE_CREDENTIALS|GOOGLE_APPLICATION_CREDENTIALS|GCP_SA_KEY\w*|TF_TOKEN_\w+|TF_API_TOKEN|TFE_TOKEN|PULUMI_ACCESS_TOKEN|"
    r"DIGITALOCEAN_TOKEN|AZURE_CREDENTIALS|KUBE_CONFIG\w*|KUBECONFIG\w*)", re.I)
CRED_ACTIONS = ("aws-actions/configure-aws-credentials", "google-github-actions/auth", "azure/login")


def perm_level(permissions: Any, name: str) -> Optional[str]:
    """The level of one permission in a `permissions:` value (a scalar `read-all`/`write-all`, a mapping, or absent)."""
    if isinstance(permissions, str):
        return "write" if permissions == "write-all" else ("read" if permissions == "read-all" else None)
    if isinstance(permissions, dict):
        v = permissions.get(name)
        return str(v) if v is not None else None
    return None


def job_credentialed(wf: "Workflow", job: "Job") -> bool:
    """The job can obtain cloud credentials: `id-token: write`, a credential action, or a stored cloud key."""
    perms = job.permissions if job.permissions is not None else wf.permissions
    if perm_level(perms, "id-token") == "write":
        return True
    if any(s.uses.lower().startswith(a) for s in job.steps for a in CRED_ACTIONS):
        return True
    return bool(CRED_SECRETS.search(job.text))


_DEPLOY_USES = [
    "aws-actions/amazon-ecs-deploy-task-definition", "aws-actions/aws-cloudformation-github-deploy", "aws-actions/aws-codebuild-run-build",
    "azure/webapps-deploy", "azure/functions-action", "azure/k8s-deploy", "azure/arm-deploy", "azure/static-web-apps-deploy",
    "google-github-actions/deploy-cloudrun", "google-github-actions/deploy-appengine", "google-github-actions/deploy-cloud-functions",
    "superfly/flyctl-actions", "cloudflare/wrangler-action", "cloudflare/pages-action", "amondnet/vercel-action", "nwtgck/actions-netlify",
    "netlify/actions", "pulumi/actions", "hashicorp/terraform-github-actions", "hashicorp/tfc-workflows-github", "dflook/terraform-apply",
    "appleboy/ssh-action", "appleboy/scp-action", "easingthemes/ssh-deploy", "peaceiris/actions-gh-pages",
    "jamesives/github-pages-deploy-action", "actions/deploy-pages", "softprops/action-gh-release", "ncipollo/release-action",
    "googleapis/release-please-action", "google-github-actions/release-please-action", "changesets/action", "cycjimmy/semantic-release-action",
    "pypa/gh-action-pypi-publish", "js-devtools/npm-publish", "helm/chart-releaser-action", "digitalocean/app_action",
    "serverless/github-action", "sst/ion-action", "gruntwork-io/terragrunt-action", "dflook/terraform-destroy", "dflook/tofu-apply",
    "dawidd6/action-ansible-playbook", "railwayapp/", "render-oss/", "expo/expo-github-action", "aws-amplify/",
]


def deploy_labels(text: str) -> List[str]:
    """Deploy-class command labels found in shell text (normalised, lower-cased)."""
    norm = re.sub(r"[ \t]+", " ", normalize_shell(text.lower()))
    out = []  # type: List[str]
    for label, rx in DEPLOY_COMMANDS:
        if rx.search(norm) and label not in out:
            out.append(label)
    return out


def deploy_uses_label(step: Step) -> Optional[str]:
    if step.uses.startswith("docker://"):
        repo = docker_image(step.uses)[0]
        labs = deploy_labels(docker_command(step))
        if labs:
            return "uses docker://%s (%s)" % (repo, labs[0])
        if docker_opaque_iac(step):
            return "uses docker://%s (IaC image, command not visible)" % repo
        return None
    u = step.uses.lower().split("@", 1)[0]
    for prefix in _DEPLOY_USES:
        if u == prefix or (prefix.endswith("/") and u.startswith(prefix)):
            return "uses " + u
    if u == "docker/build-push-action":
        push = str(step.with_.get("push", "")).strip().lower()
        if push and push != "false":
            return "uses docker/build-push-action (push)"
    return None


def scan_text(text: str) -> List[str]:
    """Raw-text fallback for a workflow that did not parse: deploy labels from commands and `uses:`."""
    labels = deploy_labels(text)
    for m in re.finditer(r"(?m)^\s*-?\s*uses:\s*['\"]?([^\s'\"#]+)", text):
        fake = Step({"uses": m.group(1)}, 0)
        lab = deploy_uses_label(fake)
        if lab and lab not in labels:
            labels.append(lab)
    return labels


def branch_pattern_matches(pattern: str, ref: str) -> bool:
    """GitHub filter-pattern match: `*` stops at `/`, `**` does not, `?` one char, `!` negates (caller)."""
    rx = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                rx += ".*"
                i += 2
                continue
            rx += "[^/]*"
        elif c == "?":
            rx += "[^/]"
        elif c == "[":
            j = pattern.find("]", i)
            if j > i:
                rx += pattern[i:j + 1]
                i = j + 1
                continue
            rx += re.escape(c)
        else:
            rx += re.escape(c)
        i += 1
    return re.fullmatch(rx, ref) is not None
