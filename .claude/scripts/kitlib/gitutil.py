"""Git helpers: worktree-aware resolution and compare-and-swap (report §10.3, §19.7).

Every git call here uses a fixed argument list with `shell=False`. Callers never
pass plan, issue or model text into an argument unvalidated: refs, remotes and
object ids are checked against strict patterns before they reach git.

CAS, defined once (report §10.3):
  * L1, nothing pushed:  `update_ref_cas(ref, new, old)` -> `git update-ref ref new old`
  * L2+:                 `push_cas(remote, ref, new, old)` ->
                         `git push --force-with-lease=<ref>:<old> <remote> <new>:<ref>`
`old=None` means "the ref must not exist" (create-only): update-ref gets the all-zero
object id and push gets `--force-with-lease=<ref>:` (empty expected value). A bare
`--force-with-lease` (no `<ref>:<old>`) is never used, because it compares against the
last fetch and not against what the caller read.

Hooks are pinned off (`core.hooksPath=/dev/null`) for the CAS calls so a repo hook
cannot run during a state update. Pass `isolated=True` to also ignore the user's
global and system git config (loopctl does this; tests set the env themselves).
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence

__all__ = ["GitError", "CasResult", "run_git", "git_env", "nearest_existing_dir", "toplevel",
           "common_dir", "git_dir", "is_linked_worktree", "current_branch", "rev_parse", "ref_exists",
           "show_file", "is_ancestor", "is_clean", "ls_remote_ref", "update_ref_cas", "delete_ref_cas",
           "push_cas", "valid_ref", "valid_oid", "ZERO_OID_SHA1"]

ZERO_OID_SHA1 = "0" * 40

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF_RE = re.compile(r"^refs/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")

# Environment variables that redirect git away from the directory we pass with -C.
_STRIP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
              "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
              "GIT_QUARANTINE_PATH")


class GitError(RuntimeError):
    def __init__(self, message: str, returncode: Optional[int] = None, stderr: str = "") -> None:
        RuntimeError.__init__(self, message)
        self.returncode = returncode
        self.stderr = stderr


class CasResult(object):
    """Outcome of a CAS. Truthy iff the swap happened."""

    def __init__(self, ok: bool, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return "CasResult(ok=%r, stderr=%r)" % (self.ok, self.stderr.strip()[:120])


def valid_ref(ref: str) -> bool:
    return isinstance(ref, str) and bool(_REF_RE.match(ref)) and ".." not in ref \
        and not ref.endswith(".lock") and not ref.endswith("/") and "/." not in ref and not ref.endswith(".")


def valid_oid(oid: str) -> bool:
    return isinstance(oid, str) and bool(_OID_RE.match(oid))


def git_env(extra: Optional[Mapping[str, str]] = None, isolated: bool = False) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C")
    if isolated:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        env.update(extra)
    return env


def run_git(args: Sequence[str], cwd: Optional[str] = None, check: bool = False,
            env: Optional[Mapping[str, str]] = None, input_text: Optional[str] = None,
            isolated: bool = False, timeout: Optional[float] = 60,
            pin_hooks: bool = False, binary: bool = False):
    """Run `git <args>` with shell=False. Returns CompletedProcess (text mode unless `binary`)."""
    argv = ["git"]
    if pin_hooks:
        argv += ["-c", "core.hooksPath=" + os.devnull]
    if cwd is not None:
        argv += ["-C", cwd]
    argv += list(args)
    try:
        proc = subprocess.run(
            argv, input=(input_text.encode("utf-8") if input_text is not None else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=timeout,
            env=git_env(env, isolated=isolated))
    except FileNotFoundError:
        raise GitError("git executable not found")
    except subprocess.TimeoutExpired:
        raise GitError("git %s timed out" % (args[0] if args else ""))
    if not binary:
        proc.stdout = proc.stdout.decode("utf-8", "replace")
        proc.stderr = proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode("utf-8", "replace")
        raise GitError("git %s failed (%d): %s" % (" ".join(args[:2]), proc.returncode, err.strip()),
                       proc.returncode, err)
    return proc


def nearest_existing_dir(path: str) -> Optional[str]:
    """Walk up from `path` to the first existing directory (a file to be created may
    not exist yet, and neither may its parent directories)."""
    p = os.path.abspath(path)
    while True:
        if os.path.isdir(p):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _first_line(proc) -> Optional[str]:
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out.splitlines()[0] if out else None


def toplevel(path: str, **kw) -> Optional[str]:
    """Working-tree root containing `path` (a linked worktree returns its own root), or None."""
    d = nearest_existing_dir(path)
    if d is None:
        return None
    top = _first_line(run_git(["rev-parse", "--show-toplevel"], cwd=d, **kw))
    return os.path.realpath(top) if top else None


def _abs_git_path(base: str, value: str) -> str:
    return os.path.realpath(value if os.path.isabs(value) else os.path.join(base, value))


def common_dir(path: str, **kw) -> Optional[str]:
    """`git rev-parse --git-common-dir` as an absolute path: shared by every worktree."""
    d = nearest_existing_dir(path)
    if d is None:
        return None
    val = _first_line(run_git(["rev-parse", "--git-common-dir"], cwd=d, **kw))
    return _abs_git_path(d, val) if val else None


def git_dir(path: str, **kw) -> Optional[str]:
    """The per-worktree git dir (differs from `common_dir` in a linked worktree)."""
    d = nearest_existing_dir(path)
    if d is None:
        return None
    val = _first_line(run_git(["rev-parse", "--git-dir"], cwd=d, **kw))
    return _abs_git_path(d, val) if val else None


def is_linked_worktree(path: str, **kw) -> bool:
    g, c = git_dir(path, **kw), common_dir(path, **kw)
    return g is not None and c is not None and g != c


def current_branch(path: str, **kw) -> Optional[str]:
    """Short branch name, or None when detached or not a repo."""
    d = nearest_existing_dir(path)
    if d is None:
        return None
    return _first_line(run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=d, **kw))


def rev_parse(rev: str, cwd: str, **kw) -> Optional[str]:
    """Full object id for `rev`, or None if it does not resolve."""
    if rev.startswith("-"):
        raise ValueError("revision may not start with '-'")
    return _first_line(run_git(["rev-parse", "--verify", "-q", rev + "^{object}"], cwd=cwd, **kw))


def ref_exists(ref: str, cwd: str, **kw) -> bool:
    return rev_parse(ref, cwd, **kw) is not None


def show_file(rev: str, path: str, cwd: str, **kw) -> Optional[str]:
    """Contents of `path` at `rev` (`git show <rev>:<path>`), or None if absent."""
    if rev.startswith("-") or path.startswith("-"):
        raise ValueError("rev/path may not start with '-'")
    proc = run_git(["show", "%s:%s" % (rev, path)], cwd=cwd, binary=True, **kw)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def is_ancestor(ancestor: str, descendant: str, cwd: str, **kw) -> bool:
    return run_git(["merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd, **kw).returncode == 0


def is_clean(cwd: str, **kw) -> bool:
    proc = run_git(["status", "--porcelain=v1", "--untracked-files=normal"], cwd=cwd, **kw)
    return proc.returncode == 0 and not proc.stdout.strip()


def _check_cas_args(ref: str, new: str, old: Optional[str]) -> None:
    if not valid_ref(ref):
        raise ValueError("unsafe or malformed ref: %r" % (ref,))
    if not valid_oid(new):
        raise ValueError("new must be a full object id, got %r" % (new,))
    if old is not None and not valid_oid(old):
        raise ValueError("old must be a full object id or None, got %r" % (old,))


def update_ref_cas(ref: str, new: str, old: Optional[str], cwd: str, isolated: bool = False) -> CasResult:
    """L1 CAS: set `ref` to `new` only if it currently holds `old` (None: must not exist)."""
    _check_cas_args(ref, new, old)
    expect = old if old is not None else "0" * len(new)
    proc = run_git(["update-ref", "--no-deref", ref, new, expect], cwd=cwd, isolated=isolated, pin_hooks=True)
    return CasResult(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)


def delete_ref_cas(ref: str, old: str, cwd: str, isolated: bool = False) -> CasResult:
    """Delete `ref` only if it currently holds `old`."""
    if not valid_ref(ref) or not valid_oid(old):
        raise ValueError("unsafe ref or object id")
    proc = run_git(["update-ref", "--no-deref", "-d", ref, old], cwd=cwd, isolated=isolated, pin_hooks=True)
    return CasResult(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)


def _check_remote(remote: str) -> None:
    if not isinstance(remote, str) or not remote or remote.startswith("-") or "\n" in remote:
        raise ValueError("unsafe remote: %r" % (remote,))


def ls_remote_ref(remote: str, ref: str, cwd: str, isolated: bool = False) -> Optional[str]:
    """The sha `remote` holds for `ref`, or None if it does not exist."""
    _check_remote(remote)
    if not valid_ref(ref):
        raise ValueError("unsafe ref: %r" % (ref,))
    proc = run_git(["ls-remote", "--exit-code", "--", remote, ref], cwd=cwd, isolated=isolated, pin_hooks=True)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return None


def push_cas(remote: str, ref: str, new: str, old: Optional[str], cwd: str, isolated: bool = False) -> CasResult:
    """L2+ CAS: push `new` to `ref` only if the remote ref still holds `old`.

    `old=None` creates the ref only if it does not exist
    (`--force-with-lease=<ref>:` with an empty expected value).
    """
    _check_remote(remote)
    _check_cas_args(ref, new, old)
    lease = "--force-with-lease=%s:%s" % (ref, old if old is not None else "")
    argv = ["push", "--porcelain", lease, "--", remote, "%s:%s" % (new, ref)]
    proc = run_git(argv, cwd=cwd, isolated=isolated, pin_hooks=True, timeout=120)
    return CasResult(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)
