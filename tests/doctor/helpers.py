"""Shared builders for the boundary tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, List, Mapping, Optional

from kitlib import miniyaml

from doctorlib import (ci, codeowners, creds, deploy, github, levelcalc, secrets, settings, versions)  # noqa: F401  (register checks)
from doctorlib.model import Context, Result, default_run, run_checks

from tests import REPO_ROOT
from tests.helpers import BaseTestCase, TempRepo

DOCTOR = os.path.join(REPO_ROOT, ".claude", "scripts", "doctor.py")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FLOOR = "2.1.302"

KIT_VERSION_V2 = "2.0.0-rc.1 (2026-10-26)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.310\n"


def to_yaml(d: Mapping[str, Any], indent: int = 0) -> List[str]:
    """Render a small nested dict/list as the YAML subset miniyaml reads."""
    out = []  # type: List[str]
    pad = "  " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            out.append("%s%s:" % (pad, k))
            out.extend(to_yaml(v, indent + 1))
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            out.append("%s%s:" % (pad, k))
            for item in v:
                first = True
                for ik, iv in item.items():
                    out.append("%s  %s%s: %s" % (pad, "- " if first else "  ", ik, miniyaml.dump_scalar(iv)))
                    first = False
        elif isinstance(v, list):
            out.append("%s%s: [%s]" % (pad, k, ", ".join(miniyaml.dump_scalar(x) for x in v)))
        else:
            out.append("%s%s: %s" % (pad, k, miniyaml.dump_scalar(v)))
    return out


def config_text(values: Optional[Mapping[str, Any]] = None) -> str:
    body = "\n".join(to_yaml(values or {})) if values else ""
    return "# Loop Config\n\n```yaml\n%s\n```\n" % body


_SKELETON = []  # type: List[str]


def _skeleton() -> str:
    """A freshly initialised .git directory, built once per process and copied for each test repo (saves a `git init` per test)."""
    if not _SKELETON:
        import atexit
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="kit-skel-"))
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _SKELETON.append(d)
        atexit.register(shutil.rmtree, d, True)
    return _SKELETON[0]


class FastRepo(TempRepo):
    """A TempRepo that copies a prebuilt .git instead of running `git init`; no initial commit."""

    def __init__(self) -> None:
        import shutil
        import tempfile
        from tests.helpers.gitrepo import git_test_env
        self._owns_parent = True
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="kit-repo-"))
        self.path = os.path.join(self.parent, "repo")
        os.makedirs(self.path)
        shutil.copytree(os.path.join(_skeleton(), ".git"), os.path.join(self.path, ".git"))
        self.env = git_test_env()

    def add_remote(self, name: str, url: str) -> None:
        with open(os.path.join(self.path, ".git", "config"), "a") as fh:
            fh.write('[remote "%s"]\n\turl = %s\n\tfetch = +refs/heads/*:refs/remotes/%s/*\n' % (name, url, name))


def guarded_run(argv, timeout=10.0, env=None, cwd=None, input_text=None):
    """The default process seam in tests: real git, nothing else. `aws`, `gh`, `ssh`, `docker`, `sudo` and `claude` are never started."""
    if argv and argv[0] == "git":
        return default_run(argv, timeout, env, cwd, input_text)
    if argv and argv[0] == "sudo":
        return 1, "", "sudo: a password is required"
    return 127, "", "not found (blocked in tests): %s" % (argv[0] if argv else "")


class BoundaryCase(BaseTestCase):
    """A base class that builds a kit-shaped repo and a doctor Context over it."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_home = self.home()
        self.fs_root = self.tmpdir("kit-root-")

    # -- repo -------------------------------------------------------------------------------
    def kit_repo(self, config: Optional[Mapping[str, Any]] = None, origin: str = "https://github.com/acme/widgets.git",
                 kit_version: str = KIT_VERSION_V2, files: Optional[Mapping[str, str]] = None, commit: bool = False) -> TempRepo:
        """A repo with the kit's files staged (so `git ls-files` sees them). `commit=True` also commits them."""
        r = FastRepo()
        self._repos.append(r)
        r.write(".claude/KIT_VERSION", kit_version)
        r.write(".claude/loop.config.md", config_text(config))
        r.write(".gitignore", "")
        for rel, text in (files or {}).items():
            r.write(rel, text)
        if origin:
            r.add_remote("origin", origin)
        r.git("add", "-A")
        if commit:
            r.git("commit", "-q", "-m", "kit files")
        return r

    # -- context ----------------------------------------------------------------------------
    def ctx(self, repo: TempRepo, env_extra: Optional[Mapping[str, str]] = None, ghapi=None, reach=None, live: bool = False,
            claude_version: Optional[str] = "2.1.305", run=None, **kw) -> Context:
        env = self.fake_home.env(env_extra)
        c = Context(repo=repo.path, home=self.fake_home.path, env=env, fs_root=self.fs_root, live=live, now=kw.pop("now", 1792000000.0),
                    ghapi=ghapi, reach=reach, claude_version=claude_version, run=run or guarded_run, platform=kw.pop("platform", "linux"),
                    groups=kw.pop("groups", []), **kw)
        return c

    GROUPS = None  # a set of check groups to run; None runs everything

    def results(self, ctx: Context, groups=None) -> List[Result]:
        return run_checks(ctx, groups if groups is not None else self.GROUPS)

    def check_one(self, ctx: Context, check_id: str) -> List[Result]:
        """Run a single registered check (no other gatherers, no git unless that check needs it)."""
        from doctorlib.model import CHECKS
        fn = [c["fn"] for c in CHECKS if c["id"] == check_id][0]
        res = fn(ctx)
        return [res] if isinstance(res, Result) else list(res)

    @staticmethod
    def by_id(results: List[Result], prefix: str) -> List[Result]:
        return [r for r in results if r.id == prefix or r.id.startswith(prefix)]

    def assertFails(self, results: List[Result], prefix: str, msg: str = "") -> List[Result]:
        got = [r for r in self.by_id(results, prefix) if r.status == "fail"]
        self.assertTrue(got, "expected a failing %s; got %s" % (prefix, [(r.id, r.status) for r in self.by_id(results, prefix)]))
        return got

    def assertClean(self, results: List[Result], prefix: str) -> None:
        got = self.by_id(results, prefix)
        self.assertTrue(got, "no result for %s" % prefix)
        bad = [r for r in got if r.status == "fail"]
        self.assertFalse(bad, "unexpected failure: %s" % bad)

    def write_json(self, repo: TempRepo, rel: str, data: Any) -> str:
        return repo.write(rel, json.dumps(data, indent=2))


def load_fixture(name: str) -> Any:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_doctor(args: List[str], env: Mapping[str, str], cwd: Optional[str] = None, timeout: int = 60):
    p = subprocess.run([sys.executable, DOCTOR] + args, env=dict(env), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
