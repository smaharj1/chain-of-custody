"""B5: the deployment surface and its attestation (report §5.1 B5, §8.4, RC-1, DEC-38).

A push the bot is allowed to make must not become a deploy nobody approved. This module
inventories every path by which a commit becomes a deploy:

  * workflows that deploy on push, release, tag, `workflow_run`, schedule, comments or
    `repository_dispatch` (anything the bot's `contents: write` token can cause),
  * config files of git-integrated hosts (Vercel, Netlify, Amplify, Fly, Render, Railway,
    Cloudflare, ...) and of CD systems we cannot read (GitLab CI, CircleCI, Jenkins, ...),
  * GitOps directories, where a merge to main is the deploy.

The repo cannot show everything (hosts are configured in dashboards), so a human attests the
result in loop.config (`deploy_triggers`, `git_integrated_hosts`, `deploy_surface.*`). The
attestation carries a fingerprint of the surface; when a deploy-surface file changes, the
attestation is void until a human re-attests.

Caps: a deploy firing on a bot-pushable ref with no human gate caps the level at L1; on main,
at L4. An unattested or stale surface caps at L1 (we cannot show L2 is safe).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import safeio, workflows
from .model import (Context, Result, check, failed, gatherer, passed, skipped, unknown, ERROR, WARN)

GATES = ("none", "gated-by-environment", "manual")
SAFE_PREVIEWS = ("none", "label")
MAIN_NAMES = ("main", "master")

# H3 deploy-surface floor (report §6.1) plus the files we scan.
FLOOR_PATTERNS = [
    "serverless.yml", "serverless.yaml", "serverless.ts", "sst.config.ts", "sst.config.js", "amplify/*", "amplify.yml", "amplify.yaml",
    "cdk.json", "bin/*.ts", "packages/infra/*", "Dockerfile*", "*/Dockerfile*", "docker-compose*", "*/docker-compose*", "fly.toml",
    "vercel.json", ".vercel/*", "netlify.toml", "wrangler.toml", "wrangler.json", "wrangler.jsonc", "render.yaml", "railway.json", "railway.toml",
    "Procfile", "heroku.yml", "k8s/*", "helm/*", "kustomization.yaml", "*/kustomization.yaml", "Chart.yaml", "*/Chart.yaml",
    "app.yaml", "cloudbuild.yaml", "firebase.json", ".firebaserc", ".do/*", "staticwebapp.config.json", "buildspec.yml", "appspec.yml",
    "samconfig.toml", "template.yaml", "Pulumi.yaml", "Pulumi.*.yaml", "main.bicep", ".platform.app.yaml", "codemagic.yaml", "bitrise.yml",
    ".github/workflows/*", ".github/actions/*", ".github/dependabot.yml",
    "atlantis.yaml", "atlantis.yml", ".spacelift/*", "env0.yml", ".nojekyll", "CNAME", "docs/CNAME", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/*", "bitbucket-pipelines.yml", "azure-pipelines.yml", ".travis.yml", ".drone.yml",
    ".buildkite/*", "appveyor.yml", ".woodpecker.yml", ".woodpecker/*", ".argocd/*", "argocd/*", "flux-system/*", "*/flux-system/*",
]

# file -> (host name, kind). kind: 'host' = git-integrated host config, 'ci' = CD system we cannot read, 'gitops'.
HOST_FILES = [
    ("vercel.json", "vercel", "host"), (".vercel/project.json", "vercel", "host"), ("netlify.toml", "netlify", "host"),
    ("amplify.yml", "amplify", "host"), ("amplify.yaml", "amplify", "host"), ("amplify/*", "amplify", "host"),
    ("fly.toml", "fly", "host"), ("wrangler.toml", "cloudflare", "host"), ("wrangler.json", "cloudflare", "host"), ("wrangler.jsonc", "cloudflare", "host"),
    ("render.yaml", "render", "host"), ("railway.json", "railway", "host"), ("railway.toml", "railway", "host"),
    ("Procfile", "heroku", "host"), ("heroku.yml", "heroku", "host"), ("app.yaml", "gcp-app-engine", "host"), ("cloudbuild.yaml", "gcp-cloud-build", "host"),
    ("firebase.json", "firebase", "host"), (".do/app.yaml", "digitalocean", "host"), ("staticwebapp.config.json", "azure-static-web-apps", "host"),
    ("buildspec.yml", "aws-codebuild", "ci"), ("appspec.yml", "aws-codedeploy", "ci"), (".platform.app.yaml", "platform-sh", "host"),
    ("codemagic.yaml", "codemagic", "ci"), ("bitrise.yml", "bitrise", "ci"),
    (".gitlab-ci.yml", "gitlab-ci", "ci"), ("Jenkinsfile", "jenkins", "ci"), (".circleci/config.yml", "circleci", "ci"),
    ("bitbucket-pipelines.yml", "bitbucket-pipelines", "ci"), ("azure-pipelines.yml", "azure-pipelines", "ci"), (".travis.yml", "travis", "ci"),
    (".drone.yml", "drone", "ci"), (".buildkite/*", "buildkite", "ci"), ("appveyor.yml", "appveyor", "ci"), (".woodpecker.yml", "woodpecker", "ci"),
    (".woodpecker/*", "woodpecker", "ci"),
    ("atlantis.yaml", "atlantis", "ci"), ("atlantis.yml", "atlantis", "ci"), (".spacelift/*", "spacelift", "ci"), ("env0.yml", "env0", "ci"),
    (".nojekyll", "github-pages", "host"), ("CNAME", "github-pages", "host"), ("docs/CNAME", "github-pages", "host"),
    ("flux-system/*", "gitops", "gitops"), ("*/flux-system/*", "gitops", "gitops"), (".argocd/*", "gitops", "gitops"), ("argocd/*", "gitops", "gitops"),
]  # type: List[Tuple[str, str, str]]

IAC_FILES = [("*.tf", "terraform"), ("*.tofu", "opentofu"), ("cdk.json", "cdk"), ("Pulumi.yaml", "pulumi"), ("serverless.yml", "serverless"),
             ("serverless.yaml", "serverless"), ("sst.config.ts", "sst"), ("sst.config.js", "sst"), ("samconfig.toml", "sam"), ("main.bicep", "bicep"),
             ("Chart.yaml", "helm"), ("kustomization.yaml", "kustomize"), ("*/cloudformation/*.yml", "cloudformation"), ("*/cloudformation/*.yaml", "cloudformation"),
             ("cloudformation/*.yml", "cloudformation"), ("cloudformation/*.yaml", "cloudformation"), ("template.yaml", "sam-or-cfn"),
             ("terragrunt.hcl", "terragrunt"), ("*/terragrunt.hcl", "terragrunt")]


# Patterns only the repository root can honour (GitHub reads only the root `.github/`; these names mean something else deeper down).
_ROOT_ONLY_PREFIXES = (".github/", ".circleci/", ".buildkite/", ".woodpecker/", ".do/", ".spacelift/", ".vercel/", ".argocd/")
_ROOT_ONLY_NAMES = ("app.yaml", "template.yaml", "CNAME", ".nojekyll", "bin/*.ts", "docs/CNAME", ".platform.app.yaml", "Jenkinsfile", ".gitlab-ci.yml", ".travis.yml",
                    "bitbucket-pipelines.yml", "azure-pipelines.yml", ".drone.yml", "appveyor.yml", ".woodpecker.yml", "atlantis.yaml", "atlantis.yml")


def _match(rel: str, pattern: str) -> bool:
    """fnmatch with gitignore-like depth: `fnmatch`'s `*` crosses `/` (k8s/* covers k8s/a/b.yaml); a bare name matches at any depth
    (a monorepo keeps apps/web/vercel.json and services/api/fly.toml); root-only names and prefixes stay at the root."""
    if fnmatch.fnmatchcase(rel, pattern):
        return True
    if pattern.startswith(_ROOT_ONLY_PREFIXES) or pattern in _ROOT_ONLY_NAMES:
        return False
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatchcase(rel, "*/" + pattern)


def surface_files(files: List[str]) -> List[str]:
    out = []  # type: List[str]
    for f in files:
        rel = f.replace(os.sep, "/")
        if any(_match(rel, p) for p in FLOOR_PATTERNS):
            out.append(rel)
    return sorted(set(out))


def fingerprint(repo: str, rels: List[str]) -> str:
    """sha256 over path + content hash of every deploy-surface file, and of package.json scripts."""
    lines = []  # type: List[str]
    for rel in sorted(rels):
        data, problem = safeio.read_bytes(os.path.join(repo, rel), 4 << 20)
        # a deploy-surface path that is not a regular file (a pipe, a device) fingerprints as such: swapping one in voids the attestation
        digest = hashlib.sha256(data).hexdigest() if data is not None else "unreadable:%s" % problem
        lines.append("%s\0%s" % (rel, digest))
    raw = safeio.read_text(os.path.join(repo, "package.json"), 1 << 20)
    try:
        scripts = json.loads(raw).get("scripts", {}) if raw else None
        if scripts is not None:
            lines.append("package.json#scripts\0" + hashlib.sha256(json.dumps(scripts, sort_keys=True).encode()).hexdigest())
    except (ValueError, AttributeError):
        pass
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


# --- trigger classification --------------------------------------------------------------------------------

def _branch_fire(patterns: List[str]) -> List[str]:
    out = []  # type: List[str]
    positive = [p for p in patterns if not p.startswith("!")]
    if not positive:
        return ["push:branches"]  # only exclusions: every other branch fires it
    for p in positive:
        out.append("push:main" if p in MAIN_NAMES else "push:branches")
    return out


def fires_on(wf: workflows.Workflow) -> List[str]:
    """Where this workflow runs because a commit, tag, comment or dispatch happened. `workflow_dispatch`
    alone is human-started (the bot lacks `actions`) and is not listed."""
    out = []  # type: List[str]
    for ev, cfg in wf.events.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if ev == "push":
            branches = [str(b) for b in workflows._as_list(cfg.get("branches"))]
            tags = workflows._as_list(cfg.get("tags"))
            ignore = workflows._as_list(cfg.get("branches-ignore"))
            if branches:
                out += _branch_fire(branches)
            elif not tags or ignore:
                out.append("push:branches")
            if tags or not branches:
                out.append("tag")
        elif ev in ("create", "release", "registry_package", "repository_dispatch", "deployment", "deployment_status", "status",
                    "check_run", "check_suite", "issue_comment", "issues", "discussion", "discussion_comment", "label", "milestone", "page_build",
                    "project", "project_card", "project_column", "public", "watch", "fork", "gollum"):
            out.append("release" if ev == "release" else ev)
        elif ev in workflows.PR_EVENTS:
            out.append("pull_request")
        elif ev == "workflow_run":
            br = [str(b) for b in workflows._as_list(cfg.get("branches"))]
            out.append("workflow_run:main" if br and all(b in MAIN_NAMES for b in br) else "workflow_run")
        elif ev == "schedule":
            out.append("schedule:main")
    seen = []  # type: List[str]
    for o in out:
        if o not in seen:
            seen.append(o)
    return seen


def is_main_only(fire: str) -> bool:
    return fire in ("push:main", "workflow_run:main", "schedule:main")


def bot_pushable(fire: str) -> bool:
    return not is_main_only(fire)


_EXTERNAL_DEPLOY_NAME = re.compile(r"deploy|release|publish|infra|terraform|cdk|apply|rollout|\bcd\b|prod", re.I)
_EXTERNAL_ACTION_NAME = re.compile(r"deploy|publish|release|rollout|apply|terraform|cdk|pulumi|helm|kubectl|ansible", re.I)
# Actions that install a tool or fetch credentials and cannot deploy by themselves; their names contain "terraform" or similar, so the
# name heuristic above would call them uninspectable deploys (Phase 6: the infra-check and infra-plan templates use them).
_SETUP_ACTIONS = ("hashicorp/setup-terraform", "opentofu/setup-opentofu", "terraform-linters/setup-tflint", "aws-actions/configure-aws-credentials",
                  "google-github-actions/auth", "google-github-actions/setup-gcloud", "azure/login", "azure/setup-helm", "azure/setup-kubectl")
_SCRIPT_REF_RE = re.compile(r"(?:^|[\s;&|(\"'`])(?:(?:bash|sh|zsh|dash|python3?|node|ruby|perl|deno|bun|tsx|npx\s+tsx)\s+(?:-[A-Za-z]+\s+)*)?(\.?/?[\w./-]+\.(?:sh|bash|py|js|mjs|cjs|ts|rb|pl))\b")
_NPM_RUN_RE = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([\w:.-]+)")


class Expander(object):
    """Resolves one level of indirection: a workflow step that runs `./scripts/x.sh`, `make t`, `npm run t`, `just t` or a local
    composite action. A deploy hidden in a script is still a deploy; the script joins the fingerprint."""

    def __init__(self, repo: Optional[str]) -> None:
        self.repo = repo
        self.referenced = set()  # type: set

    def _read(self, rel: str, limit: int = 200000) -> str:
        if not self.repo:
            return ""
        p = os.path.normpath(os.path.join(self.repo, rel))
        if not p.startswith(os.path.abspath(self.repo) + os.sep):
            return ""
        text = safeio.read_text(p, limit)
        if text is None:
            return ""
        self.referenced.add(os.path.relpath(p, self.repo).replace(os.sep, "/"))
        return text

    def expand(self, run_text: str) -> str:
        extra = []  # type: List[str]
        for m in _SCRIPT_REF_RE.finditer(run_text):
            extra.append(self._read(m.group(1).lstrip("./") if m.group(1).startswith("./") else m.group(1)))
        if re.search(r"\bmake\b", run_text):
            extra.append(self._read("Makefile") or self._read("makefile") or self._read("GNUmakefile"))
        if re.search(r"\bjust\b", run_text):
            extra.append(self._read("justfile") or self._read("Justfile"))
        pkg = None
        for m in _NPM_RUN_RE.finditer(run_text):
            if pkg is None:
                raw = self._read("package.json")
                try:
                    pkg = json.loads(raw).get("scripts", {}) if raw else {}
                except (ValueError, AttributeError):
                    pkg = {}
            name = m.group(1)
            for key in (name, "pre" + name, "post" + name):
                if isinstance(pkg.get(key), str):
                    extra.append(pkg[key])
        return "\n".join(e for e in extra if e)

    def action(self, uses: str) -> str:
        if not uses.startswith("./"):
            return ""
        base = uses[2:].rstrip("/")
        return self._read(base + "/action.yml") or self._read(base + "/action.yaml")


def _job_deploys(job: workflows.Job, wf_by_path: Dict[str, workflows.Workflow], depth: int = 0, exp: Optional[Expander] = None) -> List[str]:
    labels = list(workflows.deploy_labels("\n".join(s.run for s in job.steps)))
    for s in job.steps:
        lab = workflows.deploy_uses_label(s)
        if lab is None and s.uses and not s.uses.startswith(("./", "docker://", "actions/")) and s.uses.split("@")[0].lower() not in _SETUP_ACTIONS \
                and _EXTERNAL_ACTION_NAME.search(s.uses.split("@")[0]):
            lab = "external action %s (cannot be inspected)" % s.uses.split("@")[0]
        if lab and lab not in labels:
            labels.append(lab)
        if exp is not None:
            for text in (exp.expand(s.run) if s.run else "", exp.action(s.uses) if s.uses else ""):
                for lab2 in workflows.deploy_labels(text) + workflows.scan_text(text):
                    lab2 = lab2 + " (via a script or local action)"
                    if lab2 not in labels:
                        labels.append(lab2)
    if job.uses.startswith("./") and depth < 2:
        target = wf_by_path.get(job.uses[2:].split("@")[0])
        if target is not None:
            for j in target.jobs.values():
                for lab in _job_deploys(j, wf_by_path, depth + 1, exp):
                    if lab not in labels:
                        labels.append(lab)
    elif job.uses and not job.uses.startswith("./") and _EXTERNAL_DEPLOY_NAME.search(job.uses):
        labels.append("external reusable workflow %s (cannot be inspected)" % job.uses.split("@")[0])
    return labels


def classify_workflow(rel: str, text: str, wf_by_path: Optional[Dict[str, workflows.Workflow]] = None, exp: Optional[Expander] = None) -> Dict[str, Any]:
    """One workflow's deploy facts. `parsed` False means the subset parser failed and the raw-text
    fallback was used: triggers are then unknown and treated as bot-pushable."""
    try:
        wf = workflows.parse(text, rel)
    except workflows.Unparsed as exc:
        labels = workflows.scan_text(text)
        return {"file": rel, "parsed": False, "error": str(exc), "deploys": bool(labels), "labels": labels, "fires_on": ["push:branches"] if labels else [],
                "jobs": [], "environment_all": False, "release_automation": any("release" in l for l in labels)}
    jobs = []  # type: List[Dict[str, Any]]
    for jid, job in wf.jobs.items():
        labels = _job_deploys(job, wf_by_path or {}, 0, exp)
        if labels:
            jobs.append({"job": jid, "labels": labels, "environment": job.environment})
    fires = fires_on(wf)
    all_labels = sorted({l for j in jobs for l in j["labels"]})
    return {"file": rel, "parsed": True, "error": "", "deploys": bool(jobs), "labels": all_labels, "fires_on": fires, "jobs": jobs,
            "environment_all": bool(jobs) and all(j["environment"] for j in jobs),
            "release_automation": any("release" in l for l in all_labels)}


# --- gatherer ---------------------------------------------------------------------------------------------

def _list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def _norm_path(p: Any) -> str:
    return str(p or "").strip().lstrip("./") if str(p or "").startswith("./") else str(p or "").strip()


def attested_triggers(ctx: Context) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for e in _list(ctx.cfg("deploy_triggers", [])):
        if isinstance(e, dict):
            out.append(e)
    return out


def attested_hosts(ctx: Context) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for e in _list(ctx.cfg("git_integrated_hosts", [])):
        if isinstance(e, dict):
            out.append(e)
    return out


@gatherer("deploy_surface")
def gather_deploy_surface(ctx: Context) -> Dict[str, Any]:
    files = ctx.fact("repo_files")
    rels = surface_files(files)
    wf_rels = sorted(r for r in rels if r.startswith(".github/workflows/") and r.endswith((".yml", ".yaml")) and r.count("/") == 2)
    texts = {}  # type: Dict[str, str]
    for r in wf_rels:
        texts[r] = safeio.read_text(os.path.join(ctx.repo, r), 1 << 20) or ""
    parsed = {}  # type: Dict[str, workflows.Workflow]
    for r, t in texts.items():
        try:
            parsed[r] = workflows.parse(t, r)
        except workflows.Unparsed:
            pass
    exp = Expander(ctx.repo)
    wfs = [classify_workflow(r, texts[r], parsed, exp) for r in wf_rels]
    for ref in sorted(exp.referenced):
        if ref not in rels:
            rels.append(ref)
    rels = sorted(set(rels))
    hosts = {}  # type: Dict[str, Dict[str, Any]]
    for rel in rels:
        for pat, host, kind in HOST_FILES:
            if _match(rel, pat):
                h = hosts.setdefault(host, {"host": host, "kind": kind, "files": []})
                h["files"].append(rel)
                break
    gh = ctx.fact("gh")
    pages = gh.get("pages") if isinstance(gh, dict) else None
    if pages:
        h = hosts.setdefault("github-pages", {"host": "github-pages", "kind": "host", "files": []})
        h["files"].append("repos/.../pages (source %s)" % ((pages.get("source") or {}).get("branch", "?") if isinstance(pages, dict) else "?"))
    iac = sorted({kind for f in files for pat, kind in IAC_FILES if _match(f.replace(os.sep, "/"), pat)})
    return {"workflows": wfs, "hosts": sorted(hosts.values(), key=lambda h: h["host"]), "files": rels, "iac": iac,
            "fingerprint": fingerprint(ctx.repo, [r for r in rels if not r.startswith("repos/")])}


# --- attestation judging ----------------------------------------------------------------------------------

def attestation_state(ctx: Context, surface: Dict[str, Any]) -> Tuple[str, str]:
    at = str(ctx.cfg("deploy_surface.attested_at", "") or "").strip()
    fp = str(ctx.cfg("deploy_surface.fingerprint", "") or "").strip()
    if not at or not fp:
        return "missing", "deploy_surface.attested_at / fingerprint are empty: nobody has attested the deployment surface"
    if fp != surface["fingerprint"]:
        return "stale", "a deploy-surface file changed since %s (fingerprint differs); the attestation is void until a human re-attests" % at
    return "current", "attested %s" % at


def check_attestation_schema(ctx: Context) -> List[str]:
    problems = []  # type: List[str]
    for i, e in enumerate(_list(ctx.cfg("deploy_triggers", []))):
        if not isinstance(e, dict) or not e.get("workflow") or not e.get("fires_on") or not e.get("gate"):
            problems.append("deploy_triggers[%d] needs workflow, fires_on and gate" % i)
        elif e.get("gate") not in GATES:
            problems.append("deploy_triggers[%d].gate must be one of %s" % (i, " | ".join(GATES)))
    for i, e in enumerate(_list(ctx.cfg("git_integrated_hosts", []))):
        if not isinstance(e, dict) or not e.get("host"):
            problems.append("git_integrated_hosts[%d] needs host" % i)
            continue
        if e.get("gate") not in GATES:
            problems.append("git_integrated_hosts[%d].gate must be one of %s" % (i, " | ".join(GATES)))
        if "previews_on" not in e or "production_on" not in e:
            problems.append("git_integrated_hosts[%d] needs previews_on and production_on (use none)" % i)
    return problems


def _norm_fires(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    return [p.strip() for p in str(value).replace(",", " ").split() if p.strip()]


def _covers(att_fires: List[str], fire: str) -> bool:
    for a in att_fires:
        if a == fire:
            return True
        if a == "push:branches" and fire == "push:branches":
            return True
        if a in ("any", "all", "*"):
            return True
    return False


def evaluate(ctx: Context, surface: Dict[str, Any]) -> Dict[str, Any]:
    """Judge the surface against the attestation. Returns the list of effective triggers and the caps."""
    att_wf = {_norm_path(e.get("workflow")): e for e in attested_triggers(ctx)}
    att_hosts = {str(e.get("host", "")).lower(): e for e in attested_hosts(ctx)}
    triggers = []  # type: List[Dict[str, Any]]
    gh = ctx.fact("gh") if "gh" in _gatherers() else None
    for wf in surface["workflows"]:
        if not wf["deploys"]:
            continue
        entry = att_wf.get(wf["file"])
        t = {"kind": "workflow", "name": wf["file"], "labels": wf["labels"], "detected_fires": wf["fires_on"], "attested": entry is not None,
             "gate": "none", "problems": [], "parsed": wf["parsed"], "release_automation": wf["release_automation"], "jobs": wf["jobs"]}
        if entry is None:
            t["fires_on"] = wf["fires_on"]
            t["gate"] = "none"
        else:
            afire = _norm_fires(entry.get("fires_on"))
            gate = str(entry.get("gate"))
            if afire == ["none"]:
                t["fires_on"] = []
                t["gate"] = "manual"
                t["note"] = "attested as not deploying"
            else:
                uncovered = [f for f in wf["fires_on"] if not _covers(afire, f)]
                t["fires_on"] = wf["fires_on"]
                t["gate"] = gate if gate in GATES else "none"
                if uncovered:
                    t["problems"].append("fires on %s, which the attestation does not list" % ", ".join(uncovered))
                    t["gate"] = "none"
                if t["gate"] == "manual" and wf["fires_on"]:
                    t["problems"].append("gate manual cannot hold for a workflow that fires on %s; use gated-by-environment, or fires_on: none if it does not deploy"
                                         % ", ".join(wf["fires_on"]))
                    t["gate"] = "none"
                if t["gate"] == "gated-by-environment":
                    if not wf["parsed"]:
                        t["problems"].append("workflow could not be parsed, so the Environment claim cannot be checked")
                        t["gate"] = "none"
                    elif not wf["environment_all"]:
                        missing = [j["job"] for j in wf["jobs"] if not j["environment"]]
                        t["problems"].append("attested gated-by-environment but job(s) %s name no environment:" % ", ".join(missing))
                        t["gate"] = "none"
                    elif gh and gh.get("environments") is not None:
                        names = {e.get("name"): e for e in gh["environments"]}
                        for j in wf["jobs"]:
                            env = names.get(j["environment"])
                            if env is None or not _env_has_reviewers(env):
                                t["problems"].append("environment %r has no required reviewers on this repo" % j["environment"])
                                t["gate"] = "none"
        triggers.append(t)
    for host in surface["hosts"]:
        entry = att_hosts.get(host["host"])
        t = {"kind": host["kind"], "name": host["host"], "files": host["files"], "attested": entry is not None, "problems": []}
        if entry is None:
            t.update({"previews_on": "unknown", "production_on": "unknown", "gate": "none"})
        else:
            t.update({"previews_on": str(entry.get("previews_on", "unknown")), "production_on": str(entry.get("production_on", "unknown")),
                      "gate": str(entry.get("gate")) if entry.get("gate") in GATES else "none"})
        triggers.append(t)
    surface_names = {t["name"] for t in triggers}
    for name, entry in att_wf.items():
        if name not in surface_names and not any(w["file"] == name for w in surface["workflows"]):
            triggers.append({"kind": "attested", "name": name, "attested": True, "fires_on": _norm_fires(entry.get("fires_on")),
                             "gate": str(entry.get("gate")) if entry.get("gate") in GATES else "none", "problems": [],
                             "note": "declared by the human (not found in the repo)"})
    for name, entry in att_hosts.items():
        if not any(t["name"] == name for t in triggers):
            triggers.append({"kind": "attested-host", "name": name, "attested": True, "previews_on": str(entry.get("previews_on", "unknown")),
                             "production_on": str(entry.get("production_on", "unknown")),
                             "gate": str(entry.get("gate")) if entry.get("gate") in GATES else "none", "problems": [],
                             "note": "declared by the human (dashboard config)"})
    # derive firing classes for each trigger
    for t in triggers:
        t["push_fires"], t["main_fires"] = _fire_classes(t)
        t["ungated_bot_pushable"] = bool(t["push_fires"]) and t["gate"] == "none"
        t["ungated_main"] = bool(t["main_fires"]) and t["gate"] == "none"
        if t["kind"] in ("ci", "gitops") and not t["attested"]:
            t["ungated_bot_pushable"] = True
    return {"triggers": triggers}


def _gatherers() -> Dict[str, Any]:
    from .model import GATHERERS
    return GATHERERS


def _env_has_reviewers(env: Dict[str, Any]) -> bool:
    for r in _list(env.get("protection_rules")):
        if isinstance(r, dict) and r.get("type") == "required_reviewers" and _list(r.get("reviewers")):
            return True
    return False


def _fire_classes(t: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    push, main = [], []  # type: List[str], List[str]
    if t["kind"] in ("workflow", "attested"):
        for f in t.get("fires_on") or []:
            (main if is_main_only(f) else push).append(f)
    else:
        prev = str(t.get("previews_on", "unknown")).lower()
        if prev not in SAFE_PREVIEWS:
            push.append("previews:" + prev)
        prod = str(t.get("production_on", "unknown")).lower()
        if prod in MAIN_NAMES:
            main.append("production:" + prod)
        elif prod not in ("none",):
            push.append("production:" + prod)
    return push, main


def level_caps(ctx: Context, surface: Dict[str, Any], ev: Dict[str, Any]) -> List[Tuple[int, str]]:
    """[(max level, reason)] from B5. Empty means no cap."""
    caps = []  # type: List[Tuple[int, str]]
    state, msg = attestation_state(ctx, surface)
    if state != "current":
        caps.append((1, msg))
    for t in ev["triggers"]:
        if t["ungated_bot_pushable"]:
            what = ", ".join(t.get("push_fires") or ["unreadable CD config"])
            caps.append((1, "%s deploys on a bot-pushable ref (%s) with no human gate" % (t["name"], what)))
        elif t["ungated_main"]:
            caps.append((4, "%s deploys on main (%s) with no human gate: no auto-merge" % (t["name"], ", ".join(t["main_fires"]))))
    return caps


# --- checks -------------------------------------------------------------------------------------------------

@check("deploy.attestation", "deploy-surface", emits=("deploy.attestation-schema",))
def check_attestation(ctx: Context) -> List[Result]:
    surface = ctx.fact("deploy_surface")
    state, msg = attestation_state(ctx, surface)
    out = []  # type: List[Result]
    rem = ("review the findings below, set deploy_triggers and git_integrated_hosts in loop.config, then set deploy_surface.attested_at to today and "
           "deploy_surface.fingerprint to %s (a human does this under H6)" % surface["fingerprint"])
    if state == "current":
        out.append(passed("deploy.attestation", msg, detail={"fingerprint": surface["fingerprint"]}))
    else:
        out.append(failed("deploy.attestation", msg, rem, severity=ERROR if state == "stale" else WARN, detail={"fingerprint": surface["fingerprint"]}))
    for p in check_attestation_schema(ctx):
        out.append(failed("deploy.attestation-schema", p, "fix the entry in loop.config"))
    return out


@check("deploy.surface", "deploy-surface", emits=("deploy.push-workflow", "deploy.main-workflow", "deploy.workflow.", "deploy.host.", "deploy.unparsed"))
def check_surface(ctx: Context) -> List[Result]:
    surface = ctx.fact("deploy_surface")
    ev = evaluate(ctx, surface)
    out = []  # type: List[Result]
    for wf in surface["workflows"]:
        if not wf["parsed"] and not wf["deploys"]:
            out.append(unknown("deploy.unparsed", "%s: unparsed (%s): manual review; the raw text shows no deploy command" % (wf["file"], wf["error"]),
                               "read it yourself; if it deploys, add it to deploy_triggers", severity=WARN))
    for t in ev["triggers"]:
        if t["kind"] == "workflow" or t["kind"] == "attested":
            fires = ", ".join(t.get("fires_on") or []) or "(none)"
            label = "%s: %s; fires on %s; gate %s%s" % (t["name"], ", ".join(t.get("labels") or ["declared"]), fires, t["gate"],
                                                         "" if t["attested"] else " (not attested)")
            if t["problems"]:
                label += "; " + "; ".join(t["problems"])
            if t["ungated_bot_pushable"]:
                out.append(failed("deploy.push-workflow", label,
                                  "gate it (job environment: with required reviewers, or workflow_dispatch only), or restrict it to main behind a gate; then attest it in deploy_triggers",
                                  detail={"workflow": t["name"]}))
            elif t["ungated_main"]:
                out.append(failed("deploy.main-workflow", label,
                                  "add a human gate (Environment reviewers), or accept that L5 auto-merge is off for this repo (cap L4)", severity=WARN,
                                  detail={"workflow": t["name"]}))
            else:
                out.append(passed("deploy.workflow.%s" % os.path.basename(t["name"]), label))
        else:
            label = "%s (%s): previews %s, production %s, gate %s%s" % (t["name"], ", ".join(t.get("files") or [t.get("note", "declared")]),
                                                                          t.get("previews_on"), t.get("production_on"), t["gate"],
                                                                          "" if t["attested"] else " (not attested)")
            rid = "deploy.host.%s" % t["name"]
            if not t["attested"]:
                out.append(failed(rid, label + ": a config file is present but nobody attested what it deploys",
                                  "add an entry to git_integrated_hosts (host, previews_on, production_on, gate; use none for \"no git integration\")"))
            elif t["ungated_bot_pushable"]:
                out.append(failed(rid, label + ": deploys on a bot-pushable ref with no human gate",
                                  "turn off branch previews (or make them label-triggered) in the host dashboard, then update the attestation"))
            elif t["ungated_main"]:
                out.append(failed(rid, label + ": deploys on main with no human gate", "add a manual promote step in the host, or accept the L4 cap", severity=WARN))
            else:
                out.append(passed(rid, label))
    if not ev["triggers"]:
        out.append(passed("deploy.surface", "no deploy workflows or host config found in the repo (git-integrated hosts configured in a dashboard still need attesting)"))
    return out


@check("deploy.release-automation", "deploy-surface")
def check_release_automation(ctx: Context) -> Result:
    surface = ctx.fact("deploy_surface")
    names = [w["file"] for w in surface["workflows"] if w["release_automation"]]
    if not names:
        return skipped("deploy.release-automation", "no release automation (release-please, changesets, semantic-release) found")
    return passed("deploy.release-automation", "release automation in %s: its PRs and tags are H5 and are never auto-merged at any level" % ", ".join(names))


@check("deploy.iac", "deploy-surface")
def check_iac(ctx: Context) -> Result:
    iac = ctx.fact("deploy_surface")["iac"]
    if not iac:
        return skipped("deploy.iac", "no infrastructure-as-code found")
    return passed("deploy.iac", "infrastructure as code: %s. Apply is H4: it needs an Environment with required reviewers or a human-only dispatch" % ", ".join(iac))
