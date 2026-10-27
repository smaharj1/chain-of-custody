"""guard-infra: a verb classifier over the infrastructure tool table (report §7.1, §8.2, §5.4, CS-4, CS-12, CO-39).

A TYPO GUARD. Infrastructure apply, deploy and destroy are human (H4); what keeps them human is the credential
boundary (B2), the sandbox (B3) and server-side policy (B4). This guard catches the honest mistake early, with a
reason, for every caller.

For each tool in the table the guard holds an ALLOW list of verbs (§8.2: what an agent may run locally). Any other
verb of a known tool is denied, so a verb the guard has never heard of fails closed:

    terraform | tofu | terragrunt   fmt, validate, version, graph, providers, init -backend=false
    cdk | cdktf                     ls, doctor, version, init, ...; `synth` only when a sandbox is attested
    pulumi                          version, help (preview executes the program)
    aws                             describe-*, list-*, get-* (not the secret-bearing ones), sts get-caller-identity
    gcloud | az | gsutil | bq       read verbs (list, describe, show, get), never auth/secrets/token verbs
    kubectl | helm                  kustomize, version --client; helm lint|template|package|create
    sam | serverless | sst | ...    validate, build, package, print, version, help
    flyctl | vercel | netlify | wrangler | railway | render | heroku | firebase | supabase | doctl ...  version, help

`plan`, `synth`, `preview` and `diff` are denied locally (CS-4: they execute provider or program code); the same
verbs run in CI after a human gate (§8.4). The guard also denies tunnels and port-forwards (CS-12), publishing
(releases are H5), migrations aimed at a shared database, cloud-credential environment assignments, and, for
`mcp__*` tools, `run_script`, `*deploy*` and mutating cloud/DB/SaaS tools (denied for subagents, asked in a main
session).

`npm|pnpm|yarn|bun run`, `make` and `just` targets are expanded by the shell analyser, so `npm run deploy` is
judged by what `deploy` runs.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .invoke import Inv
from .lib import Ctx, Decision, ask, deny

G = "guard-infra"

TF = {"terraform", "tofu", "terragrunt", "terraform-bin"}
TF_ALLOW = {"fmt", "validate", "version", "graph", "providers", "help", "hclfmt", "hclvalidate", "-version", "--version", "-help", "-h", "--help"}
TF_PLAN = {"plan", "run-all", "show"}

CDK = {"cdk", "cdktf", "cdk8s", "projen-cdk"}
CDK_ALLOW = {"ls", "list", "doctor", "version", "--version", "init", "docs", "doc", "acknowledge", "ack", "notices", "context", "help", "--help", "get"}
CDK_SYNTH = {"synth", "synthesize"}
CDK_PLAN = {"diff"}

AWS_READ_PREFIX = ("describe-", "list-", "get-", "batch-get-", "head-", "lookup-", "validate-", "search-", "check-", "estimate-", "simulate-",
                   "tail", "poll", "wait")
AWS_SECRET_VERBS = {"get-secret-value", "get-session-token", "get-login-password", "get-authorization-token", "assume-role",
                    "get-federation-token", "get-credentials-for-identity", "get-credentials", "get-open-id-token", "get-id",
                    "get-parameter", "get-parameters", "get-parameters-by-path", "get-password-data", "get-service-credentials",
                    "get-access-key-last-used", "get-sso-role-credentials", "get-pipeline-state-secret", "get-key-policy",
                    "get-random-password", "get-ssh-public-key", "get-layer-version", "get-function", "get-bucket-policy"}
AWS_SECRET_VERBS_STRICT = {"get-secret-value", "get-session-token", "get-login-password", "get-authorization-token", "assume-role",
                           "get-federation-token", "get-credentials-for-identity", "get-sso-role-credentials", "get-parameter",
                           "get-parameters", "get-parameters-by-path", "get-password-data", "get-open-id-token", "get-service-credentials"}

READ_VERBS = {"list", "describe", "show", "get", "ls", "version", "help", "--help", "--version", "-h", "info", "status", "whoami",
              "validate", "lint", "check", "config-validate"}
CLOUD_SECRET_WORDS = {"auth", "login", "token", "tokens", "secret", "secrets", "print-access-token", "access-token", "credentials",
                      "keys", "key", "ssh", "ssh-keys", "get-access-token", "impersonate-service-account", "application-default",
                      "service-accounts-keys"}

DEPLOY_CLIS = {"flyctl", "fly", "vercel", "netlify", "railway", "render", "wrangler", "wrangler2", "firebase", "supabase", "heroku",
               "doctl", "linode-cli", "scw", "oci", "ibmcloud", "aliyun", "tccli", "eb", "copilot", "ecs-cli", "amplify", "sst",
               "vagrant", "ansible", "pulumi", "kamal", "dokku", "caprover", "up", "now", "surge", "gh-pages", "gatsby-deploy",
               "expo", "eas", "fastlane", "appcenter", "pm2-deploy", "cap", "mina", "capistrano", "jenkins-cli", "argocd", "flux",
               "skaffold", "tilt", "garden", "waypoint", "nomad", "consul", "vault", "boundary", "openshift", "oc", "kops", "eksctl",
               "gkectl", "minikube", "kind", "k3d", "k3s", "microk8s", "rancher", "crossplane", "cdk8s"}
DEPLOY_ALLOW = {"version", "--version", "-v", "-version", "help", "--help", "-h"}
# CLIs for which a couple of local-only verbs are fine
EXTRA_ALLOW = {"flyctl": {("config", "validate")}, "fly": {("config", "validate")}, "kind": {("get", "clusters"), ("export", "logs")},
               "minikube": {("status",), ("version",)}, "oc": set(), "vault": {("version",)}, "nomad": {("job", "validate")}}

TUNNEL_TOOLS = {"autossh", "sshuttle", "cloudflared", "ngrok", "localtunnel", "lt", "bore", "chisel", "rathole", "zrok", "pgrok",
                "telepresence", "frpc", "frps", "tailscale", "ssh-tunnel"}

PUBLISH = {
    "npm": {"publish", "unpublish", "deprecate", "dist-tag", "owner", "access", "token", "login", "adduser", "logout", "team", "profile"},
    "pnpm": {"publish"}, "yarn": {"publish", "npm"}, "bun": {"publish"}, "lerna": {"publish"}, "changeset": {"publish"},
    "twine": {"upload"}, "poetry": {"publish"}, "flit": {"publish"}, "hatch": {"publish"}, "cargo": {"publish", "yank", "login", "owner"},
    "gem": {"push", "yank", "owner", "signin"}, "vsce": {"publish"}, "ovsx": {"publish"}, "helm": {"push"},
    "docker": {"push", "login", "logout", "trust"}, "podman": {"push", "login", "logout"}, "nerdctl": {"push", "login"},
    "dotnet": set(), "mvn": set(), "gradle": set(), "semantic-release": {""}, "release-it": {""}, "np": {""}, "goreleaser": {"release"},
}

MIGRATORS = {"prisma": {"migrate", "db"}, "knex": {"migrate:latest", "migrate:up", "migrate:rollback", "migrate:down"},
             "sequelize": {"db:migrate", "db:migrate:undo", "db:seed:all"}, "sequelize-cli": {"db:migrate", "db:migrate:undo"},
             "alembic": {"upgrade", "downgrade"}, "flyway": {"migrate", "clean", "repair"}, "liquibase": {"update", "rollback"},
             "dbmate": {"up", "down", "migrate", "rollback"}, "goose": {"up", "down", "reset"}, "migrate": {"up", "down", "force", "goto"},
             "rails": {"db:migrate", "db:rollback", "db:drop", "db:reset", "db:seed", "db:schema:load"}, "rake": {"db:migrate", "db:rollback", "db:drop", "db:reset", "db:schema:load"},
             "typeorm": {"migration:run", "migration:revert", "schema:sync", "schema:drop"}, "drizzle-kit": {"push", "migrate", "drop"},
             "kysely": {"migrate"}, "mikro-orm": {"migration:up", "migration:down", "schema:update", "schema:drop"},
             "django-admin": {"migrate"}, "manage.py": {"migrate", "flush", "loaddata"}, "atlas": {"migrate", "schema"},
             "sqitch": {"deploy", "revert"}, "supabase": {"db"}, "ecto": {"migrate"}, "mix": {"ecto.migrate", "ecto.rollback", "ecto.reset"}}
PRODISH = re.compile(r"(^|[^a-z])(prod|production|staging|stage|live|shared|main|release|preprod)([^a-z]|$)", re.I)
DB_CLIENTS = {"psql", "mysql", "mariadb", "mongosh", "mongo", "redis-cli", "pg_restore", "pg_dump", "mysqldump", "sqlcmd", "cqlsh", "clickhouse-client",
              "valkey-cli", "usql", "pgcli", "mycli"}

CRED_ENV_RE = re.compile(
    r"^(AWS_(?!REGION$|DEFAULT_REGION$|PAGER$|CA_BUNDLE$|ENDPOINT_URL.*$|RETRY.*$|MAX_ATTEMPTS$|EC2_METADATA_DISABLED$|SDK_LOAD_CONFIG$).*|"
    r"GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_CLOUD_.*|CLOUDSDK_.*|GCLOUD_.*|AZURE_(?!CORE_.*).*|ARM_.*|KUBECONFIG|DOCKER_HOST|DOCKER_CONFIG|"
    r"DOCKER_CONTEXT|TF_TOKEN_.*|TF_CLI_CONFIG_FILE|TF_VAR_.*(TOKEN|SECRET|PASSWORD|KEY).*|GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|"
    r"GITHUB_ENTERPRISE_TOKEN|GH_HOST|VERCEL_TOKEN|NETLIFY_AUTH_TOKEN|FLY_API_TOKEN|FLY_ACCESS_TOKEN|CLOUDFLARE_API_TOKEN|CF_API_TOKEN|"
    r"CLOUDFLARE_API_KEY|HEROKU_API_KEY|DIGITALOCEAN_ACCESS_TOKEN|DIGITALOCEAN_TOKEN|RAILWAY_TOKEN|RENDER_API_KEY|NPM_TOKEN|NODE_AUTH_TOKEN|"
    r"PULUMI_ACCESS_TOKEN|PULUMI_CONFIG_PASSPHRASE|VAULT_TOKEN|VAULT_ADDR|SUPABASE_ACCESS_TOKEN|FIREBASE_TOKEN|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|"
    r"OP_SERVICE_ACCOUNT_TOKEN|SSH_AUTH_SOCK|SSH_AGENT_PID)$")
FAKE_VALUE = re.compile(r"^(test|fake|dummy|local|localstack|testing|mock|xxx+|x|123|foo|bar|none|default|sandbox|ci|dev|development|us-east-1|http://localhost.*|"
                        r"http://127\.0\.0\.1.*)$|^(test|fake|dummy|mock)[-_a-z0-9]*$", re.I)


def _who(inv: Inv) -> str:
    return "" if inv.origin in ("top", "subst", "") else " (reached through %s)" % inv.origin


def _pos(args: Sequence[str], dyn: Sequence[bool], with_arg: Sequence[str] = ()) -> List[Tuple[str, bool]]:
    out, skip = [], False   # type: List[Tuple[str, bool]], bool
    for a, d in zip(args, dyn):
        if skip:
            skip = False
            continue
        if a == "--":
            continue
        if a.startswith("-") and len(a) > 1:
            if a in with_arg:
                skip = True
            continue
        out.append((a, d))
    return out


def _mut(inv: Inv, text: str) -> Decision:
    return deny(G, "mutating-verb", "%s%s: infrastructure apply, deploy and destroy are human (H4). The agent authors IaC source; CI plans and applies it "
                "behind a human gate (report §8). Do not run it, and do not look for another way to" % (text, _who(inv)))


def _plan(inv: Inv, text: str) -> Decision:
    return deny(G, "local-plan", "%s%s: plan, synth, preview and diff execute provider or program code, so they run in CI after a human gate, not on "
                "this machine (CS-4, report §8.2)" % (text, _who(inv)))


def _unlisted(inv: Inv, tool: str, verb: str) -> Decision:
    return deny(G, "unlisted-verb", "%s %s%s is not on the list of verbs an agent may run locally for %s (report §8.2). Allowed: %s" %
                (tool, verb[:40], _who(inv), tool, _allowed_text(tool)))


def _allowed_text(tool: str) -> str:
    if tool in TF:
        return "fmt, validate, init -backend=false, version, graph, providers"
    if tool in CDK:
        return "ls, doctor, version, init, and synth only inside an attested sandbox"
    if tool == "aws":
        return "describe-*, list-*, get-* (not secret-bearing), sts get-caller-identity"
    if tool in ("kubectl",):
        return "kustomize, version --client"
    if tool == "helm":
        return "lint, template, package, create, version"
    return "version, help"


# ---------------------------------------------------------------------------------------------------------------------

def check_bash(ctx: Ctx) -> Optional[Decision]:
    for inv in ctx.analysis.invs:
        d = _check_inv(ctx, inv)
        if d:
            return d
    return None


def _check_inv(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    d = _cred_env(inv)
    if d:
        return d
    name = inv.name
    args, dyn = inv.args, inv.dyn
    if name in TF:
        return _terraform(inv)
    if name in CDK:
        return _cdk(ctx, inv)
    if name == "aws":
        return _aws(inv)
    if name in ("gcloud", "az", "gsutil", "bq", "azd", "gh-copilot"):
        return _cloud_generic(inv)
    if name == "kubectl" or name == "k":
        return _kubectl(inv)
    if name == "helm":
        return _helm(inv)
    if name == "sam":
        return _allow_only(inv, {"validate", "build", "init", "local", "version", "--version", "--help", "help"}, "sam")
    if name in ("serverless", "sls"):
        return _allow_only(inv, {"print", "package", "offline", "version", "--version", "help", "--help", "-v"}, name)
    if name == "packer":
        return _allow_only(inv, {"validate", "fmt", "version", "--version", "-version", "help"}, "packer")
    if name == "ansible-playbook":
        if any(a in ("--syntax-check", "--list-tasks", "--list-hosts", "--list-tags", "--version", "-h", "--help") for a in args):
            return None
        return _mut(inv, "ansible-playbook configures real hosts")
    if name in ("puppet", "chef-client", "chef-solo", "salt", "salt-call", "salt-ssh"):
        return _mut(inv, "%s configures real hosts" % name)
    if name in DEPLOY_CLIS:
        pos = [a for a, d in _pos(args, dyn)]
        key = tuple(pos[:2])
        if any(d for d in dyn[:2]):
            return deny(G, "unlisted-verb", "%s with a verb computed at run time" % name)
        if (pos and pos[0] in DEPLOY_ALLOW) or (not pos and any(a in DEPLOY_ALLOW for a in args)) or key in EXTRA_ALLOW.get(name, set()) \
                or (pos[:1] and (pos[0],) in EXTRA_ALLOW.get(name, set())):
            return None
        if pos and pos[0] in ("preview", "diff", "plan", "dry-run"):
            return _plan(inv, "%s %s" % (name, pos[0]))
        return _mut(inv, "%s %s is deploy-shaped" % (name, " ".join(pos[:2]) or ""))
    if name in TUNNEL_TOOLS:
        if name == "tailscale" and not any(a in ("funnel", "serve", "ssh") for a in args):
            return None
        return deny(G, "tunnel", "%s opens a tunnel or exposes a port; DATABASE_URL host inspection is defeated by a local port-forward (CS-12, §5.4)" % name)
    d = _tunnel_misc(ctx, inv)
    if d:
        return d
    d = _publish(inv)
    if d:
        return d
    d = _db(inv)
    if d:
        return d
    return None


def _cred_env(inv: Inv) -> Optional[Decision]:
    names = dict(inv.env)
    if inv.name in ("export", "declare", "typeset", "readonly", "local"):
        for a in inv.args:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a, re.S)
            if m:
                names[m.group(1)] = m.group(2)
    for k, v in names.items():
        if CRED_ENV_RE.match(k) and not FAKE_VALUE.match(v or "") and v != "":
            if inv.name in ("unset",):
                continue
            return deny(G, "credential-env", "setting %s selects or supplies a real cloud, registry or GitHub credential; none exists for the agent by "
                        "design (B2). Test values (test, fake, localstack) are allowed" % k)
    return None


# -- tools ------------------------------------------------------------------------------------------------------------

def _terraform(inv: Inv) -> Optional[Decision]:
    args, dyn = inv.args, inv.dyn
    pos = _pos(args, dyn, ("-chdir",))
    if any(dyn[:3]):
        return deny(G, "unlisted-verb", "%s with a verb computed at run time" % inv.name)
    if not pos:
        return None if all(a in TF_ALLOW or a.startswith("-chdir") for a in args) else _unlisted(inv, inv.name, "(no verb)")
    sub = pos[0][0].lower()
    if sub in TF_ALLOW:
        return None
    if sub == "init":
        if any(a.lower() in ("-backend=false", "--backend=false") for a in args):
            return None
        return deny(G, "unlisted-verb", "%s init without -backend=false contacts the state backend and downloads provider code%s (report §8.2: init -backend=false)"
                    % (inv.name, _who(inv)))
    if sub == "workspace":
        return None if len(pos) > 1 and pos[1][0] in ("list", "show") else _unlisted(inv, inv.name, "workspace " + (pos[1][0] if len(pos) > 1 else ""))
    if sub in TF_PLAN:
        return _plan(inv, "%s %s" % (inv.name, sub))
    if sub in ("apply", "destroy", "import", "state", "taint", "untaint", "refresh", "force-unlock", "login", "logout", "push", "console",
               "test", "output", "get", "cloud", "rm", "mv", "replace-provider", "migrate"):
        return _mut(inv, "%s %s" % (inv.name, sub))
    return _unlisted(inv, inv.name, sub)


def _cdk(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    pos = _pos(inv.args, inv.dyn, ("-a", "--app", "-c", "--context", "-o", "--output", "--profile", "-p", "-r", "--role-arn", "-e", "--exclusively"))
    if any(inv.dyn[:3]):
        return deny(G, "unlisted-verb", "cdk with a verb computed at run time")
    if not pos:
        return None if any(a in CDK_ALLOW for a in inv.args) or not inv.args else None
    sub = pos[0][0].lower()
    if sub in CDK_ALLOW:
        return None
    if sub in CDK_SYNTH:
        iso = str(ctx.cfg("agent_isolation", "none") or "none")
        att = str(ctx.cfg("sandbox_verified", "") or "")
        if iso != "none" or att:
            return None
        return deny(G, "local-plan", "cdk synth executes the CDK app: allowed only in a sandbox with no credentials and no network (report §8.2). Attest the "
                    "sandbox in loop.config (agent_isolation or sandbox_verified, an H6 change) first%s" % _who(inv))
    if sub in CDK_PLAN:
        return _plan(inv, "cdk diff")
    return _mut(inv, "%s %s" % (inv.name, sub))


def _aws(inv: Inv) -> Optional[Decision]:
    pos = _pos(inv.args, inv.dyn, ("--profile", "--region", "--endpoint-url", "--output", "--query", "--cli-input-json", "--cli-input-yaml",
                                    "--color", "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--debug-file"))
    if any(inv.dyn):
        # `aws $SERVICE $VERB` cannot be judged
        if any(inv.dyn[:3]):
            return deny(G, "unlisted-verb", "aws with a service or verb computed at run time")
    if not pos:
        return None
    svc = pos[0][0].lower()
    verb = pos[1][0].lower() if len(pos) > 1 else ""
    if svc in ("help", "version", "--version") or verb == "help":
        return None
    if any(a == "--profile" for a in inv.args) and False:
        pass
    if svc == "sts" and verb == "get-caller-identity":
        return None
    if svc == "configure":
        return None if verb in ("list", "list-profiles", "get") else deny(G, "mutating-verb", "aws configure writes credentials and profiles (B2)")
    if svc == "s3":
        return None if verb in ("ls",) else _mut(inv, "aws s3 %s" % verb)
    if svc == "ssm" and verb.startswith("start-session"):
        return deny(G, "tunnel", "aws ssm start-session opens a shell or port-forward on an instance (CS-12)")
    if svc == "ecs" and verb == "execute-command":
        return deny(G, "tunnel", "aws ecs execute-command opens a shell in a running container (CS-12)")
    if verb in AWS_SECRET_VERBS_STRICT:
        return deny(G, "mutating-verb", "aws %s %s returns or mints a credential or secret; none is available to the agent (B2)" % (svc, verb))
    if verb.startswith(AWS_READ_PREFIX):
        return None
    return _mut(inv, "aws %s %s" % (svc, verb))


def _cloud_generic(inv: Inv) -> Optional[Decision]:
    pos = [a.lower() for a, d in _pos(inv.args, inv.dyn, ("--project", "--format", "--account", "--region", "--zone", "--subscription", "--configuration",
                                                           "--filter", "--limit", "--location", "-l", "-o", "--output", "--query", "--resource-group", "-g"))]
    if not pos:
        return None
    if pos[0] in ("version", "help", "--version", "info") and inv.name != "gcloud" or (inv.name == "gcloud" and pos[0] in ("version", "help", "info", "topic")):
        return None
    if any(w in CLOUD_SECRET_WORDS for w in pos):
        return deny(G, "mutating-verb", "%s %s touches authentication, tokens, keys or secrets (B2)" % (inv.name, " ".join(pos[:3])))
    if any("ssh" in w or "tunnel" in w or "bastion" in w or w in ("scp", "start-iap-tunnel") for w in pos):
        return deny(G, "tunnel", "%s %s opens a shell or tunnel to a machine (CS-12)" % (inv.name, " ".join(pos[:3])))
    if pos[-1] in READ_VERBS or (len(pos) > 1 and pos[1] in ("ls", "list", "show", "describe", "get") and inv.name in ("gsutil", "bq")):
        return None
    if inv.name == "gsutil" and pos[0] in ("ls", "stat", "version", "du", "hash", "cat"):
        return None
    if inv.name == "bq" and pos[0] in ("ls", "show", "head", "version", "query") and False:
        return None
    if inv.name == "bq" and pos[0] in ("ls", "show", "head", "version"):
        return None
    return _mut(inv, "%s %s" % (inv.name, " ".join(pos[:3])))


def _kubectl(inv: Inv) -> Optional[Decision]:
    pos = [a.lower() for a, d in _pos(inv.args, inv.dyn, ("-n", "--namespace", "--context", "--kubeconfig", "-f", "--filename", "-o", "--output",
                                                           "-l", "--selector", "-c", "--container", "--cluster", "--user"))]
    if not pos:
        return None
    sub = pos[0]
    if sub in ("kustomize", "completion", "options", "help"):
        return None
    if sub == "version" and any(a.startswith("--client") for a in inv.args):
        return None
    if sub in ("port-forward", "proxy", "exec", "attach", "cp", "debug"):
        return deny(G, "tunnel", "kubectl %s opens a shell, proxy or port-forward into a cluster (CS-12)" % sub)
    if sub == "diff":
        return _plan(inv, "kubectl diff")
    return _mut(inv, "kubectl %s" % sub)


def _helm(inv: Inv) -> Optional[Decision]:
    pos = [a.lower() for a, d in _pos(inv.args, inv.dyn, ("-n", "--namespace", "--kube-context", "--kubeconfig", "-f", "--values", "--set", "--version"))]
    if not pos:
        return None
    sub = pos[0]
    if sub in ("lint", "template", "package", "create", "version", "help", "env", "completion"):
        if sub == "template" and any(a in ("--validate", "--is-upgrade") for a in inv.args):
            return _plan(inv, "helm template --validate (it contacts the cluster)")
        return None
    if sub == "plugin" and len(pos) > 1 and pos[1] == "list":
        return None
    if sub in ("install", "upgrade", "uninstall", "delete", "rollback", "test", "push", "dependency", "dep", "repo", "registry", "pull", "get",
               "list", "ls", "status", "history", "show", "search", "verify"):
        if sub in ("list", "ls", "status", "history", "get", "show", "search", "verify"):
            return _unlisted(inv, "helm", sub)
        return _mut(inv, "helm %s" % sub)
    return _unlisted(inv, "helm", sub)


def _allow_only(inv: Inv, allowed: Set[str], tool: str) -> Optional[Decision]:
    pos = [a.lower() for a, d in _pos(inv.args, inv.dyn)]
    if not pos:
        return None
    if pos[0] in allowed or any(a in ("--version", "--help", "-h") for a in inv.args):
        return None
    return _mut(inv, "%s %s" % (tool, pos[0]))


def _tunnel_misc(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    name, args = inv.name, inv.args
    if name in ("ssh", "scp", "sftp", "mosh", "mosh-client", "rsync"):
        forwards = any(re.match(r"^-[A-Za-z]*[LRDwW]", a) and not a.startswith("--") or a in ("-L", "-R", "-D", "-w", "-W", "-N") for a in args if name == "ssh")
        opts = " ".join(args).lower()
        if name == "ssh" and (forwards or re.search(r"-o\s*\S*(forward|proxycommand|localcommand|permitlocalcommand)", opts)):
            return deny(G, "tunnel", "ssh port-forwarding or a proxy command (CS-12)")
        if name in ("ssh", "scp", "sftp", "mosh"):
            if ctx.is_subagent:
                return deny(G, "remote-shell", "%s reaches a remote machine; engineers and critics have no remote access (B2)" % name)
        if name == "rsync" and ctx.is_subagent and any(re.match(r"^[^/:\s-][^/\s]*:", a) or a.startswith("rsync://") or a in ("-e", "--rsh") for a in args):
            return deny(G, "remote-shell", "rsync to a remote host; engineers and critics have no remote access (B2)")
    if name in ("socat", "ncat", "nc", "netcat", "nc.openbsd", "nc.traditional"):
        low = [a.lower() for a in args]
        if name == "socat" and any("listen" in a or "tcp-l" in a or "tcp4-l" in a or "exec:" in a or "system:" in a for a in low):
            return deny(G, "tunnel", "socat listen, exec or system address (CS-12)")
        if name != "socat" and any(a in ("-l", "-e", "-c", "--listen", "--exec", "--sh-exec", "-k") or re.match(r"^-[a-z]*[lec]", a) and not a.startswith("--") and name != "nc" for a in low):
            return deny(G, "tunnel", "%s in listen or exec mode (CS-12)" % name)
        if name in ("nc", "netcat") and any(a in ("-l", "-e", "-c", "-k") or re.match(r"^-[a-z]*l", a) for a in low):
            return deny(G, "tunnel", "%s in listen or exec mode (CS-12)" % name)
    if name == "docker" or name == "podman":
        pass
    return None


def _publish(inv: Inv) -> Optional[Decision]:
    name = inv.name
    if name not in PUBLISH:
        if name in ("dotnet",) and inv.args[:2] == ["nuget", "push"]:
            return deny(G, "publish", "dotnet nuget push publishes a package (a release, H5)")
        if name == "mvn" and any(a in ("deploy", "release:perform") for a in inv.args):
            return deny(G, "publish", "mvn deploy publishes an artifact (a release, H5)")
        if name in ("gradle", "gradlew") and any(re.match(r"^(publish|upload)", a) for a in inv.args):
            return deny(G, "publish", "gradle publish uploads an artifact (a release, H5)")
        return None
    subs = PUBLISH[name]
    pos = [a.lower() for a, d in _pos(inv.args, inv.dyn, ("--prefix", "-C", "--cwd", "--registry", "-w", "--workspace", "--filter", "-F", "--tag", "--otp"))]
    if "" in subs:
        return deny(G, "publish", "%s publishes a release (H5)" % name)
    if pos and pos[0] in subs:
        return deny(G, "publish", "%s %s publishes or changes a registry entry (a release, H5; docker push is a deploy)" % (name, pos[0]))
    if name == "yarn" and pos[:2] == ["npm", "publish"]:
        return deny(G, "publish", "yarn npm publish publishes a package (a release, H5)")
    return None


def _host_of(url: str) -> Optional[str]:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]*@)?(\[[^\]]+\]|[^:/?#]+)", url)
    return m.group(1).strip("[]").lower() if m else None


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal", "", "db", "postgres", "mysql", "mariadb", "mongo", "redis", "database"}


def _remote_host(h: Optional[str]) -> bool:
    if h is None:
        return False
    return not (h in LOCAL_HOSTS or h.endswith(".localhost") or h.endswith(".local") or h.startswith("127.") or "." not in h)


def _db(inv: Inv) -> Optional[Decision]:
    name = inv.name
    env_all = " ".join("%s=%s" % kv for kv in inv.env.items())
    if name in DB_CLIENTS:
        for k, a in enumerate(inv.args):
            h = None
            if a in ("-h", "--host") and k + 1 < len(inv.args):
                h = inv.args[k + 1].lower()
            elif a.startswith("--host="):
                h = a.split("=", 1)[1].lower()
            elif "://" in a:
                h = _host_of(a)
            elif name == "redis-cli" and a == "-u" and k + 1 < len(inv.args):
                h = _host_of(inv.args[k + 1])
            if _remote_host(h):
                return deny(G, "db-remote", "%s to %s: shared and production databases are not reachable by agents (H4, §5.4)" % (name, h))
        for v in inv.env.values():
            if "://" in v and _remote_host(_host_of(v)):
                return deny(G, "db-remote", "%s with a connection URL for a non-local host (H4, §5.4)" % name)
        return None
    base = name.replace(".py", "")
    if base in MIGRATORS or (base in ("python", "python3") and False):
        subs = MIGRATORS[base]
        pos = [a.lower() for a, d in _pos(inv.args, inv.dyn)]
        if base == "prisma" and not (pos[:1] == ["migrate"] and (pos[1:2] == ["deploy"] or pos[1:2] == ["reset"] or pos[1:2] == ["resolve"])) \
                and not (pos[:2] == ["db", "push"]):
            return _migrate_env(inv, env_all, base == "prisma" and pos[:2] == ["migrate", "dev"])
        if any(p in subs for p in pos[:2]) or (base == "prisma"):
            if base == "prisma" and pos[:2] == ["migrate", "deploy"]:
                return deny(G, "db-prod-migrate", "prisma migrate deploy applies migrations to the database in DATABASE_URL; it is the production path "
                            "(local work uses migrate dev against the ephemeral DB, H4)")
            return _migrate_env(inv, env_all, False)
    if name in ("python", "python3") and inv.args[:1] and inv.args[0].endswith("manage.py") and inv.args[1:2] == ["migrate"]:
        return _migrate_env(inv, env_all, False)
    return None


def _migrate_env(inv: Inv, env_all: str, local_ok: bool) -> Optional[Decision]:
    text = " ".join(inv.args) + " " + env_all
    for m in re.finditer(r"[a-z]+://\S+", text):
        if _remote_host(_host_of(m.group(0))):
            return deny(G, "db-prod-migrate", "%s against a non-local database URL: migrations on shared or persistent databases are human (H4, §8.2)" % inv.name)
    if re.search(r"(--env(ironment)?[= ]|-e[= ]|_ENV=|ENV=|NODE_ENV=|STAGE=)\s*[\"']?(prod|production|staging|live)", text, re.I) or \
            re.search(r"\b(RAILS|NODE|APP|DJANGO|FLASK|NEXT|MIX|ENVIRONMENT|STAGE|ENV)[A-Z_]*=\s*[\"']?(prod|production|staging|live)", text, re.I):
        return deny(G, "db-prod-migrate", "%s targets a production or staging environment: migrations on shared databases are human (H4, §8.2)" % inv.name)
    return None


# ---------------------------------------------------------------------------------------------------------------------
# MCP

READ_PREFIXES = ("get", "list", "read", "search", "query", "describe", "fetch", "find", "show", "view", "check", "validate", "render",
                 "resolve", "display", "lookup", "browse", "download", "count", "inspect", "preview", "summarize", "tail", "head", "status",
                 "whoami", "ping", "help", "docs", "retrieve", "export-readonly", "interact", "navigate", "screenshot", "snapshot", "tabs",
                 "scroll", "hover", "wait", "take", "read-page", "find")
MUTATING_WORDS = {"create", "update", "delete", "remove", "put", "post", "send", "write", "set", "add", "apply", "deploy", "destroy", "terminate",
                  "invoke", "run", "execute", "exec", "start", "stop", "restart", "reboot", "publish", "merge", "push", "upload", "import",
                  "modify", "patch", "drop", "truncate", "insert", "upsert", "schedule", "cancel", "approve", "reject", "assign", "archive",
                  "transfer", "pay", "charge", "refund", "grant", "revoke", "attach", "detach", "rollback", "promote", "release", "trigger",
                  "dispatch", "reply", "comment", "react", "complete", "submit", "commit", "move", "rename", "copy", "replace", "clear",
                  "reset", "kill", "delete", "unsubscribe", "subscribe", "invite", "ban", "sign", "login", "logout", "install", "uninstall",
                  "enable", "disable", "configure", "provision", "scale", "migrate", "sync", "forward", "share", "transition", "close", "open"}


def _split_words(s: str) -> List[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s)
    return [w for w in re.split(r"[_\-\s]+", s.lower()) if w]


def check_mcp(ctx: Ctx) -> Optional[Decision]:
    tool = ctx.tool_name
    parts = tool.split("__")
    server = parts[1] if len(parts) > 2 else ""
    name = parts[-1] if len(parts) > 2 else tool
    words = _split_words(name)
    low = tool.lower()
    if "run_script" in low.replace("-", "_") or "deploy" in low:
        return deny(G, "mcp", "%s: mcp__*__run_script and mcp__*deploy* are denied in a loop session; an MCP tool is a tool plane that can reach cloud "
                    "control planes (report §5.6, CO-39)" % tool)
    first = words[0] if words else ""
    reads = bool(first) and any(first == p or first.startswith(p) for p in READ_PREFIXES) and first not in MUTATING_WORDS
    mutating = any(w in MUTATING_WORDS for w in words) and not reads
    if ctx.is_subagent:
        if reads:
            return None
        return deny(G, "mcp", "%s: engineers and critics have no MCP mutation tools; add a read-only docs server on purpose instead (report §5.6)" % tool)
    if mutating:
        return ask(G, "mcp", "%s looks like a mutating MCP tool (cloud, database or SaaS). Confirm it is intended (report §5.6)" % tool)
    return None
