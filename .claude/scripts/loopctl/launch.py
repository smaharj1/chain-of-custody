"""Launching one child `claude -p` session (report 11.1 item 2, 11.3, 11.4, 11.6, 5.8, 14.2; CS-2, CS-23, RC-17, RC-29; V5, V18, V22).

The driver is the ONLY process that starts `claude` (CS-2): no agent may, the exec guard denies it, and the loopctl write subcommands
that reach this module are denied to every caller but the registered conductor. This module is the narrow part that touches the
binary, so it carries the checks that must hold for every launch:

* NESTING. Inside a Claude Code session (`CLAUDECODE` set) the driver refuses, always. The one exception is a test harness: it
  sets `KIT_FAKE_CLAUDE_HARNESS=1` AND points `KIT_CLAUDE_BIN` at the fake, and the driver checks the file for the fake's marker.
  A real binary is never launched from inside a session, whatever the variable says.
* VERSION. The driver applies the session-start range check to itself before the first child (report 14.2): below the floor or an
  unreadable version refuses; untested or above the tested maximum refuses at L3+ and warns below.
* THE LAUNCH LINE. Fixed argv, `shell=False`, nothing from the plan or a report on it. The prompt goes in on stdin from a file the
  driver wrote. Settings, roster JSON, allowed tools and permission mode are all explicit because a child inherits nothing (V5).
* THE PROFILE. The settings are generated from the kit's headless template (Phase 2) and validated before every launch: `Workflow`
  denied and never allowed (RC-17), `dontAsk`, the sandbox on, the kit hooks present, no blanket Bash allow, no `claude` allow. A
  profile that fails is refused, not repaired.
* THE ENVIRONMENT. An allowlist (PATH, HOME, locale, toolchain locators, the Claude auth variables, KIT_*). No AWS_*, GH_*,
  GITHUB_*, SSH_AUTH_SOCK or other credential of the shell reaches a child (B3).
* THE PROCESS. Its own session and process group; stdout and stderr go to files; a poll loop watches liveness (heartbeat.py), HALT
  and the lock; on stall, HALT, a lost lock or an interrupt the whole group is terminated, then killed; the group is swept once
  more after a normal exit so a leaked dev server cannot outlive its item (L-27).
* ORPHANS. Each live child has a record `children/<session>.json` naming the driver pid. `reap_orphans` kills a child whose driver
  is dead (a SIGKILLed driver leaves its children running in their own sessions) after checking the process really is that child.
"""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from kitlib import gitutil, jsonio, levels, runstate, semver

from . import common
from .common import LoopctlError, Refused

__all__ = ["ENV_HARNESS", "LaunchRefused", "ChildSpec", "ChildResult", "Timing", "find_binary", "is_fake_binary", "check_nesting",
           "check_version", "child_env", "build_settings", "validate_settings", "allow_rules_from_commands", "parse_frontmatter",
           "agent_definition", "build_argv", "run_child", "parse_result_json", "write_child_record", "remove_child_record",
           "reap_orphans", "terminate_group", "role_kind", "FORK_ENV", "model_for"]

ENV_HARNESS = "KIT_FAKE_CLAUDE_HARNESS"
FAKE_MARKER = b"kit-fake-claude"
TIMING_ENV = "KIT_DRIVER_TEST_TIMING"
MAX_STDOUT = 4 * 1024 * 1024

# V18 (report 11.3): dispatch is event-driven, so the driver says explicitly what fork mode and background tasks do in a child.
# The names are the report's; V18 confirms them. Until it does, the driver relies on the event-driven wait alone, which is safe.
FORK_ENV = {"CLAUDE_CODE_FORK_SUBAGENT": "0", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}

BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "TMPDIR", "TZ", "SHELL")
AUTH_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
TOOLCHAIN_ENV = ("VIRTUAL_ENV", "PYENV_ROOT", "PYENV_VERSION", "NVM_DIR", "VOLTA_HOME", "ASDF_DIR", "ASDF_DATA_DIR", "CARGO_HOME",
                 "RUSTUP_HOME", "GOPATH", "GOROOT", "JAVA_HOME")
# passed from the driver's environment into the child's settings `env` block (model forcing, V18 is set above)
SETTINGS_PASSTHROUGH = ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_SUBAGENT_MODEL")

CRITIC_ROLES = ("code-reviewer", "design-critic", "security-reviewer")

# model_profile -> role -> (model, effort); `quality` passes nothing: the agent file already says opus (model-profiles.md).
PROFILE_MODELS = {
    "balanced": {"app-engineer": ("sonnet", "high"), "admin-app-engineer": ("sonnet", "high")},
    "economy": {"database-engineer": ("sonnet", "high"), "backend-engineer": ("sonnet", "high"), "app-engineer": ("sonnet", "medium"),
                "admin-app-engineer": ("sonnet", "medium"), "infra-engineer": ("sonnet", "high")},
}

_SAFE_CMD = re.compile(r"^[A-Za-z0-9_./=@:+ -]{1,120}\Z")
_SAFE_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
_PLACEHOLDER = re.compile(r"<[^>]*>")


class LaunchRefused(Refused):
    """A launch precondition failed. Nothing was started."""


# -- the binary, nesting and version ----------------------------------------------------------------------------------------

def find_binary(env: Mapping[str, str]) -> str:
    """KIT_CLAUDE_BIN (tests and probes), else `claude` on PATH. Raises LaunchRefused if neither is an executable file."""
    cand = env.get("KIT_CLAUDE_BIN")
    if cand:
        if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
            raise LaunchRefused("KIT_CLAUDE_BIN %s is not an executable file" % common.clean_text(cand, 80))
        return os.path.abspath(cand)
    for d in (env.get("PATH") or "").split(os.pathsep):
        p = os.path.join(d, "claude") if d else ""
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise LaunchRefused("claude is not on PATH (set KIT_CLAUDE_BIN to point at it)")


def is_fake_binary(path: str) -> bool:
    """True for the test double (`tests/helpers/fake_claude.py`): its first 4 KiB carry the marker. A real binary never does."""
    try:
        with open(path, "rb") as fh:
            return FAKE_MARKER in fh.read(4096)
    except OSError:
        return False


def check_nesting(env: Mapping[str, str], binary: str) -> None:
    """Refuse to launch a child from inside a Claude Code session (CS-2). Only the test harness with the fake binary may."""
    if not env.get("CLAUDECODE"):
        return
    if env.get(ENV_HARNESS) == "1" and is_fake_binary(binary):
        return
    raise LaunchRefused("the driver refuses to run inside a Claude Code session (CLAUDECODE is set): it launches `claude`, and no agent "
                        "may (CS-2). A human starts `loopctl run` from a plain terminal")


def check_version(repo: str, base_sha: Optional[str], rs: Any, env: Mapping[str, str], binary: str, configured_level: int,
                  run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """The session-start range check, applied by the driver to itself (report 14.2). Returns {status, version, how, warning}; raises
    LaunchRefused when the driver must not launch."""
    import session_start
    text, how = session_start.claude_version(dict(env, KIT_CLAUDE_BIN=binary), rs, run=run)
    kv = {"claude_code_min": None, "claude_code_tested_max": None}  # type: Dict[str, Optional[str]]
    if base_sha:
        body = gitutil.show_file(base_sha, ".claude/KIT_VERSION", repo, isolated=True)
        if body is not None:
            try:
                kv = semver.parse_kit_version_file(body)
            except ValueError:
                pass
    minimum = kv.get("claude_code_min") or session_start.DEFAULT_MIN
    status = semver.range_status(text, minimum, kv.get("claude_code_tested_max"))
    out = {"status": status, "version": text, "how": how, "warning": None}  # type: Dict[str, Any]
    if status == semver.OK:
        return out
    why = {semver.UNKNOWN: "the Claude Code version cannot be read (%s)" % how,
           semver.BELOW_MIN: "Claude Code %s is below the kit floor %s" % (text, minimum),
           semver.UNTESTED: "Claude Code %s has not passed the canary (claude_code_tested_max is none)" % text,
           semver.ABOVE_TESTED: "Claude Code %s is above the tested maximum %s" % (text, kv.get("claude_code_tested_max"))}[status]
    if status in (semver.UNKNOWN, semver.BELOW_MIN) or configured_level >= levels.L3:
        raise LaunchRefused("the driver will not launch children: %s (report 14.2)" % why)
    out["warning"] = why
    return out


# -- environment ------------------------------------------------------------------------------------------------------------

# Names `extra_allow` may never add, however a plan or the config asks (B3, RC-5): cloud, source-control, registry and container credentials.
_HARD_CREDENTIALS = re.compile(r"^(AWS_|GH_|GITHUB_|GITLAB_|BITBUCKET_|GOOGLE_|GCLOUD_|CLOUDSDK_|AZURE_|ARM_|KUBE|DOCKER_|TF_VAR_|NPM_|NODE_AUTH|SSH_|GIT_|"
                               r"ANTHROPIC_|CLAUDE|OPENAI_)", re.I)


def child_env(env: Mapping[str, str], extra_allow: Sequence[str] = (), fake: bool = False,
              overrides: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The child's environment: an allowlist, never the shell's credentials (B3). `extra_allow` is `verify_env_allow` plus the item's
    `requiresEnv` (names a human approved at H1); a name in a credential family is never passed whatever asks for it."""
    out = {}  # type: Dict[str, str]
    names = set(BASE_ENV) | set(AUTH_ENV) | set(TOOLCHAIN_ENV) | {n for n in extra_allow if isinstance(n, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z", n)
                                                                    and not _HARD_CREDENTIALS.match(n)}
    for k, v in env.items():
        if k in names or k.startswith("KIT_") or (fake and k.startswith("FAKE_CLAUDE_")):
            out[k] = v
    out.pop("CLAUDECODE", None)
    out.pop(ENV_HARNESS, None)
    if overrides:
        out.update(overrides)
    return out


# -- the headless profile ---------------------------------------------------------------------------------------------------

def _templates_dir() -> str:
    from .context import SCRIPTS_DIR
    return os.path.join(os.path.dirname(SCRIPTS_DIR), "templates", "settings")


def role_kind(role: str, roster: Mapping[str, str]) -> str:
    """`critic` for a read-only reviewer role, else `engineer` (the guards' agent-blind default, IMP-7 V1)."""
    if roster.get(role) == "critic" or role in CRITIC_ROLES:
        return "critic"
    return "engineer"


def allow_rules_from_commands(commands: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """`Bash(<command>:*)` allow rules for the project's own verify commands, from the BASE sha's config. A command with shell
    syntax, a placeholder or a name on the never list cannot be expressed as a safe prefix rule and is skipped (returned second)."""
    rules, skipped = [], []  # type: List[str], List[str]
    for c in commands:
        if not isinstance(c, str):
            continue
        c = c.strip()
        if not c or _PLACEHOLDER.search(c):
            continue
        low = c.lower()
        if not _SAFE_CMD.match(c) or low.split()[0] in ("claude", "sudo", "su", "doas", "sh", "bash", "zsh", "eval", "env", "curl", "wget") \
                or "@anthropic-ai" in low or ".." in c:
            skipped.append(common.clean_text(c, 60))
            continue
        rule = "Bash(%s:*)" % c
        if rule not in rules:
            rules.append(rule)
    return rules, skipped


def build_settings(kind: str, rs: Any, cfg: Any, extra_commands: Sequence[Any] = (), env: Optional[Mapping[str, str]] = None,
                   template_dir: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """The settings file a child is started with: the kit's template for its kind (`engineer` -> headless-child, `critic` ->
    critic), plus the project's verify commands as allow rules, the runstate directory write-denied and the held-out tests
    read-denied (V5, report 10.3), the fork-mode and background-task variables (V18) and the compact hook (10.6). Returns
    (settings, skipped commands). Validated by the caller with `validate_settings`."""
    name = "critic.settings.json" if kind == "critic" else "headless-child.settings.json"
    path = os.path.join(template_dir or _templates_dir(), name)
    try:
        s = jsonio.read_json(path)
    except (OSError, ValueError) as exc:
        raise LaunchRefused("the headless settings template %s cannot be read: %s" % (name, common.clean_text(exc, 80)))
    if not isinstance(s, dict) or not isinstance(s.get("permissions"), dict):
        raise LaunchRefused("the headless settings template %s is malformed" % name)
    s = copy.deepcopy(s)
    cmds = [cfg.get(k) for k in ("verify_build", "verify_lint", "verify_typecheck", "verify_tests")] + list(extra_commands)
    rules, skipped = allow_rules_from_commands(cmds)
    allow = s["permissions"].setdefault("allow", [])
    for r in rules:
        if r not in allow:
            allow.append(r)
    sb = s.setdefault("sandbox", {}).setdefault("filesystem", {})
    for key, path_ in (("denyWrite", rs.root), ("denyRead", rs.heldout_dir)):
        lst = sb.setdefault(key, [])
        if path_ not in lst:
            lst.append(path_)
    senv = dict(s.get("env") or {})
    senv.update(FORK_ENV)
    for k in SETTINGS_PASSTHROUGH:
        if env and env.get(k):
            senv[k] = env[k]
    s["env"] = senv
    compact_cmd = 'python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/loopctl/compact_hook.py"'
    ss = s.setdefault("hooks", {}).setdefault("SessionStart", [])
    if not any("compact_hook.py" in json.dumps(e) for e in ss):
        ss.append({"matcher": "compact", "hooks": [{"type": "command", "command": compact_cmd, "timeout": 10}]})
    return s, skipped


_BLANKET = ("Bash", "Bash(*)", "*", "Edit(*)", "Write(*)")


def validate_settings(s: Any) -> List[str]:
    """Problems that make a child profile unsafe to launch (RC-17, CS-2, V5). Empty = fine."""
    if not isinstance(s, dict):
        return ["the settings are not an object"]
    bad = []  # type: List[str]
    perms = s.get("permissions") if isinstance(s.get("permissions"), dict) else {}
    allow = [a for a in perms.get("allow", []) if isinstance(a, str)] if isinstance(perms.get("allow", []), list) else None
    deny = [d for d in perms.get("deny", []) if isinstance(d, str)] if isinstance(perms.get("deny", []), list) else None
    if allow is None or deny is None:
        return ["permissions.allow and permissions.deny must be lists"]
    if perms.get("defaultMode") != "dontAsk":
        bad.append("permissions.defaultMode is not dontAsk (a headless child must never prompt or bypass)")
    for a in allow:
        low = a.strip().lower()
        if low == "workflow" or low.startswith("workflow("):
            bad.append("%s is allowed: a headless child must not launch a Workflow (RC-17)" % a)
        if low.startswith("bash(claude") or "@anthropic-ai/claude-code" in low or low.startswith("bash(npx claude"):
            bad.append("%s is allowed: no agent launches claude (CS-2)" % a)
        if a.strip() in _BLANKET:
            bad.append("%s is a blanket allow" % a)
        if low.startswith("mcp__") and ("deploy" in low or "run_script" in low):
            bad.append("%s allows a deploy-class MCP tool" % a)
    if "Workflow" not in deny:
        bad.append("Workflow is not denied (RC-17)")
    if "Bash(claude:*)" not in deny:
        bad.append("Bash(claude:*) is not denied (CS-2)")
    if s.get("disableAllHooks"):
        bad.append("disableAllHooks is set: the guards would not run")
    if s.get("enableAllProjectMcpServers"):
        bad.append("enableAllProjectMcpServers is on")
    sb = s.get("sandbox") if isinstance(s.get("sandbox"), dict) else {}
    if sb.get("enabled") is not True or sb.get("allowUnsandboxedCommands") is not False:
        bad.append("the sandbox is not enabled, or unsandboxed commands are allowed (B3)")
    hooks = s.get("hooks") if isinstance(s.get("hooks"), dict) else {}
    if not hooks.get("PreToolUse") or not hooks.get("SessionStart"):
        bad.append("the kit hooks (PreToolUse guards and the SessionStart canary) are missing: a child inherits nothing (V5)")
    return bad


# -- the agent definition (--agents) ----------------------------------------------------------------------------------------

def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """A tiny frontmatter reader: `key: value` lines between the first two `---` lines. (Body, dict). No YAML features."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}  # type: Dict[str, str]
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", lines[i])
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return {}, text


def _csv(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def model_for(profile: str, role: str, round_n: int, env: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """(model, effort) override for a role under the profile, or (None, None) to leave the agent file's own values (model-profiles.md).
    Round 2 and later of an engineer retry runs on opus at xhigh whatever the profile. Nothing when the model is forced."""
    if env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        return None, None
    if round_n >= 2 and role_kind(role, {}) == "engineer":
        return "opus", "xhigh"
    got = PROFILE_MODELS.get(profile, {}).get(role)
    return got if got else (None, None)


def agent_definition(repo: str, base_sha: Optional[str], role: str, model: Optional[str] = None, effort: Optional[str] = None) -> Dict[str, Any]:
    """The `--agents` entry for one role, from `.claude/agents/<role>.md` AT THE BASE SHA (an item cannot edit its own reviewer's
    tools). Raises LaunchRefused when the role is not a plain name or the file is not there."""
    if not _SAFE_ROLE.match(role or ""):
        raise LaunchRefused("%r is not a role name" % common.clean_text(role, 40))
    text = None
    if base_sha:
        text = gitutil.show_file(base_sha, ".claude/agents/%s.md" % role, repo, isolated=True)
    if text is None:
        raise LaunchRefused("no agent definition .claude/agents/%s.md at the base sha" % role)
    meta, body = parse_frontmatter(text)
    d = {"description": meta.get("description", role), "prompt": body}  # type: Dict[str, Any]
    if meta.get("tools"):
        d["tools"] = _csv(meta["tools"])
    if meta.get("disallowedTools"):
        d["disallowedTools"] = _csv(meta["disallowedTools"])
    m = model or meta.get("model")
    e = effort or meta.get("effort")
    if m:
        d["model"] = m
    if e:
        d["effort"] = e
    return d


# -- the launch line --------------------------------------------------------------------------------------------------------

class ChildSpec(object):
    """Everything one launch needs. No field is ever shell-interpreted."""

    def __init__(self, role: str, session_id: str, prompt_file: str, cwd: str, settings_path: str, agent: Dict[str, Any],
                 allowed_tools: Sequence[str], budget_usd: float, env: Mapping[str, str], item: str = "", stage: str = "",
                 extra: Optional[Dict[str, Any]] = None) -> None:
        self.role = role
        self.session_id = session_id
        self.prompt_file = prompt_file
        self.cwd = cwd
        self.settings_path = settings_path
        self.agent = agent
        self.allowed_tools = list(allowed_tools)
        self.budget_usd = budget_usd
        self.env = dict(env)
        self.item = item
        self.stage = stage
        self.extra = dict(extra or {})          # brief / report paths for the compact hook's child record


def build_argv(binary: str, spec: ChildSpec) -> List[str]:
    """claude -p --agent <role> --agents <json> --settings <file> --allowedTools <rule> <rule> ... --permission-mode dontAsk
    --max-budget-usd <remaining> --output-format json --session-id <uuid>   (report 11.1; the prompt arrives on stdin)."""
    if not runstate.valid_id(spec.session_id) or not _SAFE_ROLE.match(spec.role):
        raise LaunchRefused("invalid session id or role for a launch line")
    if not (isinstance(spec.budget_usd, (int, float)) and not isinstance(spec.budget_usd, bool) and 0 < spec.budget_usd < 1e6):
        raise LaunchRefused("the launch budget must be a positive number")
    tools = [t for t in spec.allowed_tools if isinstance(t, str) and t and not t.startswith("-")]      # one argument per rule: a rule may hold spaces
    return [binary, "-p", "--agent", spec.role, "--agents", json.dumps({spec.role: spec.agent}, sort_keys=True, ensure_ascii=True),
            "--settings", spec.settings_path, "--allowedTools"] + tools + ["--permission-mode", "dontAsk",
            "--max-budget-usd", "%.4f" % spec.budget_usd, "--output-format", "json", "--session-id", spec.session_id]


# -- running it -------------------------------------------------------------------------------------------------------------

class Timing(object):
    """All the waits. `from_config` reads dispatch_timeout_min; tests shorten them through KIT_DRIVER_TEST_TIMING, which is honoured
    only when the binary is the fake (a real run cannot be given a one-second stall limit by an environment variable)."""

    def __init__(self, poll_s: float = 0.5, stall_s: float = 1200.0, canary_wait_s: float = 90.0, term_grace_s: float = 10.0,
                 beat_s: float = 30.0, wall_s: Optional[float] = None) -> None:
        self.poll_s, self.stall_s, self.canary_wait_s, self.term_grace_s, self.beat_s = poll_s, stall_s, canary_wait_s, term_grace_s, beat_s
        # An absolute limit on one child. Liveness alone cannot end a child that keeps calling tools and never finishes; its budget does
        # when it spends, and this does when it does not. Six stall limits (two hours by default) is long for one engineer, never forever.
        self.wall_s = float(wall_s) if wall_s else 6.0 * stall_s

    @classmethod
    def from_config(cls, cfg: Any, env: Mapping[str, str], fake: bool) -> "Timing":
        t = cls(stall_s=max(1, cfg.get_int("dispatch_timeout_min")) * 60.0)
        raw = env.get(TIMING_ENV)
        if raw and fake:
            try:
                over = json.loads(raw)
                for k, v in over.items():
                    if k in ("poll_s", "stall_s", "canary_wait_s", "term_grace_s", "beat_s", "wall_s") and isinstance(v, (int, float)) \
                            and not isinstance(v, bool) and v > 0:
                        setattr(t, k, float(v))
                if not isinstance(over.get("wall_s"), (int, float)):
                    t.wall_s = 6.0 * t.stall_s
            except (ValueError, AttributeError):
                pass
        return t


class ChildResult(object):
    def __init__(self) -> None:
        self.pid = 0
        self.returncode = None  # type: Optional[int]
        self.signal_name = None  # type: Optional[str]
        self.stdout = ""
        self.stderr_tail = ""
        self.parsed = None  # type: Optional[Dict[str, Any]]
        self.reason = None  # type: Optional[str]      # stall | timeout | halt | lock_lost | interrupted | canary (we ended it)
        self.canary = None  # type: Optional[bool]
        self.duration_s = 0.0
        self.session_id = ""

    @property
    def ended_by_us(self) -> bool:
        return self.reason is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"pid": self.pid, "returncode": self.returncode, "signal": self.signal_name, "reason": self.reason, "canary": self.canary,
                "duration_s": round(self.duration_s, 3), "session_id": self.session_id, "parsed": bool(self.parsed)}


def parse_result_json(text: str) -> Optional[Dict[str, Any]]:
    """The `-p --output-format json` object. The whole output first, else its last non-empty line; None if neither is a JSON
    object (malformed: the cost is then unknown and the ledger parks the run)."""
    text = (text or "").strip()
    if not text:
        return None
    for cand in (text, text.splitlines()[-1].strip()):
        try:
            obj = common.strict_loads(cand, max_depth=10, max_bytes=MAX_STDOUT)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _read_tail(path: str, nbytes: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
            return fh.read(nbytes).decode("utf-8", "replace")
    except OSError:
        return ""


def _sig_name(rc: Optional[int]) -> Optional[str]:
    if rc is not None and rc < 0:
        try:
            return signal.Signals(-rc).name
        except ValueError:
            return "SIG%d" % -rc
    return None


def terminate_group(pgid: int, grace_s: float, alive: Callable[[], bool]) -> None:
    """SIGTERM the whole process group, wait up to `grace_s` for the leader, then SIGKILL the group."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    end = time.time() + max(0.0, grace_s)
    while time.time() < end and alive():
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def write_child_record(rs: Any, rec: Dict[str, Any]) -> str:
    d = os.path.join(rs.root, "children")
    jsonio.ensure_dir(d)
    p = os.path.join(d, rec["sessionId"] + ".json")
    jsonio.atomic_write_json(p, rec, 0o600)
    return p


def remove_child_record(rs: Any, session_id: str) -> None:
    for suffix in (".json", ".settings.json", ".prompt.md", ".out", ".err"):
        try:
            os.unlink(os.path.join(rs.root, "children", session_id + suffix))
        except OSError:
            pass


def _process_command(pid: int) -> str:
    try:
        proc = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=10)
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def reap_orphans(rs: Any, my_pid: Optional[int] = None, grace_s: float = 2.0) -> List[Dict[str, Any]]:
    """Kill children whose driver is dead. A record is acted on only when its driver pid is gone AND the process at the recorded pid
    is still a `claude` launched with that session id (so a recycled pid is never touched). Returns what was reaped."""
    my_pid = os.getpid() if my_pid is None else my_pid
    d = os.path.join(rs.root, "children")
    out = []  # type: List[Dict[str, Any]]
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".settings.json"))
    except OSError:
        return out
    for n in names:
        try:
            rec = jsonio.read_json(os.path.join(d, n))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(rec, dict) or not runstate.valid_id(rec.get("sessionId")):
            continue
        dpid = rec.get("driverPid")
        if dpid == my_pid or runstate.pid_alive(dpid):
            continue                                     # its driver is alive (this one, or another that holds the lock)
        pid, pgid = rec.get("pid"), rec.get("pgid")
        killed = False
        if isinstance(pid, int) and isinstance(pgid, int) and pid > 1 and pgid > 1 and runstate.pid_alive(pid) and rec["sessionId"] in _process_command(pid):
            terminate_group(pgid, grace_s, lambda p=pid: runstate.pid_alive(p))
            killed = True
        rs.unregister_session(rec["sessionId"])
        remove_child_record(rs, rec["sessionId"])
        out.append({"sessionId": rec["sessionId"], "item": rec.get("item"), "role": rec.get("role"), "killed": killed})
    return out


def run_child(binary: str, spec: ChildSpec, rs: Any, timing: Timing, run_id: str, driver_pid: int,
              tick: Optional[Callable[[], Optional[str]]] = None, activity: Optional[Callable[[float], float]] = None,
              canary: Optional[Callable[[], bool]] = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.time, popen: Callable[..., Any] = subprocess.Popen) -> ChildResult:
    """Start the child and wait for it. `tick()` runs every poll and may return a reason to end the child (`halt`, `lock_lost`,
    `interrupted`); `activity(started)` returns the time of the child's last sign of life; `canary()` says whether its SessionStart
    nonce exists. Always leaves no process of the child's group behind."""
    argv = build_argv(binary, spec)
    res = ChildResult()
    res.session_id = spec.session_id
    d = os.path.join(rs.root, "children")
    jsonio.ensure_dir(d)
    out_path, err_path = os.path.join(d, spec.session_id + ".out"), os.path.join(d, spec.session_id + ".err")
    started = clock()
    fin = open(spec.prompt_file, "rb")
    fout = open(out_path, "wb")
    ferr = open(err_path, "wb")
    proc = None
    try:
        proc = popen(argv, cwd=spec.cwd, env=spec.env, stdin=fin, stdout=fout, stderr=ferr, shell=False, start_new_session=True)
        res.pid = proc.pid
        pgid = proc.pid                                  # start_new_session makes the child its own group leader
        rec = dict(spec.extra)
        rec.update({"sessionId": spec.session_id, "role": spec.role, "item": spec.item, "stage": spec.stage, "pid": proc.pid,
                    "pgid": pgid, "driverPid": driver_pid, "runId": run_id, "startedAt": common.iso(), "worktree": spec.cwd})
        write_child_record(rs, rec)
        reason = None  # type: Optional[str]
        canary_ok = None  # type: Optional[bool]
        while proc.poll() is None:
            sleep(timing.poll_s)
            now = clock()
            if canary is not None and canary_ok is None:
                if canary():
                    canary_ok = True
                elif now - started > timing.canary_wait_s:
                    reason = "canary"                    # the hooks did not load: nothing runs (report 5.8)
                    break
            if tick is not None:
                r = tick()
                if r:
                    reason = r
                    break
            last = activity(started) if activity is not None else started
            if now - max(last, started) > timing.stall_s:
                reason = "stall"
                break
            if now - started > timing.wall_s:
                reason = "timeout"
                break
        if reason:
            res.reason = reason
            terminate_group(pgid, timing.term_grace_s, lambda: proc.poll() is None)
        try:
            proc.wait(timeout=max(5.0, timing.term_grace_s + 5.0))
        except subprocess.TimeoutExpired:
            terminate_group(pgid, 0.0, lambda: False)
            proc.wait()
        try:                                             # sweep what the child leaked into its group (a dev server, L-27)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        res.returncode = proc.returncode
        res.signal_name = _sig_name(proc.returncode)
        if canary is not None:
            res.canary = bool(canary_ok) or bool(canary())
    finally:
        if proc is not None and proc.poll() is None:     # the driver itself is going down: take the child with it
            terminate_group(proc.pid, 0.5, lambda: proc.poll() is None)
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        for fh in (fin, fout, ferr):
            try:
                fh.close()
            except OSError:
                pass
        res.duration_s = clock() - started
        res.stdout = _read_tail(out_path, MAX_STDOUT)
        res.stderr_tail = _read_tail(err_path, 2000)
        res.parsed = parse_result_json(res.stdout)
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return res
