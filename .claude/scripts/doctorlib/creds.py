"""B2: every credential an agent's OS user could reach (report §5.1 B2, §8.1 Layer 2, RC-5).

Principle: assume an agent with Bash can read anything its OS user can. So the doctor looks
for credentials by *reachability*, not by whether the agent is "supposed" to use them: env
vars, config files, credential helpers, key files, agent sockets, container sockets and the
cloud metadata endpoints. It reports names and paths, never values.

Every file-based check follows one shape: a gatherer returns hits as dicts
`{id, category, what, where}`, and `judge_hits` turns them into Results. A hit is an error
(it caps the level at L1) unless it is provably the bot's own credential.
"""
from __future__ import annotations

import glob
import os
import re
import stat
from typing import Any, Dict, Iterable, List, Optional, Tuple

from kitlib import redact

from . import safeio
from .model import (Context, Result, check, failed, gatherer, passed, skipped, unknown, ERROR, WARN)

# --- environment ------------------------------------------------------------------------------------

AWS_SAFE = {"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PAGER", "AWS_DEFAULT_OUTPUT", "AWS_SDK_LOAD_CONFIG",
            "AWS_EC2_METADATA_DISABLED", "AWS_EC2_METADATA_V1_DISABLED", "AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE",
            "AWS_STS_REGIONAL_ENDPOINTS", "AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT", "AWS_CLI_AUTO_PROMPT",
            "AWS_CLI_FILE_ENCODING", "AWS_CA_BUNDLE", "AWS_METADATA_SERVICE_TIMEOUT", "AWS_METADATA_SERVICE_NUM_ATTEMPTS",
            "AWS_SDK_UA_APP_ID", "AWS_SDK_JS_SUPPRESS_MAINTENANCE_MODE_MESSAGE"}

# (category, name regex). First match wins. Names only; values are never printed.
ENV_RULES = [
    ("github", re.compile(r"^(?:GH|GITHUB|HOMEBREW_GITHUB|COPILOT_GITHUB)(?:_ENTERPRISE)?_(?:[A-Z_]*)?(?:TOKEN|PAT|KEY|SECRET|PASSWORD|CREDENTIALS?)$")),
    ("github", re.compile(r"^(?:GH_PAT|GITHUB_PAT|CR_PAT|GITHUB_PERSONAL_ACCESS_TOKEN)$")),
    ("cloud", re.compile(r"^(?:GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_OAUTH_ACCESS_TOKEN|GOOGLE_CREDENTIALS|GCLOUD_[A-Z_]*|CLOUDSDK_(?:AUTH|CORE)_[A-Z_]*|CLOUDSDK_CONFIG|GCP_[A-Z_]*(?:KEY|TOKEN|CREDENTIALS?))$")),
    ("cloud", re.compile(r"^(?:AZURE_(?:CLIENT_SECRET|CLIENT_ID|TENANT_ID|SUBSCRIPTION_ID|CLIENT_CERTIFICATE_PATH|USERNAME|PASSWORD|FEDERATED_TOKEN_FILE|CONFIG_DIR|STORAGE_KEY|STORAGE_CONNECTION_STRING)|ARM_(?:CLIENT_SECRET|CLIENT_ID|SUBSCRIPTION_ID|TENANT_ID|ACCESS_KEY|USE_OIDC|OIDC_TOKEN[A-Z_]*))$")),
    ("cloud", re.compile(r"^(?:KUBECONFIG|KUBE_TOKEN|KUBERNETES_SERVICE_ACCOUNT_TOKEN|DIGITALOCEAN_(?:ACCESS_)?TOKEN|DO_API_TOKEN|HCLOUD_TOKEN|LINODE_TOKEN|SCW_SECRET_KEY|OCI_[A-Z_]*KEY[A-Z_]*|ALICLOUD_[A-Z_]*|VULTR_API_KEY)$")),
    ("deploy", re.compile(r"^(?:CLOUDFLARE_(?:API_TOKEN|API_KEY|EMAIL)|CF_API_(?:TOKEN|KEY)|FLY_(?:API_)?(?:ACCESS_)?TOKEN|VERCEL_(?:TOKEN|ORG_ID|PROJECT_ID)|NETLIFY_(?:AUTH_)?TOKEN|RAILWAY_(?:API_)?TOKEN|RENDER_API_KEY|HEROKU_API_KEY|PULUMI_ACCESS_TOKEN|TF_TOKEN_[A-Za-z0-9_]+|TF_CLI_CONFIG_FILE|TF_VAR_[A-Za-z0-9_]*(?i:key|token|secret|password)[A-Za-z0-9_]*|TERRAFORM_TOKEN|SUPABASE_(?:ACCESS_TOKEN|SERVICE_ROLE_KEY|DB_PASSWORD)|NEON_API_KEY|PLANETSCALE_[A-Z_]*TOKEN|FIREBASE_TOKEN|EXPO_TOKEN|SENTRY_AUTH_TOKEN|AMPLIFY_[A-Z_]*TOKEN|SST_[A-Z_]*TOKEN)$")),
    ("docker", re.compile(r"^(?:DOCKER_HOST|DOCKER_AUTH_CONFIG|DOCKER_PASSWORD|DOCKER_TOKEN|REGISTRY_PASSWORD|REGISTRY_TOKEN|GHCR_TOKEN|ECR_PASSWORD|DOCKERHUB_[A-Z_]*)$")),
    ("registry", re.compile(r"^(?:NPM_TOKEN|NODE_AUTH_TOKEN|NPM_CONFIG_[A-Z_]*(?:AUTH|TOKEN|PASSWORD)[A-Z_]*|YARN_NPM_AUTH_(?:TOKEN|IDENT)|PNPM_AUTH_TOKEN|BUN_AUTH_TOKEN|PYPI_(?:API_)?TOKEN|TWINE_(?:USERNAME|PASSWORD)|UV_PUBLISH_(?:TOKEN|PASSWORD|USERNAME)|POETRY_PYPI_TOKEN_[A-Z_]+|POETRY_HTTP_BASIC_[A-Z_]+|CARGO_REGISTRY_TOKEN|CARGO_REGISTRIES_[A-Z_]+_TOKEN|RUBYGEMS_API_KEY|GEM_HOST_API_KEY|NUGET_API_KEY|HEX_API_KEY|COMPOSER_AUTH|MAVEN_[A-Z_]*PASSWORD|OSSRH_[A-Z_]*|GRADLE_PUBLISH_[A-Z_]*)$")),
    ("ssh", re.compile(r"^(?:SSH_AUTH_SOCK|SSH_AGENT_PID|GIT_ASKPASS|SSH_ASKPASS|GIT_SSH|GIT_SSH_COMMAND|GCM_[A-Z_]+|GIT_CREDENTIAL_[A-Z_]+|GIT_TOKEN|GIT_PASSWORD)$")),
    ("netrc", re.compile(r"^NETRC$")),
    ("vault", re.compile(r"^(?:VAULT_TOKEN|VAULT_ADDR_TOKEN|OP_SERVICE_ACCOUNT_TOKEN|OP_CONNECT_TOKEN|OP_SESSION_[A-Za-z0-9_]+|BW_SESSION|BW_CLIENTSECRET|DOPPLER_TOKEN|INFISICAL_TOKEN|BWS_ACCESS_TOKEN)$")),
    ("database", re.compile(r"^(?:PGPASSWORD|PGPASSFILE|PGSSLKEY|MYSQL_PWD|REDISCLI_AUTH|MONGO(?:DB)?_(?:PASSWORD|PASS|PWD)|MSSQL_SA_PASSWORD|SNOWSQL_PWD|SNOWFLAKE_PASSWORD|DB_PASSWORD|DATABASE_PASSWORD)$")),
    ("shell", re.compile(r"^(?:BASH_ENV|ENV)$")),
]  # type: List[Tuple[str, Any]]

_GIT_INJECT_RE = re.compile(r"credential|extraheader|insteadof|sshcommand|askpass", re.I)
_DB_URL_NAME_RE = re.compile(r"(?:^|_)(?:DATABASE|DB|POSTGRES|POSTGRESQL|MYSQL|MONGO|MONGODB|REDIS|AMQP|BROKER)(?:_[A-Z]+)?_(?:URL|URI|DSN)$")
_USERINFO_RE = re.compile(r"://([^/@\s:]*):([^/@\s]+)@([^/\s:?#]+)")
_GENERIC_SECRET_NAME_RE = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|APIKEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIALS?|AUTH)$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
# Model-provider keys the session itself needs; they spend money but do not touch infrastructure.
_SPEND_ONLY_RE = re.compile(r"^(?:ANTHROPIC_[A-Z_]*|CLAUDE_CODE_[A-Z_]*|OPENAI_[A-Z_]*|CLAUDE_[A-Z_]*)$")


def _secret_shaped(value: str) -> List[str]:
    """Kinds of secret found in `value`, ignoring Stripe test keys (dev and test values are allowed locally, §5.4)."""
    kinds = []  # type: List[str]
    for kind, start, end in redact.find_secrets(value):
        if kind == "stripe-key" and "_test_" in value[start:end]:
            continue
        if kind == "url-credentials":
            hosts = [m.group(3) for m in _USERINFO_RE.finditer(value)]
            if hosts and all(_host_is_local(h) for h in hosts):
                continue  # postgres://dev:dev@localhost is a development database
        kinds.append(kind)
    return kinds


def env_name_is_credential(name: str) -> bool:
    """True if an environment variable name is one the doctor treats as a credential."""
    return classify_env_name(name) is not None


def classify_env_name(name: str) -> Optional[str]:
    if name.startswith("AWS_"):
        return None if name in AWS_SAFE else "aws"
    if name.lower().startswith("npm_config_") and re.search(r"auth|token|password", name, re.I):
        return "registry"
    for cat, rx in ENV_RULES:
        if rx.match(name):
            return cat
    return None


def _host_is_local(host: str) -> bool:
    return host.lower() in _LOCAL_HOSTS or host.lower().endswith(".localhost") or host.lower().endswith(".test")


@gatherer("env_creds")
def gather_env(ctx: Context) -> List[Dict[str, str]]:
    hits = []  # type: List[Dict[str, str]]
    seen = set()  # type: set
    for name in sorted(ctx.env):
        value = ctx.env[name]
        if value == "":
            continue
        cat = classify_env_name(name)
        if cat == "shell" and not os.path.isfile(_expand(ctx, value)):
            continue  # ENV=production is not a startup file
        if cat == "ssh" and name in ("GIT_SSH", "GIT_SSH_COMMAND") and not re.search(r"\s-i\s|identityfile|\.pem|id_", value, re.I):
            cat = None  # a plain ssh wrapper without a key is not a credential
        if cat is None and name == "CLAUDECODE":
            continue
        if cat is not None:
            hits.append({"id": "env", "category": cat, "what": name, "where": "environment variable"})
            seen.add(name)
            continue
        if name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS") or re.match(r"^GIT_CONFIG_(?:KEY|VALUE)_\d+$", name):
            if _GIT_INJECT_RE.search(value) or (name == "GIT_CONFIG_PARAMETERS"):
                hits.append({"id": "env", "category": "ssh", "what": name,
                             "where": "environment variable injects git config (credential helper, extra header or key)"})
            continue
        if _DB_URL_NAME_RE.search(name):
            m = _USERINFO_RE.search(value)
            if m and not _host_is_local(m.group(3)):
                hits.append({"id": "env", "category": "database", "what": name,
                             "where": "environment variable holds a database URL with credentials for host %s" % m.group(3)})
                continue
        if re.match(r"^(?:PIP_(?:EXTRA_)?INDEX_URL|UV_(?:EXTRA_)?INDEX_URL|POETRY_REPOSITORIES_[A-Z_]+_URL)$", name) and _USERINFO_RE.search(value):
            hits.append({"id": "env", "category": "registry", "what": name, "where": "environment variable holds an index URL with credentials"})
            continue
        if _secret_shaped(value) and not _SPEND_ONLY_RE.match(name):
            hits.append({"id": "env", "category": "secret-shaped", "what": name, "where": "environment variable value looks like a secret"})
            continue
        if _GENERIC_SECRET_NAME_RE.search(name) and not _SPEND_ONLY_RE.match(name) and name not in seen:
            hits.append({"id": "env", "category": "generic", "what": name, "where": "environment variable name suggests a secret"})
    return hits


_ENV_ID = {"aws": "cred.env.aws", "github": "cred.env.github", "cloud": "cred.env.cloud", "deploy": "cred.env.deploy",
           "docker": "cred.env.docker", "registry": "cred.env.registry", "ssh": "cred.env.ssh", "netrc": "cred.env.netrc",
           "vault": "cred.env.vault", "database": "cred.env.database", "shell": "cred.env.shell", "secret-shaped": "cred.env.secret-shaped",
           "generic": "cred.env.generic"}
_ENV_REMEDIATION = {
    "aws": "unset it for the agent's session (strip AWS_* from the driver's child env, report §8.3); mutating cloud credentials live only in CI via OIDC",
    "github": "unset it; the agent acts as the bot only (B1). A human token in the environment lets the agent push or merge as you",
    "cloud": "unset it; no cloud credential, not even read-only, is reachable by the agent (DEC-6)",
    "deploy": "unset it; deploy-class tokens belong to CI behind an Environment",
    "docker": "unset it; a registry or remote daemon credential is a deploy path",
    "registry": "unset it; a publish token lets the agent ship a package (supply chain, H5)",
    "ssh": "unset it; an SSH agent or key GitHub accepts lets the agent push as you",
    "netrc": "unset it and remove the file it names",
    "vault": "unset it; a password-manager session or service token unlocks every secret it guards",
    "shell": "unset it, or make sure the file it names holds no credentials: it runs in every shell the agent starts",
    "database": "use a local database for the loop (durable_db_scope: local); shared and production database credentials are never present locally",
    "secret-shaped": "unset it or rotate it: the value matches a known secret format",
    "generic": "check what it grants; unset it unless it is a dev or test value",
}


@check("cred.env", "credentials", emits=("cred.env.aws", "cred.env.github", "cred.env.cloud", "cred.env.deploy", "cred.env.docker", "cred.env.registry", "cred.env.ssh", "cred.env.netrc", "cred.env.vault", "cred.env.database", "cred.env.shell", "cred.env.secret-shaped", "cred.env.generic", "cred.env.other"))
def check_env(ctx: Context) -> List[Result]:
    hits = ctx.fact("env_creds")
    bot_ok = _bot_login(ctx)
    out = []  # type: List[Result]
    by_cat = {}  # type: Dict[str, List[Dict[str, str]]]
    for h in hits:
        by_cat.setdefault(h["category"], []).append(h)
    for cat in sorted(by_cat):
        names = ", ".join(h["what"] for h in by_cat[cat])
        sev = WARN if cat in ("generic", "shell") else ERROR
        out.append(failed(_ENV_ID.get(cat, "cred.env.other"), "%s: %s" % (by_cat[cat][0]["where"] if len(by_cat[cat]) == 1 else "environment variables set", names),
                          _ENV_REMEDIATION.get(cat, ""), severity=sev,
                          detail={"names": [h["what"] for h in by_cat[cat]]}))
    if not out:
        out.append(passed("cred.env", "no credential-bearing environment variables are set"))
    return out


# --- file-based credentials ---------------------------------------------------------------------------

def _read(path: str, limit: int = 1 << 20) -> Optional[str]:
    return safeio.read_text(path, limit)


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _expand(ctx: Context, path: str) -> str:
    """`~` and `~/x` mean the HOME this doctor was told about, not the process's own."""
    if path == "~":
        return ctx.home
    if path.startswith("~/"):
        return ctx.home_path(path[2:])
    return path


def _tilde(ctx: Context, path: str) -> str:
    if path.startswith(ctx.home + os.sep):
        return "~" + path[len(ctx.home):]
    return path


def _xdg_config(ctx: Context) -> str:
    return ctx.getenv("XDG_CONFIG_HOME") or ctx.home_path(".config")


def _bot_login(ctx: Context) -> str:
    v = str(ctx.cfg("bot.login", "") or "").strip().lower()
    return v[:-5] if v.endswith("[bot]") else v


# --- AWS ----------------------------------------------------------------------------------------------

_AWS_KEY_KINDS = {
    "aws_access_key_id": "static access key", "aws_secret_access_key": "static secret key", "aws_session_token": "session token",
    "credential_process": "credential_process (runs a program that prints credentials)", "sso_session": "SSO session",
    "sso_start_url": "SSO login", "sso_account_id": "SSO account", "sso_role_name": "SSO role", "role_arn": "assumable role",
    "web_identity_token_file": "web identity token file", "credential_source": "credential_source (instance or container metadata)",
    "source_profile": "source_profile chain", "mfa_serial": "MFA-gated profile",
}


def parse_aws_ini(text: str) -> Dict[str, Dict[str, str]]:
    """Section -> {key: value}. Tolerant: indented sub-keys (s3 settings) are folded in flat."""
    out = {}  # type: Dict[str, Dict[str, str]]
    cur = None  # type: Optional[str]
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            name = m.group(1).strip()
            if name.startswith("profile "):
                name = name[8:].strip()
            if name.startswith("sso-session "):
                cur = None
                continue
            cur = name
            out.setdefault(cur, {})
            continue
        if cur is not None and "=" in line:
            k, _, v = line.partition("=")
            out[cur][k.strip().lower()] = v.strip()
    return out


@gatherer("aws_files")
def gather_aws(ctx: Context) -> Dict[str, Any]:
    cred_path = ctx.getenv("AWS_SHARED_CREDENTIALS_FILE") or ctx.home_path(".aws", "credentials")
    conf_path = ctx.getenv("AWS_CONFIG_FILE") or ctx.home_path(".aws", "config")
    profiles = {}  # type: Dict[str, Dict[str, Any]]
    files = []  # type: List[str]
    for path in (cred_path, conf_path):
        text = _read(path)
        if text is None:
            continue
        files.append(_tilde(ctx, path))
        for name, kv in parse_aws_ini(text).items():
            kinds = sorted(_AWS_KEY_KINDS[k] for k in kv if k in _AWS_KEY_KINDS and kv[k] != "")
            if kinds:
                p = profiles.setdefault(name, {"name": name, "kinds": [], "files": []})
                p["kinds"] = sorted(set(p["kinds"]) | set(kinds))
                p["files"].append(_tilde(ctx, path))
    cache_hits = []  # type: List[Dict[str, Any]]
    now = ctx.now
    for pattern in (ctx.home_path(".aws", "sso", "cache", "*.json"), ctx.home_path(".aws", "cli", "cache", "*.json"),
                    ctx.home_path(".aws", "boto", "cache", "*.json"), ctx.home_path(".aws", "login", "cache", "*.json")):
        for path in sorted(glob.glob(pattern)):
            text = _read(path) or ""
            live_token = bool(re.search(r'"(?:accessToken|AccessKeyId|SecretAccessKey|SessionToken)"', text))
            refresh = bool(re.search(r'"(?:refreshToken|clientSecret)"', text))
            if not live_token and not refresh:
                continue
            expired = _expired(text, now)
            cache_hits.append({"path": _tilde(ctx, path), "expired": expired and not refresh, "refresh": refresh})
    return {"profiles": sorted(profiles.values(), key=lambda p: p["name"]), "files": files, "cache": cache_hits}


def _expired(text: str, now: float) -> bool:
    from kitlib import runstate
    m = re.search(r'"(?:expiresAt|Expiration)"\s*:\s*"([^"]+)"', text)
    if not m:
        return False
    s = m.group(1).replace("+00:00", "Z")
    s = re.sub(r"\.\d+", "", s)
    if not s.endswith("Z"):
        s = s + "Z"
    ts = runstate.parse_iso(s)
    return ts is not None and ts < now


@check("cred.aws.profile", "credentials")
def check_aws_profiles(ctx: Context) -> List[Result]:
    f = ctx.fact("aws_files")
    out = []  # type: List[Result]
    for p in f["profiles"]:
        out.append(failed("cred.aws.profile", "AWS profile %r in %s: %s" % (p["name"], ", ".join(sorted(set(p["files"]))), ", ".join(p["kinds"])),
                          "remove the profile or run the agent as another OS user or in a credential-free container; read-only cloud access is not exempt (DEC-6)",
                          detail={"profile": p["name"]}))
    if not f["profiles"]:
        out.append(passed("cred.aws.profile", "no AWS profile carries credentials, SSO, a role or a credential_process"))
    return out


@check("cred.aws.sso-cache", "credentials")
def check_aws_cache(ctx: Context) -> List[Result]:
    f = ctx.fact("aws_files")
    out = []  # type: List[Result]
    for c in f["cache"]:
        if c["expired"]:
            out.append(failed("cred.aws.sso-cache", "%s holds an expired AWS token" % c["path"], "delete it", severity=WARN))
        else:
            out.append(failed("cred.aws.sso-cache", "%s holds a usable AWS %s" % (c["path"], "refresh token or client secret" if c["refresh"] else "token"),
                              "run `aws sso logout` and delete the cache; the agent must not be able to use it"))
    if not f["cache"]:
        out.append(passed("cred.aws.sso-cache", "no AWS SSO or CLI credential cache"))
    return out


# --- GitHub CLI, git, SSH, netrc ---------------------------------------------------------------------------

def gh_config_dirs(ctx: Context) -> List[str]:
    dirs = []  # type: List[str]
    for d in (ctx.getenv("GH_CONFIG_DIR"), os.path.join(ctx.getenv("XDG_CONFIG_HOME"), "gh") if ctx.getenv("XDG_CONFIG_HOME") else "",
              ctx.home_path(".config", "gh")):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def parse_gh_hosts(text: str) -> Dict[str, Dict[str, Any]]:
    """host -> {users: [...], has_token: bool}. Line-based so a slightly odd file still parses."""
    hosts = {}  # type: Dict[str, Dict[str, Any]]
    cur = None  # type: Optional[str]
    in_users = False
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0 and line.endswith(":"):
            cur = line[:-1].strip().strip('"')
            hosts[cur] = {"users": [], "has_token": False}
            in_users = False
            continue
        if cur is None:
            continue
        m = re.match(r"^user:\s*(\S+)", line)
        if m and indent <= 8:
            hosts[cur]["users"].append(m.group(1).strip("\"'"))
        if re.match(r"^oauth_token:\s*\S+", line):
            hosts[cur]["has_token"] = True
        if line == "users:":
            in_users = True
            continue
        if in_users and indent >= 8 and line.endswith(":") and not line.startswith(("oauth_token", "git_protocol")):
            hosts[cur]["users"].append(line[:-1].strip("\"'"))
    for h in hosts.values():
        h["users"] = sorted(set(h["users"]))
    return hosts


@gatherer("gh_hosts")
def gather_gh_hosts(ctx: Context) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for d in gh_config_dirs(ctx):
        path = os.path.join(d, "hosts.yml")
        text = _read(path)
        if text is None:
            continue
        for host, info in parse_gh_hosts(text).items():
            out.append({"path": _tilde(ctx, path), "host": host, "users": info["users"], "has_token": info["has_token"]})
    return out


@check("cred.gh.hosts", "credentials")
def check_gh_hosts(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    bot = _bot_login(ctx)
    for h in ctx.fact("gh_hosts"):
        users = [u.lower().replace("[bot]", "") for u in h["users"]]
        if bot and users and all(u == bot for u in users):
            out.append(passed("cred.gh.hosts", "%s: %s is logged in as the bot %r" % (h["path"], h["host"], bot)))
            continue
        who = ", ".join(h["users"]) or "an account"
        out.append(failed("cred.gh.hosts", "%s: gh is logged in to %s as %s%s" % (h["path"], h["host"], who, " (token stored in the file)" if h["has_token"] else " (token in the OS keychain)"),
                          "run `gh auth logout` for the agent's OS user, or run the agent where this file does not exist; only the bot's credential may be reachable (RC-5)"))
    if not ctx.fact("gh_hosts"):
        out.append(passed("cred.gh.hosts", "no gh hosts.yml login found"))
    return out


@gatherer("git_config")
def gather_git_config(ctx: Context) -> Dict[str, Any]:
    """Every git config entry with its origin, via `git config --list --show-origin -z`."""
    rc, out, err = ctx.git(["config", "--list", "--show-origin", "-z"], harden=False)
    if rc != 0:
        # outside a repo `git config --list` still works for global/system; any other failure is unknown
        return {"ok": False, "error": (err or "git config failed").strip().split("\n")[0], "entries": []}
    entries = []  # type: List[Dict[str, str]]
    parts = out.split("\0")
    i = 0
    while i + 1 < len(parts):
        origin = parts[i]
        kv = parts[i + 1]
        i += 2
        key, _, value = kv.partition("\n")
        entries.append({"origin": origin, "key": key, "value": value})
    return {"ok": True, "error": "", "entries": entries}


def git_origin_level(origin: str, ctx: Context) -> str:
    path = origin.split(":", 1)[1] if ":" in origin else origin
    if origin.startswith("command line"):
        return "command line"
    if not os.path.isabs(path):
        path = os.path.join(ctx.repo, path)
    if "config.worktree" in path:
        return "worktree"
    if os.sep + ".git" + os.sep in path or path.endswith(os.sep + ".git" + os.sep + "config") or path.startswith(ctx.repo + os.sep):
        return "local"
    if path.startswith(ctx.home + os.sep) or path.startswith(os.path.dirname(ctx.home) + os.sep):
        return "global"
    if ctx.getenv("GIT_CONFIG_GLOBAL") and os.path.abspath(path) == os.path.abspath(ctx.getenv("GIT_CONFIG_GLOBAL")):
        return "global"
    return "system"


def userinfo_secret(value: str) -> bool:
    m = _USERINFO_RE.search(value)
    if m:
        return True
    m2 = re.search(r"://([^/@\s:]+)@", value)
    if m2 and m2.group(1) not in ("git", "ssh", "hg"):
        return bool(redact.find_secrets(m2.group(1))) or len(m2.group(1)) >= 20
    return False


def judge_git_config(entries: List[Dict[str, str]], ctx: Context) -> List[Dict[str, str]]:
    hits = []  # type: List[Dict[str, str]]
    for e in entries:
        k, v = e["key"].lower(), e["value"]
        level = git_origin_level(e["origin"], ctx)
        where = "%s git config (%s)" % (level, _tilde(ctx, e["origin"].split(":", 1)[-1]))
        if re.match(r"^credential(\..*)?\.helper$", k):
            if v.strip():
                hits.append({"category": "git-helper", "what": "%s = %s" % (e["key"], v.strip().split()[0][:40]), "where": where})
        elif re.match(r"^http(\..*)?\.extraheader$", k):
            if re.search(r"authorization|bearer|basic|token", v, re.I):
                hits.append({"category": "git-header", "what": e["key"], "where": where + " carries an Authorization header (actions/checkout leaves one behind)"})
        elif re.match(r"^url\..*\.(insteadof|pushinsteadof)$", k):
            if userinfo_secret(k) or userinfo_secret(v):
                hits.append({"category": "git-url", "what": "url rewrite with an embedded credential", "where": where})
        elif re.match(r"^remote\..*\.(url|pushurl)$", k):
            if userinfo_secret(v):
                hits.append({"category": "git-url", "what": "remote %s embeds a credential" % k.split(".")[1], "where": where})
        elif k == "core.sshcommand":
            if re.search(r"\s-i\s|identityfile", v, re.I):
                hits.append({"category": "ssh-key", "what": "core.sshCommand names an identity file", "where": where})
        elif k == "core.askpass":
            if v.strip():
                hits.append({"category": "git-helper", "what": "core.askPass = %s" % v.strip().split()[0][:40], "where": where})
    return hits


@check("cred.git.config", "credentials", emits=("cred.git.credential-helper", "cred.git.extraheader", "cred.git.url-credential", "cred.git.ssh-command"))
def check_git_config(ctx: Context) -> List[Result]:
    g = ctx.fact("git_config")
    if not g["ok"]:
        return [unknown("cred.git.config", "could not read git config: %s" % g["error"], "install git or fix the repository", severity=WARN)]
    hits = judge_git_config(g["entries"], ctx)
    out = []  # type: List[Result]
    for h in hits:
        rid = {"git-helper": "cred.git.credential-helper", "git-header": "cred.git.extraheader", "git-url": "cred.git.url-credential",
               "ssh-key": "cred.git.ssh-command"}[h["category"]]
        rem = {"git-helper": "remove the helper for the agent's OS user (it may hand out your token), or run `doctor --live` to prove it returns only the bot (RC-5)",
               "git-header": "run `git config --unset-all <key>`; pass persist-credentials: false to actions/checkout",
               "git-url": "remove the rewrite and rotate the token it names",
               "ssh-key": "remove it; the agent must not hold a key GitHub accepts"}[h["category"]]
        out.append(failed(rid, "%s: %s" % (h["where"], h["what"]), rem))
    if not hits:
        out.append(passed("cred.git.config", "git config has no credential helper, Authorization header, embedded URL credential or SSH key at any level"))
    return out


@gatherer("git_credential_files")
def gather_git_credential_files(ctx: Context) -> List[Dict[str, str]]:
    out = []  # type: List[Dict[str, str]]
    for path in (ctx.home_path(".git-credentials"), os.path.join(_xdg_config(ctx), "git", "credentials")):
        text = _read(path)
        if text and "://" in text:
            out.append({"path": _tilde(ctx, path), "category": "git-credentials", "what": "plain-text git credential store"})
    return out


def parse_netrc(text: str) -> List[Dict[str, Any]]:
    toks = re.findall(r'"[^"]*"|\S+', re.sub(r"(?m)^\s*#.*$", "", text))
    entries = []  # type: List[Dict[str, Any]]
    cur = None  # type: Optional[Dict[str, Any]]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("machine", "default"):
            cur = {"machine": toks[i + 1] if t == "machine" and i + 1 < len(toks) else "default", "login": False, "password": False}
            entries.append(cur)
            i += 2 if t == "machine" else 1
            continue
        if cur is not None and t in ("login", "password", "account") and i + 1 < len(toks):
            if t in ("login", "password"):
                cur[t] = True
            i += 2
            continue
        i += 1
    return entries


@gatherer("netrc")
def gather_netrc(ctx: Context) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    paths = [ctx.getenv("NETRC"), ctx.home_path(".netrc"), ctx.home_path("_netrc")]
    for path in [p for p in paths if p]:
        text = _read(path)
        if text is None:
            continue
        for e in parse_netrc(text):
            if e["login"] or e["password"]:
                out.append({"path": _tilde(ctx, path), "machine": e["machine"], "password": e["password"]})
    return out


@check("cred.git.credentials-file", "credentials")
def check_git_credentials_file(ctx: Context) -> List[Result]:
    out = [failed("cred.git.credentials-file", "%s: %s" % (h["path"], h["what"]), "delete it; rotate the token inside") for h in ctx.fact("git_credential_files")]
    return out or [passed("cred.git.credentials-file", "no ~/.git-credentials store")]


@check("cred.netrc", "credentials")
def check_netrc(ctx: Context) -> List[Result]:
    out = [failed("cred.netrc", "%s has a login for %s%s" % (e["path"], e["machine"], " with a password" if e["password"] else ""),
                  "remove the entry; curl, git and many CLIs read .netrc without being asked") for e in ctx.fact("netrc")]
    return out or [passed("cred.netrc", "no .netrc logins")]


def _ssh_private_key(path: str) -> Optional[str]:
    """'unencrypted', 'encrypted' or None if the file is not a private key."""
    text = _read(path, 16384)
    if not text or "PRIVATE KEY" not in text[:200]:
        return None
    if "ENCRYPTED" in text.split("\n", 3)[0] or re.search(r"Proc-Type:\s*4,ENCRYPTED", text):
        return "encrypted"
    if "BEGIN OPENSSH PRIVATE KEY" in text:
        import base64
        body = "".join(l for l in text.split("\n") if l and not l.startswith("-----"))
        try:
            raw = base64.b64decode(body + "=" * (-len(body) % 4))
        except ValueError:
            return "unencrypted"
        magic = b"openssh-key-v1\x00"
        if raw.startswith(magic) and len(raw) > len(magic) + 4:
            n = int.from_bytes(raw[len(magic):len(magic) + 4], "big")
            cipher = raw[len(magic) + 4:len(magic) + 4 + n]
            return "unencrypted" if cipher == b"none" else "encrypted"
    return "unencrypted"


@gatherer("ssh")
def gather_ssh(ctx: Context) -> Dict[str, Any]:
    keys = []  # type: List[Dict[str, str]]
    d = ctx.home_path(".ssh")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for n in names:
        p = os.path.join(d, n)
        if not os.path.isfile(p) or n.endswith(".pub") or n in ("known_hosts", "config", "authorized_keys"):
            continue
        kind = _ssh_private_key(p)
        if kind:
            keys.append({"path": _tilde(ctx, p), "state": kind})
    sock_candidates = [ctx.getenv("SSH_AUTH_SOCK"), ctx.home_path(".1password", "agent.sock"),
                       ctx.home_path("Library", "Group Containers", "2BUA8C4S2C.com.1password", "t", "agent.sock"),
                       ctx.home_path(".gnupg", "S.gpg-agent.ssh"), ctx.home_path(".ssh", "agent.sock")]
    xrd = ctx.getenv("XDG_RUNTIME_DIR")
    if xrd:
        sock_candidates += [os.path.join(xrd, "ssh-agent.socket"), os.path.join(xrd, "gcr", "ssh"), os.path.join(xrd, "keyring", "ssh")]
    socks = []  # type: List[Dict[str, Any]]
    for s in sock_candidates:
        if not s:
            continue
        exists = os.path.exists(s)
        is_sock = exists and stat.S_ISSOCK(os.stat(s).st_mode)
        from_env = s == ctx.getenv("SSH_AUTH_SOCK")
        if from_env or is_sock:
            socks.append({"path": _tilde(ctx, s), "exists": exists, "from_env": from_env})
    for g in glob.glob(ctx.host_path("/private/tmp/com.apple.launchd.*/Listeners")) + glob.glob(ctx.host_path("/tmp/ssh-*/agent.*")):
        socks.append({"path": g, "exists": True, "from_env": False})
    cfg = _read(ctx.home_path(".ssh", "config")) or ""
    identity_agent = bool(re.search(r"(?mi)^\s*IdentityAgent\s+\S+", cfg))
    return {"keys": keys, "sockets": socks, "identity_agent": identity_agent}


@check("cred.ssh.agent", "credentials")
def check_ssh_agent(ctx: Context) -> List[Result]:
    f = ctx.fact("ssh")
    out = []  # type: List[Result]
    for s in f["sockets"]:
        why = "SSH_AUTH_SOCK is set" if s["from_env"] else "an agent socket exists"
        out.append(failed("cred.ssh.agent", "%s (%s%s): the agent can push as whoever's keys it holds" % (s["path"], why, "" if s["exists"] else ", socket missing"),
                          "unset SSH_AUTH_SOCK for the agent and do not forward an agent into its container (RC-5)"))
    if f["identity_agent"]:
        out.append(failed("cred.ssh.agent", "~/.ssh/config sets IdentityAgent (a password-manager or forwarded agent)", "remove it for the agent's OS user", severity=WARN))
    return out or [passed("cred.ssh.agent", "SSH_AUTH_SOCK is unset and no agent socket was found")]


@check("cred.ssh.keys", "credentials")
def check_ssh_keys(ctx: Context) -> List[Result]:
    f = ctx.fact("ssh")
    out = []  # type: List[Result]
    for k in f["keys"]:
        if k["state"] == "unencrypted":
            out.append(failed("cred.ssh.keys", "%s is an unencrypted private key: usable by any process of this user" % k["path"],
                              "move it out of the agent's reach; use a deploy key or the bot's key only"))
        else:
            out.append(failed("cred.ssh.keys", "%s is a passphrase-protected private key (usable if an agent or the passphrase is reachable)" % k["path"],
                              "move it out of the agent's reach", severity=WARN))
    return out or [passed("cred.ssh.keys", "no private keys in ~/.ssh")]


# --- container sockets, kube, other clouds -----------------------------------------------------------

@gatherer("docker_sockets")
def gather_docker(ctx: Context) -> List[Dict[str, str]]:
    cands = ["/var/run/docker.sock", "/run/docker.sock", "/var/run/podman/podman.sock", "/run/podman/podman.sock",
             "/var/run/containerd/containerd.sock", "/run/containerd/containerd.sock"]
    cands = [ctx.host_path(c) for c in cands]
    cands += [ctx.home_path(".docker", "run", "docker.sock"), ctx.home_path(".colima", "default", "docker.sock"),
              ctx.home_path(".colima", "docker.sock"), ctx.home_path(".rd", "docker.sock"),
              ctx.home_path(".orbstack", "run", "docker.sock"), ctx.home_path(".lima", "docker", "sock", "docker.sock"),
              ctx.home_path(".local", "share", "containers", "podman", "machine", "podman.sock")]
    xrd = ctx.getenv("XDG_RUNTIME_DIR")
    if xrd:
        cands += [os.path.join(xrd, "docker.sock"), os.path.join(xrd, "podman", "podman.sock")]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    cands += [ctx.host_path("/run/user/%d/docker.sock" % uid), ctx.host_path("/run/user/%d/podman/podman.sock" % uid)]
    cands += glob.glob(ctx.home_path(".local", "share", "containers", "podman", "machine", "*", "podman.sock"))
    dh = ctx.getenv("DOCKER_HOST")
    if dh.startswith("unix://"):
        cands.append(dh[len("unix://"):])
    out = []  # type: List[Dict[str, str]]
    seen = set()  # type: set
    for c in cands:
        try:
            st = os.stat(c)
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode):
            real = os.path.realpath(c)
            if real in seen:
                continue
            seen.add(real)
            out.append({"path": _tilde(ctx, c), "access": "rw" if os.access(c, os.W_OK) else "no write access"})
    return out


@check("cred.docker.socket", "credentials")
def check_docker_socket(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    for s in ctx.fact("docker_sockets"):
        if s["access"] == "rw":
            out.append(failed("cred.docker.socket", "%s is reachable: a container with the host mounted is root on the machine" % s["path"],
                              "run the agent where no container socket exists, or in a rootless sandbox; do not add docker to sandbox excludedCommands"))
        else:
            out.append(failed("cred.docker.socket", "%s exists but is not writable by this user" % s["path"], "keep it that way", severity=WARN))
    dh = ctx.getenv("DOCKER_HOST")
    if dh and not dh.startswith("unix://"):
        out.append(failed("cred.docker.socket", "DOCKER_HOST points at a remote daemon (%s)" % re.sub(r"//[^@/]*@", "//", dh), "unset it"))
    return out or [passed("cred.docker.socket", "no container daemon socket is reachable")]


@gatherer("docker_config")
def gather_docker_config(ctx: Context) -> List[Dict[str, str]]:
    import json
    out = []  # type: List[Dict[str, str]]
    d = ctx.getenv("DOCKER_CONFIG") or ctx.home_path(".docker")
    text = _read(os.path.join(d, "config.json"))
    if not text:
        return out
    try:
        data = json.loads(text)
    except ValueError:
        return [{"path": _tilde(ctx, os.path.join(d, "config.json")), "what": "unparsable docker config (may hold credentials)"}]
    p = _tilde(ctx, os.path.join(d, "config.json"))
    for reg, v in (data.get("auths") or {}).items():
        if isinstance(v, dict) and (v.get("auth") or v.get("identitytoken") or v.get("registrytoken")):
            out.append({"path": p, "what": "registry login for %s" % reg})
    if data.get("credsStore"):
        out.append({"path": p, "what": "credsStore %s hands out registry credentials" % data["credsStore"]})
    for reg, helper in (data.get("credHelpers") or {}).items():
        out.append({"path": p, "what": "credential helper %s for %s" % (helper, reg)})
    return out


@check("cred.docker.config", "credentials")
def check_docker_config(ctx: Context) -> List[Result]:
    out = [failed("cred.docker.config", "%s: %s" % (h["path"], h["what"]), "run `docker logout` for the agent's OS user; a registry push is a deploy path")
           for h in ctx.fact("docker_config")]
    return out or [passed("cred.docker.config", "no docker registry credentials")]


def parse_kube_contexts(text: str) -> List[str]:
    names = []  # type: List[str]
    in_ctx = False
    base = None  # type: Optional[int]
    for raw in text.split("\n"):
        if re.match(r"^contexts:\s*$", raw):
            in_ctx, base = True, None
            continue
        if in_ctx and re.match(r"^[A-Za-z]", raw):
            in_ctx = False
        if in_ctx:
            m = re.match(r"^(\s*)- ", raw)
            if m and base is None:
                base = len(m.group(1))
            m2 = re.match(r"^(\s*)(?:- )?name:\s*(\S+)", raw)
            if m2 and base is not None and len(m2.group(1)) in (base, base + 2):
                names.append(m2.group(2).strip("\"'"))
    return names


@gatherer("kube")
def gather_kube(ctx: Context) -> List[Dict[str, Any]]:
    paths = [p for p in ctx.getenv("KUBECONFIG").split(os.pathsep) if p] + [ctx.home_path(".kube", "config")]
    out = []  # type: List[Dict[str, Any]]
    seen = set()  # type: set
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        text = _read(p)
        if text is None:
            continue
        names = parse_kube_contexts(text)
        has_cred = bool(re.search(r"(?m)^\s*(token|client-key-data|client-key|password|exec|auth-provider|tokenFile):", text))
        if names or has_cred:
            out.append({"path": _tilde(ctx, p), "contexts": names, "has_credentials": has_cred})
    sa = ctx.host_path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    if os.path.exists(sa):
        out.append({"path": sa, "contexts": ["in-cluster service account"], "has_credentials": True})
    return out


@check("cred.kube", "credentials")
def check_kube(ctx: Context) -> List[Result]:
    out = [failed("cred.kube", "%s: kube contexts %s%s" % (k["path"], ", ".join(k["contexts"]) or "(none listed)", " with credentials" if k["has_credentials"] else ""),
                  "remove the contexts for the agent's OS user; cluster changes go through CI (kubectl diff with a read-only kubeconfig there)")
           for k in ctx.fact("kube")]
    return out or [passed("cred.kube", "no kube contexts")]


def _first_existing(paths: Iterable[str]) -> List[str]:
    return [p for p in paths if os.path.exists(p)]


# (check suffix, human label, relative-to-home globs, content regex that proves a credential or None = any non-empty file)
HOME_CREDENTIAL_FILES = [
    ("gcloud", "gcloud credentials", [".config/gcloud/application_default_credentials.json", ".config/gcloud/credentials.db",
                                      ".config/gcloud/access_tokens.db", ".config/gcloud/legacy_credentials/*/adc.json"], None),
    ("azure", "Azure CLI tokens or subscriptions", [".azure/msal_token_cache.json", ".azure/msal_token_cache.bin", ".azure/accessTokens.json",
                                                    ".azure/service_principal_entries.json", ".azure/service_principal_entries.bin"], None),
    ("terraform", "Terraform Cloud credentials", [".terraform.d/credentials.tfrc.json", ".terraformrc", "terraform.rc"], r"token|credentials"),
    ("vault", "Vault token", [".vault-token"], None),
    ("fly", "Fly.io login", [".fly/config.yml", ".config/fly/config.yml", ".fly/config.yaml"], r"access_token|token"),
    ("vercel", "Vercel login", [".vercel/auth.json", ".local/share/com.vercel.cli/auth.json", "Library/Application Support/com.vercel.cli/auth.json"], r"token"),
    ("netlify", "Netlify login", [".netlify/config.json", ".config/netlify/config.json", "Library/Preferences/netlify/config.json"], r"token|auth"),
    ("cloudflare", "Wrangler login", [".wrangler/config/default.toml", ".config/.wrangler/config/default.toml",
                                      "Library/Preferences/.wrangler/config/default.toml", ".config/wrangler/config/default.toml"], r"token|key"),
    ("railway", "Railway login", [".railway/config.json"], r"token"),
    ("doctl", "DigitalOcean login", [".config/doctl/config.yaml"], r"access-token|token"),
    ("pulumi", "Pulumi login", [".pulumi/credentials.json"], r"accessTokens|token"),
    ("firebase", "Firebase login", [".config/configstore/firebase-tools.json"], r"refresh_token|tokens"),
    ("supabase", "Supabase login", [".supabase/access-token"], None),
    ("stripe", "Stripe CLI login", [".config/stripe/config.toml"], r"api_key|secret"),
    ("heroku", "Heroku login", [".local/share/heroku/credentials", ".heroku/credentials"], None),
    ("glab", "GitLab CLI login", [".config/glab-cli/config.yml"], r"token"),
    ("hub", "hub login", [".config/hub"], r"oauth_token"),
    ("helm", "Helm repository credentials", [".config/helm/repositories.yaml"], r"password|passCredentialsAll"),
    ("npm", "npm registry token", [".npmrc"], r"_authToken\s*=\s*(?!\$\{)\S|_auth\s*=\s*(?!\$\{)\S|_password\s*=\s*(?!\$\{)\S"),
    ("yarn", "Yarn registry token", [".yarnrc.yml", ".yarnrc"], r"npmAuthToken\s*:\s*(?!\"?\$\{)\S|npmAuthIdent|_authToken"),
    ("bun", "Bun registry token", [".bunfig.toml", ".bun/bunfig.toml"], r"token\s*=\s*\"(?!\$)"),
    ("pypi", "PyPI upload credentials", [".pypirc"], r"password\s*[:=]\s*\S"),
    ("pip", "pip index with credentials", [".pip/pip.conf", ".config/pip/pip.conf", "pip/pip.ini"], r"://[^/\s:@]+:[^/\s@]+@"),
    ("poetry", "Poetry credentials", [".config/pypoetry/auth.toml", "Library/Application Support/pypoetry/auth.toml"], r"password|token"),
    ("cargo", "crates.io token", [".cargo/credentials.toml", ".cargo/credentials"], r"token"),
    ("gem", "RubyGems key", [".gem/credentials"], r":rubygems_api_key|api_key|:\w+:"),
    ("nuget", "NuGet credentials", [".nuget/NuGet/NuGet.Config"], r"ClearTextPassword|packageSourceCredentials|ApiKey"),
    ("maven", "Maven server passwords", [".m2/settings.xml"], r"<password>\s*(?!\$\{)[^<\s]"),
    ("gradle", "Gradle credentials", [".gradle/gradle.properties"], r"(?i)(password|token|secret|apikey)\s*="),
    ("composer", "Composer auth", [".composer/auth.json", ".config/composer/auth.json"], r"token|password"),
    ("pgpass", "PostgreSQL password file", [".pgpass"], r":"),
    ("mysql", "MySQL client credentials", [".my.cnf", ".mylogin.cnf"], r"(?i)password"),
    ("pgservice", "PostgreSQL service file", [".pg_service.conf"], r"(?i)password"),
    ("pass", "pass password store", [".password-store/*.gpg", ".password-store/*/*.gpg"], None),
    ("op", "1Password CLI config", [".config/op/config", ".op/config"], r"(?i)account|url"),
    ("bitwarden", "Bitwarden CLI data", [".config/Bitwarden CLI/data.json", "Library/Application Support/Bitwarden CLI/data.json"], r"(?i)token|access"),
]  # type: List[Tuple[str, str, List[str], Optional[str]]]


@gatherer("home_credential_files")
def gather_home_files(ctx: Context) -> List[Dict[str, str]]:
    hits = []  # type: List[Dict[str, str]]
    az = ctx.getenv("AZURE_CONFIG_DIR")
    gc = ctx.getenv("CLOUDSDK_CONFIG")
    for suffix, label, globs, rx in HOME_CREDENTIAL_FILES:
        patterns = [ctx.home_path(*g.split("/")) for g in globs]
        if suffix == "azure" and az:
            patterns += [os.path.join(az, "msal_token_cache.json"), os.path.join(az, "accessTokens.json")]
        if suffix == "gcloud" and gc:
            patterns += [os.path.join(gc, "application_default_credentials.json"), os.path.join(gc, "credentials.db")]
        for pat in patterns:
            for path in sorted(glob.glob(pat)):
                if not os.path.isfile(path) or not _nonempty(path):
                    continue
                if rx is not None:
                    text = _read(path) or ""
                    if not re.search(rx, text):
                        continue
                hits.append({"id": suffix, "path": _tilde(ctx, path), "what": label})
    gac = ctx.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and os.path.isfile(gac):
        hits.append({"id": "gcloud", "path": gac, "what": "service account key named by GOOGLE_APPLICATION_CREDENTIALS"})
    return hits


@check("cred.files", "credentials", emits=("cred.files.",))
def check_home_files(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    for h in ctx.fact("home_credential_files"):
        out.append(failed("cred.files.%s" % h["id"], "%s: %s" % (h["path"], h["what"]),
                          "delete the file or log out for the agent's OS user; credentials like this belong in CI behind OIDC and an Environment"))
    return out or [passed("cred.files", "no cloud, deploy-host or package-registry credential files in the home directory")]


# --- credentials lying in the working tree -------------------------------------------------------------------

_TREE_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", ".next", ".nuxt", ".terraform", "vendor", ".tox", "site-packages"}


@gatherer("repo_files")
def gather_repo_files(ctx: Context) -> List[str]:
    """Relative paths of files in the working tree (gitignored ones included), capped and pruned."""
    out = []  # type: List[str]
    base = ctx.repo
    limit = 60000
    for dirpath, dirnames, filenames in os.walk(base):
        rel = os.path.relpath(dirpath, base)
        if rel == ".":
            rel = ""
        depth = 0 if not rel else rel.count(os.sep) + 1
        if rel.replace(os.sep, "/").startswith(".claude/worktrees"):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _TREE_SKIP and depth < 9]
        for fn in filenames:
            out.append(os.path.join(rel, fn) if rel else fn)
            if len(out) >= limit:
                return out
    return out


_TREE_RULES = [
    ("env-production", re.compile(r"(?:^|/)\.env\.(?:prod|production|live)(?:\.[^/]*)?$"), ERROR, ".env.production* file: production configuration the agent could read"),
    ("tfstate", re.compile(r"(?:^|/)[^/]*\.tfstate(?:\.backup)?$"), ERROR, "Terraform state (holds resource secrets)"),
    ("private-key", re.compile(r"(?:^|/)[^/]*\.(?:pem|key|p12|pfx|jks)$|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"), None, "key file"),
    ("kubeconfig", re.compile(r"(?:^|/)kubeconfig[^/]*$|(?:^|/)\.kube/config$"), ERROR, "kubeconfig"),
    ("aws-dir", re.compile(r"(?:^|/)\.aws/(?:credentials|config)$"), ERROR, ".aws directory inside the repo"),
    ("netrc", re.compile(r"(?:^|/)\.netrc$"), ERROR, ".netrc"),
    ("service-account", re.compile(r"(?:^|/)[^/]*service[-_]?account[^/]*\.json$"), None, "service account file"),
]


@check("cred.tree", "credentials", emits=("cred.tree.",))
def check_tree(ctx: Context) -> List[Result]:
    """Credential files lying in the working tree, tracked or not (B2 names `.env.production`)."""
    out = []  # type: List[Result]
    for rel in ctx.fact("repo_files"):
        norm = rel.replace(os.sep, "/")
        for rid, rx, sev, what in _TREE_RULES:
            if not rx.search(norm):
                continue
            if sev is None:
                text = _read(os.path.join(ctx.repo, rel), 16384) or ""
                if rid == "private-key":
                    if "PRIVATE KEY" not in text:
                        continue
                elif rid == "service-account" and '"private_key"' not in text:
                    continue
                sev = ERROR
            out.append(failed("cred.tree.%s" % rid, "%s: %s" % (norm, what),
                              "move it out of the repo and out of the agent's reach; production secrets are never provisioned locally (report §5.4)", severity=sev))
            break
    return out or [passed("cred.tree", "no production env files, state files, key files or kubeconfigs in the working tree")]


# --- metadata endpoints (network) ---------------------------------------------------------------------------

METADATA_ENDPOINTS = [("169.254.169.254", 80, "instance metadata (IMDS, GCE, Azure)"), ("169.254.170.2", 80, "ECS task credentials"),
                      ("169.254.170.23", 80, "EKS Pod Identity agent")]


@gatherer("metadata_probe")
def gather_metadata(ctx: Context) -> List[Dict[str, Any]]:
    """Probe results, or [] when the doctor was not allowed to touch the network (default)."""
    if ctx.reach is None:
        return []
    out = []  # type: List[Dict[str, Any]]
    for host, port, label in METADATA_ENDPOINTS:
        out.append({"host": host, "port": port, "label": label, "reachable": ctx.reach(host, port)})
    return out


@check("cred.net.metadata", "credentials", emits=("cred.net.ecs-env",))
def check_metadata(ctx: Context) -> List[Result]:
    from . import settings as settings_mod
    probes = ctx.fact("metadata_probe")
    out = []  # type: List[Result]
    for p in probes:
        if p["reachable"]:
            out.append(failed("cred.net.metadata", "%s (%s) is reachable: any process can mint cloud credentials from it" % (p["host"], p["label"]),
                              "deny 169.254.0.0/16 in the sandbox network settings, or run where the endpoint does not exist (B3)"))
    ecs = ctx.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or ctx.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if ecs:
        out.append(failed("cred.net.ecs-env", "AWS_CONTAINER_CREDENTIALS_* is set: this process runs with a task role", "run the agent without a task role"))
    if out:
        return out
    if probes:
        return [passed("cred.net.metadata", "metadata endpoints 169.254.169.254, 169.254.170.2 and 169.254.170.23 are unreachable")]
    if settings_mod.sandbox_denies_metadata(ctx.fact("settings")):
        return [passed("cred.net.metadata", "not probed; the sandbox network settings deny 169.254.0.0/16")]
    return [unknown("cred.net.metadata", "metadata endpoints were not probed and the sandbox does not deny 169.254.0.0/16",
                    "run `doctor.py --credentials --live` from a plain terminal (it probes them), or add the deny to the sandbox network settings", severity=WARN)]


# --- credentials that hide in startup files, history and indirection ---------------------------------------------------

_ASSIGN_RES = [re.compile(r"^\s*(?:export\s+|declare\s+-x\s+|typeset\s+-x\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$"),
               re.compile(r"^\s*set\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$"),          # fish: set -gx NAME value
               re.compile(r"^\s*setenv\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$")]
_PLACEHOLDER_VALUE = re.compile(r"^(?:|changeme|change-me|todo|xxx+|\*+|<[^>]*>|your[-_ ].*|example.*|dummy.*|placeholder.*|none|null)$", re.I)


def _clean_value(v: str) -> str:
    v = v.strip()
    v = re.sub(r"\s+#.*$", "", v)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def scan_assignments(text: str) -> List[Tuple[str, str]]:
    """(name, value) pairs for credential-looking assignments in a shell or dotenv file."""
    out = []  # type: List[Tuple[str, str]]
    for line in text.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        for rx in _ASSIGN_RES:
            m = rx.match(line)
            if m:
                name, value = m.group(1), _clean_value(m.group(2))
                if _PLACEHOLDER_VALUE.match(value):
                    break
                cat = classify_env_name(name)
                if cat is not None and cat not in ("shell",):
                    out.append((name, cat))
                elif _secret_shaped(value):
                    out.append((name, "secret-shaped"))
                elif _DB_URL_NAME_RE.search(name):
                    m2 = _USERINFO_RE.search(value)
                    if m2 and not _host_is_local(m2.group(3)):
                        out.append((name, "database"))
                break
    return out


SHELL_FILES = [".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".config/fish/config.fish",
               ".config/fish/conf.d/*.fish", ".config/environment.d/*.conf", ".pam_environment", ".xprofile", ".xsessionrc"]
REPO_ENV_FILES = [".envrc", ".env", ".env.local", ".env.development", ".env.development.local"]


@gatherer("shell_env_files")
def gather_shell_env(ctx: Context) -> List[Dict[str, str]]:
    hits = []  # type: List[Dict[str, str]]
    files = []  # type: List[Tuple[str, str]]
    for g in SHELL_FILES:
        for p in sorted(glob.glob(ctx.home_path(*g.split("/")))):
            files.append((p, "shell startup file"))
    for var in ("BASH_ENV", "ENV"):
        v = ctx.getenv(var)
        if v and os.path.isfile(_expand(ctx, v)):
            files.append((_expand(ctx, v), var + " startup file"))
    for rel in REPO_ENV_FILES:
        p = ctx.repo_path(rel)
        if os.path.isfile(p):
            files.append((p, "repo environment file"))
    for path, what in files:
        text = _read(path)
        if not text:
            continue
        for name, cat in scan_assignments(text):
            hits.append({"path": _tilde(ctx, path), "what": what, "name": name, "category": cat})
    return hits


@check("cred.shellrc", "credentials")
def check_shell_env(ctx: Context) -> List[Result]:
    """A token exported from .zshrc, BASH_ENV, .envrc or .env reaches every shell the agent starts, whatever this process's own environment shows."""
    out = []  # type: List[Result]
    for h in ctx.fact("shell_env_files"):
        sev = ERROR
        out.append(failed("cred.shellrc", "%s (%s) sets %s, a %s credential" % (h["path"], h["what"], h["name"], h["category"]),
                          "remove it from the file; the agent's shell loads it. Dev and test values belong in .env.local, production values nowhere on this machine",
                          severity=sev))
    return out or [passed("cred.shellrc", "no credential is exported from shell startup files, BASH_ENV, .envrc or .env")]


_HISTORY_FILES = [".bash_history", ".zsh_history", ".zhistory", ".local/share/fish/fish_history", ".sh_history", ".python_history", ".node_repl_history", ".psql_history", ".mysql_history"]
_HISTORY_WARN_KINDS = {"secret-assignment", "bearer", "url-credentials", "basic-auth"}


@gatherer("history_secrets")
def gather_history(ctx: Context) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for rel in _HISTORY_FILES:
        p = ctx.home_path(*rel.split("/"))
        text = safeio.read_text(p, 400000)
        if not text:
            continue
        kinds = sorted({k for k in _secret_shaped(text)})
        if kinds:
            out.append({"path": _tilde(ctx, p), "kinds": kinds})
    return out


@check("cred.history", "credentials")
def check_history(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    for h in ctx.fact("history_secrets"):
        sure = [k for k in h["kinds"] if k not in _HISTORY_WARN_KINDS]
        out.append(failed("cred.history", "%s contains secret-shaped text (%s): the agent can read shell history" % (h["path"], ", ".join(h["kinds"])),
                          "rotate what it holds, then clear the history (`history -c`, delete the file); use a password manager prompt instead of typing secrets",
                          severity=ERROR if sure else WARN))
    return out or [passed("cred.history", "no secret-shaped text in the tail of shell or REPL history files")]


@gatherer("ssh_identity_files")
def gather_ssh_identity(ctx: Context) -> List[Dict[str, str]]:
    cfg = _read(ctx.home_path(".ssh", "config")) or ""
    out = []  # type: List[Dict[str, str]]
    seen = set()  # type: set
    for m in re.finditer(r"(?mi)^\s*IdentityFile\s+(\S+)", cfg):
        raw = m.group(1).strip("\"'")
        path = _expand(ctx, raw.replace("%d", ctx.home).replace("${HOME}", ctx.home))
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        kind = _ssh_private_key(path)
        if kind and not path.startswith(ctx.home_path(".ssh") + os.sep):
            out.append({"path": _tilde(ctx, path), "state": kind})
    return out


@check("cred.ssh.identity-files", "credentials")
def check_ssh_identity(ctx: Context) -> List[Result]:
    out = [failed("cred.ssh.identity-files", "~/.ssh/config points at a private key outside ~/.ssh: %s (%s)" % (k["path"], k["state"]),
                  "move the key out of the agent's reach", severity=ERROR if k["state"] == "unencrypted" else WARN) for k in ctx.fact("ssh_identity_files")]
    return out or [passed("cred.ssh.identity-files", "ssh config names no private key outside ~/.ssh")]


@gatherer("docker_contexts")
def gather_docker_contexts(ctx: Context) -> List[str]:
    d = os.path.join(ctx.getenv("DOCKER_CONFIG") or ctx.home_path(".docker"), "contexts", "meta")
    out = []  # type: List[str]
    for meta in sorted(glob.glob(os.path.join(d, "*", "meta.json"))):
        text = _read(meta) or ""
        m = re.search(r'"Name"\s*:\s*"([^"]+)"', text)
        host = re.search(r'"Host"\s*:\s*"([^"]+)"', text)
        if host and not host.group(1).startswith("unix://"):
            out.append("%s (%s)" % (m.group(1) if m else os.path.basename(os.path.dirname(meta)), re.sub(r"//[^@/]*@", "//", host.group(1))))
    return out


@check("cred.docker.contexts", "credentials")
def check_docker_contexts(ctx: Context) -> List[Result]:
    out = [failed("cred.docker.contexts", "docker context %s points at a remote daemon" % c, "`docker context rm` it for the agent's OS user") for c in ctx.fact("docker_contexts")]
    return out or [passed("cred.docker.contexts", "no remote docker contexts")]


@check("cred.repo-files", "credentials", emits=("cred.repo-files.",))
def check_repo_credential_files(ctx: Context) -> List[Result]:
    """The same files as `cred.files`, but lying in the working tree (.npmrc with a token, .pypirc, .cargo/credentials, ...)."""
    out = []  # type: List[Result]
    for suffix, label, globs, rx in HOME_CREDENTIAL_FILES:
        if suffix in ("gcloud", "azure", "doctl", "firebase", "stripe", "glab", "hub", "helm", "heroku", "supabase", "vault", "terraform", "pulumi"):
            continue
        for g in globs:
            for path in sorted(glob.glob(ctx.repo_path(*g.split("/")))):
                if not os.path.isfile(path) or not _nonempty(path):
                    continue
                if rx is not None and not re.search(rx, _read(path) or ""):
                    continue
                out.append(failed("cred.repo-files.%s" % suffix, "%s in the repository: %s" % (os.path.relpath(path, ctx.repo), label),
                                  "remove the literal credential (reference an environment variable instead) and rotate it; tracked or not, the agent can read it"))
    return out or [passed("cred.repo-files", "no package-registry credentials in the working tree")]


@gatherer("submodule_git_configs")
def gather_submodule_configs(ctx: Context) -> List[Dict[str, str]]:
    """Credential-bearing entries in per-submodule and per-worktree git configs (`git config --list` of the main repo misses them)."""
    cd = gitutil_common_dir(ctx)
    hits = []  # type: List[Dict[str, str]]
    if not cd:
        return hits
    paths = sorted(glob.glob(os.path.join(cd, "modules", "**", "config"), recursive=True)) + sorted(glob.glob(os.path.join(cd, "worktrees", "*", "config.worktree")))
    for p in paths[:50]:
        rc, out, err = ctx.git(["config", "--file", p, "--list", "-z"], harden=False)
        if rc != 0:
            continue
        entries = []
        for rec in out.split("\0"):
            if rec:
                k, _, v = rec.partition("\n")
                entries.append({"origin": "file:" + p, "key": k, "value": v})
        for h in judge_git_config(entries, ctx):
            h["where"] = "%s (%s)" % (os.path.relpath(p, cd), h["category"])
            hits.append(h)
    return hits


def gitutil_common_dir(ctx: Context) -> Optional[str]:
    rc, out, err = ctx.git(["rev-parse", "--git-common-dir"], harden=False)
    if rc != 0 or not out.strip():
        return None
    p = out.strip()
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(ctx.repo, p))


@check("cred.git.submodules", "credentials")
def check_submodule_configs(ctx: Context) -> List[Result]:
    out = [failed("cred.git.submodules", "%s: %s" % (h["where"], h["what"]), "remove the credential from that config") for h in ctx.fact("submodule_git_configs")]
    return out or [passed("cred.git.submodules", "no credentials in submodule or per-worktree git configs")]


_ROOT_EQUIVALENT_GROUPS = ("docker", "lxd", "libvirt", "libvirtd", "disk", "kvm", "root", "adm")


@check("cred.groups", "credentials")
def check_groups(ctx: Context) -> Result:
    hit = [g for g in ctx.groups if g in ("docker", "lxd", "libvirt", "libvirtd", "disk")]
    if hit:
        return failed("cred.groups", "this user is in group %s: that is root on the machine without a password" % ", ".join(hit),
                      "run the agent as a user that is not in it (or in a container); membership gives docker or disk access whether or not a socket is visible")
    return passed("cred.groups", "this user is in no root-equivalent group (docker, lxd, libvirt, disk)")


@check("cred.sudo", "credentials")
def check_sudo(ctx: Context) -> Result:
    rc, out, err = ctx.run(["sudo", "-n", "-l"], 5.0, ctx.env, None)
    if rc == 127 or rc == 126:
        return passed("cred.sudo", "sudo is not available to this user")
    if rc == 0 and re.search(r"NOPASSWD|\(ALL(?: : ALL)?\)\s+ALL", out):
        return failed("cred.sudo", "sudo works without a password for this user: the agent could become root and read anything on the machine",
                      "remove the NOPASSWD rule, or run the agent as a user with no sudo")
    return passed("cred.sudo", "sudo needs a password (or is not permitted) for this user")


@check("cred.same-user", "credentials")
def check_same_user(ctx: Context) -> Result:
    """The doctor sees what is stored; it cannot see what the human's running processes hold (their environment, the unlocked keychain, browser sessions)."""
    iso = str(ctx.cfg("agent_isolation", "none") or "none")
    if iso != "none":
        return passed("cred.same-user", "agent_isolation: %s (attested): the agent does not run as your everyday OS user" % iso)
    extra = ""
    if ctx.platform == "darwin":
        extra = " On macOS the login keychain is readable by the same user (`security find-internet-password`)."
    elif ctx.platform.startswith("linux"):
        extra = " On Linux a process can read the environment of the user's other processes (/proc/<pid>/environ), and the secret-service keyring is on the user's D-Bus."
    return unknown("cred.same-user", "agent_isolation is none: the agent would run as your OS user, so everything your user's processes hold is reachable, beyond what this check can see." + extra,
                   "run the agent as a separate OS user or in a credential-free container, then set agent_isolation (docs/security/credentials.md)", severity=WARN)


@check("cred.keyring", "credentials")
def check_keyring(ctx: Context) -> Result:
    bits = []  # type: List[str]
    if ctx.getenv("DBUS_SESSION_BUS_ADDRESS") and ctx.platform.startswith("linux"):
        bits.append("a D-Bus session (secret-service keyring)")
    for rel in (".password-store", ".gnupg/private-keys-v1.d"):
        p = ctx.home_path(*rel.split("/"))
        try:
            if os.path.isdir(p) and os.listdir(p):
                bits.append("~/" + rel)
        except OSError:
            pass
    if bits:
        return failed("cred.keyring", "a secret store the agent could unlock or read is present: %s" % ", ".join(bits),
                      "run the agent as another OS user, or remove the store from this account", severity=WARN)
    return passed("cred.keyring", "no D-Bus keyring, pass store or gpg private keys found")


# --- live checks (human only) -----------------------------------------------------------------------------------

def _live_skip(rid: str) -> Result:
    return skipped(rid, "needs --live (run by a human from a plain terminal; agents never run it)")


@check("cred.live.sts", "credentials")
def check_live_sts(ctx: Context) -> List[Result]:
    if not ctx.live:
        return [_live_skip("cred.live.sts")]
    profiles = [p["name"] for p in ctx.fact("aws_files")["profiles"]]
    env = dict(ctx.env)
    env["AWS_PAGER"] = ""
    out = []  # type: List[Result]
    targets = profiles or [None]
    for name in targets:
        argv = ["aws", "sts", "get-caller-identity", "--output", "json"] + (["--profile", name] if name else [])
        rc, so, se = ctx.run(argv, 20.0, env, None)
        label = name or "(default credential chain)"
        if rc == 127:
            out.append(unknown("cred.live.sts", "the aws CLI is not installed; sts was not run for %s" % label, "install it to run this check", severity=WARN))
        elif rc == 0:
            arn = re.search(r'"Arn"\s*:\s*"([^"]+)"', so)
            out.append(failed("cred.live.sts", "sts get-caller-identity works for %s as %s" % (label, arn.group(1) if arn else "an identity"),
                              "remove the credential; no cloud identity may be usable from the agent's environment"))
        else:
            out.append(passed("cred.live.sts", "sts get-caller-identity fails for %s (no usable identity)" % label))
    return out


@check("cred.live.gh", "credentials")
def check_live_gh(ctx: Context) -> Result:
    if not ctx.live:
        return _live_skip("cred.live.gh")
    rc, so, se = ctx.run(["gh", "auth", "status"], 15.0, ctx.env, None)
    text = so + se
    if rc == 127:
        return unknown("cred.live.gh", "the gh CLI is not installed", "", severity=WARN)
    users = sorted(set(re.findall(r"account\s+([A-Za-z0-9-]+(?:\[bot\])?)", text)))
    bot = _bot_login(ctx)
    if rc != 0 and not users:
        return passed("cred.live.gh", "gh is not logged in")
    if bot and users and all(u.lower().replace("[bot]", "") == bot for u in users):
        return passed("cred.live.gh", "gh is logged in only as the bot")
    return failed("cred.live.gh", "gh is logged in as %s" % (", ".join(users) or "an account"), "run `gh auth logout`; only the bot may be reachable (RC-5)")


@check("cred.live.git-fill", "credentials")
def check_live_git_fill(ctx: Context) -> Result:
    if not ctx.live:
        return _live_skip("cred.live.git-fill")
    env = dict(ctx.env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    rc, so, se = ctx.run(["git", "credential", "fill"], 15.0, env, ctx.repo, "protocol=https\nhost=github.com\n\n")
    if rc == 127:
        return unknown("cred.live.git-fill", "git is not installed", "", severity=WARN)
    user = re.search(r"(?m)^username=(.*)$", so)
    has_pw = bool(re.search(r"(?m)^password=.+$", so))
    if rc != 0 or not has_pw:
        return passed("cred.live.git-fill", "git credential fill returns nothing for github.com")
    who = user.group(1).strip() if user else ""
    bot = _bot_login(ctx)
    if bot and who.lower().replace("[bot]", "") == bot:
        return passed("cred.live.git-fill", "git credential fill returns the bot's credential only")
    return failed("cred.live.git-fill", "git credential fill returns a credential for github.com (user %s)" % (who or "unknown"),
                  "remove the credential helper or its stored login (RC-5)")


@check("cred.live.ssh", "credentials")
def check_live_ssh(ctx: Context) -> Result:
    if not ctx.live:
        return _live_skip("cred.live.ssh")
    rc, so, se = ctx.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes", "git@github.com"],
                         15.0, ctx.env, None)
    text = so + se
    m = re.search(r"Hi ([A-Za-z0-9-]+)!", text)
    if rc == 127:
        return unknown("cred.live.ssh", "ssh is not installed", "", severity=WARN)
    if not m:
        return passed("cred.live.ssh", "ssh -T git@github.com does not authenticate")
    bot = _bot_login(ctx)
    if bot and m.group(1).lower() == bot:
        return passed("cred.live.ssh", "ssh authenticates to GitHub as the bot")
    return failed("cred.live.ssh", "ssh authenticates to GitHub as %s" % m.group(1), "remove the key or agent that GitHub accepts (RC-5)")
