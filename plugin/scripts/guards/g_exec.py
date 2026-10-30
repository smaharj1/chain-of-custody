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

import fnmatch
import os
import re
from typing import List, Optional, Sequence, Set, Tuple

from . import g_file, paths
from .invoke import Inv
from .lib import Ctx, Decision, deny

from kitlib import loopctl_table  # noqa: E402  (guards.lib puts the scripts directory on sys.path)

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
KIT_INTERNALS_RE = re.compile(r"(\b(from|import)\s+(kitlib|guards|doctorlib)\b|\brequire\(['\"][^'\"]*(kitlib|guards)|(import_module|__import__)\s*\(\s*['\"](kitlib|guards|doctorlib)|"
                              r"register_session|unregister_session|clear_halt|set_halt|"
                              r"acquire_lock|steal_lock|release_lock|write_nonce|write_run\b|RunState\b|loopctl/(sessions|run\.json|HALT|lock|nonces|taint|events))", re.I)
# What an agent-authored SCRIPT FILE may not name (D1 item 6). Narrower than KIT_INTERNALS_RE: the kit's own tests legitimately `import kitlib`, so a file
# is judged by the writers of run state only. A typo guard like the rest of this module; the sandbox denyWrite on the registry is the real stop.
STATE_PATH_RE = re.compile(r"loopctl[\W_]{1,8}(sessions|run\.json|HALT|lock|nonces)", re.I)
# The writers' names (`acquire_lock`, `release_lock`, `sessions_dir`, `write_run` are ordinary names in application code) count only in a file that also
# reaches for the kit: names its modules, its scripts directory, or builds an import at run time (R-A-7)
STATE_NAME_RE = re.compile(r"(register_session|unregister_session|clear_halt|set_halt|acquire_lock|steal_lock|release_lock|write_nonce|write_run\b|sessions_dir)", re.I)
KIT_REF_RE = re.compile(r"(\b(kitlib|runstate|RunState|loopctl|doctorlib|guards)\b|\.claude/scripts|\bimportlib\b|__import__|\bimp\.load)", re.I)


def _state_write(text: str) -> bool:
    return bool(STATE_PATH_RE.search(text) or (STATE_NAME_RE.search(text) and KIT_REF_RE.search(text)))


# Code that imports loopctl itself and calls into it: the guard cannot classify a call that never goes through the command line (D11 item 7)
LOOPCTL_IMPORT_RE = re.compile(r"(\b(from|import)\s+loopctl\b|(import_module|__import__)\s*\(\s*['\"]loopctl|require\(['\"][^'\"]*loopctl)", re.I)
LOOPCTL_CALL_RE = re.compile(r"\b(main|Loop|Driver|State|approve|approve_plan|approve_item|cmd_approve|cmd_resume|set_status|advance)\b")
MAX_IMPORT_SCAN = 10
# a loopctl command line inside code (`os.system("loopctl approve plan")`, `['python3', '.claude/scripts/loopctl', 'approve', ...]`)
LOOPCTL_LINE_RE = re.compile(r"(?:^|-m|[\s'\"`(\[,;|&=])(?:[^\s'\"`()\[\],;|&=]*/)?loopctl(?:\.py|/__main__\.py)?/?(?=[\s'\"`,)\]]|$)([^;&|`\n]*)")
OBFUSCATION_RE = re.compile(r"(\b(exec|eval|compile)\s*\(|\bFunction\s*\(|new\s+Function|__import__|\bvm\.run)", re.I)
DECODE_RE = re.compile(r"(b64decode|base64|fromhex|unhexlify|atob\(|Buffer\.from|zlib|decompress|rot13|codecs\.decode|\bchr\b|String\.fromCharCode|"
                       r"\\x[0-9a-f]{2}|bytes\.fromhex|marshal|pickle)", re.I)

# Who may run which loopctl command is `kitlib/loopctl_table.py`: the same table `loopctl/cli.py` uses (review fixes D1b, D3, D11).

# the real path of the loopctl package, its `loopctl.py` / `__main__.py`, or a directory or symlink that resolves to one
LOOPCTL_REAL_RE = re.compile(r"(^|/)loopctl(\.py|/__main__\.py|/__init__\.py)?/?$", re.I)      # re.I: a case-insensitive file system (macOS)
GUARDS_RUN_RE = re.compile(r"(^|/)guards/run\.py$", re.I)
GUARDS_WIRING_RE = re.compile(r"(^|/)guards/wiring\.py$", re.I)
PY_NAME_RE = re.compile(r"^(python[0-9.]*|pypy[0-9.]*)$")
# `python -m X script ...` modules that run another file or module named in their arguments
PY_RUNNERS = frozenset({"runpy", "cprofile", "profile", "trace", "pdb", "coverage"})
GLOB_CHARS_RE = re.compile(r"[*?\[]")
# the body of loopctl's own `__main__.py`: a copy of it under another name is still the loopctl command line
LOOPCTL_SHIM_RE = re.compile(r"loopctl\.cli\s+import\s+main\b[\s\S]*\bmain\(\s*sys\.argv")
MAX_PY_WORDS = 6

HUMAN_APPLY_FLAGS = {"--apply", "--resolved", "--take-kit"}
# The kit's human-run apply steps (Phase 9 and 11a, GD-8): script basename -> (a path fragment the script's real path must contain, or "" for none;
# the flags that make it act; the positional word that does). They also refuse inside a Claude Code session (humanonly.py), a tripwire that
# `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` passes; this table is what denies the agent that does that. Generic names (labels.py, setup.sh) need
# the kit's directory in their real path, so a project's own setup.sh is not caught.
HUMAN_APPLY = {
    "migrate.py": ("", HUMAN_APPLY_FLAGS, ()), "init_project.py": ("", HUMAN_APPLY_FLAGS, ()),
    "bootstrap_config.py": ("", frozenset(), ("apply",)),
    "apply-boundary.sh": ("", frozenset({"--apply"}), ()), "apply-labels.sh": ("", frozenset({"--apply"}), ()),
    "verify-boundary.sh": ("", frozenset({"--run"}), ()),
    "boundary.py": ("github/", frozenset({"--apply", "--run"}), ()), "labels.py": ("github/", frozenset({"--apply"}), ()),
    "setup.sh": (".claude/bootstrap/", frozenset({"--apply"}), ()),
}

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
            if sf.lang == "python":
                for helper, text in _agent_authored_imports(sf.path, content, ana.vfs):
                    d = _inline(ctx, InlineCode("python", text, sf.inv, False, "", True))
                    if d:
                        return d
        d = _script_file(ctx, sf, ana)
        if d:
            return d
    return None


_IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+(\.*)([A-Za-z_][\w.]*)?[ \t]+import[ \t]+([\w, \t]+)|import[ \t]+([\w., \t]+))", re.M)


def _import_candidates(script: str, text: str) -> List[str]:
    """Paths of the local modules a Python source may import, from the script's own directory: `import a.b`, `from a import b`, `from . import b`."""
    base = os.path.dirname(script)
    out = []   # type: List[str]
    for m in _IMPORT_RE.finditer(text):
        mods = []   # type: List[str]
        if m.group(4) is not None:
            mods = [x.strip().split(" ")[0] for x in m.group(4).split(",") if x.strip()]
        else:
            dots, mod, names = m.group(1) or "", m.group(2) or "", m.group(3) or ""
            if mod:
                mods.append(mod)
            for nm in names.split(","):
                nm = nm.strip().split(" ")[0]
                if nm and nm != "*":
                    mods.append((mod + "." if mod else "") + nm)
        for mod in mods:
            rel = mod.replace(".", "/")
            for cand in (rel + ".py", rel + "/__init__.py"):
                out.append(os.path.normpath(os.path.join(base, cand)))
    seen = set()   # type: Set[str]
    return [c for c in out if not (c in seen or seen.add(c))]


def _agent_authored_imports(script: str, text: str, vfs) -> List[Tuple[str, str]]:
    """The agent-authored `.py` files a Python script imports from its own directory tree, transitively: at most MAX_IMPORT_SCAN files of 256 KB.
    Best effort (a module found through sys.path tricks or a computed name is not seen); the sandbox is the real stop."""
    found = []   # type: List[Tuple[str, str]]
    todo = [(script, text)]
    done = {os.path.normpath(script)}
    while todo and len(found) < MAX_IMPORT_SCAN:
        cur, body = todo.pop(0)
        for cand in _import_candidates(cur, body):
            if cand in done or len(found) >= MAX_IMPORT_SCAN:
                continue
            done.add(cand)
            content = vfs.get(cand)
            if content is None:
                if not os.path.isfile(cand) or not paths.agent_authored(cand):
                    continue
                content = paths.safe_read(cand, 256 * 1024)
            if content is None:
                continue
            found.append((cand, content))
            todo.append((cand, content))
    return found


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
        typed_here = p.detail in ("pure:top", "pure:subst", "pure:procsub")
        if typed_here:
            return deny(G, "opaque-command", "%s. A command whose whole name is computed at run time (`$cmd`, `$(...)`, `\"$@\"`) runs whatever a variable or a file holds, "
                        "unseen (CS-6); name the program, or put the logic in a script file the guard can read" % p.message)
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
    # -- human-only apply steps: the upgrade, the project-settings writer, the bootstrap config/labels/boundary/setup scripts (H6, GD-8) --
    d = _human_apply(ctx, inv)
    if d:
        return d
    # -- the guards' own state CLI: the script is found by its real path from the command's working directory (`cd guards && python3 run.py`, `-mguards.run`) --
    for reals, rest, _rdyn, _loose in _py_targets(inv, ctx.home):
        if any(GUARDS_WIRING_RE.search(r) for r in reals) and _flag_hit(rest, ("--write",)):
            return deny(G, "guards-cli", "wiring.py --write regenerates settings.v2.json and the settings templates, which are H6 files: a human runs it")
        if any(GUARDS_RUN_RE.search(r) for r in reals):
            d = _guards_cli_words(rest)
            if d:
                return d
    if inv.cwd_unknown and PY_NAME_RE.match(name) and any(x in inv.args for x in _STATE_WORDS):
        return deny(G, "guards-cli", "the guards' state commands (register-session, set-halt, clear-halt) are human commands, and a `cd` to a directory computed at run time "
                    "hides which script this is: run them with `!`")
    if name.startswith("py-module:") and re.match(r"py-module:(guards|kitlib|doctorlib)(\.|$)", name):
        if name.startswith("py-module:guards") and any(x in inv.args for x in ("--selftest", "--explain")) and not any(
                x in inv.args for x in ("register-session", "clear-halt", "set-halt", "unregister-session")):
            pass
        else:
            return deny(G, "guards-cli", "python -m %s reaches the kit's internals (the session registry, HALT, the lock): those are written by loopctl and by humans, not by an agent" % name[10:])
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
    if name in ("sed", "gsed"):
        prog = _sed_program(inv.args)
        if SED_EXEC_RE.search(prog) and (DANGER_CMD_RE.search(command_scan_text(prog)) or ctx.is_subagent):
            return deny(G, "sed-exec", "GNU sed runs a shell command from its script (the `e` command, the `s///e` flag): the verbs the guards deny are denied through sed too")
    # -- subagent-only --
    if ctx.is_subagent:
        d = _subagent_only(ctx, inv)
        if d:
            return d
    return None


# a sed script's `e` command (`1e git push`, `$!e`, `/x/,/y/ e cmd`, `;e`) or `s///e` flag, GNU sed only
SED_EXEC_RE = re.compile(r"(?:^|[;{}\n])\s*(?:(?:\d+|\$|/[^/\n]*/)(?:\s*,\s*(?:\d+|\$|/[^/\n]*/))?)?\s*!?\s*e(?:\s|$|;|\})|"
                         r"(?:^|[;{}\n\s])\s*(?:(?:\d+|\$|/[^/\n]*/)\s*!?\s*)?s([^\w\s\\])(?:(?!\1).|\\.)*\1(?:(?!\1).|\\.)*\1[a-zA-Z0-9]*e[a-zA-Z0-9]*(?:$|[;}\s])")


def _sed_program(args: Sequence[str]) -> str:
    """The sed script text of a command line: the values of every -e / --expression, else the first word that is not an option."""
    scripts = []   # type: List[str]
    first = None   # type: Optional[str]
    k = 0
    while k < len(args):
        a = args[k]
        if a in ("-e", "--expression") and k + 1 < len(args):
            scripts.append(args[k + 1])
            k += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-e") and len(a) > 2 and not a.startswith("--"):
            scripts.append(a[2:])
        elif a in ("-f", "--file") or a in ("-l", "--line-length"):
            k += 2
            continue
        elif a and not a.startswith("-") and first is None:
            first = a
        k += 1
    return "\n".join(scripts) if scripts else (first or "")


def _flag_hit(args: Sequence[str], flags) -> bool:
    """`--flag`, `--flag=...`, or an unambiguous argparse abbreviation (`--ap`, three or more characters) of one of `flags`."""
    for a in args:
        if not a.startswith("--"):
            continue
        name = a.split("=", 1)[0]
        if name in flags or (len(name) >= 3 and any(f.startswith(name) for f in flags)):
            return True
    return False


def _human_apply(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    """A kit script that applies a change, called by name, by a path whose real file has that name (a symlink), or as `python -m <module>`."""
    cands = []   # type: List[Tuple[str, List[str], List[bool]]]     # (script as written, its arguments, their dynamic flags)
    if inv.raw and "/" in inv.raw or _basename_lc(inv.raw) in HUMAN_APPLY:
        cands.append((inv.raw, list(inv.args), list(inv.dyn)))
    seen = 0
    for k, a in enumerate(inv.args):
        if a.startswith("-"):
            continue
        cands.append((a, list(inv.args[k + 1:]), list(inv.dyn[k + 1:])))
        seen += 1
        if seen >= 3:
            break
    if inv.name.startswith("py-module:"):
        mod = inv.name[10:]
        cands.append((mod.split(".")[-1] + ".py", list(inv.args), list(inv.dyn)))
        # `cd .claude/scripts/github && python3 -m labels --apply`: the module's file next to the working directory
        for real in _module_reals(mod, inv, ctx.home):
            cands.append((real, list(inv.args), list(inv.dyn)))
    for script, rest, rdyn in cands:
        if not script:
            continue
        forms = {_basename_lc(script)}
        # the script's real path from the command's working directory, a glob expanded: `cd .claude/scripts/github && python3 labels.py --apply`
        reals = _reals(script, inv, ctx.home) if "\x00" not in script else []
        for real in reals:
            forms.add(_basename_lc(real))
        for base in forms:
            spec = HUMAN_APPLY.get(base)
            if spec is None:
                continue
            need, flags, words = spec
            if need and not inv.cwd_unknown and not any(need in x.replace(os.sep, "/").lower() for x in [script] + reals if x) and "\x00" not in script:
                continue
            hit = _flag_hit(rest, flags) or any(w in rest for w in words) or any(rdyn)
            if hit:
                return deny(G, "human-apply", "%s applies a change to the kit's own instructions, scripts and settings, to the repository's labels and rulesets, or to a config "
                            "plan (H6): its apply step is a human command. Read the dry run, then run it yourself from a plain terminal" % base)
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


def _reals(word: str, inv: Inv, home: str) -> List[str]:
    """The real paths a command-line word may name from `inv.cwd`: a glob is expanded against the file system (as the shell would), symlinks are resolved,
    and both the lexical (`a/../b`) and the physical reading of `..` are tried."""
    if not word or "\x00" in word or len(word) > 1024:
        return []
    words = paths.expand_glob(word, inv.cwd, home) if GLOB_CHARS_RE.search(word) else [word]
    out = []   # type: List[str]
    for w in words:
        try:
            forms = (os.path.realpath(paths.lexical(w, inv.cwd, home)), paths.physical(w, inv.cwd, home))
        except (OSError, ValueError):
            continue
        for f in forms:
            f = f.replace(os.sep, "/")
            if f not in out:
                out.append(f)
    return out


def _unknown_cwd_script(word: str, inv: Inv) -> bool:
    """After a `cd` to a directory computed at run time a relative script name means nothing: `__main__.py`, `.`, `..` and a glob that could be `loopctl` are treated as it."""
    if not inv.cwd_unknown:
        return False
    base = word.rstrip("/").rsplit("/", 1)[-1]
    if base in ("__main__.py", ".", ".."):
        return True
    return bool(GLOB_CHARS_RE.search(base)) and any(fnmatch.fnmatchcase(n, base) for n in ("loopctl", "loopctl.py"))


def _module_reals(module: str, inv: Inv, home: str) -> List[str]:
    """The files `python -m MODULE` may run from `inv.cwd`: `MODULE.py`, `MODULE/__main__.py`, `MODULE/__init__.py` (the current directory is first on sys.path)."""
    rel = module.replace(".", "/")
    if not rel or "\x00" in rel or rel.startswith("/") or "*" in rel:
        return []
    out = []   # type: List[str]
    for cand in (rel + ".py", rel + "/__main__.py", rel + "/__init__.py"):
        out.extend(x for x in _reals(cand, inv, home) if x not in out)
    return out


def _py_targets(inv: Inv, home: str) -> List[Tuple[List[str], List[str], List[bool], bool]]:
    """Every file a python invocation may run, as (real paths, the arguments after it, their dynamic flags, True if only a loose reading says so). A plain script
    is any of the first few words (the guard does not model python's own options); `-m NAME` is NAME's file next to `inv.cwd`, and the runner modules
    (`-m runpy PATH`, `-m cProfile PATH`, `-m coverage run PATH`) also name the file they run."""
    args = inv.args
    dyn = inv.dyn if len(inv.dyn) == len(args) else [False] * len(args)
    out = []   # type: List[Tuple[List[str], List[str], List[bool], bool]]
    if inv.name.startswith("py-module:"):
        mod = inv.name[10:]
        out.append((_module_reals(mod, inv, home), list(args), list(dyn), False))
        if mod.split(".")[0].lower() in PY_RUNNERS:
            seen = 0
            for k, a in enumerate(args):
                if a.startswith("-"):
                    continue
                out.append((_reals(a, inv, home) + _module_reals(a, inv, home), list(args[k + 1:]), list(dyn[k + 1:]), _unknown_cwd_script(a, inv)))
                seen += 1
                if seen >= 3:
                    break
        return out
    if PY_NAME_RE.match(inv.name):
        out.extend(_py_targets_of_words(inv, args, dyn, home))
    return out


def _py_targets_of_words(inv: Inv, args: Sequence[str], dyn: Sequence[bool], home: str) -> List[Tuple[List[str], List[str], List[bool], bool]]:
    """The first few plain words of `args` as files, each with the words after it."""
    out = []   # type: List[Tuple[List[str], List[str], List[bool], bool]]
    for k, a in enumerate(args):
        if a.startswith("-"):
            continue
        out.append((_reals(a, inv, home), list(args[k + 1:]), list(dyn[k + 1:]), _unknown_cwd_script(a, inv)))
        if len(out) >= MAX_PY_WORDS:
            break
    return out


def _is_loopctl_shim(real: str) -> bool:
    """A file (or a directory's `__main__.py`) that is a copy of loopctl's entry point: `cp .claude/scripts/loopctl/__main__.py /tmp/x.py` in an earlier command."""
    cand = os.path.join(real, "__main__.py") if os.path.isdir(real) else real
    if not cand.endswith(".py") or not os.path.isfile(cand):
        return False
    text = paths.safe_read(cand, 64 * 1024)
    return bool(text and LOOPCTL_SHIM_RE.search(text))


def _loopctl_rest(inv: Inv, home: str = "") -> Optional[Tuple[List[str], List[bool]]]:
    """(the arguments after `loopctl`, their dynamic flags) for a loopctl invocation, else None. The script is identified by its REAL path, resolved against
    the command's working directory with globs expanded: `l?opctl`, `cd .claude/scripts/loopctl && python3 __main__.py`, `-mloopctl` and a symlink to it all count."""
    args = inv.args
    dyn = inv.dyn if len(inv.dyn) == len(args) else [False] * len(args)
    nm = inv.name
    if nm in ("loopctl", "loopctl.py"):
        return list(args), list(dyn)
    if nm.startswith("py-module:") and nm.split(":")[1].startswith("loopctl"):
        return list(args), list(dyn)
    if "\x00" not in inv.raw and ("/" in inv.raw or GLOB_CHARS_RE.search(inv.raw)):      # run by a path, or through a symlink to it
        if any(LOOPCTL_REAL_RE.search(r) for r in _reals(inv.raw, inv, home)) or _unknown_cwd_script(inv.raw, inv):
            return list(args), list(dyn)
    for reals, rest, rdyn, loose in _py_targets(inv, home):
        if loose or any(LOOPCTL_REAL_RE.search(r) or _is_loopctl_shim(r) for r in reals):
            return rest, rdyn
    if PY_NAME_RE.match(nm):
        for k, a in enumerate(args):
            if not a.startswith("-") and re.search(r"(^|/)loopctl(\.py|/__main__\.py)?/?$", a):
                return list(args[k + 1:]), list(dyn[k + 1:])
    return None


def _is_loopctl_path(script: str, inv: Inv, home: str = "") -> bool:
    """True when `script` is, or is a symlink to, the `loopctl` package or its `loopctl.py`/`__main__.py`."""
    return any(LOOPCTL_REAL_RE.search(r) for r in _reals(script, inv, home or os.environ.get("HOME", "")))


# subcommands whose class depends on a word the guard must be able to read: a run-time-computed word there is treated as a human step
_DYNAMIC_SENSITIVE = frozenset({"approve", "reconcile", "parallel", "migrate-plan", "remote-lock", "claim", "resume"})


def loopctl_classify(inv: Inv, home: str = "") -> Optional[Tuple[str, str, Optional[str]]]:
    """None if `inv` is not a loopctl invocation, else `loopctl_table.classify` of its arguments: ("read"|"write"|"human", sub, action).
    A word the guard cannot read (`$x`, `$(...)`) in the command position, or anywhere in a command whose class depends on its words,
    makes the invocation a human step (fail closed)."""
    parsed = _loopctl_rest(inv, home)
    if parsed is None:
        return _dynamic_script_human(inv)
    return classify_rest(parsed[0], parsed[1])


def _dynamic_script_human(inv: Inv) -> Optional[Tuple[str, str, Optional[str]]]:
    """`python3 $X approve item F1`: the script is named by a run-time value the guard cannot read. When the words after it are a human-only loopctl command it is
    treated as one (in doubt, a human step); any other command line after an unreadable script is not judged, or every `python3 "$script" --flag` would be."""
    if not PY_NAME_RE.match(inv.name) or len(inv.dyn) != len(inv.args):
        return None
    for k, a in enumerate(inv.args):
        if a.startswith("-"):
            continue
        if not inv.dyn[k]:
            return None
        got = classify_rest(inv.args[k + 1:], inv.dyn[k + 1:])
        return got if got[0] == "human" else None
    return None


def classify_rest(rest: Sequence[str], dyn: Sequence[bool]) -> Tuple[str, str, Optional[str]]:
    """`loopctl_table.classify(rest)` with the dynamic-word rule of `loopctl_classify`."""
    cls, sub, action = loopctl_table.classify(rest)
    dyn = list(dyn) if len(dyn) == len(rest) else [False] * len(rest)
    first = next((k for k, a in enumerate(rest) if not a.startswith("-")), None)
    if cls != "read" and any(dyn) and ((first is not None and dyn[first]) or sub in _DYNAMIC_SENSITIVE):
        return "human", sub, action
    return cls, sub, action


def loopctl_is_read(inv: Inv) -> Optional[bool]:
    """None if `inv` is not a loopctl invocation; True if it is a read-only loopctl command; False if it may write state or is a human step."""
    c = loopctl_classify(inv)
    return None if c is None else c[0] == "read"


def _loopctl(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    c = loopctl_classify(inv, ctx.home)
    if c is None or c[0] == "read":
        return None
    cls, sub, action = c
    what = "loopctl %s" % " ".join(x for x in (sub, action) if x)
    if cls == "human":
        # denied to every caller, the registered conductor included: it grants an approval, lifts a stop or writes to a remote (D3, D11).
        # The CLI cannot tell a human from the conductor, so this guard is the boundary (`!` and a plain terminal do not pass through it).
        return deny(G, "loopctl-human-only", "%s is a human step: it grants an approval, lifts a stop or pushes to a remote. Run it yourself in a plain terminal, or with `!` "
                    "in the prompt. Text in this session cannot approve." % what)
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
    if name in ("expect", "autoexpect"):
        return deny(G, "inline-interpreter", "expect runs a Tcl script that spawns whatever it names: an interpreter walks around every verb guard (CS-6)")
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
    if KIT_INTERNALS_RE.search(norm) if not ic.from_file else _state_write(norm):
        return deny(G, "kit-internals", "%s %s code reaches the kit's own libraries or its run state (the session registry, HALT, the lock, the nonce). An agent that can "
                    "register its own session defeats the caller classification (report §5.7); loopctl and humans write that state"
                    % ("a script file's" if ic.from_file else "inline", low_lang))
    if LOOPCTL_IMPORT_RE.search(norm) and LOOPCTL_CALL_RE.search(norm):
        return deny(G, "kit-internals", "%s %s code imports loopctl and calls into it: the guard classifies loopctl by its command line (who may approve, resume, "
                    "push), and a call that never goes through the command line cannot be classified" % ("a script file's" if ic.from_file else "inline", low_lang))
    d = _loopctl_in_code(ctx, scan, low_lang)
    if d is None:
        d = _loopctl_argv_of_code(ctx, ic, norm)
    if d:
        return d
    if SDK_ANTHROPIC_INLINE_RE.search(norm):
        return deny(G, "sdk-inline", "inline %s code imports the Anthropic SDK; an SDK script is a way to start a session with no hooks (CS-2). Run a reviewed script file "
                    "instead" % low_lang)
    if DECODE_RE.search(norm) and (OBFUSCATION_RE.search(norm) or (not ic.from_file and (g_file.WRITE_RE.search(norm) or g_file.WRITE_RE.search(g_file.normalise_code(code, join=False))))):
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


def _loopctl_in_code(ctx: Ctx, scan: str, lang: str) -> Optional[Decision]:
    """A loopctl command line that code (inline or a script file the agent wrote) would run. Best effort, like the rest of this module: the command
    is read from the text, and a human-class or write-class line is judged exactly like the same words typed on a command line."""
    for m in LOOPCTL_LINE_RE.finditer(scan):
        toks = re.findall(r"[^\s'\",()\[\];|&]+", m.group(1))[:24]
        cls, sub, action = loopctl_table.classify(toks)
        if cls == "human":
            return deny(G, "loopctl-human-only", "%s code runs `loopctl %s%s`, a human step: it grants an approval, lifts a stop or pushes to a remote. Run it yourself in a "
                        "plain terminal, or with `!` in the prompt" % (lang, sub, " " + action if action else ""))
        if cls == "write" and ctx.kind != "conductor":
            return deny(G, "loopctl-write", "%s code runs `loopctl %s`, which changes the run state; only the registered conductor session may (RC-13)" % (lang, sub or "(odd invocation)"))
    return None


RUNPY_RE = re.compile(r"\b(runpy|run_path|run_module)\b", re.I)
RUNPY_LITERAL_RE = re.compile(r"""run_(?:path|module)\s*\(\s*(?:r|u)?(['"])([^'"]+)\1""")


def _argv_after_code(ic) -> Optional[Tuple[List[str], List[bool]]]:
    """The words a python `-c CODE` is handed after the code (`sys.argv[1:]`), else None (stdin and script-file code have none to read)."""
    inv = ic.inv
    if ic.lang != "python" or ic.from_file or inv is None:
        return None
    dyn = inv.dyn if len(inv.dyn) == len(inv.args) else [False] * len(inv.args)
    for k, a in enumerate(inv.args):
        if a == ic.code or a.endswith(ic.code) and a.startswith("-c"):
            return list(inv.args[k + 1:]), list(dyn[k + 1:])
    if "-" in inv.args:            # `python3 - arg ... <<EOF`: the code is on stdin, the words after `-` are its arguments
        k = inv.args.index("-")
        return list(inv.args[k + 1:]), list(dyn[k + 1:])
    return None


SYS_ARGV_RE = re.compile(r"sys\.argv(\[[^\]=]*\])?\s*=\s*\[([^\]]*)\]")
_STATE_WORDS = ("register-session", "clear-halt", "set-halt", "unregister-session")


def _code_argvs(ic, norm: str) -> List[Tuple[List[str], List[bool]]]:
    """The argument vectors inline python code runs with: the words after `-c CODE`, and a literal `sys.argv = [...]` assignment in the code (its program name
    dropped, unless the assignment is to a slice)."""
    out = []   # type: List[Tuple[List[str], List[bool]]]
    got = _argv_after_code(ic)
    if got and got[0]:
        out.append(got)
    for m in SYS_ARGV_RE.finditer(g_file.normalise_code(ic.code, join=False)):       # the list form: joining would fuse `'a','b'` into one word
        toks = re.findall(r"[^\s'\",\[\]]+", m.group(2))
        if toks:
            whole = m.group(1) is not None            # `sys.argv[1:] = [...]` has no program name
            out.append((toks if whole else toks[1:], [False] * len(toks if whole else toks[1:])))
    return out


def _loopctl_argv_of_code(ctx: Ctx, ic, norm: str) -> Optional[Decision]:
    """`python3 -c "import runpy; runpy.run_path('.claude/scripts/loopctl', run_name='__main__')" approve item F1` hands the command line to loopctl through the
    code's own arguments (or through `sys.argv = [...]` in the code), which `_loopctl_in_code` (it reads the code text only) never sees. The arguments are judged as
    loopctl's when the code names loopctl, when the first of them is the loopctl path (`runpy.run_path(sys.argv[1])`), and, for code that runs another file or
    module whose name the guard cannot read, always: whatever is not a read is a human step then. The same arguments are checked against the guards CLI."""
    if ic.lang != "python":
        return None
    inv = ic.inv
    best = None   # type: Optional[Tuple[str, str, Optional[str]]]
    for argv, adyn in _code_argvs(ic, norm):
        judged = None   # type: Optional[Tuple[str, str, Optional[str]]]
        for reals, rest, rdyn, loose in _py_targets_of_words(inv, argv, adyn, ctx.home):
            if any(GUARDS_RUN_RE.search(r) for r in reals):
                d = _guards_cli_words(rest)
                if d:
                    return d
            if loose or any(LOOPCTL_REAL_RE.search(r) for r in reals):
                judged = classify_rest(rest, rdyn)
                break
        if judged is None and re.search(r"loopctl", norm, re.I):
            judged = classify_rest(argv, adyn)
        if judged is None and RUNPY_RE.search(norm):
            lit = RUNPY_LITERAL_RE.search(norm)
            named_elsewhere = False
            if lit:
                tgt = lit.group(2)
                named_elsewhere = not (LOOPCTL_REAL_RE.search(tgt) or tgt.split(".")[0].lower().startswith("loopctl") or GLOB_CHARS_RE.search(tgt)) and \
                    not any(LOOPCTL_REAL_RE.search(r) for r in _reals(tgt, inv, ctx.home))
                if GUARDS_RUN_RE.search(tgt) or tgt.split(".")[0].lower() == "guards":
                    d = _guards_cli_words(argv)
                    if d:
                        return d
            if not named_elsewhere:
                c = classify_rest(argv, adyn)
                judged = ("human", c[1], c[2]) if c[0] != "read" else c
        if judged is not None and (best is None or {"human": 2, "write": 1, "read": 0}[judged[0]] > {"human": 2, "write": 1, "read": 0}[best[0]]):
            best = judged
    if best is None:
        return None
    cls, sub, action = best
    what = "loopctl %s" % " ".join(x for x in (sub, action) if x)
    if cls == "human":
        return deny(G, "loopctl-human-only", "%s is a human step (inline %s code is handed it as its arguments): it grants an approval, lifts a stop or pushes to a remote. Run it "
                    "yourself in a plain terminal, or with `!` in the prompt" % (what, ic.lang))
    if cls == "write" and ctx.kind != "conductor":
        return deny(G, "loopctl-write", "inline %s code is handed `%s` as its arguments, which changes the run state; only the registered conductor session may (RC-13)" % (ic.lang, what))
    return None


def _guards_cli_words(rest: Sequence[str]) -> Optional[Decision]:
    """The guards CLI is handed `rest`: refused unless it is only the self-test, `--explain` or help."""
    if not any(x in ("--selftest", "--explain", "--help", "-h") for x in rest) or any(x in _STATE_WORDS for x in rest):
        return deny(G, "guards-cli", "the guards' state commands (register-session, set-halt, clear-halt) are human commands: run them with `!`")
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
