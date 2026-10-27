"""guard-git v2 (report §7.1, §5.9, §5.7, §19.7; ports every rule of the v1 guard-git.sh).

Applies to every `git`, `gh`, `hub` and `glab` invocation that the shell parser finds, however it was
wrapped, quoted, chained or nested.

Rights by caller (the registered `session_id` and `agent_id`, §5.7):

    tier 0  read-only git      engineers, critics, unknown subagents, and an unregistered main session while
                               `git_autonomy` is `none`
    tier 1  + stage, branch    an unregistered main session with `git_autonomy: branch`
    tier 2  + commit, merge    an unregistered main session with `git_autonomy: branch+commit` (merges only into
                               a non-protected branch)
    tier 3  conductor          the registered conductor session: also reset/clean in a loop worktree, branch
                               delete on its own branches, tags, merge to the local protected branch under
                               `delivery: local`, and push per `delivery` and the refspec allowlist

Always denied, for every caller (rule ids are `guard-git:<name>`):
    --no-verify / -n; -c core.* / alias.* (and every other -c key outside a short harmless list); git config
    writes; GIT_* environment assignments; update-ref, filter-branch, replace, tag -f; rebase; push --force,
    --mirror, --delete, tag pushes and refspecs outside feat/*; `gh api` with any method other than GET;
    gh release | secret | workflow run | repo edit | ruleset | auth; `gh pr merge`.

An unresolvable git invocation (a dynamic subcommand, an unknown subcommand, an alias the guard cannot read)
is denied: the typo guard fails closed.
"""
from __future__ import annotations

import re
import shlex
from typing import Dict, List, Optional, Sequence, Tuple

from . import invoke, paths
from .invoke import Inv
from .lib import Ctx, Decision, deny

G = "guard-git"

READ_SUBS = {
    "status", "diff", "log", "show", "rev-parse", "rev-list", "describe", "blame", "annotate", "shortlog", "ls-files",
    "ls-tree", "cat-file", "merge-base", "name-rev", "for-each-ref", "show-ref", "grep", "diff-tree", "diff-index",
    "diff-files", "count-objects", "check-ignore", "check-attr", "whatchanged", "range-diff", "cherry", "version",
    "help", "show-branch", "verify-commit", "verify-tag", "verify-pack", "var", "request-pull", "fsck", "get-tar-commit-id",
    "ls-remote-local", "stripspace", "interpret-trailers", "mailinfo", "column", "check-ref-format", "check-mailmap",
    "patch-id", "diff-pairs", "rev-diff", "fast-export", "bugreport", "difftool-helper", "unpack-file-read", "multi-pack-index-read",
    "fmt-merge-msg", "mailsplit-read", "show-index",
}
KNOWN_CONDUCTOR = {
    "add", "rm", "mv", "restore", "stash", "clean", "checkout", "switch", "reset", "apply", "sparse-checkout", "commit", "merge",
    "cherry-pick", "revert", "am", "pull", "fetch", "ls-remote", "tag", "branch", "worktree", "bisect", "gc", "hash-object",
    "commit-tree", "write-tree", "read-tree", "update-index", "checkout-index", "mktree", "mktag", "format-patch", "bundle",
    "archive", "notes", "submodule", "lfs", "clone", "init", "merge-file", "remote", "config", "symbolic-ref", "reflog",
    "push", "rebase", "update-ref", "filter-branch", "replace", "pack-objects", "index-pack", "unpack-objects", "repack",
    "prune", "prune-packed", "maintenance", "credential", "rerere", "mergetool", "difftool", "gui", "citool", "gitk",
    "daemon", "instaweb", "send-email", "svn", "p4", "fast-import", "filter-repo", "receive-pack", "upload-pack",
    "upload-archive", "shell", "remote-ext", "update-server-info", "pack-refs", "cvsimport", "cvsexportcommit",
    "quiltimport", "imap-send", "scalar", "backfill", "refs", "history", "hook", "sh-setup", "web--browse",
    "mergetool--lib", "multi-pack-index", "commit-graph", "pack-redundant", "unpack-file", "whatchanged",
}

ALWAYS_DENIED = {
    "update-ref": "update-ref moves refs directly (loopctl does it from its own process, outside the guards)",
    "filter-branch": "filter-branch rewrites history",
    "filter-repo": "filter-repo rewrites history",
    "replace": "git replace changes what objects resolve to",
    "rebase": "git rebase rewrites history",
    "credential": "git credential reads and writes stored credentials (B2)",
    "mergetool": "mergetool starts an external program",
    "difftool": "difftool starts an external program",
    "gui": "a GUI is not a headless tool", "citool": "a GUI is not a headless tool", "gitk": "a GUI is not a headless tool",
    "daemon": "git daemon serves the repository", "instaweb": "instaweb serves the repository",
    "send-email": "send-email sends mail", "svn": "git svn talks to another VCS", "p4": "git p4 talks to another VCS",
    "cvsimport": "foreign VCS import", "cvsexportcommit": "foreign VCS export", "quiltimport": "applies a patch series",
    "fast-import": "fast-import writes objects and refs from a stream", "receive-pack": "receive-pack runs a push server",
    "upload-pack": "upload-pack runs a fetch server", "upload-archive": "upload-archive runs an archive server",
    "shell": "git shell restricts to commands the guard cannot see", "remote-ext": "remote-ext runs a command",
    "maintenance": "maintenance registers background jobs and writes config", "scalar": "scalar writes config",
    "imap-send": "imap-send sends mail", "hook": "git hook run starts hook programs", "update-server-info": "writes server files",
    "pack-refs": "pack-refs rewrites ref storage", "prune": "prune deletes unreachable objects", "prune-packed": "deletes objects",
}
RULE_NAME = {"update-ref": "update-ref", "filter-branch": "filter-branch", "filter-repo": "filter-branch", "replace": "replace",
             "rebase": "rebase", "credential": "credential", "hook": "hook"}
NEVER_RUN_RULE = "never-run"      # every other subcommand in ALWAYS_DENIED: external programs, servers, GUIs, foreign VCS

# harmless `-c key=value` overrides (everything else is denied; core.* and alias.* have their own rule id)
SAFE_C_KEYS = [
    r"user\.(name|email|signingkey)$", r"color\.[a-z.]+$", r"advice\.[a-z]+$", r"init\.defaultbranch$", r"commit\.gpgsign$",
    r"tag\.gpgsign$", r"log\.[a-z.]+$", r"diff\.(algorithm|context|renames|colormoved|wserrorhighlight|"
    r"noprefix|mnemonicprefix|submodule)$", r"status\.[a-z.]+$", r"column\.[a-z.]+$", r"help\.[a-z.]+$", r"gc\.auto$",
    r"fetch\.(prune|prunetags|parallel)$", r"merge\.(ff|conflictstyle|stat)$", r"protocol\.version$",
    r"rebase\.autostash$", r"push\.autosetupremote$", r"branch\.sort$", r"tag\.sort$", r"i18n\.[a-z]+$",
    r"credential\.helper$-never-match",
]
SAFE_C_RE = [re.compile(p, re.I) for p in SAFE_C_KEYS]

BENIGN_GIT_ENV = re.compile(r"^GIT_(AUTHOR|COMMITTER)_(NAME|EMAIL|DATE)$|^GIT_TERMINAL_PROMPT$|^GIT_OPTIONAL_LOCKS$|^GIT_FLUSH$|"
                            r"^GIT_MERGE_AUTOEDIT$")

READ_LIST_BRANCH_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l", "--show-current",
                          "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format",
                          "--column", "--no-column", "--color", "--no-color", "-i", "--ignore-case", "-q", "--quiet",
                          "--abbrev", "--no-abbrev"}

_FEAT_BASE = re.compile(r"^feat/[A-Za-z0-9._][A-Za-z0-9._/-]*$")


class _FeatRef(object):
    """feat/<anything git accepts as a ref name>: no `..`, no `//`, no `/.` component, no trailing `/` or `.lock`."""

    @staticmethod
    def match(ref: str) -> bool:
        return bool(_FEAT_BASE.match(ref)) and ".." not in ref and "//" not in ref and "/." not in ref and not ref.endswith(("/", ".lock", ".")) and "@{" not in ref


FEAT_RE = _FeatRef()
DISCARDABLE_PREFIXES = ("feat/", "dead/", "blocked/")
LOOP_BRANCH_PREFIXES = ("feat/", "dead/", "blocked/")


def _abbrev(a: str, full: str, minlen: int = 4) -> bool:
    """Git accepts any unambiguous prefix of a long option; treat a prefix of `full` as that option."""
    name = a.split("=", 1)[0]
    return name.startswith("--") and len(name) >= max(minlen, 3) and full.startswith(name)


def _cluster_has(a: str, letter: str, arg_letters: str = "") -> bool:
    """Does a bundled short-option argument such as `-anm` contain `letter` before an option that takes a value?"""
    if not a.startswith("-") or a.startswith("--") or len(a) < 2:
        return False
    for ch in a[1:]:
        if ch == letter:
            return True
        if ch in arg_letters:
            return False
    return False


# ---------------------------------------------------------------------------------------------------------------

def check(ctx: Ctx) -> Optional[Decision]:
    ana = ctx.analysis
    for inv in ana.invs:
        d = _env(ctx, inv)
        if d:
            return d
        if inv.name == "git":
            d = _check_git(ctx, inv, 0)
        elif inv.name in ("gh", "hub", "glab"):
            d = _check_gh(ctx, inv)
        elif inv.name in ("curl", "wget", "http", "https", "xh", "httpie"):
            d = _check_github_http(ctx, inv)
        else:
            d = None
        if d:
            return d
    return None


def _env(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    names = dict(inv.env)
    if inv.name in ("export", "declare", "typeset", "readonly", "local", "set"):
        for a in inv.args:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a, re.S)
            if m:
                names[m.group(1)] = m.group(2)
    for name in names:
        if name.upper().startswith("GIT_") and not BENIGN_GIT_ENV.match(name.upper()):
            return deny(G, "git-env", "setting %s redirects or reconfigures git (hooks, ssh, config, pager); only the GIT_AUTHOR_*, "
                        "GIT_COMMITTER_*, GIT_TERMINAL_PROMPT and GIT_OPTIONAL_LOCKS variables may be set (report §5.9)" % name)
    return None


# -- git ---------------------------------------------------------------------------------------------------------

class _Git(object):
    """The parsed shape of one `git` invocation."""

    def __init__(self) -> None:
        self.sub = ""
        self.rest = []         # type: List[str]
        self.rest_dyn = []     # type: List[bool]
        self.cwd = ""
        self.git_dir = None    # type: Optional[str]
        self.work_tree = None  # type: Optional[str]
        self.c_pairs = []      # type: List[str]


def _tier(ctx: Ctx) -> int:
    if ctx.force_tier is not None:
        return ctx.force_tier
    if ctx.is_subagent:
        return 0
    if ctx.kind == "conductor":
        return 3
    return {"none": 0, "branch": 1, "branch+commit": 2}[ctx.git_autonomy]


def _tier_name(t: int) -> str:
    return {0: "read-only", 1: "stage and branch", 2: "commit", 3: "conductor"}[t]


def _need(ctx: Ctx, tier: int, what: str, sub: str) -> Optional[Decision]:
    have = _tier(ctx)
    if have >= tier:
        return None
    who = ctx.caller.kind if ctx.is_subagent else "this session"
    if ctx.is_subagent:
        hint = "engineers and critics have read-only git; surface the change as a suggestion in your report and the conductor runs it (engineer-protocol §8)"
    elif ctx.kind == "unknown":
        hint = ("this session is not the registered conductor and git_autonomy is %r; run it yourself with `! git %s ...`, or raise "
                "git_autonomy in loop.config (a human commit under H6)" % (ctx.git_autonomy, sub))
    else:
        hint = "not allowed for this caller"
    return deny(G, "readonly-caller", "git %s (%s) is not allowed for %s: %s" % (sub, what, who, hint))


def _check_c(kv: str) -> Optional[Decision]:
    key = kv.split("=", 1)[0].strip().lower()
    if not key or "=" not in kv and not key:
        return deny(G, "config-override", "git -c with an empty key")
    if key.startswith(("core.", "alias.")):
        return deny(G, "config-core-alias", "git -c %s overrides hooks, fsmonitor, ssh, pager, editor or an alias; that runs code in the "
                    "conductor's context (report §5.9)" % key)
    for rx in SAFE_C_RE:
        if rx.match(key):
            return None
    return deny(G, "config-override", "git -c %s is not on the short list of harmless overrides (user.*, color.*, advice.*, ...)" % key)


def _globals(ctx: Ctx, inv: Inv) -> Tuple[Optional[_Git], Optional[Decision]]:
    g = _Git()
    args, dyn = inv.args, inv.dyn
    g.cwd = inv.cwd
    i, n = 0, len(args)
    while i < n and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if dyn[i]:
            return None, deny(G, "dynamic-args", "a git option is computed at run time and cannot be checked")
        if a == "-C":
            if i + 1 >= n or dyn[i + 1]:
                return None, deny(G, "dynamic-args", "git -C with a path computed at run time")
            g.cwd = paths.lexical(args[i + 1], g.cwd, ctx.home)
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2 and not a.startswith("--"):
            g.cwd = paths.lexical(a[2:], g.cwd, ctx.home)
            i += 1
            continue
        if a == "-c" or (a.startswith("-c") and len(a) > 2 and not a.startswith("--")):
            kv = args[i + 1] if a == "-c" and i + 1 < n else a[2:]
            if (a == "-c" and (i + 1 >= n or dyn[i + 1])) or NUL_IN(kv):
                return None, deny(G, "config-override", "git -c with a value computed at run time")
            d = _check_c(kv)
            if d:
                return None, d
            g.c_pairs.append(kv)
            i += 2 if a == "-c" else 1
            continue
        if a.startswith("--config-env"):
            val = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < n else "")
            d = _check_c(val.split("=", 1)[0] + "=x")
            if d:
                return None, d
            return None, deny(G, "config-override", "git --config-env sets config from the environment; use a plain -c on the short list")
        if a.startswith("--exec-path") or a == "--exec-path":
            return None, deny(G, "exec-path", "git --exec-path runs git subcommands from another directory")
        key = a.split("=", 1)[0]
        if key in ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--attr-source"):
            val = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < n else "")
            if key == "--git-dir":
                g.git_dir = paths.lexical(val, g.cwd, ctx.home)
            elif key == "--work-tree":
                g.work_tree = paths.lexical(val, g.cwd, ctx.home)
            i += 1 if "=" in a else 2
            continue
        i += 1
    if i >= n:
        return None, None
    if dyn[i]:
        return None, deny(G, "dynamic-args", "the git subcommand is computed at run time and cannot be checked")
    g.sub = args[i].lower()
    g.rest = args[i + 1:]
    g.rest_dyn = dyn[i + 1:]
    return g, None


def NUL_IN(s: str) -> bool:
    return "\x00" in s


_ALIAS_CACHE = {}  # type: Dict[str, Dict[str, str]]


def _aliases(ctx: Ctx, cwd: str) -> Dict[str, str]:
    key = cwd
    if key in _ALIAS_CACHE and _ALIAS_CACHE[key] is not None:
        return _ALIAS_CACHE[key]
    out = {}  # type: Dict[str, str]
    try:
        from kitlib import gitutil
        proc = gitutil.run_git(["-c", "core.fsmonitor=false", "config", "--null", "--get-regexp", r"^alias\."], cwd=cwd if cwd else None,
                               timeout=5)
        if proc.returncode == 0:
            for rec in proc.stdout.split("\0"):
                if "\n" in rec:
                    k, v = rec.split("\n", 1)
                    out[k[len("alias."):].lower()] = v
    except Exception:
        pass
    if len(_ALIAS_CACHE) > 64:
        _ALIAS_CACHE.clear()
    _ALIAS_CACHE[key] = out
    return out


def _resolve_dir(ctx: Ctx, g: _Git) -> str:
    return g.work_tree or g.cwd


def _current_branch(ctx: Ctx, g: _Git) -> Optional[str]:
    d = _resolve_dir(ctx, g)
    if g.git_dir:
        from kitlib import gitutil
        try:
            proc = gitutil.run_git(["--git-dir", g.git_dir, "symbolic-ref", "--short", "-q", "HEAD"], cwd=d if d else None, timeout=5)
            return proc.stdout.strip() or None if proc.returncode == 0 else None
        except Exception:
            return None
    return ctx.branch_at(d)


def _check_git(ctx: Ctx, inv: Inv, depth: int) -> Optional[Decision]:
    if depth > 4:
        return deny(G, "alias", "git aliases nest too deeply to check")
    g, d = _globals(ctx, inv)
    if d:
        return d
    if g is None:
        return None            # bare `git`, `git -C x` : prints help
    sub = g.sub
    # `--no-verify` / `-n` : checked first because it applies to several subcommands and to aliases
    known = sub in READ_SUBS or sub in KNOWN_CONDUCTOR
    if not known:
        alias = _aliases(ctx, g.cwd).get(sub)
        if alias is None:
            return deny(G, "unknown-subcommand", "git %s is not a git command the guard knows (aliases are resolved from git config); "
                        "run it yourself with `!` if it is intended" % sub[:40])
        if alias.startswith("!"):
            ana = invoke.analyze(alias[1:] + " " + " ".join(shlex.quote(a) for a in g.rest), g.cwd, ctx.env, ctx.home)
            for sub_inv in ana.invs:
                dd = _env(ctx, sub_inv)
                if dd:
                    return dd
                if sub_inv.name == "git":
                    dd = _check_git(ctx, sub_inv, depth + 1)
                elif sub_inv.name in ("gh", "hub", "glab"):
                    dd = _check_gh(ctx, sub_inv)
                else:
                    dd = None
                if dd:
                    return dd
            for p in ana.problems:
                return deny(G, "alias", "the shell alias git %s cannot be analysed: %s" % (sub, p.message))
            return _need(ctx, 3, "an alias that runs a shell command", sub)
        try:
            words = shlex.split(alias)
        except ValueError:
            return deny(G, "alias", "git alias %s cannot be parsed" % sub)
        new = Inv("git", "git", words + g.rest, [False] * len(words) + g.rest_dyn, dict(inv.env), g.cwd, inv.cmd, inv.origin,
                  inv.depth, inv.wrappers)
        return _check_git(ctx, new, depth + 1)

    handler = _HANDLERS.get(sub)
    if sub in ALWAYS_DENIED:
        return deny(G, RULE_NAME.get(sub, NEVER_RUN_RULE), "git %s is never run by an agent: %s" % (sub, ALWAYS_DENIED[sub]))
    if handler is not None:
        return handler(ctx, g)
    if sub in READ_SUBS:
        return _read_sub(ctx, g)
    return _generic_conductor(ctx, g)


def _flags(rest: Sequence[str]) -> List[str]:
    out = []   # type: List[str]
    for a in rest:
        if a == "--":
            break
        if a.startswith("-"):
            out.append(a)
    return out


def _positionals(rest: Sequence[str], with_arg: Sequence[str] = ()) -> List[str]:
    out, skip = [], False   # type: List[str], bool
    seen_dd = False
    for a in rest:
        if skip:
            skip = False
            continue
        if seen_dd:
            out.append(a)
            continue
        if a == "--":
            seen_dd = True
            continue
        if a.startswith("-") and len(a) > 1:
            if a in with_arg:
                skip = True
            continue
        out.append(a)
    return out


def _no_verify(rest: Sequence[str], commit_like: bool) -> bool:
    for a in rest:
        if a == "--":
            break
        if a.startswith("--"):
            if _abbrev(a, "--no-verify", 5):
                return True
        elif commit_like and _cluster_has(a, "n", "mFCctuS"):
            return True
    return False


def _read_sub(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        name = a.split("=", 1)[0]
        if name in ("--output", "--output-indicator-new") or (name.startswith("--out") and _abbrev(a, "--output", 5)):
            d = _need(ctx, 3, "writing a file with --output", g.sub)
            if d:
                return d
        if g.sub == "grep" and (a == "-O" or a.startswith("-O") or _abbrev(a, "--open-files-in-pager", 6)):
            return deny(G, "pager-exec", "git grep -O starts a program")
        if g.sub == "fsck" and _abbrev(a, "--lost-found", 6):
            d = _need(ctx, 3, "fsck --lost-found writes objects", g.sub)
            if d:
                return d
        if name in ("--ext-diff", "--textconv") and g.sub in ("diff", "log", "show"):
            pass
    return None


def _generic_conductor(ctx: Ctx, g: _Git) -> Optional[Decision]:
    return _need(ctx, 3, "this subcommand", g.sub)


# -- individual subcommands -----------------------------------------------------------------------------------------

def _h_add_like(ctx: Ctx, g: _Git) -> Optional[Decision]:
    return _need(ctx, 1, "stage or restore files", g.sub)


def _h_stash(ctx: Ctx, g: _Git) -> Optional[Decision]:
    sub2 = (_positionals(g.rest) or ["push"])[0].lower() if g.rest and not g.rest[0].startswith("-") else "push"
    if sub2 in ("list", "show"):
        return None
    if sub2 in ("drop", "clear"):
        return _need(ctx, 3, "delete a stash", g.sub)
    return _need(ctx, 1, "stash", g.sub)


def _discard_ok(ctx: Ctx, g: _Git) -> Tuple[bool, str]:
    """reset --hard / clean may run only in a loop worktree on a non-shared branch, never the human's checkout."""
    d = _resolve_dir(ctx, g)
    branch = _current_branch(ctx, g)
    if ctx.is_protected_branch(branch):
        return False, "the current branch %s is shared" % branch
    from kitlib import gitutil
    linked = False
    try:
        linked = gitutil.is_linked_worktree(d)
    except Exception:
        linked = False
    loop_branch = bool(branch) and branch.startswith(LOOP_BRANCH_PREFIXES)
    if branch is None and not linked:
        return False, "HEAD is detached outside a loop worktree"
    if not (linked or loop_branch):
        return False, "this is not a loop worktree or a feat/*, dead/* or blocked/* branch (the human's checkout is never discarded)"
    return True, ""


def _is_shared_ref(ctx: Ctx, ref: str) -> bool:
    low = ref.strip().lower()
    if low in ("fetch_head", "@{u}", "@{upstream}", "@{push}"):
        return True
    base = re.sub(r"^refs/(heads|remotes)/", "", low)
    base = re.sub(r"[~^].*$", "", base)
    if ctx.is_protected_branch(base) or low.startswith("refs/remotes/") or base in ("origin", "upstream"):
        return True
    return "/" in base and base.split("/")[0] in ("origin", "upstream", "github", "gitlab") or ctx.is_protected_branch(base.split("/")[-1])


def _h_reset(ctx: Ctx, g: _Git) -> Optional[Decision]:
    rest = g.rest
    hard = any(a in ("--hard", "--merge", "--keep") or _abbrev(a, "--hard", 3) or _abbrev(a, "--merge", 4) or _abbrev(a, "--keep", 4) for a in rest)
    pos = _positionals(rest)
    if hard:
        d = _need(ctx, 3, "reset --hard", "reset")
        if d:
            return d
        for a in pos:
            if _is_shared_ref(ctx, a):
                return deny(G, "reset-hard-shared", "git reset --hard %s would move a shared branch (main|master|origin/*)" % a)
        ok, why = _discard_ok(ctx, g)
        if not ok:
            return deny(G, "reset-hard-outside-loop", "git reset --hard: %s (report §19.7 discard)" % why)
        return None
    if "--" in rest or not pos or pos == ["HEAD"]:
        return _need(ctx, 1, "unstage", "reset")
    return _need(ctx, 3, "move the branch pointer", "reset")


def _h_clean(ctx: Ctx, g: _Git) -> Optional[Decision]:
    flags = _flags(g.rest)
    dry = any(f in ("-n", "--dry-run") or (f.startswith("-") and not f.startswith("--") and "n" in f[1:]) for f in flags)
    if dry:
        return None
    d = _need(ctx, 3, "git clean deletes untracked files", "clean")
    if d:
        return d
    ok, why = _discard_ok(ctx, g)
    if not ok:
        return deny(G, "clean-outside-loop", "git clean: %s (report §19.7 discard)" % why)
    return None


def _h_checkout(ctx: Ctx, g: _Git) -> Optional[Decision]:
    rest = g.rest
    creating = None   # type: Optional[str]
    for k, a in enumerate(rest):
        if a in ("-b", "-B", "--orphan", "-c", "-C", "--create", "--force-create") and k + 1 < len(rest):
            creating = rest[k + 1]
            if a in ("-B", "-C", "--force-create") and ctx.is_protected_branch(creating):
                return deny(G, "branch-protected", "git %s %s %s would reset a shared branch" % (g.sub, a, creating))
    if creating is not None:
        if ctx.is_protected_branch(creating):
            return deny(G, "branch-protected", "creating a branch named %s (a shared branch name)" % creating)
        return _need(ctx, 1, "create a branch", g.sub)
    return _need(ctx, 1, "switch or restore", g.sub)


def _h_commit(ctx: Ctx, g: _Git) -> Optional[Decision]:
    if _no_verify(g.rest, True):
        return deny(G, "no-verify", "git %s --no-verify (or -n) skips the pre-commit hooks (report §5.9)" % g.sub)
    d = _need(ctx, 2, "create a commit", g.sub)
    if d:
        return d
    if g.sub == "commit" and any(_abbrev(a, "--amend", 5) for a in g.rest):
        b = _current_branch(ctx, g)
        if ctx.is_protected_branch(b):
            return deny(G, "amend-protected", "git commit --amend on the shared branch %s rewrites shared history" % b)
    return None


def _h_merge(ctx: Ctx, g: _Git) -> Optional[Decision]:
    if _no_verify(g.rest, False) or any(a == "-n" for a in g.rest):
        return deny(G, "no-verify", "git merge --no-verify (or -n) skips the merge hooks (report §5.9)")
    d = _need(ctx, 2, "merge", "merge")
    if d:
        return d
    if "--abort" in g.rest or "--quit" in g.rest:
        return None
    b = _current_branch(ctx, g)
    if ctx.is_protected_branch(b):
        if ctx.kind != "conductor":
            return deny(G, "merge-protected", "merging into the shared branch %s is the conductor's job" % b)
        delivery = str(ctx.cfg("delivery", "local") or "local")
        if delivery != "local" and not (g.sub == "pull" and "--ff-only" in g.rest):
            return deny(G, "merge-protected", "merging into %s locally is only done under delivery: local (this repo uses %s); the merge "
                        "happens on GitHub" % (b, delivery))
    return None


def _h_pull(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        if _abbrev(a, "--upload-pack", 5) or _abbrev(a, "--receive-pack", 5):
            return deny(G, "exec-option", "git pull %s starts a program" % a)
    if _no_verify(g.rest, False):
        return deny(G, "no-verify", "git pull --no-verify skips the merge hooks (report §5.9)")
    if any((a == "-r" or a.startswith("--rebase")) and a not in ("--rebase=false",) for a in g.rest):
        return deny(G, "rebase", "git pull --rebase rewrites history")
    return _h_merge(ctx, g)


def _h_fetch(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        if _abbrev(a, "--upload-pack", 5) or a == "-u":
            return deny(G, "exec-option", "git fetch %s starts a program" % a)
    for spec in _positionals(g.rest)[1:]:
        src, colon, dst = spec.lstrip("+").partition(":")
        if colon:
            ref = re.sub(r"^refs/heads/", "", dst)
            if ctx.is_protected_branch(ref) or ref == "HEAD":
                return deny(G, "fetch-protected", "git fetch %s writes the shared branch %s locally, bypassing the merge rules" % (spec[:50], ref))
    if "--update-head-ok" in g.rest or any(_abbrev(a, "--update-head-ok", 6) for a in g.rest):
        return deny(G, "fetch-protected", "git fetch --update-head-ok moves the checked-out branch")
    return _need(ctx, 1, "fetch", g.sub)


def _h_revert_like(ctx: Ctx, g: _Git) -> Optional[Decision]:
    if _no_verify(g.rest, True):
        return deny(G, "no-verify", "git %s --no-verify skips hooks" % g.sub)
    return _need(ctx, 2, g.sub, g.sub)


def _h_branch(ctx: Ctx, g: _Git) -> Optional[Decision]:
    rest = g.rest
    flags = _flags(rest)
    pos = _positionals(rest, ("--sort", "--format", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                              "--column", "--abbrev", "-u", "--set-upstream-to", "--color"))

    def has(*names: str, letters: str = "") -> bool:
        for f in flags:
            if f in names:
                return True
            if letters and f.startswith("-") and not f.startswith("--") and any(ch in letters for ch in f[1:]):
                return True
            if f.startswith("--") and any(_abbrev(f, n, 4) for n in names if n.startswith("--")):
                return True
        return False
    delete = has("--delete", letters="dD")
    force = has("--force", letters="fD")
    big_d = any(f == "-D" or (f.startswith("-") and not f.startswith("--") and "D" in f[1:]) for f in flags)
    move = has("--move", letters="mM")
    copy = has("--copy", letters="cC")
    if delete:
        d = _need(ctx, 3, "delete a branch", "branch")
        if d:
            return d
        for t in pos:
            if ctx.is_protected_branch(t):
                return deny(G, "branch-delete-protected", "deleting the shared branch %s" % t)
        if big_d or (force and delete):
            if not pos or not all(t.startswith(DISCARDABLE_PREFIXES) for t in pos):
                return deny(G, "branch-force-delete", "git branch -D is allowed only for feat/*, dead/* and blocked/* branches (the loop's fail path)")
        return None
    if move or copy:
        d = _need(ctx, 3, "rename or copy a branch", "branch")
        if d:
            return d
        for t in pos:
            if ctx.is_protected_branch(t):
                return deny(G, "branch-protected", "renaming or copying onto/from the shared branch %s" % t)
        return None
    if has("--set-upstream-to", "--unset-upstream", "--edit-description", letters="u") or any(f.startswith("--set-upstream-to") for f in flags):
        return _need(ctx, 3, "change branch config", "branch")
    listing = any(f in READ_LIST_BRANCH_FLAGS or f.split("=", 1)[0] in READ_LIST_BRANCH_FLAGS for f in flags)
    if has("--list", letters="l") or (not pos) or (listing and not pos):
        return None
    if listing and has("--list", letters="l"):
        return None
    # create
    for t in pos[:1]:
        if ctx.is_protected_branch(t):
            return deny(G, "branch-protected", "creating a branch named %s (a shared branch name)" % t)
        if force:
            return deny(G, "branch-protected", "git branch -f %s moves an existing branch pointer" % t)
    return _need(ctx, 1, "create a branch", "branch")


def _h_tag(ctx: Ctx, g: _Git) -> Optional[Decision]:
    flags = _flags(g.rest)
    pos = _positionals(g.rest, ("-m", "-F", "-u", "--sort", "--format", "--contains", "--points-at", "--merged", "--no-merged", "-n", "--column"))
    if any(f == "--force" or _abbrev(f, "--force", 4) or _cluster_has(f, "f", "mFu") for f in flags):
        return deny(G, "tag-force", "git tag -f moves an existing tag (a release marker, H5)")
    listing = any(f in ("-l", "--list", "--contains", "--points-at", "--merged", "--no-merged", "--sort", "-v", "--verify") or f.startswith(("--sort=", "--format=", "-n", "--contains=", "--points-at="))
                  for f in flags)
    if listing or not pos:
        return None
    return _need(ctx, 3, "create or delete a tag (tags are releases, H5)", "tag")


def _h_config(ctx: Ctx, g: _Git) -> Optional[Decision]:
    rest = g.rest
    flags = _flags(rest)
    pos = _positionals(rest, ("--file", "-f", "--blob", "--type", "--default", "--get-urlmatch", "--includes", "--worktree"))
    first = pos[0].lower() if pos else ""
    read_flags = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "--show-origin", "--show-scope",
                  "--name-only", "--get-color", "--get-colorbool", "-z", "--null", "--type", "--bool", "--int", "--path",
                  "--default", "--includes", "--no-includes", "--global", "--local", "--system", "--worktree", "--file", "-f"}
    write_flags = {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit",
                   "--set", "--set-all", "--comment"}
    if any(f.split("=", 1)[0] in write_flags for f in flags) or "set" == first or first in ("unset", "edit", "rename-section", "remove-section"):
        return deny(G, "config-write", "git config writes can set hooks, aliases, credential helpers and ssh commands (report §5.9)")
    if first in ("get", "list"):
        return None
    isread = any(f.split("=", 1)[0] in read_flags and f.split("=", 1)[0] not in ("--global", "--local", "--system", "--file", "-f", "--worktree",
                                                                                    "--type", "--bool", "--int", "--path", "--default",
                                                                                    "--includes", "--no-includes") for f in flags)
    if isread or len(pos) <= 1:
        return None
    return deny(G, "config-write", "git config <key> <value> writes config (report §5.9)")


def _h_remote(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    sub2 = pos[0].lower() if pos else ""
    if not sub2 or sub2 in ("show", "get-url"):
        return None
    if sub2 in ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update"):
        if sub2 in ("prune", "update"):
            return _need(ctx, 3, "update remote-tracking refs", "remote")
        return deny(G, "config-write", "git remote %s edits .git/config (urls, refspecs); that is a guardrail change (H6)" % sub2)
    return deny(G, "unknown-subcommand", "git remote %s is not understood" % sub2[:30])


def _h_symbolic_ref(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest, ("-m",))
    if len(pos) >= 2 or any(f in ("-d", "--delete", "-m") for f in _flags(g.rest)):
        return deny(G, "update-ref", "git symbolic-ref with a value rewrites HEAD or a ref")
    return None


def _h_reflog(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    if pos and pos[0].lower() in ("expire", "delete", "drop"):
        return deny(G, "reflog-expire", "git reflog %s destroys recovery data" % pos[0])
    return None


def _h_worktree(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    if pos and pos[0].lower() == "list":
        return None
    return _need(ctx, 3, "manage worktrees", "worktree")


def _h_submodule(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    s2 = pos[0].lower() if pos else "status"
    if s2 in ("status", "summary"):
        return None
    if s2 == "foreach":
        return deny(G, "submodule-foreach", "git submodule foreach runs a command in every submodule")
    return _need(ctx, 3, "submodule " + s2, "submodule")


def _h_notes(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    if not pos or pos[0].lower() in ("list", "show"):
        return None
    return _need(ctx, 3, "edit notes", "notes")


def _h_bisect(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    if pos and pos[0].lower() == "run":
        return deny(G, "bisect-run", "git bisect run executes a command repeatedly")
    if pos and pos[0].lower() in ("log", "visualize", "view", "replay"):
        return None
    return _need(ctx, 3, "bisect", "bisect")


def _h_gc(ctx: Ctx, g: _Git) -> Optional[Decision]:
    flags = _flags(g.rest)
    if flags == ["--auto"] or flags == ["--auto", "--quiet"] or flags == ["--quiet", "--auto"]:
        return _need(ctx, 3, "gc --auto", "gc")
    return deny(G, "gc", "git gc outside --auto can prune unreachable objects")


def _h_lfs(ctx: Ctx, g: _Git) -> Optional[Decision]:
    pos = _positionals(g.rest)
    if pos and pos[0].lower() in ("install", "uninstall", "update", "track", "untrack", "migrate"):
        return deny(G, "config-write", "git lfs %s writes hooks, filters and .gitattributes (report §5.9)" % pos[0])
    return _need(ctx, 3, "lfs", "lfs")


def _h_update_index(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        if a in ("--assume-unchanged", "--skip-worktree", "--really-refresh") or _abbrev(a, "--assume-unchanged", 5) or _abbrev(a, "--skip-worktree", 6):
            return deny(G, "update-index-hide", "git update-index %s hides changes from review and the integrity gate" % a)
    return _need(ctx, 3, "update-index", "update-index")


def _h_clone_init(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        if a in ("-c", "--config", "-u") or a.startswith(("--config=", "--template", "--upload-pack", "--receive-pack")):
            return deny(G, "exec-option", "git %s %s starts a program or writes config" % (g.sub, a.split("=")[0]))
    return _need(ctx, 1, g.sub, g.sub)


def _h_apply(ctx: Ctx, g: _Git) -> Optional[Decision]:
    return _need(ctx, 1, "apply a patch", g.sub)


def _h_ls_remote(ctx: Ctx, g: _Git) -> Optional[Decision]:
    for a in g.rest:
        if _abbrev(a, "--upload-pack", 5):
            return deny(G, "exec-option", "git ls-remote --upload-pack starts a program")
    return _need(ctx, 1, "ls-remote", g.sub)


def _h_hash_object(ctx: Ctx, g: _Git) -> Optional[Decision]:
    if any(f == "-w" or (f.startswith("-") and not f.startswith("--") and "w" in f[1:]) for f in _flags(g.rest)):
        return _need(ctx, 3, "hash-object -w writes objects", g.sub)
    return None


def _h_archive_like(ctx: Ctx, g: _Git) -> Optional[Decision]:
    return _need(ctx, 3, g.sub, g.sub)


# -- push -----------------------------------------------------------------------------------------------------------------

PUSH_LONG = ["--all", "--branches", "--prune", "--mirror", "--dry-run", "--porcelain", "--delete", "--tags", "--follow-tags",
             "--signed", "--no-signed", "--atomic", "--push-option", "--receive-pack", "--exec", "--repo", "--set-upstream",
             "--thin", "--no-thin", "--quiet", "--verbose", "--progress", "--recurse-submodules", "--no-recurse-submodules",
             "--verify", "--no-verify", "--force", "--force-with-lease", "--force-if-includes", "--no-force-with-lease",
             "--no-force-if-includes", "--ipv4", "--ipv6", "--help", "--no-follow-tags", "--no-atomic", "--no-prune"]


def _push_longs(a: str) -> List[str]:
    name = a.split("=", 1)[0]
    if name in PUSH_LONG:
        return [name]
    return [f for f in PUSH_LONG if f.startswith(name)] if len(name) >= 3 else []


def _h_push(ctx: Ctx, g: _Git) -> Optional[Decision]:
    rest, dyn = g.rest, g.rest_dyn
    force = delete = mirror = bulk = tags = no_verify = False
    exec_opt = False
    lease = None   # type: Optional[str]
    pos = []       # type: List[str]
    dynamic_arg = False
    i, n = 0, len(rest)
    dd = False
    while i < n:
        a = rest[i]
        if dd or not a.startswith("-") or a == "-":
            pos.append(a)
            if dyn[i]:
                dynamic_arg = True
            i += 1
            continue
        if a == "--":
            dd = True
            i += 1
            continue
        if a.startswith("--"):
            cands = _push_longs(a)
            if not cands and len(a) > 2:
                cands = ["--?unknown"]
            for c in cands:
                if c == "--force":
                    force = True
                elif c == "--force-with-lease":
                    lease = a.split("=", 1)[1] if "=" in a else ""
                elif c == "--force-if-includes":
                    pass
                elif c == "--mirror":
                    mirror = True
                elif c in ("--delete", "--prune"):
                    delete = True
                elif c in ("--all", "--branches"):
                    bulk = True
                elif c in ("--tags", "--follow-tags"):
                    tags = True
                elif c == "--no-verify":
                    no_verify = True
                elif c in ("--receive-pack", "--exec", "--repo"):
                    exec_opt = True
            if "--push-option" in cands and "=" not in a:
                i += 1
            i += 1
            continue
        # short options, possibly bundled
        letters = a[1:]
        for ch in letters:
            if ch == "f":
                force = True
            elif ch == "d":
                delete = True
            elif ch == "o":
                break
        if "o" in letters and letters.endswith("o"):
            i += 1
        i += 1
    if no_verify:
        return deny(G, "no-verify", "git push --no-verify skips the pre-push hook (report §5.9)")
    # who may push at all
    if ctx.kind != "conductor":
        who = "an engineer or critic" if ctx.is_subagent else "this session"
        return deny(G, "push", "git push is a remote write: agents never push (humans push at checkpoints, or loopctl does with the bot "
                    "identity). %s is not the registered conductor; run `! git push ...` yourself or use pr_prepare" % who)
    if ctx.tainted:
        return deny(G, "push-tainted", "this session has read untrusted content (§5.3): it holds no push authority")
    delivery = str(ctx.cfg("delivery", "local") or "local")
    if delivery not in ("push-branch", "draft-pr", "pr-automerge"):
        return deny(G, "push-delivery", "delivery is %r: nothing is pushed by an agent (set delivery in loop.config, an H6 change)" % delivery)
    if dynamic_arg:
        return deny(G, "dynamic-args", "git push with a remote or refspec computed at run time cannot be checked against the refspec allowlist")
    if mirror:
        return deny(G, "push-mirror", "git push --mirror overwrites the remote's refs")
    if delete:
        return deny(G, "push-delete", "git push --delete / --prune deletes remote refs")
    if bulk:
        return deny(G, "push-refspec", "git push --all / --branches pushes every branch; name the feat/* branch")
    if tags:
        return deny(G, "push-tags", "pushing tags is a release (H5)")
    if force:
        return deny(G, "push-force", "git push --force rewrites remote history; use --force-with-lease=<ref>:<sha> on the run's own feat/* branch")
    if exec_opt:
        return deny(G, "exec-option", "git push --receive-pack / --exec / --repo starts a program or redirects the push")
    remote = pos[0] if pos else None
    specs = pos[1:]
    if remote is not None and not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", remote):
        return deny(G, "push-url", "git push to %r: only a named remote (origin) is allowed, not a URL or path" % remote[:60])
    if remote == "tag" or "tag" in specs[:1]:
        return deny(G, "push-tags", "pushing tags is a release (H5)")
    branch = _current_branch(ctx, g)
    if not specs:
        return deny(G, "push-refspec", "git push with no refspec pushes whatever push.default and the remote's push config say (matching, upstream, ...); "
                    "name the branch: git push origin feat/<slug>")
    for spec in specs:
        d = _refspec_ok(ctx, spec, branch)
        if d:
            return d
    if lease is not None:
        m = re.match(r"^([^:]+):([0-9a-fA-F]{7,64})$", lease)
        if not m or not FEAT_RE.match(re.sub(r"^refs/heads/", "", m.group(1))):
            return deny(G, "push-force", "--force-with-lease needs an explicit <feat/ref>:<expected-sha> (a bare lease compares with the last fetch, "
                        "report §10.3)")
    return None


def _refspec_ok(ctx: Ctx, spec: str, branch: Optional[str]) -> Optional[Decision]:
    s = spec
    if s.startswith("+"):
        return deny(G, "push-force", "refspec %s is a forced update" % spec[:60])
    src, colon, dst = s.partition(":")
    if colon and src == "":
        return deny(G, "push-delete", "refspec %s deletes a remote ref" % spec[:60])
    ref = dst if colon else src
    if ref in ("HEAD", "@"):
        ref = branch or ""
    if "*" in ref or "?" in ref or "[" in ref:
        return deny(G, "push-refspec", "refspec %s uses a wildcard" % spec[:60])
    if ref.startswith("refs/loopctl/"):
        return deny(G, "push-refspec", "refs/loopctl/* is pushed only by loopctl, from its own process (report §19.7)")
    if ref.startswith(("refs/tags/", "tags/")):
        return deny(G, "push-tags", "pushing a tag is a release (H5)")
    ref = re.sub(r"^refs/heads/", "", ref)
    if ref.startswith("refs/"):
        return deny(G, "push-refspec", "refspec %s targets %s: only refs/heads/feat/* may be pushed" % (spec[:60], ref[:40]))
    if ctx.is_protected_branch(ref):
        return deny(G, "push-protected", "pushing to the shared branch %s (H0/H3: reach it through a pull request)" % ref)
    if not FEAT_RE.match(ref):
        return deny(G, "push-refspec", "refspec %s: only feat/* branches may be pushed by an agent (refspec allowlist)" % spec[:60])
    return None


_HANDLERS = {
    "add": _h_add_like, "rm": _h_add_like, "mv": _h_add_like, "restore": _h_add_like, "sparse-checkout": _h_add_like,
    "stash": _h_stash, "reset": _h_reset, "clean": _h_clean, "checkout": _h_checkout, "switch": _h_checkout,
    "commit": _h_commit, "merge": _h_merge, "pull": _h_pull, "fetch": _h_fetch, "cherry-pick": _h_revert_like,
    "revert": _h_revert_like, "am": _h_revert_like, "branch": _h_branch, "tag": _h_tag, "config": _h_config, "remote": _h_remote,
    "symbolic-ref": _h_symbolic_ref, "reflog": _h_reflog, "worktree": _h_worktree, "submodule": _h_submodule, "notes": _h_notes,
    "bisect": _h_bisect, "gc": _h_gc, "lfs": _h_lfs, "update-index": _h_update_index, "clone": _h_clone_init, "init": _h_clone_init,
    "apply": _h_apply, "ls-remote": _h_ls_remote, "hash-object": _h_hash_object, "archive": _h_archive_like, "bundle": _h_archive_like,
    "format-patch": _h_archive_like, "push": _h_push,
}


# -- gh, hub, glab ----------------------------------------------------------------------------------------------------------

GH_READ = {
    "pr": {"view", "list", "diff", "checks", "status"}, "issue": {"view", "list", "status"}, "run": {"view", "list", "watch"},
    "workflow": {"view", "list"}, "repo": {"view", "list"}, "release": {"view", "list"}, "ruleset": {"list", "view", "check"},
    "label": {"list"}, "cache": {"list"}, "gist": {"view", "list"}, "config": {"get", "list"}, "extension": {"list", "search"},
    "variable": {"list", "get"}, "alias": {"list"}, "auth": {"status"}, "project": {"view", "list", "field-list", "item-list"},
    "org": {"list"}, "codespace": set(), "secret": set(), "attestation": {"verify", "download"},
}
GH_ANY = {"search", "status", "completion", "help", "version", "--version", "-h", "--help", "browse"}
GH_RULES = {"merge": "gh-pr-merge", "release": "gh-release", "secret": "gh-secret", "workflow": "gh-workflow", "repo": "gh-repo",
            "ruleset": "gh-ruleset", "auth": "gh-auth", "variable": "gh-secret", "codespace": "gh-mutation"}
GH_GLOBAL_ARG = {"-R", "--repo", "--hostname"}


def _check_gh(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    args, dyn = inv.args, inv.dyn
    if inv.name != "gh":
        return _check_hub_glab(ctx, inv)
    i, n = 0, len(args)
    while i < n and args[i].startswith("-"):
        if dyn[i]:
            return deny(G, "dynamic-args", "a gh option is computed at run time")
        i += 2 if (args[i] in GH_GLOBAL_ARG and "=" not in args[i]) else 1
    if i >= n:
        return None
    if dyn[i]:
        return deny(G, "dynamic-args", "the gh command is computed at run time")
    group = args[i].lower()
    rest, rdyn = args[i + 1:], dyn[i + 1:]
    if group == "api":
        return _gh_api(ctx, rest, rdyn)
    if group in GH_ANY:
        if group == "browse":
            return None
        return None
    action = ""
    for k, a in enumerate(rest):
        if not a.startswith("-"):
            action = a.lower()
            break
    if group == "pr" and action == "merge":
        return deny(G, "gh-pr-merge", "gh pr merge: merges are made by a human, or by auto-merge for low-risk paths only at L5 (report §6.1 H3, merge_policy)")
    if group in GH_READ and action in GH_READ[group]:
        return None
    rule = GH_RULES.get(group, "gh-mutation")
    if group == "auth" and action in ("status",):
        return None
    return deny(G, rule, "gh %s %s changes GitHub state or credentials; run it yourself with `!` (report §7.1: gh api non-GET, release, secret, workflow, "
                "repo edit, ruleset are denied to agents)" % (group, action or "..."))


def _gh_api(ctx: Ctx, rest: List[str], rdyn: List[bool]) -> Optional[Decision]:
    method = None   # type: Optional[str]
    has_body = False
    graphql_query = None   # type: Optional[str]
    endpoint = None
    i, n = 0, len(rest)
    while i < n:
        a = rest[i]
        if rdyn[i]:
            return deny(G, "gh-api-write", "gh api with an argument computed at run time cannot be proved to be a GET")
        key = a.split("=", 1)[0]
        val = a.split("=", 1)[1] if "=" in a and a.startswith("--") else None
        if a in ("-X", "--method") or key == "--method" or (a.startswith("-X") and len(a) > 2):
            v = val if val is not None else (a[2:] if a.startswith("-X") and len(a) > 2 else (rest[i + 1] if i + 1 < n else ""))
            method = v.upper()
            i += 1 if (val is not None or (a.startswith("-X") and len(a) > 2)) else 2
            continue
        if a in ("-f", "-F", "--field", "--raw-field") or key in ("--field", "--raw-field") or (a.startswith(("-f", "-F")) and len(a) > 2 and "=" in a):
            v = val if val is not None else (a[2:] if a.startswith(("-f", "-F")) and len(a) > 2 else (rest[i + 1] if i + 1 < n else ""))
            has_body = True
            if v.startswith("query="):
                graphql_query = v[6:]
            i += 1 if (val is not None or (a.startswith(("-f", "-F")) and len(a) > 2)) else 2
            continue
        if a == "--input" or key == "--input":
            has_body = True
            i += 1 if val is not None else 2
            continue
        if a in ("-H", "--header", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "--preview", "-p") or key in ("--header", "--jq", "--template", "--hostname", "--cache", "--preview"):
            i += 1 if (val is not None) else 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if endpoint is None:
            endpoint = a
        i += 1
    is_graphql = endpoint is not None and endpoint.lower().lstrip("/") == "graphql"
    if is_graphql:
        if graphql_query is None or "mutation" in graphql_query.lower() or graphql_query.lstrip().startswith("@") \
                or (method not in (None, "POST", "GET")):
            return deny(G, "gh-api-write", "gh api graphql is allowed only for an inline query with no mutation")
        return None
    eff = method or ("POST" if has_body else "GET")
    if eff != "GET":
        return deny(G, "gh-api-write", "gh api with method %s writes to GitHub; only GET is allowed to agents (report §5.9)" % eff)
    return None


def _check_hub_glab(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    pos = [a for a in inv.args if not a.startswith("-")]
    read = {"list", "view", "show", "diff", "status", "ci-status", "browse", "checks", "version", "help"}
    if not pos or pos[0].lower() in read or (len(pos) > 1 and pos[1].lower() in read):
        return None
    return deny(G, "gh-mutation", "%s %s changes forge state; run it yourself with `!`" % (inv.name, pos[0][:30]))


def _check_github_http(ctx: Ctx, inv: Inv) -> Optional[Decision]:
    text = " ".join(inv.args)
    if "api.github.com" not in text and "uploads.github.com" not in text and "github.com/api" not in text:
        return None
    mutating = False
    args = inv.args
    for k, a in enumerate(args):
        if a in ("-X", "--request", "--method") and k + 1 < len(args) and args[k + 1].upper() != "GET":
            mutating = True
        elif a.startswith("-X") and len(a) > 2 and a[2:].upper() != "GET":
            mutating = True
        elif a.startswith("--request=") and a.split("=", 1)[1].upper() != "GET":
            mutating = True
        elif a.startswith(("--data", "--form", "--upload-file", "--json", "--post")):
            mutating = True
        elif a.startswith("-") and not a.startswith("--") and any(ch in "dFT" for ch in a[1:]) and not a.startswith("-X"):
            mutating = True          # curl: -d data, -F form, -T upload (-f is --fail and is fine)
        elif a.lower() in ("put", "post", "patch", "delete") and inv.name in ("http", "https", "xh", "httpie"):
            mutating = True
    if mutating:
        return deny(G, "gh-api-write", "a non-GET request to the GitHub API (report §5.9: gh api non-GET is denied; the same goes for curl)")
    return None
