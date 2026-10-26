"""Settings, MCP, sandbox, managed settings and devcontainer checks (report §5.1 B3, §5.6, §5.8, §7.2).

Rules this module enforces:
  * `Workflow` in any allow list we can see is an error, except a file the caller names as the
    human-run /orchestrate profile (RC-17, boundary fixture "Workflow headlessly").
  * the sandbox is configured fail-closed and a human attested it really works (V19).
  * no deploy-capable MCP server or plugin is enabled (CS-10, Install B's `deploy-on-aws`).
  * `bypassPermissions` is not the default mode.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import safeio
from .model import (Context, Result, check, failed, gatherer, passed, skipped, unknown, ERROR, WARN)

MANAGED_PATHS = ("/Library/Application Support/ClaudeCode/managed-settings.json",
                 "/etc/claude-code/managed-settings.json")

# precedence, highest first. `--settings` files sit above project scopes and below managed.
SCOPE_ORDER = ("managed", "cli", "local", "project", "user")


# --- helpers ---------------------------------------------------------------------------------

def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas from JSON-with-comments, string-aware."""
    out = []  # type: List[str]
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json_file(path: str, jsonc: bool = False) -> Tuple[Optional[Any], Optional[str]]:
    """(data, error). A missing file is (None, None)."""
    text, problem = safeio.read_text_status(path, 4 << 20)
    if problem == safeio.MISSING:
        return None, None
    if problem:
        return None, problem
    try:
        return json.loads(strip_jsonc(text) if jsonc else text), None
    except ValueError as exc:
        return None, "not valid JSON: %s" % exc


def dig(data: Any, dotted: str, default: Any = None) -> Any:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _rules(data: Any, *paths: str) -> List[str]:
    out = []  # type: List[str]
    for p in paths:
        v = dig(data, p)
        if isinstance(v, list):
            out.extend(x for x in v if isinstance(x, str))
    return out


def allow_rules(data: Any) -> List[str]:
    return _rules(data, "permissions.allow", "allowedTools")


def deny_rules(data: Any) -> List[str]:
    return _rules(data, "permissions.deny", "disallowedTools")


_WORKFLOW_RE = re.compile(r"^\s*Workflow\s*(\(.*\))?\s*$")


def workflow_allow_rules(data: Any) -> List[str]:
    """Allow rules that let a session launch a Workflow. Also catches a bare `*`-style allow of
    every tool, which includes Workflow."""
    hits = []  # type: List[str]
    for r in allow_rules(data):
        if _WORKFLOW_RE.match(r) or r.strip() in ("*", "Workflow*", "mcp__*Workflow*"):
            hits.append(r)
    return hits


def effective(settings: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    """First value found in precedence order (managed, cli, local, project, user)."""
    for scope in SCOPE_ORDER:
        for f in settings["files"]:
            if f["scope"] == scope and f["data"] is not None:
                v = dig(f["data"], dotted, None)
                if v is not None:
                    return v
    return default


def union_list(settings: Dict[str, Any], *paths: str) -> List[str]:
    out = []  # type: List[str]
    for f in settings["files"]:
        if f["data"] is not None:
            for r in _rules(f["data"], *paths):
                if r not in out:
                    out.append(r)
    return out


# --- gatherers -----------------------------------------------------------------------------------

@gatherer("settings")
def gather_settings(ctx: Context) -> Dict[str, Any]:
    """Every settings file the doctor can see: managed, `--settings` extras, project, local, user."""
    cands = []  # type: List[Tuple[str, str]]
    for mp in MANAGED_PATHS:
        cands.append(("managed", ctx.host_path(mp)))
    for p in ctx.extra_settings:
        cands.append(("cli", os.path.abspath(p)))
    cands.append(("local", ctx.repo_path(".claude", "settings.local.json")))
    cands.append(("project", ctx.repo_path(".claude", "settings.json")))
    cands.append(("user", ctx.home_path(".claude", "settings.json")))
    files = []  # type: List[Dict[str, Any]]
    for scope, path in cands:
        data, err = load_json_file(path)
        exists = os.path.exists(path)
        if not exists and scope != "cli":
            continue
        files.append({"scope": scope, "path": path, "data": data if isinstance(data, dict) else None,
                      "error": err or (None if data is None or isinstance(data, dict) else "top level is not an object"),
                      "exists": exists})
    return {"files": files}


def _frontmatter(text: str) -> Dict[str, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}  # type: Dict[str, str]
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


@gatherer("agents")
def gather_agents(ctx: Context) -> List[Dict[str, Any]]:
    d = ctx.repo_path(".claude", "agents")
    out = []  # type: List[Dict[str, Any]]
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".md"):
            continue
        text = safeio.read_text(os.path.join(d, n), 65536)
        if text is None:
            continue
        fm = _frontmatter(text)
        tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()]
        dis = [t.strip() for t in fm.get("disallowedTools", "").split(",") if t.strip()]
        out.append({"file": ".claude/agents/" + n, "name": fm.get("name", n[:-3]), "tools": tools, "disallowed": dis,
                    "has_frontmatter": bool(fm)})
    return out


def _server_entry(name: str, cfg: Any, source: str) -> Dict[str, Any]:
    d = cfg if isinstance(cfg, dict) else {}
    args = d.get("args") if isinstance(d.get("args"), list) else []
    env = d.get("env") if isinstance(d.get("env"), dict) else {}
    return {"name": name, "source": source, "command": str(d.get("command", "")),
            "args": [str(a) for a in args][:12], "url": str(d.get("url", "")),
            "env_names": sorted(str(k) for k in env), "env_values": [str(v) for v in env.values()]}


@gatherer("mcp_servers")
def gather_mcp(ctx: Context) -> Dict[str, Any]:
    servers = []  # type: List[Dict[str, Any]]
    errors = []  # type: List[str]
    data, err = load_json_file(ctx.repo_path(".mcp.json"))
    if err:
        errors.append(".mcp.json: " + err)
    if isinstance(data, dict):
        for name, cfg in (data.get("mcpServers") or {}).items():
            servers.append(_server_entry(name, cfg, ".mcp.json"))
    udata, uerr = load_json_file(ctx.home_path(".claude.json"))
    if uerr:
        errors.append("~/.claude.json: " + uerr)
    if isinstance(udata, dict):
        for name, cfg in (udata.get("mcpServers") or {}).items():
            servers.append(_server_entry(name, cfg, "user (~/.claude.json)"))
        proj = (udata.get("projects") or {}).get(ctx.repo)
        if isinstance(proj, dict):
            for name, cfg in (proj.get("mcpServers") or {}).items():
                servers.append(_server_entry(name, cfg, "local (~/.claude.json project)"))
    st = ctx.fact("settings")
    plugins = []  # type: List[str]
    for f in st["files"]:
        ep = dig(f["data"], "enabledPlugins") if f["data"] else None
        if isinstance(ep, dict):
            plugins.extend(k for k, v in ep.items() if v and isinstance(k, str))
        elif isinstance(ep, list):
            plugins.extend(k for k in ep if isinstance(k, str))
    return {"servers": servers, "plugins": sorted(set(plugins)), "errors": errors,
            "mcp_json_present": os.path.exists(ctx.repo_path(".mcp.json"))}


# --- MCP classification --------------------------------------------------------------------------

_CLOUD_RE = re.compile(
    r"aws|amazon|\bcdk\b|cloudformation|deploy|terraform|\btofu\b|pulumi|kubernetes|\bk8s\b|kubectl|helm|gcp|gcloud|"
    r"google-cloud|azure|vercel|netlify|cloudflare|wrangler|\bfly\b|flyctl|railway|render\.com|heroku|\bsst\b|serverless|"
    r"\bssh\b|docker|podman|github|gitlab|bitbucket|supabase|neon|planetscale|turso", re.I)
_DATA_RE = re.compile(r"postgres|mysql|mariadb|mongo|redis|sqlite|dynamo|bigquery|snowflake|stripe|slack|email|smtp|twilio|"
                      r"jira|linear|notion|asana|sendgrid|paypal|salesforce|zendesk|intercom", re.I)


def classify_mcp(entry_or_name: Any) -> str:
    """'cloud' (deploy-capable, errors at L2+), 'data' (mutating SaaS or DB, warns) or 'other'."""
    if isinstance(entry_or_name, dict):
        e = entry_or_name
        text = " ".join([e.get("name", ""), e.get("command", ""), " ".join(e.get("args", [])), e.get("url", "")])
    else:
        text = str(entry_or_name)
    if _CLOUD_RE.search(text):
        return "cloud"
    if _DATA_RE.search(text):
        return "data"
    return "other"


def _allowed_mcp(ctx: Context) -> List[str]:
    v = ctx.cfg("mcp_allowed_servers", [])
    return [str(x) for x in v] if isinstance(v, list) else []


def _name_matches_allowed(name: str, allowed: Iterable[str]) -> bool:
    base = name.split("@", 1)[0]
    return any(a == name or a == base for a in allowed)


# --- checks --------------------------------------------------------------------------------------

@check("settings.parse", "settings")
def check_parse(ctx: Context) -> List[Result]:
    st = ctx.fact("settings")
    out = []  # type: List[Result]
    bad = [f for f in st["files"] if f["error"]]
    for f in bad:
        out.append(failed("settings.parse", "%s (%s): %s" % (f["path"], f["scope"], f["error"]),
                          "fix or remove the file; a settings file that does not parse may silently disable guards"))
    if not bad:
        out.append(passed("settings.parse", "%d settings file(s) parsed" % sum(1 for f in st["files"] if f["exists"])))
    return out


@check("settings.workflow-allow", "settings")
def check_workflow_allow(ctx: Context) -> List[Result]:
    """RC-17: Workflow must not be in any allow list except the human-run /orchestrate profile."""
    st = ctx.fact("settings")
    out = []  # type: List[Result]
    for f in st["files"]:
        if f["data"] is None:
            continue
        hits = workflow_allow_rules(f["data"])
        if not hits:
            continue
        if os.path.abspath(f["path"]) in ctx.workflow_exempt:
            out.append(Result("settings.workflow-allow", "pass", "%s allows Workflow and is declared the human-run profile" % f["path"],
                              "info"))
            continue
        out.append(failed("settings.workflow-allow",
                          "%s (%s) allows %s: a headless child could launch a Workflow (RC-17)" % (f["path"], f["scope"], ", ".join(hits)),
                          "remove it and add \"Workflow\" to permissions.deny; only a human-run /orchestrate profile may allow it"))
    if not out:
        out.append(passed("settings.workflow-allow", "no settings file allows Workflow"))
    return out


@check("settings.agent-workflow", "settings")
def check_agent_workflow(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    for a in ctx.fact("agents"):
        if "Workflow" in a["tools"] or any(t.startswith("Workflow(") for t in a["tools"]):
            out.append(failed("settings.agent-workflow", "%s lists Workflow in tools:" % a["file"],
                              "remove Workflow from tools: and add it to disallowedTools"))
    if not out:
        out.append(passed("settings.agent-workflow", "no agent definition grants Workflow"))
    return out


@check("settings.bypass-mode", "settings")
def check_bypass(ctx: Context) -> List[Result]:
    st = ctx.fact("settings")
    out = []  # type: List[Result]
    for f in st["files"]:
        if f["data"] is None:
            continue
        if dig(f["data"], "permissions.defaultMode") == "bypassPermissions" or f["data"].get("skipDangerousModePermissionPrompt") is True:
            out.append(failed("settings.bypass-mode", "%s sets bypassPermissions as the default (or skips its warning)" % f["path"],
                              "remove it; deny rules still apply in bypass mode but nothing prompts", severity=WARN))
    if not out:
        out.append(passed("settings.bypass-mode", "no settings file defaults to bypassPermissions"))
    return out


_SANDBOX_REM = "merge .claude/templates/security/sandbox.settings.json into your settings (report §7.2)"


@check("sandbox.enabled", "sandbox")
def check_sandbox_enabled(ctx: Context) -> Result:
    v = effective(ctx.fact("settings"), "sandbox.enabled")
    return passed("sandbox.enabled", "sandbox.enabled is true") if v is True else \
        failed("sandbox.enabled", "sandbox.enabled is %s" % ("not set" if v is None else v), _SANDBOX_REM)


@check("sandbox.failIfUnavailable", "sandbox")
def check_sandbox_fail_if_unavailable(ctx: Context) -> Result:
    v = effective(ctx.fact("settings"), "sandbox.failIfUnavailable")
    return passed("sandbox.failIfUnavailable", "sandbox.failIfUnavailable is true") if v is True else \
        failed("sandbox.failIfUnavailable", "sandbox.failIfUnavailable is %s: a missing sandbox would run commands unsandboxed" % ("not set" if v is None else v), _SANDBOX_REM)


@check("sandbox.allowUnsandboxedCommands", "sandbox")
def check_sandbox_unsandboxed(ctx: Context) -> Result:
    v = effective(ctx.fact("settings"), "sandbox.allowUnsandboxedCommands")
    return passed("sandbox.allowUnsandboxedCommands", "allowUnsandboxedCommands is false") if v is False else \
        failed("sandbox.allowUnsandboxedCommands", "allowUnsandboxedCommands is %s: the agent can retry a command outside the sandbox" % ("not set" if v is None else v), _SANDBOX_REM)


@check("sandbox.excludedCommands", "sandbox")
def check_sandbox_excluded(ctx: Context) -> Result:
    excl = effective(ctx.fact("settings"), "sandbox.excludedCommands", [])
    bad = [c for c in (excl if isinstance(excl, list) else []) if isinstance(c, str) and re.match(r"^\s*(docker|podman|nerdctl)\b", c)]
    if bad:
        return failed("sandbox.excludedCommands", "docker/podman are excluded from the sandbox (%s): a container escapes it" % ", ".join(bad),
                      "remove them from sandbox.excludedCommands (report §5.1 B3)")
    return passed("sandbox.excludedCommands", "docker and podman are not excluded from the sandbox")


def sandbox_denies(settings: Dict[str, Any], kind: str) -> List[str]:
    """denyRead / denyWrite entries, tolerant of the three places the report's names could live."""
    vals = []  # type: List[str]
    for f in settings["files"]:
        if f["data"] is None:
            continue
        for path in ("sandbox.filesystem.%s" % kind, "sandbox.%s" % kind, "sandbox.permissions.%s" % kind):
            v = dig(f["data"], path)
            if isinstance(v, list):
                vals.extend(x for x in v if isinstance(x, str))
    return vals


@check("sandbox.deny-paths", "sandbox")
def check_sandbox_paths(ctx: Context) -> List[Result]:
    st = ctx.fact("settings")
    rd, wr = sandbox_denies(st, "denyRead"), sandbox_denies(st, "denyWrite")
    need_read = [".aws", ".ssh", ".config/gh", ".kube", ".env.production"]
    need_write = ["hooks", "config", "info"]
    miss_r = [n for n in need_read if not any(n in x for x in rd)]
    miss_w = [n for n in need_write if not any(("/" + n) in x or x.endswith(n) or ("/" + n + "/") in x for x in wr)]
    if not rd and not wr:
        return unknown("sandbox.deny-paths", "no sandbox denyRead/denyWrite lists found",
                       "merge .claude/templates/security/sandbox.settings.json; the key names follow report §7.2 and must be checked against your Claude Code version (V19)")
    if miss_r or miss_w:
        return failed("sandbox.deny-paths", "sandbox lists miss: denyRead %s; denyWrite on git dir %s" % (miss_r or "ok", miss_w or "ok"),
                      "see .claude/templates/security/sandbox.settings.json", severity=WARN)
    return passed("sandbox.deny-paths", "sandbox denyRead/denyWrite cover credential dirs and the git hooks/config/info dirs")


def sandbox_denies_metadata(settings: Dict[str, Any]) -> bool:
    """True if the sandbox network settings deny 169.254.0.0/16 or the metadata addresses."""
    for f in settings["files"]:
        if f["data"] is None:
            continue
        net = dig(f["data"], "sandbox.network")
        if net is None:
            continue
        deny_keys = [k for k in (net.keys() if isinstance(net, dict) else []) if any(w in k.lower() for w in ("deny", "denied", "block"))]
        for k in deny_keys:
            if "169.254" in json.dumps(net.get(k)):
                return True
    return False


@check("sandbox.attestation", "sandbox")
def check_sandbox_attested(ctx: Context) -> Result:
    """V19: the config can be right and the sandbox still absent. A human confirms it works."""
    when = str(ctx.cfg("sandbox_verified", "") or "")
    iso = str(ctx.cfg("agent_isolation", "none") or "none")
    if when:
        return passed("sandbox.attestation", "sandbox_verified: %s" % when)
    if iso != "none":
        return passed("sandbox.attestation", "agent_isolation: %s (attested)" % iso)
    return unknown("sandbox.attestation", "sandbox_verified is empty: nobody has confirmed the sandbox is active on this OS (V19)",
                   "run a command that must fail inside the sandbox (read ~/.aws, write .git/hooks), then set sandbox_verified: <date> in loop.config",
                   severity=WARN)


# --- MCP -----------------------------------------------------------------------------------------

@check("mcp.enable-all-project-servers", "mcp")
def check_mcp_enable_all(ctx: Context) -> Result:
    st = ctx.fact("settings")
    on = [f["path"] for f in st["files"] if f["data"] is not None and f["data"].get("enableAllProjectMcpServers") is True]
    if on:
        return failed("mcp.enable-all-project-servers", "enableAllProjectMcpServers is true in %s: any server a PR adds to .mcp.json starts"
                      % ", ".join(on), "set it to false and enable servers by name")
    return passed("mcp.enable-all-project-servers", "enableAllProjectMcpServers is not true")


@check("mcp.servers", "mcp", emits=("mcp.server.", "mcp.plugin.", "mcp.parse"))
def check_mcp_servers(ctx: Context) -> List[Result]:
    m = ctx.fact("mcp_servers")
    allowed = _allowed_mcp(ctx)
    out = []  # type: List[Result]
    for e in m["errors"]:
        out.append(failed("mcp.parse", e, "fix the file", severity=WARN))
    for e in m["servers"]:
        cls = classify_mcp(e)
        label = "%s (%s)" % (e["name"], e["source"])
        if _name_matches_allowed(e["name"], allowed):
            out.append(Result("mcp.server.%s" % e["name"], "pass", "%s is accepted in mcp_allowed_servers" % label, "info"))
        elif cls == "cloud":
            out.append(failed("mcp.server.%s" % e["name"], "%s looks deploy-capable (cloud, host, git or container tooling)" % label,
                              "remove it from the loop's environment, or accept it by name in mcp_allowed_servers if it is read-only (report §5.6)"))
        elif cls == "data":
            out.append(failed("mcp.server.%s" % e["name"], "%s can mutate a database or SaaS account" % label,
                              "deny its mutating tools (permissions.deny mcp__%s__*) or remove it" % e["name"], severity=WARN))
        else:
            out.append(failed("mcp.server.%s" % e["name"], "%s is enabled; engineers and critics get disallowedTools mcp__*" % label,
                              "keep it only on purpose; name it in mcp_allowed_servers", severity=WARN))
    for p in m["plugins"]:
        cls = classify_mcp(p)
        if _name_matches_allowed(p, allowed):
            continue
        if cls == "cloud":
            out.append(failed("mcp.plugin.%s" % p.split("@", 1)[0], "plugin %s is enabled and looks deploy-capable (ambient deploy plugin)" % p,
                              "disable it in factory profiles (report §8.3), or accept it in mcp_allowed_servers if it is read-only"))
    if not m["servers"] and not any(classify_mcp(p) == "cloud" for p in m["plugins"]):
        out.append(passed("mcp.servers", "no MCP servers or deploy-capable plugins are enabled for this repo"))
    return out


@check("mcp.deny-rules", "mcp")
def check_mcp_deny(ctx: Context) -> Result:
    st = ctx.fact("settings")
    deny = union_list(st, "permissions.deny", "disallowedTools")
    need = ["mcp__*__run_script", "mcp__*deploy*"]
    miss = [n for n in need if n not in deny]
    if miss:
        return failed("mcp.deny-rules", "permissions.deny lacks %s" % ", ".join(miss),
                      "add them (see .claude/templates/security/mcp-lockdown.settings.json); a typo guard, not a boundary", severity=WARN)
    return passed("mcp.deny-rules", "permissions.deny covers mcp__*__run_script and mcp__*deploy*")


# --- managed settings (L3+) --------------------------------------------------------------------------

@check("managed.settings", "settings")
def check_managed(ctx: Context) -> Result:
    st = ctx.fact("settings")
    managed = [f for f in st["files"] if f["scope"] == "managed" and f["data"] is not None]
    iso = str(ctx.cfg("agent_isolation", "none") or "none")
    if managed:
        d = managed[0]["data"]
        problems = []
        if d.get("allowManagedHooksOnly") is not True:
            problems.append("allowManagedHooksOnly is not true")
        if dig(d, "permissions.disableBypassPermissionsMode") != "disable":
            problems.append("permissions.disableBypassPermissionsMode is not \"disable\"")
        if problems:
            return failed("managed.settings", "managed settings at %s: %s" % (managed[0]["path"], "; ".join(problems)),
                          "see .claude/templates/security/managed-settings.example.json", severity=WARN)
        return passed("managed.settings", "managed settings found with allowManagedHooksOnly and disableBypassPermissionsMode")
    if iso in ("devcontainer", "container"):
        return passed("managed.settings", "no managed settings file; agent_isolation: %s (credential-free container attested)" % iso)
    when = str(ctx.cfg("managed_settings_verified", "") or "")
    if when:
        return passed("managed.settings", "managed_settings_verified: %s (attested; not visible to this OS user)" % when)
    return unknown("managed.settings", "no managed settings visible and no container attested (needed at L3+)",
                   "install .claude/templates/security/managed-settings.example.json as the OS-level managed settings for the user that runs the loop, then set managed_settings_verified; or run in a credential-free container (agent_isolation)",
                   severity=WARN)


@check("settings.web-tools", "settings")
def check_web_tools(ctx: Context) -> Result:
    """L3+: the conductor and verifier have no WebFetch/WebSearch (taint rule, §5.3)."""
    st = ctx.fact("settings")
    deny = union_list(st, "permissions.deny", "disallowedTools")
    miss = [t for t in ("WebFetch", "WebSearch") if t not in deny]
    if miss:
        return failed("settings.web-tools", "the main session can still use %s" % ", ".join(miss),
                      "add WebFetch and WebSearch to permissions.deny for L3+ (engineers get a domain allowlist instead)", severity=WARN)
    return passed("settings.web-tools", "WebFetch and WebSearch are denied for the main session")


# --- devcontainer --------------------------------------------------------------------------------

_SENSITIVE_MOUNT_RE = re.compile(r"docker\.sock|podman\.sock|[/\\]\.ssh|[/\\]\.aws|[/\\]\.config[/\\]gh|\.gitconfig|\.git-credentials|[/\\]\.kube|[/\\]\.azure|gcloud|[/\\]\.docker\b|\.netrc|\.npmrc", re.I)
_FORWARD_ENV_RE = re.compile(r"\$\{localEnv:([A-Za-z0-9_]+)\}")


def devcontainer_files(repo: str) -> List[str]:
    out = []  # type: List[str]
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        p = os.path.join(repo, rel)
        if os.path.isfile(p):
            out.append(p)
    d = os.path.join(repo, ".devcontainer")
    try:
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n, "devcontainer.json")
            if os.path.isfile(p):
                out.append(p)
    except OSError:
        pass
    return out


def lint_devcontainer(data: Any, env_is_credential) -> List[Tuple[str, str, str]]:
    """[(severity, message, remediation)] for one parsed devcontainer.json."""
    out = []  # type: List[Tuple[str, str, str]]
    if not isinstance(data, dict):
        return [(ERROR, "devcontainer.json is not an object", "fix it")]
    mounts = list(data.get("mounts") or []) + [data.get("workspaceMount") or ""]
    for m in mounts:
        text = m if isinstance(m, str) else json.dumps(m)
        if _SENSITIVE_MOUNT_RE.search(text):
            out.append((ERROR, "mounts a credential path or container socket: %s" % text[:80], "remove the mount"))
    run_args = [str(a) for a in (data.get("runArgs") or [])]
    for a in run_args:
        if re.match(r"^--(privileged|cap-add|pid=host|net=host|network=host|ipc=host|userns=host)", a) or "docker.sock" in a or a.startswith("--device"):
            out.append((ERROR, "runArgs weakens the container: %s" % a, "remove it; the container is a security boundary (B3)"))
    feats = data.get("features") or {}
    if isinstance(feats, dict):
        for k in feats:
            if "docker-in-docker" in k or "docker-outside-of-docker" in k:
                out.append((ERROR, "feature %s gives the agent a docker daemon" % k, "remove it"))
    for section in ("remoteEnv", "containerEnv"):
        envs = data.get(section) or {}
        if isinstance(envs, dict):
            for k, v in envs.items():
                for m in _FORWARD_ENV_RE.finditer(str(v)):
                    if env_is_credential(m.group(1)) or env_is_credential(k):
                        out.append((ERROR, "%s forwards host variable %s into the container" % (section, m.group(1)),
                                    "remove it; the container must not see the host's credentials"))
    settings = {}  # type: Dict[str, Any]
    for loc in (data.get("settings"), dig(data, "customizations.vscode.settings")):
        if isinstance(loc, dict):
            settings.update(loc)
    if settings.get("remote.containers.copyGitConfig") is not False and settings.get("dev.containers.copyGitConfig") is not False:
        out.append((WARN, "remote.containers.copyGitConfig is not false: the host's git config (and its credential helper) is copied in",
                    "set it to false; it is also a host-side VS Code setting, so check your user settings too"))
    if settings.get("dev.containers.gitCredentialHelperConfigLocation") != "none":
        out.append((WARN, "dev.containers.gitCredentialHelperConfigLocation is not \"none\": VS Code forwards git credentials by default",
                    "set it to \"none\" (host-side VS Code user setting as well)"))
    if str(data.get("remoteUser", "")) == "root":
        out.append((WARN, "remoteUser is root", "run as an unprivileged user"))
    return out


@check("devcontainer.lint", "settings")
def check_devcontainer(ctx: Context) -> List[Result]:
    files = devcontainer_files(ctx.repo)
    if not files:
        return [skipped("devcontainer.lint", "no devcontainer in this repo")]
    from . import creds
    out = []  # type: List[Result]
    for p in files:
        rel = os.path.relpath(p, ctx.repo)
        data, err = load_json_file(p, jsonc=True)
        if err:
            out.append(failed("devcontainer.lint", "%s: %s" % (rel, err), "fix the file"))
            continue
        findings = lint_devcontainer(data, creds.env_name_is_credential)
        for sev, msg, rem in findings:
            out.append(failed("devcontainer.lint", "%s: %s" % (rel, msg), rem, severity=sev))
        if not findings:
            out.append(passed("devcontainer.lint", "%s: no credential mounts, privileged flags or env forwarding" % rel))
    return out
