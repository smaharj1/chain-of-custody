"""guard-exec: what an agent may launch, and who may write loopctl state (report §7.1, CS-2, CS-6, CO-13, RC-13).

Denied for EVERY caller (nothing here is something an agent needs):
  * launching `claude` (also `npx @anthropic-ai/claude-code`, `node .../claude-code/cli.js`, a symlink to it) and
    Agent SDK entrypoints, whether in a command, a script file or inline code: a child session can switch off every
    hook and permission (`--bare`, `--settings '{"disableAllHooks":true}'`), so the only process that launches
    children is the human-started driver (CS-2)
  * `sudo`, `su`, `doas`, `pkexec`; `docker run --privileged`, added capabilities, host namespaces, device and
    socket mounts; `curl ... | sh` and any pipe that feeds a shell something computed at run time; obfuscated
    inline code (`exec(base64...)`); `PATH`, `LD_*`, `DYLD_*`, `BASH_ENV` and `NODE_OPTIONS --require` hijacks
  * inline interpreter code that names the Agent SDK, a cloud SDK, or a dangerous command line, or that writes a
    protected path (the latter in g_file)
  * `loopctl` write subcommands from anything but the registered conductor session (RC-13). Registered driver
    children and every subagent are refused; read subcommands (status, validate, doctor, ...) are open.

Denied for SUBAGENTS (engineers, critics, unknown subagents):
  * inline interpreters (`python -c`, `node -e`, `ruby -e`, `perl -e`, `deno eval`, `bun -e`) unless the code is
    trivially harmless (a handful of pure stdlib imports and no file, process or network access)
  * JavaScript package installs without `--ignore-scripts` (§5.5); `npx` of a package that is not a known
    local dev tool; a command whose name is computed at run time

A TYPO GUARD. Arbitrary obfuscated code is not caught; the real stop is B2/B3 (no credentials, a sandbox with no
route out). docs/security/guards.md lists what the heuristics cannot see.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence, Set, Tuple

from . import g_file, paths
from .invoke import Inv
from .lib import Ctx, Decision, deny

G = "guard-exec"

CLAUDE_NAMES = {"claude", "claude-code", "claude.exe", "claude-code.exe", "claude-bg", "ccr", "claude-flow"}
SDK_RE = re.compile(r"(claude[_-]agent[_-]sdk|@anthropic-ai/claude-(code|agent-sdk)|anthropic[_.]claude[_-]code|claude_code_sdk|claude-code-sdk)", re.I)
SDK_ANTHROPIC_INLINE_RE = re.compile(r"(\bimport\s+anthropic\b|\bfrom\s+anthropic\b|require\(\s*['\"]@anthropic-ai/sdk|from\s+['\"]@anthropic-ai/sdk|"
                                     r"@anthropic-ai/sdk)", re.I)
CLAUDE_CALL_RE = re.compile(r"""(subprocess|popen|spawn|exec|run|check_output|check_call|system|execfile|execsync|execvp?e?)\w*\s*\(\s*(\[\s*)?(r?['"]claude(\s|['"]|\.exe)|r?['"][^'"]*/claude['"])""", re.I)
CLAUDE_SHELL_RE = re.compile(r"""(^|[;&|`(\s'"])claude(\.exe)?\s+(-p|--print|--bare|--settings|--agent|--model|--dangerously|-c\b|--continue|--resume|mcp|--version)""",
                             re.I)

CLOUD_SDK_RE = re.compile(r"(\bimport\s+(boto3|botocore|aiobotocore)\b|\bfrom\s+(boto3|botocore|google\.cloud|azure\.|kubernetes|pulumi)\b|\bimport\s+(google\.cloud|azure|"
                          r"kubernetes|pulumi)\b|require\(\s*['\"](aws-sdk|@aws-sdk/|@google-cloud/|@azure/|@kubernetes/|@pulumi/)|from\s+['\"](@aws-sdk/|aws-cdk-lib|"
                          r"@google-cloud/|@azure/|@kubernetes/|@pulumi/)|aws-cdk-lib)", re.I)
DANGER_CMD_RE = re.compile(
    r"\b(terraform|tofu|terragrunt)\s+(apply|destroy|import|state|taint|plan|refresh)\b|\bcdk\s+(deploy|destroy|bootstrap|diff|synth)\b|"
    r"\bpulumi\s+(up|destroy|preview|refresh)\b|\bkubectl\s+(apply|delete|replace|patch|exec|port-forward|create|scale|rollout)\b|"
    r"\bhelm\s+(install|upgrade|uninstall|rollback)\b|\bsam\s+deploy\b|\b(serverless|sls)\s+(deploy|remove)\b|\bgit\s+(-\S+\s+)*(push|rebase|"
    r"filter-branch|update-ref|replace)\b|\bgit\s+-c\s+core\.|\bgh\s+(pr\s+merge|release|secret|workflow\s+run|api\b.*-X)|--no-verify\b|"
    r"\bdocker\s+(push|login)\b|\bnpm\s+publish\b|\baws\s+\S+\s+(create|delete|put|update|run|terminate|deploy|invoke|start|stop)\w*\b|"
    r"\bsudo\b|\bcurl\b[^|;\n]*\|\s*(ba|z)?sh\b", re.I)
EXEC_API_RE = re.compile(r"(os\.system|os\.popen|os\.exec|os\.spawn|subprocess|child_process|execSync|spawnSync|\bexec\s*\(|\bsystem\s*\(|popen|Runtime\.getRuntime|%x[(\[{]|"
                         r"IO\.popen|Kernel\.|shell_exec|passthru|proc_open|\bspawn\b|\beval\s*\()", re.I)
CRED_FILE_RE = re.compile(r"(^|/)(\.env\.(production|prod|staging|live)(\..*)?|id_(rsa|ed25519|ecdsa|dsa)|[^/]*\.(pem|p12|pfx|jks)|\.netrc|\.git-credentials|"
                          r"\.pypirc|\.npmrc|(private|secret|server|tls|ssl|signing|jwt|client|service|deploy)[^/]*\.key)$|/(secrets?|keys|certs|ssl|tls|private)/[^/]*\.key$", re.I)
CRED_DIR_RE = re.compile(r"(^|/)(\.aws|\.ssh|\.kube|\.azure|\.gnupg|\.config/gh|\.config/gcloud|\.docker)(/|$)", re.I)
OBFUSCATION_RE = re.compile(r"(\b(exec|eval|compile)\s*\(|\bFunction\s*\(|new\s+Function|__import__|\bvm\.run)", re.I)
DECODE_RE = re.compile(r"(b64decode|base64|fromhex|unhexlify|atob\(|Buffer\.from|zlib|decompress|rot13|codecs\.decode|\bchr\b|String\.fromCharCode|"
                       r"\\x[0-9a-f]{2}|bytes\.fromhex|marshal|pickle)", re.I)

LOOPCTL_READ = {"validate", "status", "render", "doctor", "help", "--help", "-h", "version", "--version"}
LOOPCTL_FLAG_READ = {"reconcile": "--dry-run", "migrate-plan": "--check", "cost": "--projection", "evidence": "--check", "lock": "status"}

NPX_DEV = {"tsc", "eslint", "prettier", "jest", "vitest", "mocha", "playwright", "cypress", "tsx", "ts-node", "next", "vite", "nuxt", "astro",
           "remix", "nx", "turbo", "prisma", "drizzle-kit", "tailwindcss", "postcss", "esbuild", "rollup", "webpack", "babel", "nodemon",
           "concurrently", "rimraf", "cross-env", "lint-staged", "knip", "depcheck", "madge", "tsup", "unbuild", "storybook", "sb", "biome",
           "oxlint", "openapi-typescript", "graphql-codegen", "kysely-codegen", "typeorm", "sequelize-cli", "knex", "mikro-orm", "nest",
           "stylelint", "svelte-kit", "svelte-check", "vue-tsc", "ng", "expo", "metro", "react-native", "wrangler-types", "msw", "orval",
           "sherif", "syncpack", "size-limit", "bundlesize", "jscpd", "ts-prune", "attw", "publint", "vitepress", "docusaurus", "eleventy",
           "serve", "http-server", "live-server", "json-server", "ts-jest", "jest-cli", "c8", "nyc", "ava", "tap", "uvu", "zx", "dotenv",
           "npm-check-updates", "ncu", "license-checker", "audit-ci", "snyk", "gitleaks", "semgrep"}
NPX_DEV_FLAGS = {"--no-install", "--no", "--offline"}

INSTALL_SUBS = {"install", "i", "in", "ins", "add", "ci", "clean-install", "ic", "update", "up", "upgrade", "rebuild", "rb", "install-test", "it",
                "link", "dedupe", "isntall"}

TRIVIAL_PY_IMPORTS = {"json", "sys", "math", "re", "datetime", "time", "platform", "textwrap", "string", "collections", "itertools", "functools",
                      "statistics", "decimal", "fractions", "hashlib", "uuid", "pathlib", "shlex", "csv", "io", "operator", "random", "secrets",
                      "enum", "typing", "dataclasses", "unicodedata", "difflib", "pprint", "calendar", "fnmatch", "copy", "heapq", "bisect", "numbers"}
TRIVIAL_PY_BAD = re.compile(r"(\bopen\s*\(|\bexec\b|\beval\b|\bcompile\b|__|\bwrite|system|popen|remove|unlink|rename|rmtree|subprocess|socket|getattr|setattr|"
                            r"globals|locals|vars\s*\(|breakpoint|\binput\s*\(|mkdir|chmod|touch|environ|getenv|\.env\b|ctypes|urlopen|request|b64|decode\(|"
                            r"read_text|read_bytes|\.read\(|glob|walk|listdir|scandir|import\s+(os|shutil|tempfile|pty|signal|threading|multiprocessing|asyncio))")
TRIVIAL_NODE_BAD = re.compile(r"(require|import|process|eval|Function|child_process|\bfs\b|fetch|XMLHttp|WebSocket|Deno|Bun|globalThis|constructor|"
                              r"__proto__|\bwith\b)")
SAFE_REQUIRE = re.compile(r"require\(\s*['\"]\./[A-Za-z0-9_./-]+\.json['\"]\s*\)")


def _basename_lc(raw: str) -> str:
    return raw.rstrip("/").rsplit("/", 1)[-1].lower()


def check_bash(ctx: Ctx) -> Optional[Decision]:
    if ctx.tool_input.get("dangerouslyDisableSandbox") or ctx.tool_input.get("dangerously_disable_sandbox"):
        return deny(G, "sandbox-escape", "dangerouslyDisableSandbox runs the command outside the sandbox (B3: allowUnsandboxedCommands is false); a human "
                    "runs unsandboxed commands")
    ana = ctx.analysis
    for p in ana.problems:
        d = _problem(ctx, ana, p)
        if d:
            return d
    for inv in ana.invs:
        d = _check_inv(ctx, inv)
        if d:
            return d
    for ic in ana.inline:
        d = _inline(ctx, ic)
        if d:
            return d
    for sf in ana.scripts:
        content = ana.vfs.get(sf.path)
        if content is None and paths.agent_authored(sf.path):
            content = paths.safe_read(sf.path, 256 * 1024)
        if content is not None:      # written in this command, or untracked/modified: agent-authored, so judged like inline code
            from .invoke import InlineCode
            d = _inline(ctx, InlineCode(sf.lang, content, sf.inv, False, " ".join(sf.inv.args), True))
            if d:
                return d
        d = _script_file(ctx, sf, ana)
        if d:
            return d
    return None


def _problem(ctx: Ctx, ana, p) -> Optional[Decision]:
    if p.kind == "parse-limit":
        return deny(G, "parse-limit", "%s; a command the guard cannot analyse is refused (fail closed). Split it into simpler commands" % p.message)
    if p.kind == "pipe-to-shell":
        return deny(G, "pipe-to-shell", "%s. `curl ... | sh` and `base64 -d | sh` run code nobody reviewed (CS-2, CS-6)" % p.message)
    if p.kind == "opaque-eval":
        if any(i.origin in ("subst", "procsub") and i.name in ("curl", "wget", "nc", "ncat", "http", "xh", "fetch", "base64", "openssl") for i in ana.invs):
            return deny(G, "pipe-to-shell", "%s, and the string comes from a download or a decode: this is `curl | sh` by another route" % p.message)
        if ctx.is_subagent:
            return deny(G, "opaque-command", "%s. Subagents may not run commands built at run time (CS-6)" % p.message)
        return None
    if p.kind == "opaque-command":
        verbs = ("apply", "destroy", "deploy", "push", "delete", "publish", "up", "install", "merge", "force", "prune")
        if ctx.is_subagent or any(a in verbs for inv in ana.invs for a in inv.args):
            return deny(G, "opaque-command", "%s. Subagents may not run commands whose name is built at run time, and nobody may when it carries a "
                        "deploy-class verb (CS-6)" % p.message)
    return None


def _check_inv(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    name = inv.name
    raw_base = _basename_lc(inv.raw)
    # -- claude and the SDK --
    sdk_arg = name in ("npx", "pnpx", "bunx", "node", "npm", "pnpm", "yarn", "bun", "deno") and \
        any(SDK_RE.search(a) for a in inv.args[:6] if not a.startswith("-") or "=" in a)
    if name in CLAUDE_NAMES or raw_base in CLAUDE_NAMES or sdk_arg or (name.startswith("npx:") and SDK_RE.search(inv.raw)):
        return deny(G, "claude", "launching `claude` or the Agent SDK is refused: a child session can disable every hook and permission (--bare, "
                    "--settings '{\"disableAllHooks\":true}'). Only the human-started driver launches child sessions (CS-2, report §11.1)")
    if "/" in inv.raw and not inv.name_dynamic:
        try:
            real = os.path.realpath(paths.lexical(inv.raw, inv.cwd, ctx.home))
        except OSError:
            real = ""
        if real and _basename_lc(real) in CLAUDE_NAMES:
            return deny(G, "claude", "%s resolves to the claude binary (CS-2)" % inv.raw[:80])
    if name in ("node", "nodejs", "deno", "bun") and inv.args and re.search(r"claude-code|claude-agent-sdk|/claude/cli", inv.args[0] if not inv.args[0].startswith("-") else " ".join(inv.args[:3]), re.I):
        return deny(G, "claude", "running the Claude Code package entrypoint is refused (CS-2)")
    # -- privilege --
    if name in ("sudo", "su", "doas", "pkexec", "runuser", "sudoedit"):
        return deny(G, "sudo", "%s escalates privilege; no agent needs it, and the credential boundary assumes it is absent (B2, §7.2 deny block)" % name)
    # -- docker flags --
    if name in ("docker", "podman", "nerdctl") or any(w in ("docker run", "docker create", "docker exec") for w in inv.wrappers):
        d = _docker(inv)
        if d:
            return d
    # -- env hijacks --
    d = _env(inv)
    if d:
        return d
    # -- loopctl --
    d = _loopctl(ctx, inv)
    if d:
        return d
    # -- the guards' own state CLI --
    if name in ("python", "python3") or re.match(r"python[0-9.]+$", name):
        for a in inv.args:
            if re.search(r"(^|/)guards/wiring\.py$", a) and "--write" in inv.args:
                return deny(G, "guards-cli", "wiring.py --write regenerates settings.v2.json and the settings templates, which are H6 files: a human runs it")
            if re.search(r"(^|/)guards/run\.py$", a) or a in ("guards.run",):
                rest = [x for x in inv.args if x != a]
                if not any(x in ("--selftest", "--explain", "--help", "-h") for x in rest) or any(x in ("register-session", "clear-halt", "set-halt", "unregister-session") for x in rest):
                    return deny(G, "guards-cli", "the guards' state commands (register-session, set-halt, clear-halt) are human commands: run them with `!`")
    d = _persistence_and_config(ctx, inv)
    if d:
        return d
    d = _credential_files(ctx, inv)
    if d:
        return d
    if name in ("awk", "gawk", "mawk", "nawk"):
        prog = " ".join(a for a in inv.args if not a.startswith("-"))
        if re.search(r"system\s*\(|\|\s*getline|\|&|print[^;}]*\|\s*[\"']", prog):
            if DANGER_CMD_RE.search(command_scan_text(prog)) or ctx.is_subagent:
                return deny(G, "awk-system", "awk runs a shell command from its program (system(), |getline, print |): the verbs the guards deny are denied through awk too")
    # -- subagent-only --
    if ctx.is_subagent:
        d = _subagent_only(ctx, inv)
        if d:
            return d
    return None


def _persistence_and_config(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    name, args = inv.name, inv.args
    pos = [a.lower() for a in args if not a.startswith("-")]
    if name == "crontab" and "-l" not in args:
        return deny(G, "persistence", "crontab installs a job that runs outside every guard; schedule work through the loop, or run it yourself with `!`")
    if name == "launchctl" and pos[:1] and pos[0] in ("load", "bootstrap", "submit", "kickstart", "enable", "start", "bootout", "unload", "remove", "stop"):
        return deny(G, "persistence", "launchctl %s changes background jobs outside every guard" % pos[0])
    if name == "systemctl" and pos[:1] and pos[0] in ("enable", "start", "restart", "stop", "disable", "mask", "daemon-reload", "edit", "set-environment",
                                                      "import-environment", "reload", "link"):
        return deny(G, "persistence", "systemctl %s changes services outside every guard" % pos[0])
    if name in ("npm", "pnpm", "yarn", "pip", "pip3", "poetry", "cargo", "bun", "gem", "bundle", "uv"):
        if pos[:1] == ["config"] and len(pos) > 1 and pos[1] in ("set", "delete", "edit", "unset", "rm", "remove", "set-default"):
            return deny(G, "pkg-config-write", "%s config %s rewrites the registry and auth settings (report §5.5 pins registries; a human changes them)" % (name, pos[1]))
        if name == "npm" and pos[:1] in (["set"], ["set-script"]):
            return deny(G, "pkg-config-write", "npm %s rewrites npm config or package scripts" % pos[0])
        if name in ("npm", "pnpm") and pos[:1] == ["pkg"] and len(pos) > 1 and pos[1] in ("set", "delete") and ctx.is_subagent:
            return deny(G, "package-scripts", "%s pkg %s edits package.json; engineers do not change package.json#scripts (report §5.9)" % (name, pos[1]))
    return None


def _credential_files(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    """Typo guard for B2: a command that names a credential file. The real stop is the sandbox denyRead and the absence of
    credentials; this makes the honest mistake fail fast with a reason."""
    if inv.name in (":assign", ":redirect") and not inv.redirs:
        return None
    cands = [(a, d) for a, d in zip(inv.args, inv.dyn) if not a.startswith("-") or "=" in a]
    for r in inv.redirs:
        if r.target is not None and r.op in ("<", "<>") and not r.target.dynamic:
            cands.append((r.target.text, False))
    for a, d in cands:
        if d or "\x00" in a or len(a) > 400:
            continue
        val = a.split("=", 1)[1] if "=" in a and not a.startswith("/") and "/" in a.split("=", 1)[1] else a
        if not (val.startswith(("/", "~", ".")) or "/" in val or CRED_FILE_RE.search(val)):
            continue
        p = paths.lexical(val, inv.cwd, ctx.home)
        if p.endswith(".pub") or "/node_modules/" in p or p.endswith(".pem.example"):
            continue
        fp = paths.fold(p)
        h = paths.fold(ctx.home)
        under_home = fp.startswith(h + "/")
        if CRED_FILE_RE.search(fp) or (under_home and CRED_DIR_RE.search(fp[len(h):])) or "/loopctl/heldout" in fp:
            if inv.name in ("ssh-keygen",) and any(x in inv.args for x in ("-y", "-l")):
                continue
            return deny(G, "credential-file", "%s names a credential or held-out file (%s); no mutating credential exists for the agent by design, and reading one "
                        "would break the boundary (B2, B3)" % (inv.name, os.path.basename(p)))
    return None


def _docker(inv: Inv) -> Optional[Decision]:
    args = inv.args
    if inv.name in ("docker", "podman", "nerdctl") and re.search(r"(^|\s)(-H|--host)[= ]\s*(tcp|ssh|unix)://", " ".join(args)):
        return deny(G, "docker-mount", "docker against a remote or socket host")
    sub = ""
    for a in args:
        if not a.startswith("-"):
            sub = a
            break
    if inv.name in ("docker", "podman", "nerdctl") and sub not in ("run", "create", "exec", "container", "compose", "buildx", "build"):
        return None
    flags = " ".join(args)
    low = flags.lower()
    if re.search(r"(^|\s)--privileged(\s|=|$)", low):
        return deny(G, "docker-privileged", "docker --privileged gives the container the host's devices and capabilities; it is an escape from B3")
    m = re.search(r"--cap-add[= ]\s*(\S+)", low)
    if m and m.group(1).strip("'\"") in ("all", "sys_admin", "sys_ptrace", "sys_module", "net_admin", "dac_override", "sys_rawio"):
        return deny(G, "docker-privileged", "docker --cap-add=%s is a container escape primitive" % m.group(1))
    if re.search(r"--(pid|ipc|userns|uts|cgroupns)[= ]\s*host", low):
        return deny(G, "docker-privileged", "docker with a host namespace (pid, ipc, userns) is an escape from B3")
    if re.search(r"--security-opt[= ]\s*(seccomp|apparmor|label)[=:]?\s*(unconfined|disable)", low) or "systempaths=unconfined" in low:
        return deny(G, "docker-privileged", "docker with an unconfined security profile is an escape from B3")
    if re.search(r"--device[= ]", low):
        return deny(G, "docker-privileged", "docker --device passes a host device through")
    for m in re.finditer(r"(?:-v|--volume|--mount)[= ]\s*['\"]?([^\s'\"]+)", flags):
        spec = m.group(1)
        src = spec
        mm = re.search(r"(?:source|src)=([^,]+)", spec)
        if mm:
            src = mm.group(1)
        else:
            src = spec.split(":", 1)[0]
        s = src.lower()
        if "docker.sock" in s or "containerd.sock" in s or "podman.sock" in s or s in ("/", "/etc", "/root", "/var", "/var/run", "/proc", "/sys", "/dev", "/home", "/users", "~") or \
                s.startswith(("/etc/", "/root/", "/var/run/", "/var/lib/docker", "/proc/", "/sys/", "~/.ssh", "~/.aws", "~/.config/gh", "~/.kube", "/users/")) and "/." in s or \
                re.search(r"(^|/)\.(ssh|aws|kube|gnupg|docker)(/|$)|\.config/(gh|gcloud)", s):
            return deny(G, "docker-mount", "docker mounts %s: the Docker socket, the root, the home directory or a credential directory would break the "
                        "boundary (B2, B3)" % src[:60])
    if re.search(r"(^|\s)(-H|--host)[= ]\s*(tcp|ssh|unix)://", flags):
        return deny(G, "docker-mount", "docker against a remote or socket host")
    return None


def _env(inv: Inv) -> Optional[Decision]:
    names = dict(inv.env)
    if inv.name in ("export", "declare", "typeset", "readonly", "local", "env"):
        for a in inv.args:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a, re.S)
            if m:
                names[m.group(1)] = m.group(2)
    for k, v in names.items():
        ku = k.upper()
        if ku.startswith(("LD_", "DYLD_")) and ku not in ("LD_LIBRARY_PATH",) or ku in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "PS4", "PYTHONSTARTUP",
                                                                                      "PYTHONHOME", "RUBYOPT", "PERL5OPT", "NODE_PATH_PRELOAD", "GCONV_PATH", "LD_AUDIT"):
            return deny(G, "env-hijack", "setting %s makes the next program load or run code you did not name" % k)
        if ku == "NODE_OPTIONS" and re.search(r"(--require|-r\s|--import|--loader|--experimental-loader|--eval|-e\s)", v):
            return deny(G, "env-hijack", "NODE_OPTIONS with --require/--import/--loader runs a preload script")
        if ku == "PATH":
            for comp in v.split(":"):
                if "\x00" in comp or comp.startswith("$") or comp == "$PATH":
                    continue
                if comp == "" or comp.startswith(".") or not comp.startswith(("/", "~")) or comp.startswith(("/tmp", "/var/tmp", "/private/tmp", "/dev/shm")):
                    return deny(G, "path-hijack", "PATH gets a relative, empty or temporary component (%s): the next `git` or `terraform` could be a planted "
                                "program" % (comp or "(empty)"))
        if ku == "IFS" and inv.name == ":assign" and v not in (" \t\n", ""):
            return deny(G, "env-hijack", "IFS is reassigned: word splitting then hides commands from the guard")
    return None


def _loopctl(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    sub = None   # type: Optional[str]
    args = inv.args
    nm = inv.name
    rest = []   # type: List[str]
    if nm in ("loopctl", "loopctl.py"):
        rest = list(args)
    elif nm == "py-module:loopctl" or (nm.startswith("py-module:") and nm.split(":")[1].startswith("loopctl")):
        rest = list(args)
    elif re.match(r"^(python[0-9.]*|pypy[0-9.]*)$", nm):
        for k, a in enumerate(args):
            if re.search(r"(^|/)loopctl(\.py|/__main__\.py)?/?$", a) and not a.startswith("-"):
                rest = args[k + 1:]
                break
        else:
            return None
    else:
        return None
    pos = [a for a in rest if not a.startswith("-")]
    sub = pos[0].lower() if pos else ""
    flags = [a for a in rest if a.startswith("-")]
    is_read = sub in LOOPCTL_READ or not sub
    if sub in LOOPCTL_FLAG_READ and not is_read:
        need = LOOPCTL_FLAG_READ[sub]
        is_read = (need in flags) or (need in pos[1:2])
    if is_read:
        return None
    if ctx.kind == "conductor":
        return None
    who = "an engineer or critic" if ctx.is_subagent else ("a driver child" if ctx.caller.role else "a session that is not the registered conductor")
    return deny(G, "loopctl-write", "loopctl %s changes the run state; only the registered conductor session may (RC-13, CS-25). Called by %s. Without this rule an "
                "engineer could replay `loopctl set ...`" % (sub, who))


def _subagent_only(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    name, args = inv.name, inv.args
    if name in ("npm", "pnpm", "yarn", "bun") or any(w == name for w in inv.wrappers):
        pos = [a.lower() for a in args if not a.startswith("-")]
        sub = pos[0] if pos else ("install" if name == "yarn" else "")
        ignore = any(a.lower() in ("--ignore-scripts", "--ignore-scripts=true") for a in args) or \
            inv.env.get("npm_config_ignore_scripts", "").lower() == "true" or inv.env.get("NPM_CONFIG_IGNORE_SCRIPTS", "").lower() == "true" or \
            any(a.startswith("--mode=skip-build") for a in args)
        if sub in INSTALL_SUBS and not ignore:
            return deny(G, "install-scripts", "%s %s runs the packages' install scripts (postinstall, prepare); use `%s %s --ignore-scripts` (report §5.5)"
                        % (name, sub or "install", name, sub or "install"))
    if name.startswith("npx:"):
        tool = name[4:]
        wrappers = inv.wrappers
        if tool not in NPX_DEV and tool.split("/")[-1] not in NPX_DEV:
            return deny(G, "npx-remote", "npx %s would download and run a package; engineers use the project's own dev tools (report §5.5, CS-6)" % tool[:50])
    return None


# -- inline code and script files ------------------------------------------------------------------------------------------

def command_scan_text(code: str) -> str:
    """Inline code with list-form argv (`['git','push']`) turned into a command line, then the usual string clean-up."""
    c = re.sub(r"""['"]\s*,\s*['"]""", " ", code)
    return g_file.normalise_code(c)


def _inline(ctx: Ctx, ic) -> Optional[Decision]:
    code = ic.code
    low_lang = ic.lang
    norm = g_file.normalise_code(code)
    scan = command_scan_text(code)
    if SDK_RE.search(norm) or CLAUDE_CALL_RE.search(scan) or CLAUDE_SHELL_RE.search(scan):
        return deny(G, "claude", "inline %s code launches `claude` or the Agent SDK (CS-2)" % low_lang)
    if SDK_ANTHROPIC_INLINE_RE.search(norm):
        return deny(G, "sdk-inline", "inline %s code imports the Anthropic SDK; an SDK script is a way to start a session with no hooks (CS-2). Run a reviewed script file "
                    "instead" % low_lang)
    if OBFUSCATION_RE.search(norm) and DECODE_RE.search(norm):
        return deny(G, "obfuscated-code", "inline %s code decodes and executes a string (exec/eval + base64/hex/chr); the guard cannot read it, so it is refused" % low_lang)
    if ic.dynamic:
        if ctx.is_subagent:
            return deny(G, "opaque-command", "inline %s code is built at run time" % low_lang)
    if ic.extra and EXEC_API_RE.search(norm):
        m2 = DANGER_CMD_RE.search(command_scan_text(ic.extra))
        if m2:
            return deny(G, "inline-command", "inline %s code is handed `%s` and runs commands; the verbs the guards deny are denied through an interpreter's "
                        "arguments or stdin too (CS-6)" % (low_lang, m2.group(0)[:40]))
    m = DANGER_CMD_RE.search(scan)
    if m:
        return deny(G, "inline-command", "inline %s code runs `%s`; the verbs the guards deny are denied through an interpreter too (CS-6)" % (low_lang, m.group(0)[:40]))
    if CLOUD_SDK_RE.search(norm):
        return deny(G, "sdk-inline", "inline %s code uses a cloud SDK: agents have no cloud credentials (B2) and an SDK call is not a verb the guard can classify" % low_lang)
    if ctx.is_subagent and not ic.from_file and not _trivial(ic):
        return deny(G, "inline-interpreter", "inline %s code (-c / -e) is refused for engineers and critics: an interpreter walks around every verb guard (CS-6). Write "
                    "a script file and run it, or use the project's own commands" % low_lang)
    return None


def _trivial(ic) -> bool:
    code = ic.code
    if len(code) > 600:
        return False
    if ic.lang == "python":
        if TRIVIAL_PY_BAD.search(code):
            return False
        for m in re.finditer(r"\b(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", code):
            if m.group(1).split(".")[0] not in TRIVIAL_PY_IMPORTS:
                return False
        return True
    if ic.lang in ("node", "bun", "deno"):
        c = SAFE_REQUIRE.sub("", code)
        return not TRIVIAL_NODE_BAD.search(c)
    return False


def _script_file(ctx: Ctx, sf, ana=None) -> Optional[Decision]:
    text = ana.vfs.get(sf.path) if ana is not None and sf.path in ana.vfs else paths.safe_read(sf.path, 256 * 1024)
    if text is None:
        return None
    if SDK_RE.search(text) or CLAUDE_CALL_RE.search(command_scan_text(text)):
        return deny(G, "claude", "%s launches `claude` or the Agent SDK (CS-2); scripts the agent runs may not" % os.path.basename(sf.path))
    return None
