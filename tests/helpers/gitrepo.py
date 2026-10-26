"""Temp git repo and local bare-remote factories (IMP-14).

Git runs with a pinned identity, fixed dates and no global/system config, so results do
not depend on the machine. Tests push only to a bare repo created here, never to a real
`origin`.
"""
from __future__ import annotations

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


class TempRepo(object):
    """A fresh repo on branch `main`, with one commit unless `initial_commit=False`."""

    def __init__(self, parent: Optional[str] = None, name: str = "repo", branch: str = "main",
                 initial_commit: bool = True) -> None:
        self._owns_parent = parent is None
        self.parent = os.path.realpath(parent or tempfile.mkdtemp(prefix="kit-repo-"))
        self.path = os.path.join(self.parent, name)
        os.makedirs(self.path)
        run(["init", "-q", "-b", branch], self.path)
        self.env = git_test_env()
        if initial_commit:
            self.commit_file("README.md", "# temp repo\n", "initial commit")

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
        return self.git("rev-parse", "HEAD")

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
