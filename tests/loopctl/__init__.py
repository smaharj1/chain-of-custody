"""Tests for loopctl core (Phase 4a): plan, state, lock, failure, ledger, gates runner, render, status, reconcile, abort, CLI.

Every test uses a temp git repo (`LoopRepo`), a temp HOME, no network and no real `claude`. Commands run in-process through
`loopctl.cli.main` with an explicit environment, which is what makes the whole package testable in seconds; the entry point
`python3 .claude/scripts/loopctl` is exercised by a few subprocess tests.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Sequence

from .. import REPO_ROOT, SCRIPTS_DIR  # noqa: F401  (puts .claude/scripts on sys.path)
from ..helpers import TempRepo, clean_env
from ..helpers.gitrepo import git_test_env

from loopctl import cli, common  # noqa: E402

CONFIG_TEMPLATE = """# Loop Config

```yaml
loopctl_tier2: %(tier2)s
delivery: local
%(extra)s
```
"""


def item_doc(iid: str, deps: Sequence[str] = (), slug: Optional[str] = None, size: str = "S", milestone: str = "M1",
             acs: int = 2, **extra: Any) -> Dict[str, Any]:
    d = {"schema": "loopctl.item.v1", "id": iid, "milestone": milestone, "title": "Item %s" % iid, "slug": slug or ("slice-" + iid.lower()),
         "size": size, "dependsOn": list(deps), "intent": "Build slice %s." % iid,
         "acceptance": [{"id": "AC-%s-%d" % (iid, n), "text": "`GET /%s/%d` returns 200 and the test `tests/test_%s.py` passes" % (iid.lower(), n, iid.lower())}
                        for n in range(1, acs + 1)],
         "requiresEnv": []}
    d.update(extra)
    return d


class Result(object):
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.rc, self.out, self.err = rc, out, err

    @property
    def json(self) -> Any:
        return json.loads(self.out)

    def __repr__(self) -> str:
        return "Result(rc=%d, out=%r, err=%r)" % (self.rc, self.out[:200], self.err[:300])


class LoopRepo(object):
    """A temp repo on `main` with loop.config and a committed plan. `ctl()` runs loopctl in-process."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, tier2: bool = True, extra_cfg: str = "", plan_name: str = "demo",
                 milestones: Optional[List[str]] = None, commit: bool = True, real_gates: bool = False) -> None:
        # Phase 4a's tests judge the runner and the state machine with fake gates, so the gate modules Phase 4b installs are hidden from
        # discovery (`gates.REQUIRED_FOR` would otherwise bind every `advance`). tests/loopctl/gates/ passes real_gates=True.
        from loopctl import gates as _gates
        self._real_gates = real_gates
        if not real_gates:
            _gates._discovered = {}
        self.repo = TempRepo()
        self.path = self.repo.path
        self.home = tempfile.mkdtemp(prefix="loopctl-home-")
        self.env = clean_env(home=self.home)
        self.env.pop("CLAUDECODE", None)
        # The doctor would otherwise run the `claude` on PATH for its version (it only declines inside a Claude Code session). No test
        # may start the real binary, so the version is given explicitly and the binary override points at a path that does not exist.
        self.env["KIT_CLAUDE_VERSION"] = "2.1.302"
        self.env["KIT_CLAUDE_BIN"] = os.path.join(self.home, "no-such-claude")
        self.items = items if items is not None else [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"])]
        self.plan_name = plan_name
        self.cfg_text = CONFIG_TEMPLATE % {"tier2": "true" if tier2 else "false", "extra": extra_cfg}
        self.milestones = milestones or ["M1"]
        self.session = "sess-1"
        if commit:
            self.write_plan(self.items)
            self.repo.write(".claude/loop.config.md", self.cfg_text)
            self.repo.write(".gitignore", "")
            self.repo.git("add", "-A")
            self.repo.git("commit", "-q", "-m", "plan and config")
        self.base = self.repo.head()

    def write_plan(self, items: List[Dict[str, Any]]) -> None:
        self.repo.write("plan/plan.json", json.dumps({"schema": "loopctl.plan.v1", "name": self.plan_name, "goal": "Build the demo.",
                                                      "milestones": self.milestones}, indent=1))
        for fn in os.listdir(os.path.join(self.path, "plan", "items")) if os.path.isdir(os.path.join(self.path, "plan", "items")) else []:
            os.unlink(os.path.join(self.path, "plan", "items", fn))
        for it in items:
            self.repo.write("plan/items/%s.json" % it["id"], json.dumps(it, indent=1))

    def commit_all(self, msg: str = "change") -> str:
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", msg)
        return self.repo.head()

    def ctl(self, *args: str, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> Result:
        out, err = io.StringIO(), io.StringIO()
        full = list(args)
        if "--repo" not in full and args and args[0] not in ("help", "version"):
            full += ["--repo", cwd or self.path]
        e = dict(self.env)
        e.update(env or {})
        old = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(e)
            rc = cli.main(full, env=e, stdout=out, stderr=err)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return Result(rc, out.getvalue(), err.getvalue())

    # -- driving a run -----------------------------------------------------------------------------
    def canary(self, sid: Optional[str] = None) -> str:
        from kitlib import runstate
        rs = runstate.RunState.for_path(self.path)
        return rs.write_nonce(sid or self.session)

    def start_run(self, approve: bool = True) -> Result:
        if approve:
            r = self.ctl("approve", "plan", "--by", "Pat")
            assert r.rc == 0, r
        nonce = self.canary()
        return self.ctl("run", "--session", self.session, "--nonce", nonce)

    def c(self, *args: str) -> Result:
        """A conductor command: adds --session."""
        return self.ctl(*(list(args) + ["--session", self.session]))

    # -- the in-process API (about half the cost of a CLI call: no argument parsing, one Context for many calls) ----------------
    def context(self) -> Any:
        from loopctl.context import Context
        return Context.build(self.path, self.env)

    def loop(self, fresh: bool = False) -> Any:
        from loopctl.state import Loop
        if fresh or getattr(self, "_loop", None) is None:
            self._loop = Loop(self.context())
        return self._loop

    def fast_start(self, by: str = "Pat") -> Any:
        """Approve the plan and take the lock as the conductor, without the CLI's preflight (the CLI `run` has its own tests)."""
        loop = self.loop(fresh=True)
        loop.approve_plan(by)
        ctx = loop.ctx
        ctx.lock().acquire("run-1", self.session, ctx.repo, None, "rev")
        ctx.rs.register_session(self.session, "conductor", registered_by="test")
        ctx.rs.write_run({"runId": "run-1", "plan": self.plan_name, "baseSha": ctx.base_sha, "worktree": ctx.repo, "runner": "sequential",
                          "conductor_session_id": self.session})
        return loop

    def walk(self, iid: str, *stages: str) -> None:
        for st in stages:
            self.loop().advance(iid, st, self.session)

    def complete(self, iid: str, approve: bool = False) -> str:
        """Drive an item to merged: the stages, a real --no-ff merge with the trailer, then advance. Returns the merge commit."""
        loop = self.loop()
        loop.begin(iid, self.session)
        self.walk(iid, "design_review", "building", "verifying", "reviewing")
        mc = self.do_merge(iid, file_content="work for %s\n" % iid)
        if approve:
            loop.approve("item", iid, "Pat")
        loop.advance(iid, "merged", self.session, merge_commit=mc)
        return mc

    def quick_merge(self, iid: str, trailer: bool = True) -> str:
        """A real --no-ff merge with the trailer, and the state set to merged in ONE compare-and-swap (for tests that are about
        something other than the state machine; `complete()` walks the machine)."""
        mc = self.do_merge(iid, trailer=trailer, file_content="work for %s\n" % iid)
        from loopctl import state as sm

        def mutate(st: Any) -> None:
            r = st.item(iid)
            r["mergeCommit"] = mc
            r["branch"] = None
            sm._force_status(st, iid, "merged", "test", "quick merge")
        loop = self.loop()
        st, _ = loop.store.update(mutate, "quick merge %s" % iid)
        loop._after_merge(st, iid)                       # as advance() does: release the footprint lock
        from loopctl import branches
        for b in branches.list_branches(self.path, ("feat",)):
            branches.delete_merged(loop.ctx, b["name"], "main")        # merged branches are deleted after pass
        return mc

    def state(self) -> Any:
        from loopctl.context import Context
        ctx = Context.build(self.path, self.env)
        st, _ = ctx.store().read()
        return st

    def item(self, iid: str) -> Dict[str, Any]:
        return self.state().items[iid]

    def do_merge(self, iid: str, slug: Optional[str] = None, attempt: int = 1, trailer: bool = True, file_content: str = "x\n") -> str:
        """Simulate the loop's work for an item: a feat branch with one commit, merged --no-ff into main with the Loop-Item trailer."""
        slug = slug or [i for i in self.items if i["id"] == iid][0]["slug"]
        br = "feat/%s-a%d" % (slug, attempt)
        self.repo.git("switch", "-q", "-c", br, "main")
        self.repo.commit_file("src/%s.txt" % iid, file_content, "work for %s" % iid)
        self.repo.git("switch", "-q", "main")
        msg = "Merge branch '%s'" % br
        if trailer:
            msg += "\n\nLoop-Item: %s\nLoop-Attempt: %d" % (iid, attempt)
        self.repo.git("merge", "--no-ff", "-q", "-m", msg, br)
        return self.repo.head()

    def cleanup(self) -> None:
        if not getattr(self, "_real_gates", True):
            from loopctl import gates as _gates
            _gates._discovered = None
        self.repo.cleanup()
        shutil.rmtree(self.home, ignore_errors=True)


class LoopTestCase(unittest.TestCase):
    def lr(self, **kw: Any) -> LoopRepo:
        r = LoopRepo(**kw)
        self.addCleanup(r.cleanup)
        return r
