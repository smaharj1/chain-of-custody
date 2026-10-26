"""From a command string to the list of programs that could run (report §7.1 wrapper normalisation).

`analyze(command, cwd)` parses the string (`shellparse`), then unwraps everything that runs another
program: `sudo`/`env`/`command`/`exec`/`nohup`/`timeout`/`nice`/`xargs`/`watch`/`find -exec`, `bash -c`,
`sh -c`, `eval`, `source`, scripts piped or fed to a shell, `npx`/`pnpm dlx`/`poetry run`, and the script
runners `npm|pnpm|yarn|bun run`, `make`, `just` (their bodies are read from package.json, the Makefile and
the justfile). The result is a flat list of `Inv` (one program invocation: lowercase basename, arguments,
environment prefix, working directory) that the guards judge one by one.

Anything it cannot resolve is reported as a `Problem` instead of being ignored, so a guard can fail closed.

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import paths
from .shellparse import Cmd, NUL, ParseLimit, Script, Word, parse, static_eval

__all__ = ["Inv", "Problem", "InlineCode", "ScriptFile", "Analysis", "analyze", "SHELLS", "INTERPRETERS"]

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh", "rbash", "rzsh", "ksh93", "pdksh"}
INTERPRETER_RE = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|ruby|perl[0-9.]*|php[0-9.]*|lua[0-9.]*|luajit|deno|bun|"
                            r"osascript|rscript|tclsh|julia|groovy|jshell|swift|dotnet-script|ts-node|tsx|npx-node)$")
MAX_EXPANSION_DEPTH = 5
STDIN_PATHS = ("/dev/stdin", "/dev/fd/0", "/proc/self/fd/0", "-")
MAX_PKG_FILES = 40


class Problem(object):
    __slots__ = ("kind", "message", "detail")

    def __init__(self, kind: str, message: str, detail: str = "") -> None:
        self.kind = kind
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:
        return "Problem(%s: %s)" % (self.kind, self.message)


class Inv(object):
    """One program invocation after unwrapping."""

    __slots__ = ("name", "raw", "args", "dyn", "env", "env_dyn", "wrappers", "cwd", "cmd", "origin", "depth",
                 "name_dynamic", "xargs", "stdin_text", "cwd_unknown")

    def __init__(self, name: str, raw: str, args: List[str], dyn: List[bool], env: Dict[str, str], cwd: str,
                 cmd: Optional[Cmd], origin: str, depth: int, wrappers: Optional[List[str]] = None,
                 name_dynamic: bool = False) -> None:
        self.name = name
        self.raw = raw
        self.args = args
        self.dyn = dyn
        self.env = env
        self.env_dyn = {}  # type: Dict[str, bool]
        self.wrappers = wrappers or []
        self.cwd = cwd
        self.cmd = cmd
        self.origin = origin
        self.depth = depth
        self.name_dynamic = name_dynamic
        self.xargs = False
        self.cwd_unknown = False     # a `cd` to a directory computed at run time came earlier: relative paths mean nothing
        self.stdin_text = cmd.stdin_text if cmd is not None else None

    @property
    def redirs(self) -> list:
        return self.cmd.redirs if self.cmd is not None else []

    def any_dynamic(self) -> bool:
        return any(self.dyn)

    def __repr__(self) -> str:
        return "Inv(%s %s%s)" % (self.name, self.args, " env=%s" % self.env if self.env else "")


class InlineCode(object):
    """Code given to an interpreter on the command line or on stdin. `extra` is everything else the program is handed
    (its arguments and its piped input), because `os.system(sys.argv[1])` keeps the command out of the code."""
    __slots__ = ("lang", "code", "inv", "dynamic", "extra", "from_file")

    def __init__(self, lang: str, code: str, inv: Inv, dynamic: bool, extra: str = "", from_file: bool = False) -> None:
        self.from_file = from_file     # the code is an agent-authored script file, not -c / -e text
        self.lang = lang
        self.code = code
        self.inv = inv
        self.dynamic = dynamic
        self.extra = extra


class ScriptFile(object):
    __slots__ = ("lang", "path", "inv")

    def __init__(self, lang: str, path: str, inv: Inv) -> None:
        self.lang = lang
        self.path = path
        self.inv = inv


class Analysis(object):
    def __init__(self) -> None:
        self.invs = []        # type: List[Inv]
        self.problems = []    # type: List[Problem]
        self.inline = []      # type: List[InlineCode]
        self.scripts = []     # type: List[ScriptFile]
        self.notes = []       # type: List[str]
        self.vfs = {}         # type: Dict[str, str]   # files this same command writes with static content, then may run
        self._seen = set()    # type: Set[Tuple[str, str]]

    def problem(self, kind: str, message: str, detail: str = "") -> None:
        self.problems.append(Problem(kind, message, detail))


# ---------------------------------------------------------------------------------------------------------------
# wrapper grammar

def _lc_base(name: str) -> str:
    return name.rsplit("/", 1)[-1].lower()


# name -> (options that take an argument, options without one, number of fixed positionals before the command)
WRAPPERS = {
    "command": ({"-p"}, set(), 0),
    "builtin": (set(), set(), 0),
    "exec": ({"-a"}, {"-c", "-l"}, 0),
    "nohup": (set(), set(), 0),
    "setsid": (set(), {"-c", "-f", "-w", "--ctty", "--fork", "--wait"}, 0),
    "nice": ({"-n", "--adjustment"}, set(), 0),
    "ionice": ({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"}, {"-t"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, {"-v", "--foreground", "--preserve-status", "--verbose"}, 1),
    "gtimeout": ({"-s", "--signal", "-k", "--kill-after"}, {"-v", "--foreground", "--preserve-status"}, 1),
    "stdbuf": ({"-i", "-o", "-e"}, set(), 0),
    "caffeinate": ({"-t", "-w"}, {"-d", "-i", "-m", "-s", "-u"}, 0),
    "unbuffer": (set(), {"-p"}, 0),
    "chronic": (set(), {"-e", "-v"}, 0),
    "taskset": ({"-c"}, set(), 1),
    "flock": ({"-w", "-E", "--timeout", "--conflict-exit-code"}, {"-n", "-x", "-s", "-u", "-o"}, 1),
    "strace": ({"-o", "-e", "-p", "-s", "-u", "-E", "-S"}, {"-f", "-ff", "-c", "-t", "-tt", "-T", "-v", "-q", "-y"}, 0),
    "ltrace": ({"-o", "-e", "-p", "-s", "-u"}, {"-f", "-c", "-t", "-T"}, 0),
    "sandbox-exec": ({"-f", "-p", "-n", "-D"}, set(), 0),
    "arch": (set(), {"-arm64", "-x86_64", "-i386"}, 0),
    "time": ({"-f", "-o"}, {"-p", "-v", "-a"}, 0),
    "sshpass": ({"-p", "-f", "-d", "-P"}, {"-e"}, 0),
    "chroot": ({"--userspec", "--groups"}, set(), 1),
    "winpty": (set(), set(), 0),
    "rlwrap": ({"-f", "-H", "-s"}, set(), 0),
}

# <runner> <subcommand> [options] [--] command...   (run a command with extra environment or credentials)
RUN_PREFIXES = {
    ("poetry", "run"): 0, ("uv", "run"): 0, ("pipenv", "run"): 0, ("pdm", "run"): 0, ("rye", "run"): 0,
    ("hatch", "run"): 0, ("conda", "run"): 0, ("mamba", "run"): 0, ("micromamba", "run"): 0, ("direnv", "exec"): 1,
    ("op", "run"): 0, ("aws-vault", "exec"): 1, ("doppler", "run"): 0, ("infisical", "run"): 0, ("chamber", "exec"): 1,
    ("sops", "exec-env"): 1, ("sops", "exec-file"): 1, ("saml2aws", "exec"): 0, ("dotenv", "run"): 0, ("npx-run", ""): 0,
    ("pnpm", "exec"): 0, ("npm", "exec"): 0, ("yarn", "exec"): 0, ("bun", "x"): 0, ("npm", "x"): 0,
    ("pnpm", "dlx"): 0, ("yarn", "dlx"): 0, ("nix", "run"): 0, ("devbox", "run"): 0, ("mise", "exec"): 0, ("mise", "x"): 0,
    ("asdf", "exec"): 0, ("rtx", "exec"): 0, ("nvm", "exec"): 1, ("fnm", "exec"): 0, ("volta", "run"): 0,
}
_RUN_ARG_OPTS = {"--with", "--python", "-p", "--env-file", "-c", "--config", "--profile", "--project", "--directory", "-C",
                 "--package", "-w", "--workspace", "--filter", "-F", "--node", "--using", "-n", "--name", "--prefix",
                 "--call", "--cwd", "--dir", "-e", "--extra"}

NPX_LIKE = {"npx", "pnpx", "bunx"}
NPX_ARG_OPTS = {"-p", "--package", "-c", "--call", "--node-arg", "-n", "--prefix", "--cache", "--userconfig", "--registry"}

TRIVIAL = {"true", "false", ":", "test", "["}
SANDBOX_WRAPPERS = {"bwrap", "firejail", "proot", "systemd-run", "unshare", "nsenter", "sandbox-exec", "setpriv", "capsh", "flatpak-spawn", "toolbox",
                    "distrobox-enter", "runcon", "chrt", "taskset", "cpulimit", "daemonize", "start-stop-daemon", "launchctl-submit"}
INTERESTING_TOOLS = {"git", "gh", "hub", "glab", "terraform", "tofu", "terragrunt", "cdk", "pulumi", "kubectl", "helm", "aws", "gcloud", "az", "claude",
                     "npx", "npm", "pnpm", "yarn", "bun", "make", "just", "sh", "bash", "zsh", "dash", "python", "python3", "node", "docker", "ssh",
                     "loopctl", "sam", "serverless", "sls", "sst", "fly", "flyctl", "vercel", "netlify", "wrangler", "rm", "cp", "mv", "tee", "sed",
                     "curl", "wget", "perl", "ruby", "env", "sudo"}


def _options(args: Sequence[str], dyn: Sequence[bool], with_arg: Set[str], flags: Set[str], positional: int = 0
             ) -> Tuple[int, List[str]]:
    """Skip leading options. Returns (index of first non-option, short options seen)."""
    i, seen = 0, []  # type: Tuple[int, List[str]]
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        key = a.split("=", 1)[0]
        seen.append(key)
        if key in with_arg and "=" not in a:
            i += 2
            continue
        if key in with_arg or key in flags or a.startswith("--"):
            i += 1
            continue
        # bundled short options such as -dmS (screen) or -Nf: the last letter may take the next word as its value
        if len(a) > 2 and ("-" + a[-1]) in with_arg:
            i += 2
            continue
        i += 1
    i += positional if positional and i + positional <= n else 0
    return min(i, n), seen


class _Ctx(object):
    def __init__(self, ana: Analysis, base_env: Mapping[str, str], home: str) -> None:
        self.cwd_unknown = False
        self.ana = ana
        self.base_env = base_env
        self.home = home
        self.visited = set()   # type: Set[Tuple[str, str]]


def analyze(command: str, cwd: str, base_env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> Analysis:
    ana = Analysis()
    ctx = _Ctx(ana, base_env if base_env is not None else os.environ, home or paths.home_dir(base_env))
    from . import shellparse

    def reader(path: str) -> Optional[str]:
        if "\x00" in path:
            return None
        lp = paths.lexical(path, cwd, ctx.home)
        if lp in ana.vfs:
            return ana.vfs[lp]
        return paths.safe_read(lp, 64 * 1024)
    shellparse.set_file_reader(reader)
    try:
        _script(ctx, command, cwd, 0, "top", {})
    finally:
        shellparse.set_file_reader(None)
    return ana


def _script(ctx: _Ctx, text: str, cwd: str, depth: int, origin: str, env: Dict[str, str]) -> None:
    ana = ctx.ana
    try:
        sc = parse(text, ctx.base_env)
    except ParseLimit as exc:
        ana.problem("parse-limit", "cannot analyse the command: %s" % exc)
        return
    cur = cwd
    for cmd in sc.cmds:
        if cmd.kind in ("for", "test", "arith", "funcdef", "case") and not cmd.words and not cmd.assigns:
            continue
        cur = _emit(ctx, sc, cmd, cur, depth, cmd.origin if cmd.origin != "top" else origin, env)


def _emit(ctx: _Ctx, sc: Script, cmd: Cmd, cwd: str, depth: int, origin: str, outer_env: Dict[str, str]) -> str:
    ana = ctx.ana
    env = dict(outer_env)
    env_dyn = {}  # type: Dict[str, bool]
    for name, value, dyn in cmd.assigns:
        env[name] = value
        env_dyn[name] = dyn
    if not cmd.words:
        if cmd.assigns:
            inv = Inv(":assign", ":assign", [], [], env, cwd, cmd, origin, depth)
            inv.env_dyn = env_dyn
            ana.invs.append(inv)
        # a redirection with no command still writes its target
        elif cmd.redirs:
            inv = Inv(":redirect", ":redirect", [], [], env, cwd, cmd, origin, depth)
            ana.invs.append(inv)
        return cwd
    words = cmd.words
    first = words[0]
    name_raw = first.text
    args = [w.text for w in words[1:]]
    dyn = [w.dynamic for w in words[1:]]
    if first.maybe_empty and first.dynamic and len(words) > 1:
        # `$UNSET git push` runs `git push` when the variable is empty: judge that too
        w2 = words[1]
        _dispatch(ctx, sc, cmd, w2.text, args[1:], dyn[1:], env, env_dyn, cwd, depth, origin, [], w2.dynamic)
    cwd2 = cwd
    _dispatch(ctx, sc, cmd, name_raw, args, dyn, env, env_dyn, cwd, depth, origin, [], first.dynamic)
    _record_vfs(ctx, sc, cmd, cwd)
    low = _lc_base(name_raw)
    if low in ("cd", "pushd", "chdir", "popd"):
        opts = [a for a in args if not (a.startswith("-") and a != "-")]
        if low == "popd" or (opts and opts[0] == "-") or any(dyn):
            ctx.cwd_unknown = True
        elif opts:
            cwd2 = paths.lexical(opts[0], cwd, ctx.home)
        elif low == "cd":
            cwd2 = ctx.home
    return cwd2


def _record_vfs(ctx: _Ctx, sc: Script, cmd: Cmd, cwd: str) -> None:
    """`echo ... > f.sh; sh f.sh` in ONE command: the file does not exist yet when the hook runs, so remember what this
    command writes when the content is static, and let a later `sh f.sh` read it."""
    writes = [r for r in cmd.redirs if r.op in (">", ">>", ">|", "&>") and r.target is not None and not r.target.dynamic]
    if not writes or not cmd.words:
        return
    name = _lc_base(cmd.words[0].text)
    if name not in ("echo", "printf", "cat", "tee", ":", "true", "base64", "rev", "tr", "xxd"):
        return
    chain = []   # type: List[Cmd]
    idx = cmd.pipe_prev
    guard = 0
    while idx is not None and guard < 20:
        chain.append(sc.cmds[idx])
        idx = sc.cmds[idx].pipe_prev
        guard += 1
    chain.reverse()
    chain.append(cmd)
    value = static_eval(chain, allow_final_redirect=True)
    if value is None and name in ("cat", "tee") and cmd.stdin_text is not None:
        value = cmd.stdin_text
    if value is None:
        return
    for r in writes:
        p = paths.lexical(r.target.text, cwd, ctx.home)
        ctx.ana.vfs[p] = (ctx.ana.vfs.get(p, "") + value) if r.op == ">>" else value


def _dispatch(ctx: _Ctx, sc: Script, cmd: Cmd, name_raw: str, args: List[str], dyn: List[bool], env: Dict[str, str],
              env_dyn: Dict[str, bool], cwd: str, depth: int, origin: str, wrappers: List[str], name_dynamic: bool,
              guard: int = 0) -> None:
    """Resolve wrappers, then record the final invocation (and recurse into whatever it runs)."""
    ana = ctx.ana
    if guard > 14:
        ana.problem("parse-limit", "wrapper nesting is too deep to analyse")
        return
    name = _lc_base(name_raw) if not name_dynamic else name_raw
    if name_dynamic and NUL in name_raw:
        pure = name_raw.replace(NUL, "")
        if not pure:
            ana.problem("opaque-command", "the command name is computed at run time", "pure:" + origin)
        else:
            ana.problem("opaque-command", "the command name is partly computed at run time", "partial:" + origin)
        base = _lc_base(pure) if pure else ""
        name = base
    inv_env = dict(env)

    def again(n: str, a: List[str], d: List[bool], extra_env: Optional[Dict[str, str]] = None, wrapper: str = "",
              nd: bool = False, cwd_to: Optional[str] = None) -> None:
        e = dict(inv_env)
        if extra_env:
            e.update(extra_env)
        _dispatch(ctx, sc, cmd, n, a, d, e, env_dyn, cwd_to or cwd, depth, origin, wrappers + ([wrapper] if wrapper else []), nd,
                  guard + 1)

    def script(text: str, why: str, dynamic: bool = False, cwd_to: Optional[str] = None) -> None:
        if dynamic:
            ana.problem("opaque-eval", "%s runs a string computed at run time" % why)
        _script(ctx, text, cwd_to or cwd, depth + 1, why, dict(inv_env))

    inv = Inv(name, name_raw, args, dyn, inv_env, cwd, cmd, origin, depth, list(wrappers), name_dynamic)
    inv.env_dyn = env_dyn
    inv.cwd_unknown = ctx.cwd_unknown

    # ---- wrappers that run another program -------------------------------------------------------------
    if name in WRAPPERS and name_raw.rsplit("/", 1)[-1].lower() == name:
        with_arg, flags, pos = WRAPPERS[name]
        if name == "command" and any(a in ("-v", "-V") for a in args[:2]):
            ana.invs.append(inv)
            return
        i, _ = _options(args, dyn, with_arg, flags, pos)
        if name in ("nice",) and args and re.match(r"^-\d+$", args[0]):
            i = max(i, 1)
        if name in ("timeout", "gtimeout") and i == 0 and args:
            i = 1
        if i >= len(args):
            ana.invs.append(inv)
            return
        ana.invs.append(inv)
        again(args[i], args[i + 1:], dyn[i + 1:], wrapper=name, nd=dyn[i])
        return
    if name == "env":
        i, extra = 0, {}  # type: Tuple[int, Dict[str, str]]
        n = len(args)
        split_string = None  # type: Optional[str]
        env_cwd = None  # type: Optional[str]
        while i < n:
            a = args[i]
            if a == "--":
                i += 1
                break
            if a in ("-C", "--chdir") and i + 1 < n:
                if dyn[i + 1]:
                    ctx.cwd_unknown = True
                else:
                    env_cwd = paths.lexical(args[i + 1], cwd, ctx.home)
                i += 2
                continue
            if a.startswith("--chdir="):
                env_cwd = paths.lexical(a.split("=", 1)[1], cwd, ctx.home)
                i += 1
                continue
            if a in ("-u", "--unset") or a in ("-P", "-a"):
                i += 2
                continue
            if a in ("-S", "--split-string"):
                split_string = args[i + 1] if i + 1 < n else ""
                i = n
                break
            if a.startswith("-S") and len(a) > 2:
                split_string = a[2:]
                i = n
                break
            if a.startswith("--split-string="):
                split_string = a.split("=", 1)[1]
                i = n
                break
            if a.startswith("-") and a != "-":
                i += 1
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a, re.S)
            if m:
                extra[m.group(1)] = m.group(2)
                i += 1
                continue
            break
        e2 = dict(inv_env)
        e2.update(extra)
        inv.env = e2
        ana.invs.append(inv)
        if split_string is not None:
            _script(ctx, split_string + " " + " ".join(args[i:]) if i < n else split_string, env_cwd or cwd, depth + 1, "env-S", e2)
            return
        if i < n:
            again(args[i], args[i + 1:], dyn[i + 1:], extra, "env", dyn[i], cwd_to=env_cwd)
        return
    if name == "xargs":
        with_arg = {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--eof", "--max-args",
                    "--max-lines", "--max-procs", "--max-chars", "--replace"}
        flags = {"-0", "-o", "-p", "-r", "-t", "-x", "--null", "--open-tty", "--interactive", "--no-run-if-empty",
                 "--verbose", "--exit", "--show-limits", "-i", "-l"}
        i = 0
        n = len(args)
        while i < n:
            a = args[i]
            if a == "--":
                i += 1
                break
            if not a.startswith("-") or a == "-":
                break
            key = a.split("=", 1)[0]
            if key in with_arg and "=" not in a and len(key) == len(a):
                i += 2
            else:
                i += 1
        ana.invs.append(inv)
        repl = None   # type: Optional[str]
        for k, a in enumerate(args[:i]):
            if a == "-I" and k + 1 < i:
                repl = args[k + 1]
            elif a.startswith("-I") and len(a) > 2:
                repl = a[2:]
            elif a.startswith("--replace="):
                repl = a.split("=", 1)[1]
            elif a in ("-i", "--replace") or a.startswith("-i") and len(a) > 2:
                repl = "{}" if len(a) <= 2 or a.startswith("--") else a[2:]
        null_sep = any(a in ("-0", "--null") for a in args[:i])
        stdin_text, stdin_known = _stdin_text(ctx, sc, inv)
        if i < n:
            before = len(ana.invs)
            if stdin_text is not None:
                items = [x for x in (stdin_text.split("\0") if null_sep else (stdin_text.split("\n") if repl else stdin_text.split())) if x != ""][:20]
                if repl:
                    for item in items or [""]:
                        a2 = [x.replace(repl, item) for x in args[i + 1:]]
                        again(args[i].replace(repl, item), a2, [False] * len(a2), wrapper="xargs", nd=dyn[i])
                else:
                    again(args[i], args[i + 1:] + items, dyn[i + 1:] + [False] * len(items), wrapper="xargs", nd=dyn[i])
            else:
                tail, tail_dyn = ([], []) if repl else ([NUL], [True])
                again(args[i], args[i + 1:] + tail, dyn[i + 1:] + tail_dyn, wrapper="xargs", nd=dyn[i])
            for extra_inv in ana.invs[before:]:
                extra_inv.xargs = True
        else:
            again("echo", [NUL], [True], wrapper="xargs")
        return
    if name in ("watch",):
        i, _ = _options(args, dyn, {"-n", "--interval", "-d", "-w"}, {"-t", "-b", "-e", "-g", "-c", "-x", "--exec"})
        ana.invs.append(inv)
        if i < len(args):
            script(" ".join(args[i:]), "watch", any(dyn[i:]))
        return
    if name == "find":
        ana.invs.append(inv)
        i = 0
        while i < len(args):
            if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                body, bdyn = [], []  # type: List[str], List[bool]
                while j < len(args) and args[j] not in (";", "+", "\\;"):
                    body.append(args[j])
                    bdyn.append(dyn[j])
                    j += 1
                if body:
                    start = next((x for x in args if not x.startswith("-") and x not in ("(", ")", "!")), ".")
                    where = paths.lexical(start, cwd, ctx.home) if args[i] in ("-execdir", "-okdir") else None
                    again(body[0], body[1:], bdyn[1:], wrapper="find", nd=bdyn[0], cwd_to=where)
                i = j
            i += 1
        return
    if name == "parallel":
        ana.invs.append(inv)
        j = args.index(":::") if ":::" in args else len(args)
        i, _ = _options(args[:j], dyn[:j], {"-j", "-n", "-N", "-S", "--jobs", "-a", "--arg-file", "--sshlogin", "-I", "--colsep"}, set())
        if i < j:
            again(args[i], args[i + 1:j] + [NUL], dyn[i + 1:j] + [True], wrapper="parallel", nd=dyn[i])
        return
    if name == "script":
        ana.invs.append(inv)
        if "-c" in args and args.index("-c") + 1 < len(args):
            script(args[args.index("-c") + 1], "script -c", dyn[args.index("-c") + 1])
        else:
            i, _ = _options(args, dyn, {"-t", "-F"}, {"-q", "-a", "-e", "-f", "-k", "-r", "-p"}, 1)
            if i < len(args):
                again(args[i], args[i + 1:], dyn[i + 1:], wrapper="script", nd=dyn[i])
        return
    if name in ("ssh", "mosh", "autossh", "slogin", "rsh"):
        ana.invs.append(inv)
        i, _ = _options(args, dyn, {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-e", "-E", "-I", "-m", "-O", "-Q", "-S", "-W", "-w", "-B",
                                    "--ssh", "--server", "--predict"}, {"-A", "-a", "-C", "-f", "-g", "-K", "-k", "-M", "-N", "-n", "-q", "-s", "-T", "-t", "-V",
                                                                          "-v", "-X", "-x", "-Y", "-y", "-4", "-6"}, 1)
        if i < len(args):
            remote = " ".join(args[i:])
            script(remote, "ssh remote command", any(dyn[i:]))
        return
    if name in ("tmux", "screen", "byobu"):
        ana.invs.append(inv)
        _multiplexer(ctx, inv, script)
        return
    if name in ("at", "batch"):
        ana.invs.append(inv)
        text, known = _stdin_text(ctx, sc, inv)
        if text is not None:
            script(text, name + " job")
        elif not known:
            ana.problem("pipe-to-shell", "%s reads a job script from a pipe whose content is computed at run time" % name)
        return
    if name in SANDBOX_WRAPPERS:
        ana.invs.append(inv)
        for k, a in enumerate(args):
            if _lc_base(a) in INTERESTING_TOOLS:
                again(a, args[k + 1:], dyn[k + 1:], wrapper=name, nd=dyn[k])
                break
        return
    if name == "busybox" and args:
        ana.invs.append(inv)
        again(args[0], args[1:], dyn[1:], wrapper="busybox", nd=dyn[0])
        return
    pm_ix, pm_sub = _pm_sub(args) if name in ("npm", "pnpm", "yarn", "bun") else (0, "")
    if name in NPX_LIKE or (name == "npm" and pm_sub in ("exec", "x")) or \
            (name in ("pnpm", "yarn") and pm_sub in ("dlx", "exec")) or (name == "bun" and pm_sub == "x"):
        ana.invs.append(inv)
        rest = args[pm_ix + 1:] if name not in NPX_LIKE else args
        rdyn = dyn[pm_ix + 1:] if name not in NPX_LIKE else dyn
        i, seen = _options(rest, rdyn, NPX_ARG_OPTS, {"-y", "--yes", "--no", "--no-install", "--ignore-existing", "-q", "--quiet",
                                                       "--no-install", "--prefer-offline", "--legacy-peer-deps", "--silent"})
        call = None
        for k, o in enumerate(rest[:i]):
            if o in ("-c", "--call") and k + 1 < len(rest):
                call = rest[k + 1]
        inv.env = dict(inv_env)
        pfx = None   # type: Optional[str]
        for k, o in enumerate(args):
            if o in ("--prefix", "-C", "--cwd", "--dir") and k + 1 < len(args) and not dyn[k + 1]:
                pfx = paths.lexical(args[k + 1], cwd, ctx.home)
            elif o.startswith(("--prefix=", "--cwd=", "--dir=")):
                pfx = paths.lexical(o.split("=", 1)[1], cwd, ctx.home)
        if call is not None:
            script(call, "npx -c", cwd_to=pfx)
        elif i < len(rest):
            pkg = rest[i]
            inv.args = list(args)
            inv.wrappers = list(wrappers)
            tool = _npx_tool(pkg)
            ana.invs.append(Inv("npx:" + tool, pkg, rest[i + 1:], rdyn[i + 1:], dict(inv_env), cwd, cmd, origin, depth,
                                list(wrappers) + [name], rdyn[i]))
            again(tool, rest[i + 1:], rdyn[i + 1:], wrapper=name, nd=rdyn[i], cwd_to=pfx)
        return
    for (rn, rsub), skip in RUN_PREFIXES.items():
        if name == rn and rsub and args[:1] == [rsub]:
            ana.invs.append(inv)
            rest, rdyn = args[1:], dyn[1:]
            j = rest.index("--") if "--" in rest else -1
            if j >= 0:
                k = j + 1
            else:
                k, _ = _options(rest, rdyn, _RUN_ARG_OPTS, set())
                k = min(k + skip, len(rest))
            if k < len(rest):
                again(rest[k], rest[k + 1:], rdyn[k + 1:], wrapper=name + " " + rsub, nd=rdyn[k])
            return
    if name == "docker" or name == "podman" or name == "nerdctl":
        ana.invs.append(inv)
        _docker_inner(ctx, inv, again)
        return
    if name in ("su", "sudo", "doas", "pkexec", "runuser"):
        ana.invs.append(inv)
        return

    # ---- things that run strings or files ---------------------------------------------------------------
    if name in SHELLS or (name == "busybox"):
        ana.invs.append(inv)
        _shell(ctx, sc, inv, script)
        return
    if name == "eval":
        ana.invs.append(inv)
        script(" ".join(args), "eval", any(dyn))
        return
    if name == "trap":
        ana.invs.append(inv)
        if args and args[0] not in ("-", "-l", "-p", "--"):
            script(args[0], "trap", dyn[0])
        return
    if name in ("source", "."):
        ana.invs.append(inv)
        if not args:
            return
        if args[0].startswith("\x01PROCSUB:"):
            script(args[0][len("\x01PROCSUB:"):], "source <(...)")
        elif args[0] in STDIN_PATHS:
            text, known = _stdin_text(ctx, sc, inv)
            if text is not None:
                script(text, "source (stdin)")
            elif not known:
                ana.problem("pipe-to-shell", "source reads a script from a pipe whose content is computed at run time")
        elif dyn[0]:
            ana.problem("opaque-eval", "source runs a file whose name is computed at run time")
        else:
            _script_file(ctx, args[0], cwd, depth, "source", inv, script)
        return
    m = INTERPRETER_RE.match(name)
    if m and name_raw.rsplit("/", 1)[-1].lower() == name:
        ana.invs.append(inv)
        _interpreter(ctx, sc, inv, script)
        return
    if name in ("npm", "pnpm", "yarn", "bun"):
        ana.invs.append(inv)
        _node_runner(ctx, inv, depth, wrappers, again)
        return
    if name in ("make", "gmake"):
        ana.invs.append(inv)
        _make(ctx, sc, inv, depth)
        return
    if name == "just":
        ana.invs.append(inv)
        _just(ctx, inv, depth)
        return
    if "/" in name_raw and not name_dynamic and "\x00" not in name_raw:
        ana.invs.append(inv)
        _exec_path(ctx, inv, script)
        return
    if name in ("turbo", "nx", "lerna", "rush", "wireit") and args:
        ana.invs.append(inv)
        m2 = None
        for k, a in enumerate(args[:-0 or None]):
            if a in ("run", "run-many", "run-script") and k + 1 < len(args):
                m2 = args[k + 1:]
                break
        names = []  # type: List[str]
        if m2:
            names = [x.split(":")[-1] if ":" in x and name == "nx" else x for x in m2 if not x.startswith("-")][:3]
        if name == "nx":
            for k, a in enumerate(args):
                if a in ("-t", "--target", "--targets") and k + 1 < len(args):
                    names.extend(args[k + 1].split(","))
        if name == "turbo" and args[0] not in ("run", "prune", "login", "link", "unlink", "daemon", "gen", "info"):
            names = [x for x in args if not x.startswith("-")][:2]
        _expand_scripts_everywhere(ctx, cwd, names, depth, "%s:%%s" % name)
        return
    ana.invs.append(inv)


def _exec_path(ctx: _Ctx, inv: Inv, script) -> None:
    """`./scripts/deploy.sh` or `/tmp/run.sh` run directly: follow the file like `sh file` (or the interpreter on its shebang line)."""
    text = _read_script(ctx, inv.raw, inv.cwd)
    if text is None or "\x00" in text[:2000]:
        return
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        m = re.search(r"(python|node|ruby|perl|php)", first)
        if m and not re.search(r"\b(ba|z|da|k)?sh\b", first):
            ctx.ana.scripts.append(ScriptFile(m.group(1), paths.lexical(inv.raw, inv.cwd, ctx.home), inv))
            return
    key = (paths.lexical(inv.raw, inv.cwd, ctx.home), "exec")
    if key in ctx.visited or inv.depth > MAX_EXPANSION_DEPTH:
        return
    ctx.visited.add(key)
    script(text, "script " + os.path.basename(inv.raw))


def _pm_sub(args: Sequence[str]) -> Tuple[int, str]:
    """(index, name) of a package manager's subcommand, after its global options (`npm --prefix x exec`, `pnpm --filter a exec`)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if (a in _PM_ARG_OPTS and "=" not in a) else 1
            continue
        return i, a
    return 0, ""


def _npx_tool(pkg: str) -> str:
    p = pkg
    if p.startswith("@") and p.count("@") > 1:
        p = p[:p.rindex("@")]
    elif not p.startswith("@") and "@" in p:
        p = p.split("@", 1)[0]
    return p.rsplit("/", 1)[-1].lower() if not p.startswith("@") else p.lower()


# ---------------------------------------------------------------------------------------------------------------
# shells, interpreters, script files

def _read_script(ctx: _Ctx, path: str, cwd: str) -> Optional[str]:
    p = paths.lexical(path, cwd, ctx.home)
    if p in ctx.ana.vfs:
        return ctx.ana.vfs[p]
    text = paths.safe_read(p, 256 * 1024)
    if text is None:
        text = paths.safe_read(paths.physical(path, cwd, ctx.home), 256 * 1024)
    return text


def _script_file(ctx: _Ctx, path: str, cwd: str, depth: int, why: str, inv: Inv, script) -> None:
    text = _read_script(ctx, path, cwd)
    if text is None:
        return
    key = (paths.physical(path, cwd, ctx.home), why)
    if key in ctx.visited:
        return
    ctx.visited.add(key)
    if depth > MAX_EXPANSION_DEPTH:
        ctx.ana.problem("parse-limit", "scripts call scripts too deeply to analyse")
        return
    if text.startswith("#!") and re.match(r"#!\s*(/usr/bin/env\s+)?\S*(python|node|ruby|perl|php)", text.split("\n", 1)[0]):
        lang = re.search(r"(python|node|ruby|perl|php)", text.split("\n", 1)[0]).group(1)
        ctx.ana.scripts.append(ScriptFile(lang, paths.physical(path, cwd, ctx.home), inv))
        return
    script(text, "script " + os.path.basename(path))


def _stdin_text(ctx: _Ctx, sc: Script, inv: Inv) -> Tuple[Optional[str], bool]:
    """Text that will be read from stdin: (text, known). `known` False means it is computed at run time."""
    cmd = inv.cmd
    if cmd is None:
        return None, True
    if cmd.stdin_text is not None:
        return cmd.stdin_text, True
    for r in cmd.redirs:
        if r.op == "<" and r.target is not None:
            t = r.target.text
            if t.startswith("\x01PROCSUB:"):
                return t[len("\x01PROCSUB:"):], True
            if r.target.dynamic:
                return None, False
            f = _read_script(ctx, t, inv.cwd)
            return (f, True) if f is not None else (None, True)
    if cmd.pipe_prev is not None:
        chain = []  # type: List[Cmd]
        idx = cmd.pipe_prev
        guard = 0
        while idx is not None and guard < 20:
            c = sc.cmds[idx]
            chain.append(c)
            idx = c.pipe_prev
            guard += 1
        chain.reverse()
        val = static_eval(chain)
        if val is not None:
            return val, True
        return None, False
    return None, True


def _shell(ctx: _Ctx, sc: Script, inv: Inv, script) -> None:
    args, dyn = inv.args, inv.dyn
    if inv.name == "busybox":
        return
    i, n = 0, len(args)
    c_flag = s_flag = False
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in ("--rcfile", "--init-file"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if a.startswith(("-", "+")) and len(a) > 1:
            letters = a[1:]
            if a.startswith("-") and "c" in letters:
                c_flag = True
            if a.startswith("-") and "s" in letters:
                s_flag = True
            i += 1 + sum(1 for ch in letters if ch in "oO")
            continue
        break
    rest, rdyn = args[i:], dyn[i:]
    if s_flag and not c_flag:
        rest, rdyn = [], []          # `bash -s -- args`: the script comes from stdin, the rest are its arguments
    if c_flag:
        if rest:
            script(rest[0], inv.name + " -c", rdyn[0])
        return
    if rest:
        if rest[0].startswith("\x01PROCSUB:"):
            script(rest[0][len("\x01PROCSUB:"):], inv.name + " <(...)")
        elif rest[0] in STDIN_PATHS:
            text, known = _stdin_text(ctx, sc, inv)
            if text is not None:
                script(text, inv.name + " (stdin)")
            elif not known:
                ctx.ana.problem("pipe-to-shell", "%s reads a script from a pipe whose content is computed at run time" % inv.name)
        elif rdyn[0]:
            ctx.ana.problem("opaque-eval", "%s runs a script file whose name is computed at run time" % inv.name)
        else:
            _script_file(ctx, rest[0], inv.cwd, inv.depth, inv.name, inv, script)
        return
    text, known = _stdin_text(ctx, sc, inv)
    if text is not None:
        script(text, inv.name + " (stdin)")
    elif not known:
        ctx.ana.problem("pipe-to-shell", "%s reads a script from a pipe whose content is computed at run time" % inv.name)


_PY_ARG = {"-W", "-X", "-Q", "--check-hash-based-pycs"}


def _interpreter(ctx: _Ctx, sc: Script, inv: Inv, script) -> None:
    name, args, dyn = inv.name, inv.args, inv.dyn
    lang = re.sub(r"[0-9.]+$", "", name)
    lang = {"nodejs": "node", "pypy": "python", "perl": "perl", "rscript": "r", "luajit": "lua"}.get(lang, lang)
    ana = ctx.ana
    i, n = 0, len(args)
    codes = []   # type: List[Tuple[str, bool]]
    module = None
    if lang == "deno" and args[:1] == ["eval"]:
        if len(args) > 1:
            ana.inline.append(InlineCode("node", args[1], inv, dyn[1]))
        return
    if lang == "bun" and args[:1] in (["run"], ["x"]):
        return  # handled by the node runner
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-":
            break
        if not a.startswith("-"):
            break
        if lang == "python":
            letters = a[1:]
            if a.startswith("--"):
                i += 1
                continue
            if "m" in letters and letters.index("m") == len(letters) - 1 and i + 1 < n:
                module = args[i + 1]
                i += 2
                break
            if "c" in letters and i + 1 < n and letters.index("c") == len(letters) - 1:
                codes.append((args[i + 1], dyn[i + 1]))
                i += 2
                break
            i += 1 + (1 if a in _PY_ARG else 0)
            continue
        if a in ("-e", "--eval", "-E", "-p", "--print", "-r") and lang in ("node", "ruby", "perl", "php", "bun", "lua", "osascript", "r", "tclsh"):
            if a in ("-r", "--require") and lang == "node":
                i += 2
                continue
            if a == "-r" and lang == "ruby":
                i += 2
                continue
            if i + 1 < n:
                codes.append((args[i + 1], dyn[i + 1]))
            i += 2
            continue
        if lang == "perl" and re.match(r"^-[a-zA-Z]*[eE]$", a) and i + 1 < n:
            codes.append((args[i + 1], dyn[i + 1]))
            i += 2
            continue
        if lang == "node" and a in ("-pe", "-ep") and i + 1 < n:
            codes.append((args[i + 1], dyn[i + 1]))
            i += 2
            continue
        if a in ("-M", "-I", "-m", "-C", "-x", "--import", "--loader", "--require") and lang in ("perl", "ruby", "node", "php"):
            i += 2
            continue
        if lang == "ruby" and a in ("-I", "-r"):
            i += 2
            continue
        i += 1
    extra = " ".join(args)
    piped, _known = _stdin_text(ctx, sc, inv)
    if piped:
        extra += " " + piped
    for code, d in codes:
        ana.inline.append(InlineCode(lang, code, inv, d, extra))
    if codes:
        return
    if module is not None:
        rest = args[i:]
        if module == "pip" or module == "pip3":
            ana.invs.append(Inv("pip", module, rest, dyn[i:], dict(inv.env), inv.cwd, inv.cmd, inv.origin, inv.depth,
                                inv.wrappers + [inv.name]))
        elif module in ("unittest", "pytest", "json.tool", "http.server", "venv", "compileall", "py_compile", "ensurepip",
                        "pyflakes", "black", "ruff", "mypy", "flake8", "isort", "coverage", "tox", "nox", "build"):
            pass
        else:
            ana.invs.append(Inv("py-module:" + module, module, rest, dyn[i:], dict(inv.env), inv.cwd, inv.cmd,
                                inv.origin, inv.depth, inv.wrappers + [inv.name]))
        return
    rest, rdyn = args[i:], dyn[i:]
    if rest and rest[0] != "-":
        if rdyn[0]:
            ana.problem("opaque-eval", "%s runs a file whose name is computed at run time" % name)
            return
        p = paths.lexical(rest[0], inv.cwd, ctx.home)
        ana.scripts.append(ScriptFile(lang, p, inv))
        if lang in ("sh",):
            pass
        return
    # python -  /  node  with a heredoc or here-string
    text = inv.stdin_text
    if text is None:
        for r in inv.redirs:
            if r.op == "<" and r.target is not None and not r.target.dynamic and not r.target.text.startswith("\x01"):
                f = _read_script(ctx, r.target.text, inv.cwd)
                if f is not None:
                    text = f
    if text is not None:
        ana.inline.append(InlineCode(lang, text, inv, False, " ".join(args)))


# ---------------------------------------------------------------------------------------------------------------
# docker

_TMUX_RUNNERS = {"new-session", "new", "new-window", "neww", "split-window", "splitw", "send-keys", "send", "run-shell", "run", "respawn-pane", "respawnp",
                 "respawn-window", "respawnw", "display-popup", "popup", "if-shell", "if", "pipe-pane", "pipep", "command-prompt"}


def _multiplexer(ctx: _Ctx, inv: Inv, script) -> None:
    """tmux and screen run strings and programs: judge them like `sh -c`."""
    args, dyn = inv.args, inv.dyn
    if inv.name == "screen":
        i, _ = _options(args, dyn, {"-S", "-s", "-t", "-c", "-p", "-T", "-e", "-h", "-L", "-Logfile", "-X"},
                        {"-d", "-m", "-D", "-R", "-r", "-A", "-l", "-ls", "-list", "-v", "-q", "-U", "-x", "-dm"})
        if i < len(args):
            script(" ".join(args[i:]), "screen command", any(dyn[i:]))
        return
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub not in _TMUX_RUNNERS:
        return
    k = args.index(sub)
    skip = False
    pieces = []   # type: List[str]
    for a in args[k + 1:]:
        if skip:
            skip = False
            continue
        if a in ("-t", "-s", "-n", "-c", "-x", "-y", "-e", "-F", "-l", "-p", "-f"):
            skip = True
            continue
        if a.startswith("-") or a in ("Enter", "C-m", "C-j", "Space", "Tab"):
            continue
        pieces.append(a)
    if pieces:
        script("\n".join(pieces) if sub in ("send-keys", "send") else " ".join(pieces), "tmux " + sub)


_DOCKER_RUN_ARG = {"-e", "--env", "-v", "--volume", "-p", "--publish", "-w", "--workdir", "-u", "--user", "--name", "--network",
                   "--net", "--entrypoint", "-h", "--hostname", "--env-file", "--label", "-l", "--mount", "--cap-add",
                   "--cap-drop", "--device", "--security-opt", "--memory", "-m", "--cpus", "--platform", "--restart",
                   "--add-host", "--dns", "--pid", "--ipc", "--shm-size", "--tmpfs", "--log-driver", "--user-ns",
                   "--group-add", "-a", "--attach", "--pull", "--runtime", "--ulimit", "--volumes-from", "--cgroupns",
                   "--cidfile", "--detach-keys", "--expose", "--gpus", "--health-cmd", "--stop-signal", "--sysctl", "-c",
                   "--cpu-shares", "--init-path"}


def _docker_inner(ctx: _Ctx, inv: Inv, again) -> None:
    args, dyn = inv.args, inv.dyn
    i, n = 0, len(args)
    while i < n and args[i].startswith("-"):
        if args[i] in ("-H", "--host", "--context", "-c", "--config", "--log-level", "-l"):
            i += 2
        else:
            i += 1
    if i >= n:
        return
    sub = args[i]
    rest, rdyn = args[i + 1:], dyn[i + 1:]
    if sub == "compose" or sub in ("buildx",):
        return
    if sub in ("run", "create", "container") and not (sub == "container" and (not rest or rest[0] not in ("run", "exec"))):
        if sub == "container":
            sub, rest, rdyn = rest[0], rest[1:], rdyn[1:]
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            key = rest[j].split("=", 1)[0]
            j += 2 if (key in _DOCKER_RUN_ARG and "=" not in rest[j]) else 1
        if j < len(rest):
            image = rest[j]
            cmd_args = rest[j + 1:]
            tool = _image_tool(image)
            if tool:
                again(tool, cmd_args, rdyn[j + 1:], wrapper="docker " + sub)
            elif cmd_args:
                again(cmd_args[0], cmd_args[1:], rdyn[j + 2:], wrapper="docker " + sub, nd=rdyn[j + 1])
    elif sub == "exec":
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            key = rest[j].split("=", 1)[0]
            j += 2 if key in ("-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys") and "=" not in rest[j] else 1
        if j + 1 < len(rest):
            again(rest[j + 1], rest[j + 2:], rdyn[j + 2:], wrapper="docker exec", nd=rdyn[j + 1])


_IMAGE_TOOLS = {"hashicorp/terraform": "terraform", "terraform": "terraform", "opentofu/opentofu": "tofu",
                "ghcr.io/opentofu/opentofu": "tofu", "amazon/aws-cli": "aws", "bitnami/kubectl": "kubectl",
                "alpine/helm": "helm", "dtzar/helm-kubectl": "kubectl", "pulumi/pulumi": "pulumi",
                "google/cloud-sdk": "gcloud", "gcr.io/google.com/cloudsdktool/cloud-sdk": "gcloud",
                "mcr.microsoft.com/azure-cli": "az", "amazon/aws-cdk": "cdk", "hashicorp/packer": "packer"}


def _image_tool(image: str) -> Optional[str]:
    img = image.split("@", 1)[0]
    base = img.rsplit(":", 1)[0] if ":" in img.rsplit("/", 1)[-1] else img
    return _IMAGE_TOOLS.get(base.lower())


# ---------------------------------------------------------------------------------------------------------------
# script runners: npm|pnpm|yarn|bun, make, just, turbo|nx

_NPM_RUN = {"run", "run-script", "rum", "urn"}
_NPM_SHORT = {"test": "test", "t": "test", "tst": "test", "start": "start", "stop": "stop", "restart": "restart"}
_NPM_INSTALL = {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall", "add", "ci",
                "clean-install", "ic", "install-clean", "update", "up", "upgrade", "udpate", "rebuild", "rb", "link", "ln",
                "dedupe", "ddp", "install-test", "it", "install-ci-test", "cit", "prune", "init"}
_NPM_PUBLISH = {"publish", "pack", "version"}
_BUILTINS = {
    "yarn": {"add", "audit", "autoclean", "bin", "cache", "check", "config", "create", "dedupe", "dlx", "exec", "explain",
             "generate-lock-entry", "global", "help", "import", "info", "init", "install", "licenses", "link", "list",
             "login", "logout", "node", "npm", "outdated", "owner", "pack", "patch", "plugin", "rebuild", "remove", "run",
             "set", "tag", "team", "unlink", "unplug", "up", "upgrade", "upgrade-interactive", "version", "versions", "why",
             "workspace", "workspaces", "constraints", "publish", "test", "start"},
    "pnpm": {"add", "i", "install", "update", "up", "upgrade", "remove", "rm", "uninstall", "un", "run", "test", "t", "start",
             "exec", "dlx", "create", "init", "link", "ln", "list", "ls", "outdated", "prune", "publish", "pack", "rebuild",
             "rb", "store", "why", "audit", "fetch", "patch", "patch-commit", "env", "config", "c", "root", "bin", "setup",
             "deploy", "import", "install-test", "it", "licenses", "doctor", "dedupe", "unlink", "self-update", "server",
             "stop", "restart", "recursive", "multi", "m", "-r"},
    "bun": {"run", "x", "install", "i", "add", "a", "remove", "rm", "update", "link", "unlink", "pm", "build", "test", "init",
            "create", "c", "upgrade", "publish", "patch", "repl", "exec", "outdated", "info", "why", "audit", "completions"},
}
_PM_ARG_OPTS = {"--prefix", "-C", "--cwd", "--dir", "--workspace", "-w", "--filter", "-F", "--registry", "--userconfig", "--cache",
                "--loglevel", "--tag", "--otp", "--config", "--script-shell", "--workspace-root", "--filter-prod"}


def _node_runner(ctx: _Ctx, inv: Inv, depth: int, wrappers: List[str], again) -> None:
    pm, args, dyn = inv.name, inv.args, inv.dyn
    prefix = None   # type: Optional[str]
    recursive = False
    i, n = 0, len(args)
    ignore_scripts = False
    while i < n:
        a = args[i]
        key = a.split("=", 1)[0]
        if a == "--":
            break
        if key in ("--ignore-scripts",) and a.lower() not in ("--ignore-scripts=false",):
            ignore_scripts = True
        if key in ("-r", "--recursive", "--workspaces", "-ws", "--workspaces=true"):
            recursive = True
        if key in ("--prefix", "-C", "--cwd", "--dir") and not dyn[i]:
            prefix = args[i + 1] if ("=" not in a and i + 1 < n) else a.split("=", 1)[-1]
        if key in ("--filter", "-F", "--workspace", "-w"):
            recursive = True
        if a.startswith("-"):
            i += 2 if (key in _PM_ARG_OPTS and "=" not in a) else 1
            continue
        break
    if i >= n:
        return
    sub = args[i]
    rest = args[i + 1:]
    if any(x.lower() == "--ignore-scripts" for x in args):
        ignore_scripts = True
    if ctx.ana is not None and (inv.env.get("npm_config_ignore_scripts", "").lower() == "true"
                                or inv.env.get("NPM_CONFIG_IGNORE_SCRIPTS", "").lower() == "true"):
        ignore_scripts = True
    base_dir = paths.lexical(prefix, inv.cwd, ctx.home) if prefix else inv.cwd
    script_names = []   # type: List[str]
    maybe_bin = None    # type: Optional[str]
    if sub in _NPM_RUN or (pm != "npm" and sub == "run"):
        r = [x for x in rest if not x.startswith("-")]
        if r:
            script_names.append(r[0])
        else:
            return
    elif sub in _NPM_SHORT:
        script_names.append(_NPM_SHORT[sub])
    elif sub in _NPM_INSTALL or (pm in _BUILTINS and sub in ("install", "i", "add", "rebuild")):
        if not ignore_scripts:
            script_names.extend(["preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare"])
    elif sub in _NPM_PUBLISH:
        if not ignore_scripts:
            if sub == "version":
                script_names.extend(["preversion", "version", "postversion"])
            else:
                script_names.extend(["prepare", "prepublishOnly", "prepack", "postpack", "publish", "postpublish"])
    elif pm in _BUILTINS and sub not in _BUILTINS[pm]:
        script_names.append(sub)
        maybe_bin = sub
    if not script_names:
        return
    names = []  # type: List[str]
    for s in script_names:
        for cand in ("pre" + s, s, "post" + s):
            if cand not in names:
                names.append(cand)
    pkg_dirs = _package_dirs(base_dir, recursive)
    found_script = False
    for d in pkg_dirs:
        scripts = _read_package_scripts(d)
        for s in names:
            body = scripts.get(s)
            if not isinstance(body, str):
                continue
            if s == maybe_bin:
                found_script = True
            key = (os.path.join(d, "package.json"), s)
            if key in ctx.visited:
                continue
            ctx.visited.add(key)
            if depth + 1 > MAX_EXPANSION_DEPTH:
                ctx.ana.problem("parse-limit", "package scripts nest too deeply to analyse")
                return
            env = dict(inv.env)
            env["npm_lifecycle_event"] = s
            _script(ctx, body, d, depth + 1, "npm:" + s, env)
    if maybe_bin is not None and not found_script:
        # `yarn cdk deploy`, `pnpm tsc`, `bun vitest`: no such script, so the package manager runs the binary
        again(maybe_bin, rest, dyn[i + 1:], wrapper=pm)


def _package_dirs(base: str, recursive: bool) -> List[str]:
    out = []  # type: List[str]
    p = os.path.normpath(base)
    while True:
        if os.path.isfile(os.path.join(p, "package.json")):
            out.append(p)
            break
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if recursive:
        root = out[0] if out else os.path.normpath(base)
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", ".claude", "dist", "build", ".next")]
            if dirpath.count(os.sep) - root.count(os.sep) > 3:
                dirnames[:] = []
                continue
            if "package.json" in filenames and dirpath not in out:
                out.append(dirpath)
                count += 1
                if count >= MAX_PKG_FILES:
                    break
    return out


def _read_package_scripts(d: str) -> Dict[str, Any]:
    text = paths.safe_read(os.path.join(d, "package.json"), 512 * 1024)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    s = data.get("scripts") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


def _expand_scripts_everywhere(ctx: _Ctx, cwd: str, names: List[str], depth: int, origin_fmt: str) -> None:
    for d in _package_dirs(cwd, True):
        scripts = _read_package_scripts(d)
        for s in names:
            body = scripts.get(s)
            if isinstance(body, str) and (os.path.join(d, "package.json"), s) not in ctx.visited:
                ctx.visited.add((os.path.join(d, "package.json"), s))
                if depth + 1 <= MAX_EXPANSION_DEPTH:
                    _script(ctx, body, d, depth + 1, origin_fmt % s, {})


# -- make ----------------------------------------------------------------------------------------------------------

def _make_vars(text: str) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(.*)$", text, re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


def _make_shell(line: str, vars_: Dict[str, str]) -> str:
    for _ in range(3):
        line = re.sub(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]", lambda m: vars_.get(m.group(1), "$__MK_" + m.group(1)), line)
    line = line.replace("$$", "\x03")
    line = re.sub(r"\$[@<^*?%+|]", "$__MK_auto", line)
    line = re.sub(r"\$\(([^()]*)\)", lambda m: "$__MK_fn", line)
    return line.replace("\x03", "$")


def _make(ctx: _Ctx, sc: Script, inv: Inv, depth: int) -> None:
    args, dyn = inv.args, inv.dyn
    directory, mfile = inv.cwd, None
    targets = []   # type: List[str]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--directory") and i + 1 < len(args):
            directory = paths.lexical(args[i + 1], inv.cwd, ctx.home)
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            directory = paths.lexical(a[2:], inv.cwd, ctx.home)
        elif a in ("-f", "--file", "--makefile") and i + 1 < len(args):
            mfile = args[i + 1]
            i += 2
            continue
        elif a.startswith("--directory="):
            directory = paths.lexical(a.split("=", 1)[1], inv.cwd, ctx.home)
        elif a.startswith("--file="):
            mfile = a.split("=", 1)[1]
        elif a.startswith("-") or "=" in a:
            pass
        else:
            targets.append(a)
        i += 1
    cands = [mfile] if mfile else ["GNUmakefile", "makefile", "Makefile"]
    text = None
    for c in cands:
        if c in STDIN_PATHS:
            text, _known = _stdin_text(ctx, sc, inv)
        elif c.startswith("\x01PROCSUB:"):
            text = c[len("\x01PROCSUB:"):]
        else:
            text = _read_script(ctx, c, directory)
        if text is not None:
            break
    if text is None:
        return
    for _inc in range(2):
        def _include(m):
            return paths.safe_read(paths.lexical(m.group(1).strip().split()[0], directory, ctx.home), 256 * 1024) or ""
        text = re.sub(r"^-?include\s+(.+)$", _include, text, flags=re.M)
    text = text.replace("\\\n", " ")
    vars_ = _make_vars(text)
    recipes = {}   # type: Dict[str, List[str]]
    deps = {}      # type: Dict[str, List[str]]
    order = []     # type: List[str]
    cur = []       # type: List[str]
    for line in text.split("\n"):
        m = re.match(r"^([A-Za-z0-9_./%-][A-Za-z0-9_./% -]*?)\s*::?\s*([^=\n#]*)(?:#.*)?$", line)
        if m and not line.startswith("\t") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:?+]?=", line):
            cur = [t for t in m.group(1).split() if t]
            for t in cur:
                recipes.setdefault(t, [])
                deps.setdefault(t, []).extend(m.group(2).split())
                if t not in order and not t.startswith("."):
                    order.append(t)
            continue
        if line.startswith("\t") and cur:
            for t in cur:
                recipes[t].append(line[1:])
            continue
        if line.strip() == "":
            continue
        if not line.startswith("\t"):
            cur = [] if not line.startswith("#") else cur
    if not targets and order:
        targets = [order[0]]
    seen = set()  # type: Set[str]
    stack = list(targets)
    while stack:
        t = stack.pop()
        if t in seen or t not in recipes:
            continue
        seen.add(t)
        stack.extend(deps.get(t, []))
        for line in recipes[t]:
            body = _make_shell(re.sub(r"^[@\-+]+", "", line.strip()), vars_)
            if body.strip() and (os.path.join(directory, "Makefile"), t + body) not in ctx.visited:
                ctx.visited.add((os.path.join(directory, "Makefile"), t + body))
                if depth + 1 <= MAX_EXPANSION_DEPTH:
                    _script(ctx, body, directory, depth + 1, "make:" + t, dict(inv.env))


# -- just ----------------------------------------------------------------------------------------------------------

def _just(ctx: _Ctx, inv: Inv, depth: int) -> None:
    args = [a for a in inv.args if not a.startswith("-")]
    directory = inv.cwd
    text = None
    for c in ("justfile", "Justfile", ".justfile"):
        text = paths.safe_read(os.path.join(directory, c), 512 * 1024)
        if text is not None:
            break
    if text is None:
        return
    recipes = {}   # type: Dict[str, List[str]]
    order = []     # type: List[str]
    cur = None     # type: Optional[str]
    for line in text.split("\n"):
        m = re.match(r"^@?([A-Za-z_][A-Za-z0-9_-]*)[^:=\n]*:(?!=)\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            cur = m.group(1)
            recipes[cur] = []
            order.append(cur)
            for d in m.group(2).split():
                recipes[cur].append("__dep:" + d)
            continue
        if line.startswith((" ", "\t")) and cur:
            recipes[cur].append(line.strip())
        elif line.strip() and not line.startswith("#"):
            cur = None
    targets = [a for a in args if a in recipes] or ([order[0]] if order else [])
    seen = set()  # type: Set[str]
    stack = list(targets)
    while stack:
        t = stack.pop()
        if t in seen or t not in recipes:
            continue
        seen.add(t)
        for line in recipes[t]:
            if line.startswith("__dep:"):
                stack.append(line[6:])
                continue
            body = re.sub(r"\{\{[^}]*\}\}", "$__JUST", line.lstrip("@-"))
            if body.startswith("#!"):
                continue
            if depth + 1 <= MAX_EXPANSION_DEPTH:
                _script(ctx, body, directory, depth + 1, "just:" + t, dict(inv.env))
