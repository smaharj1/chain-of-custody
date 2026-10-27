"""Temp git repo and local bare-remote factories (IMP-14).

Git runs with a pinned identity, fixed dates and no global/system config, so results do
not depend on the machine. Tests push only to a bare repo created here, never to a real
`origin`.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from typing import Dict, Optional, Tuple

__all__ = ["TempRepo", "BareRemote", "make_bare_remote", "make_repo_with_remote", "git_test_env"]


def git_test_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and not k.startswith("AWS_") and not k.startswith("GH_")
           and not k.startswith("GITHUB_") and k not in ("SSH_AUTH_SOCK",)}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Kit Test",
        "GIT_AUTHOR_EMAIL": "kit-test@example.invalid",
        "GIT_COMMITTER_NAME": "Kit Test",
        "GIT_COMMITTER_EMAIL": "kit-test@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
        "LC_ALL": "C",
    })
    if extra:
        env.update(extra)
    return env


def run(args, cwd, check=True, env=None, input_text=None):
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env or git_test_env(),
                          input=input_text.encode() if input_text else None,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed (%d): %s" % (" ".join(args), proc.returncode, err))
    return proc.returncode, out, err


# Speed (task 13a): the suite is dominated by process creation (every `git` costs ~4 ms of CPU on a Mac), so a repo is
# built once per test process and later `TempRepo()` calls copy it. The copy is byte-for-byte what `git init` + the
# initial commit would have produced (fixed identity and dates, so even the commit id is the same). Sample hooks are not
# copied and `maintenance.auto`/`gc.auto` are off in the repo's own config (a commit would otherwise start a second
# `git maintenance` process); nothing in the repo depends on either.
_REPO_CONFIG_TAIL = "[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"
_SCRATCH = []  # type: list


def scratch_root() -> str:
    """A per-process directory for caches (templates, snapshots); removed when the process exits."""
    if not _SCRATCH:
        d = os.path.realpath(tempfile.mkdtemp(prefix="kit-cache-"))
        _SCRATCH.append(d)
        atexit.register(shutil.rmtree, d, True)
    return _SCRATCH[0]


def copy_tree(src: str, dst: str) -> None:
    """Copy a repo (or any tree) the cheap way: symlinks kept, sample hooks skipped, no metadata beyond mtimes."""
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("*.sample"))


_TEMPLATES = {}  # type: dict


def _template_repo(branch: str, initial_commit: bool) -> str:
    key = (branch, initial_commit)
    if key not in _TEMPLATES:
        d = tempfile.mkdtemp(prefix="repo-%s-%d-" % (branch.replace("/", "_"), int(initial_commit)), dir=scratch_root())
        path = os.path.join(d, "repo")
        os.makedirs(path)
        run(["init", "-q", "-b", branch], path)
        with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write(_REPO_CONFIG_TAIL)
        if initial_commit:
            with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# temp repo\n")
            env = git_test_env()
            run(["add", "--", "README.md"], path, env=env)
            run(["commit", "-q", "-m", "initial commit"], path, env=env)
        _TEMPLATES[key] = path
    return _TEMPLATES[key]


class TempRepo(object):
    """A fresh repo on branch `main`, with one commit unless `initial_commit=False`.

    `clone_from` (a directory holding an existing repo, e.g. a snapshot) makes the new repo a copy of it instead."""

    def __init__(self, parent: Optional[str] = None, name: str = "repo", branch: str = "main",
                 initial_commit: bool = True, clone_from: Optional[str] = None) -> None:
        self._owns_parent = parent is None
        self.parent = os.path.realpath(parent or tempfile.mkdtemp(prefix="kit-repo-"))
        self.path = os.path.join(self.parent, name)
        self.env = git_test_env()
        copy_tree(clone_from or _template_repo(branch, initial_commit), self.path)

    def git(self, *args, **kw) -> str:
        """Run git, return stdout (stripped). Raises AssertionError on failure unless check=False."""
        check = kw.pop("check", True)
        rc, out, err = run(args, self.path, check=check, env=self.env, input_text=kw.pop("input_text", None))
        return out.strip()

    def git_rc(self, *args) -> Tuple[int, str, str]:
        return run(args, self.path, check=False, env=self.env)

    def write(self, rel: str, content: str = "", mode: Optional[int] = None) -> str:
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(p, mode)
        return p

    def commit_file(self, rel: str, content: str, message: str = "commit") -> str:
        self.write(rel, content)
        self.git("add", "--", rel)
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        """HEAD's commit id. Read from the repo's files when that is unambiguous (no process), else from git."""
        sha = self._read_head()
        return sha if sha else self.git("rev-parse", "HEAD")

    def _read_head(self) -> Optional[str]:
        gitdir = os.path.join(self.path, ".git")
        try:
            with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as fh:
                head = fh.read().strip()
            if not head.startswith("ref: "):
                return head if len(head) == 40 and all(c in "0123456789abcdef" for c in head) else None
            ref = head[5:]
            try:
                with open(os.path.join(gitdir, *ref.split("/")), encoding="utf-8") as fh:
                    sha = fh.read().strip()
            except FileNotFoundError:
                sha = ""
                with open(os.path.join(gitdir, "packed-refs"), encoding="utf-8") as fh:
                    for line in fh:
                        if line.rstrip("\n").endswith(" " + ref) and not line.startswith(("#", "^")):
                            sha = line.split(" ", 1)[0]
            return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
        except OSError:
            return None

    def common_dir(self) -> str:
        return os.path.realpath(os.path.join(self.path, self.git("rev-parse", "--git-common-dir")))

    def add_worktree(self, name: str, branch: Optional[str] = None) -> str:
        wt = os.path.join(self.parent, name)
        if branch:
            self.git("worktree", "add", "-q", "-b", branch, wt)
        else:
            self.git("worktree", "add", "-q", "--detach", wt)
        return os.path.realpath(wt)

    def add_remote(self, name: str, url: str) -> None:
        self.git("remote", "add", name, url)

    def cleanup(self) -> None:
        shutil.rmtree(self.parent if self._owns_parent else self.path, ignore_errors=True)


class BareRemote(object):
    """A local bare repository used as a push target."""

    def __init__(self, parent: Optional[str] = None, name: str = "remote.git") -> None:
        self._owns_parent = parent is None
        self.parent = os.path.realpath(parent or tempfile.mkdtemp(prefix="kit-remote-"))
        self.path = os.path.join(self.parent, name)
        run(["init", "-q", "--bare", "-b", "main", self.path], self.parent)

    def git(self, *args) -> str:
        return run(["--git-dir", self.path] + list(args), self.parent, env=git_test_env())[1].strip()

    def ref(self, ref: str) -> Optional[str]:
        rc, out, _ = run(["--git-dir", self.path, "rev-parse", "--verify", "-q", ref], self.parent,
                         check=False, env=git_test_env())
        return out.strip() if rc == 0 else None

    def cleanup(self) -> None:
        if self._owns_parent:
            shutil.rmtree(self.parent, ignore_errors=True)


def make_bare_remote(parent: Optional[str] = None, name: str = "remote.git") -> BareRemote:
    return BareRemote(parent, name)


def make_repo_with_remote(remote_name: str = "origin") -> Tuple[TempRepo, BareRemote]:
    """A repo whose `origin` is a fresh local bare repo, with `main` already pushed.
    The caller owns cleanup of both (BaseTestCase.repo_with_remote registers them)."""
    repo = TempRepo()
    bare = BareRemote()
    repo.add_remote(remote_name, bare.path)
    repo.git("push", "-q", remote_name, "main")
    return repo, bare
