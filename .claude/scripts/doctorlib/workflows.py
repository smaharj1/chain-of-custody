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
           "branch_pattern_matches", "PR_EVENTS", "DEPLOY_COMMANDS"]

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

DEPLOY_COMMANDS = [
    ("cdk deploy", re.compile(r"\bcdk" + _W + r"\s+(?:deploy|destroy|bootstrap)\b")),
    ("terraform apply", re.compile(r"\b(?:terraform|tofu)" + _W + r"\s+(?:apply|destroy|import|taint)\b")),
    ("terragrunt apply", re.compile(r"\bterragrunt" + _W + r"\s+(?:run-all\s+)?(?:apply|destroy)\b")),
    ("sst deploy", re.compile(r"\bsst" + _W + r"\s+(?:deploy|remove)\b")),
    ("serverless deploy", re.compile(r"\b(?:serverless|sls)" + _W + r"\s+(?:deploy|remove)\b")),
    ("amplify publish", re.compile(r"\bamplify" + _W + r"\s+(?:publish|push)\b")),
    ("fly deploy", re.compile(r"\b(?:flyctl|fly)" + _W + r"\s+(?:deploy|launch)\b")),
    ("wrangler deploy", re.compile(r"\bwrangler" + _W + r"\s+(?:deploy|publish)\b")),
    ("vercel deploy", re.compile(r"\bvercel\b(?:\s+\S+)*?\s+--prod\b|\bvercel\s+deploy\b|\bvercel\s+--prebuilt\b")),
    ("netlify deploy", re.compile(r"\bnetlify" + _W + r"\s+deploy\b")),
    ("kubectl apply", re.compile(r"\bkubectl" + _W + r"\s+(?:apply|rollout|replace|patch|set|scale|delete|create)\b")),
    ("helm upgrade", re.compile(r"\bhelm" + _W + r"\s+(?:upgrade|install|rollback|uninstall)\b")),
    ("pulumi up", re.compile(r"\bpulumi" + _W + r"\s+(?:up|destroy|refresh)\b")),
    ("cloudformation deploy", re.compile(r"\baws\s+cloudformation\s+(?:deploy|create-stack|update-stack|execute-change-set|delete-stack)\b")),
    ("aws deploy", re.compile(r"\baws\s+(?:ecs\s+(?:update-service|run-task)|lambda\s+(?:update-function-code|publish-version|update-alias)|s3\s+(?:sync|cp|mv)\s+\S+\s+s3://|deploy\s+create-deployment|elasticbeanstalk\s+update-environment|apprunner\s+start-deployment)")),
    ("gcloud deploy", re.compile(r"\bgcloud\s+(?:run\s+deploy|app\s+deploy|functions\s+deploy|builds\s+submit|compute\s+instances\s+create)")),
    ("az deploy", re.compile(r"\baz\s+(?:webapp\s+(?:deploy|up)|functionapp\s+deploy|containerapp\s+(?:up|update|create)|deployment\s+\S+\s+create)")),
    ("firebase deploy", re.compile(r"\bfirebase" + _W + r"\s+deploy\b")),
    ("railway up", re.compile(r"\brailway\s+(?:up|deploy)\b")),
    ("heroku deploy", re.compile(r"\bgit\s+push\s+heroku\b|\bheroku\s+container:release\b")),
    ("docker push", re.compile(r"\bdocker" + _W + r"\s+push\b|\bdocker\s+buildx\s+build\b[^\n]*--push\b")),
    ("package publish", re.compile(r"\b(?:npm|pnpm|yarn|bun)" + _W + r"\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b|\bpoetry\s+publish\b|\buv\s+publish\b|\bgradle\w*\s+publish\b")),
    ("gh release", re.compile(r"\bgh\s+release\s+(?:create|upload|edit)\b")),
    ("release automation", re.compile(r"\bsemantic-release\b|\bchangesets?\s+publish\b|\bchangeset\s+version\b|\brelease-please\b")),
    ("deploy script", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:deploy|release|publish|ship)(?::\S+)?\b|\bmake\s+(?:-\S+\s+)*(?:deploy|release|publish|apply)\b|\bjust\s+(?:deploy|release|publish|apply)\b|(?:^|[\s;&|])\./(?:scripts/|bin/)?(?:deploy|release|publish)[\w.-]*")),
    ("remote copy", re.compile(r"\brsync\b[^\n]*\S+@\S+:|\bscp\b[^\n]*\S+@\S+:")),
    ("deploy hook", re.compile(r"\bcurl\b[^\n]*(?:deploy[-_ ]?hook|api\.vercel\.com/v1/integrations/deploy|api\.netlify\.com/build_hooks|api\.render\.com/deploy)")),
]  # type: List[Tuple[str, Any]]

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
    "serverless/github-action", "sst/ion-action", "railwayapp/", "render-oss/", "expo/expo-github-action", "aws-amplify/",
]


def deploy_labels(text: str) -> List[str]:
    """Deploy-class command labels found in shell text (normalised, lower-cased)."""
    norm = re.sub(r"[ \t]+", " ", text.lower().replace("\\\n", " "))
    out = []  # type: List[str]
    for label, rx in DEPLOY_COMMANDS:
        if rx.search(norm) and label not in out:
            out.append(label)
    return out


def deploy_uses_label(step: Step) -> Optional[str]:
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
