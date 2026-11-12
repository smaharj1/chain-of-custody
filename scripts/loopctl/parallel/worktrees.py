"""Worker worktrees (report 11.5 item 3, 19.7 L-1 / L-3, 5.4; D-2, D-3, D-22, CO-15). The ONLY place a worktree is created or removed.

    <repo toplevel>/.claude/worktrees/<slug>-a<n>     one worker's checkout of its item's branch `feat/<slug>-a<n>`

The conductor creates them itself, off the local integration tip (D-3: `isolation: worktree` branches from the remote default branch, and
whether `worktree.baseRef: head` is honoured is unverified, V6 / V25), so a worktree is `git worktree add <path> <existing branch>` and nothing else.
Every path that reaches `git worktree remove` or `rmtree` passes `safe_path`: a plain name, directly under the repository's own
`.claude/worktrees/`, with no symlink on the way. A name or path from a hook's input, a state file or a lease that points anywhere else is
refused, never normalised into something that works.

`.worktreeinclude` (report 5.4): the project's list of ignored files to copy into a new worktree. Only `.env.local` and `.env.test` are ever
copied (the doctor's `secrets.worktreeinclude` check says the same); any other line is reported and ignored, never honoured. The file is read at the
run's base sha, and a copied file is private to its owner (0600).

A crash leaves worktrees behind. `cleanup_orphans` removes the ones that belong to loopctl (the `<slug>-a<n>` names) and that no live worker owns,
and it saves their uncommitted work first as a `WIP(loopctl)` commit on the item's own branch, so the recovery that renames the branch to `dead/*` keeps it.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kitlib import gitutil, jsonio

from .. import branches, common, lgit
from ..common import Refused, UsageError

__all__ = ["ROOT_REL", "ALLOWED_INCLUDE", "root_of", "loop_name", "valid_loop_name", "valid_hook_name", "safe_path", "ensure_excluded", "create",
           "remove", "inside_root", "registered", "children", "orphans", "cleanup_orphans", "parse_include", "read_include", "apply_include", "preserve"]

ROOT_REL = ".claude/worktrees"
ALLOWED_INCLUDE = (".env.local", ".env.test")
_LOOP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}-a[0-9]{1,4}\Z")
_HOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
MAX_INCLUDE_FILE = 64 * 1024
_IDENT = {"GIT_AUTHOR_NAME": "loopctl driver", "GIT_AUTHOR_EMAIL": "loopctl@localhost.invalid",
          "GIT_COMMITTER_NAME": "loopctl driver", "GIT_COMMITTER_EMAIL": "loopctl@localhost.invalid"}


def _git(repo: str, args: Sequence[str], **kw: Any):
    return lgit.run_git(list(args), cwd=repo, isolated=True, pin_hooks=True, **kw)


def loop_name(slug: str, attempt: int) -> str:
    n = "%s-a%d" % (slug, attempt)
    if not common.valid_slug(slug) or not _LOOP_NAME.match(n):
        raise UsageError("bad slug or attempt for a worktree name")
    return n


def valid_loop_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_LOOP_NAME.match(name))


def valid_hook_name(name: Any) -> bool:
    """A name a WorktreeCreate hook may be given: plain characters, no separator, no dot-only name, and not a `<slug>-a<n>` name (those are the driver's:
    its orphan cleanup would take a person's worktree of that name for a crashed worker's)."""
    return isinstance(name, str) and bool(_HOOK_NAME.match(name)) and name not in (".", "..") and not name.endswith(".lock") and not _LOOP_NAME.match(name)


def root_of(repo: str) -> str:
    """`<repo>/.claude/worktrees`, with the repo resolved. It need not exist yet."""
    return os.path.join(os.path.realpath(repo), *ROOT_REL.split("/"))


def _escapes(repo: str) -> Optional[str]:
    """Why the worktrees root is not a plain directory inside the repository, or None. A symlink at `.claude` or `.claude/worktrees` would turn every
    later path into a write somewhere else."""
    top = os.path.realpath(repo)
    c = os.path.join(top, ".claude")
    r = os.path.join(c, "worktrees")
    for p in (c, r):
        if os.path.islink(p):
            return "%s is a symbolic link" % os.path.relpath(p, top)
    if os.path.lexists(r) and os.path.realpath(r) != r:
        return "%s resolves outside the repository" % ROOT_REL
    return None


def safe_path(repo: str, name: str, kind: str = "loop") -> str:
    """The absolute path for a worktree called `name`, or Refused. `kind` is "loop" (`<slug>-a<n>`) or "hook" (any plain name)."""
    ok = valid_loop_name(name) if kind == "loop" else valid_hook_name(name)
    if not ok:
        raise Refused("%r is not an acceptable worktree name (a plain name, no path)" % common.clean_text(name, 40))
    why = _escapes(repo)
    if why:
        raise Refused("worktrees are not created here: %s" % why)
    return os.path.join(root_of(repo), name)


def inside_root(repo: str, path: str) -> bool:
    """True if `path`, resolved, is strictly inside the worktrees root and no component below the root is a symbolic link."""
    if _escapes(repo):
        return False
    root = root_of(repo)
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep) or real == root:
        return False
    rel = os.path.relpath(real, root).split(os.sep)
    cur = root
    for part in rel[:1]:                         # only the worktree's own directory matters: what is inside it belongs to the checkout
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
    return True


def ensure_excluded(repo: str) -> None:
    """Keep the worktrees out of `git status` of the repository that holds them (local to the clone, in info/exclude)."""
    cd = gitutil.common_dir(repo, isolated=True)
    if not cd:
        return
    path = os.path.join(cd, "info", "exclude")
    line = "/" + ROOT_REL + "/"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            have = fh.read()
    except OSError:
        have = ""
    if line in have.splitlines():
        return
    jsonio.ensure_dir(os.path.dirname(path), 0o755)
    jsonio.atomic_write_text(path, have + ("" if have.endswith("\n") or not have else "\n") + line + "\n", 0o644)


def registered(repo: str) -> List[Dict[str, Any]]:
    """`git worktree list --porcelain` as [{path, head, branch, detached, prunable}]."""
    proc = _git(repo, ["worktree", "list", "--porcelain"])
    out = []  # type: List[Dict[str, Any]]
    cur = {}  # type: Dict[str, Any]
    for ln in proc.stdout.splitlines() + [""]:
        if not ln.strip():
            if cur.get("path"):
                out.append(cur)
            cur = {}
        elif ln.startswith("worktree "):
            cur = {"path": os.path.realpath(ln[9:]), "head": None, "branch": None, "detached": False, "prunable": False}
        elif cur and ln.startswith("HEAD "):
            cur["head"] = ln[5:]
        elif cur and ln.startswith("branch "):
            cur["branch"] = ln[7:].replace("refs/heads/", "", 1)
        elif cur and ln == "detached":
            cur["detached"] = True
        elif cur and ln.startswith("prunable"):
            cur["prunable"] = True
    return out


def create(repo: str, name: str, branch: str, kind: str = "loop") -> str:
    """`git worktree add <root>/<name> <branch>`: the branch must exist (the driver cut it off the integration tip) and not be checked out
    anywhere else. Returns the worktree's real path."""
    path = safe_path(repo, name, kind)
    if os.path.lexists(path):
        raise Refused("worktree path %s already exists" % os.path.relpath(path, os.path.realpath(repo)))
    if not isinstance(branch, str) or branch.startswith("-") or not gitutil.valid_ref("refs/heads/" + branch) or not gitutil.rev_parse("refs/heads/%s^{commit}" % branch, repo, isolated=True):
        raise Refused("branch %r does not exist" % common.clean_text(branch, 60))
    ensure_excluded(repo)
    jsonio.ensure_dir(root_of(repo), 0o755)
    if _escapes(repo):
        raise Refused("worktrees are not created here: %s" % _escapes(repo))
    r = _git(repo, ["worktree", "add", "--quiet", path, branch])
    if r.returncode != 0:
        raise Refused("git worktree add failed: %s" % common.clean_text(r.stderr, 160))
    return os.path.realpath(path)


def preserve(path: str) -> bool:
    """Commit what a dead worker left in its tree to the item's own branch, so nothing is lost when the worktree goes. Only on a feat/* branch."""
    cur = branches.current(path)
    p = branches.parse(cur) if cur else None
    if not p or p[0] != "feat" or lgit.is_clean(path):
        return False
    if _git(path, ["add", "-A"]).returncode != 0:
        return False
    return _git(path, ["commit", "-q", "-m", "WIP(loopctl): work left in the worktree when the run stopped"], env=dict(_IDENT)).returncode == 0


def remove(repo: str, path: str, save: bool = True) -> bool:
    """Take a worktree away: save its uncommitted work (a `WIP(loopctl)` commit on the item's branch), `git worktree remove --force`, prune, and
    delete what is left only if it is a directory directly under the worktrees root. True if the path is gone. Anything else: Refused."""
    if not inside_root(repo, path):
        raise Refused("%s is not a worktree under %s: nothing was removed" % (common.clean_text(path, 100), ROOT_REL))
    real = os.path.realpath(path)
    if not os.path.lexists(real):
        _git(repo, ["worktree", "prune"])
        return True
    if save:
        try:
            preserve(real)
        except (OSError, Refused):
            pass
    if any(w["path"] == real for w in registered(repo)):
        r = _git(repo, ["worktree", "remove", "--force", real])
        if r.returncode != 0 and os.path.lexists(real):
            raise Refused("git worktree remove failed: %s" % common.clean_text(r.stderr, 160))
    if os.path.lexists(real) and os.path.dirname(real) == root_of(repo) and not os.path.islink(real):
        shutil.rmtree(real, ignore_errors=True)
    _git(repo, ["worktree", "prune"])
    return not os.path.lexists(real)


def children(repo: str) -> List[str]:
    """The directories directly under the worktrees root."""
    try:
        return sorted(os.path.join(root_of(repo), n) for n in os.listdir(root_of(repo)) if os.path.isdir(os.path.join(root_of(repo), n)))
    except OSError:
        return []


def orphans(repo: str, live: Iterable[str] = ()) -> List[str]:
    """loopctl's own worktrees (the `<slug>-a<n>` names) that no live worker owns."""
    keep = {os.path.realpath(p) for p in live}
    return [p for p in children(repo) if valid_loop_name(os.path.basename(p)) and os.path.realpath(p) not in keep]


def cleanup_orphans(repo: str, live: Iterable[str] = ()) -> List[str]:
    """Remove every orphan, saving its work first. Returns the paths removed; one that cannot be removed raises Refused (and is left alone)."""
    gone = []  # type: List[str]
    for p in orphans(repo, live):
        if remove(repo, p, save=True):
            gone.append(p)
    return gone


# -- .worktreeinclude ----------------------------------------------------------------------------------------------------------------

def parse_include(text: str) -> Tuple[List[str], List[str]]:
    """(names to copy, lines refused). Only the exact names in ALLOWED_INCLUDE are ever honoured: no path, no glob, no negation."""
    ok, bad = [], []  # type: List[str], List[str]
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln in ALLOWED_INCLUDE:
            if ln not in ok:
                ok.append(ln)
        else:
            bad.append(common.clean_text(ln, 60))
    return ok, bad


def read_include(repo: str, rev: Optional[str]) -> Optional[str]:
    """`.worktreeinclude` as committed at `rev` (the run's base sha), else None."""
    if not rev:
        return None
    return gitutil.show_file(rev, ".worktreeinclude", repo, isolated=True)


def apply_include(src_root: str, dst: str, names: Sequence[str]) -> List[str]:
    """Copy the named ignored files from the main checkout into the new worktree (0600). A name that is not exactly an allowed one, a source
    that is not a regular file (a symlink is refused: it could point at a credential), or a file that is large is skipped."""
    copied = []  # type: List[str]
    for n in names:
        if n not in ALLOWED_INCLUDE:
            continue
        src = os.path.join(src_root, n)
        if os.path.islink(src) or not os.path.isfile(src):
            continue
        try:
            if os.path.getsize(src) > MAX_INCLUDE_FILE:
                continue
            with open(src, "rb") as fh:
                data = fh.read(MAX_INCLUDE_FILE + 1)
        except OSError:
            continue
        target = os.path.join(dst, n)
        if os.path.islink(target):
            continue
        jsonio.atomic_write_bytes(target, data, 0o600)
        copied.append(n)
    return copied
