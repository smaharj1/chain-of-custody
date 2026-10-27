"""The serial fault-injection suite for the driver (report section 15 item 5, Phase 4c).

Every case runs the real driver (`loopctl run` with `runner: driver`, in-process through `loopctl.cli.main`) against a temp repo, a
temp HOME and the fake `claude` (`tests/helpers/fake_claude.py`, via `KIT_CLAUDE_BIN`). No test starts the real `claude`, touches the
network, or sleeps for real: the driver's waits are shortened through `KIT_DRIVER_TEST_TIMING` (honoured only when the binary is the
fake) and the backoff sleep is replaced.

`DriverRepo` is the 4a `LoopRepo` plus: the kit's agent files committed at the base sha, `runner: driver` and a budget in the config,
a fake `claude` whose scenario can be changed between runs, stub gates (the real ones are Phase 4b's), and helpers that write the
reports and reviews a child would produce.
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import shutil
import time
import types
from typing import Any, Callable, Dict, List, Optional, Sequence

from ... import REPO_ROOT, SCRIPTS_DIR
from ...helpers import bins
from .. import LoopRepo, LoopTestCase, Result, item_doc  # noqa: F401

from kitlib import runstate
from loopctl import common, driver, gates, launch
from loopctl.gates import GateResult

AGENT_NAMES = ("backend-engineer", "database-engineer", "app-engineer", "infra-engineer", "code-reviewer", "security-reviewer", "design-critic")
TIMING = {"poll_s": 0.03, "stall_s": 1.5, "canary_wait_s": 3.0, "term_grace_s": 0.5, "beat_s": 0.1}
BASE_CFG = {"loopctl_tier2": "true", "delivery": "local", "runner": "driver", "run_budget_usd": "10", "verify_tests": "python3 -m unittest",
            "security_review": "off", "max_retries_per_item": "2"}


def render_config(overrides=None):
    """A loop.config with the driver's settings and `overrides` ({key: scalar | {nested: scalar}}); a key is written once."""
    cfg = dict(BASE_CFG)
    cfg.update(overrides or {})
    lines = ["# Loop Config", "", "```yaml"]
    for k, v in cfg.items():
        if isinstance(v, dict):
            lines.append("%s:" % k)
            lines += ["  %s: %s" % (kk, vv) for kk, vv in v.items()]
        else:
            lines.append("%s: %s" % (k, v))
    lines += ["```", ""]
    return "\n".join(lines)


def read_text(path: str) -> str:
    """The file's text, or '' if it is not there (yet): for polling a pid file a fake child writes."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def agent_file(role: str) -> str:
    """The kit's own agent file for a role (so the frontmatter parser meets the real thing), else a minimal one."""
    path = os.path.join(REPO_ROOT, ".claude", "agents", "%s.md" % role)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return "---\nname: %s\ndescription: test role\ntools: Read\n---\nYou are %s.\n" % (role, role)


def report_text(status: str = "APPROVED", iteration: int = 1, files: Sequence[str] = ("src/x.py",), gaps: str = "- none") -> str:
    return ("# Backend Report\n\n## Status\n%s\n\n## Summary\nDid the work.\n\n## Files Changed\n%s\n\n## Tests Added (or N/A)\n- N/A\n\n"
            "## API Contract Status\nAS_SPECIFIED\n\n## Iteration\n%d\n\n## Open Questions / Brief Gaps\n%s\n\n## Primer Delta\nNONE\nchecked: layering\n\n"
            "## Doc Delta\nNONE\nchecked: README\n\n## Verification\n- python3 -m unittest: PASS\n"
            % (status, "\n".join("- `%s` — modified" % f for f in files), iteration, gaps))


def review_text(verdict: str = "CODE_REVIEW_APPROVED", kind: str = "Code Review") -> str:
    return "# %s — item (iteration 1)\n\n## Verdict\n%s\n\n## Summary\nLooks right.\n\n## Blockers\n- none\n" % (kind, verdict)


class GateStubs(object):
    """Stub gate modules registered under every known gate name, so the driver is tested against 4a's runner and not against 4b's gates."""

    def __init__(self) -> None:
        self.results = {}  # type: Dict[str, Any]       # gate name -> GateResult | callable(ctx) -> GateResult
        self.calls = []  # type: List[Dict[str, Any]]
        self.attrs = {}  # type: Dict[str, Dict[str, Any]]

    def install(self) -> None:
        for n in gates.KNOWN_GATES:
            gates.register(self._module(n))

    def remove(self) -> None:
        for n in gates.KNOWN_GATES:
            gates.unregister(n)

    def names(self) -> List[str]:
        return [c["gate"] for c in self.calls]

    def _module(self, n: str) -> Any:
        def run(ctx: Any) -> Any:
            self.calls.append({"gate": n, "item": ctx.item_id, "head": ctx.head_sha, "base": ctx.base_sha, "attempt": ctx.attempt, "cfg": ctx.cfg,
                               "worktree": ctx.worktree, "level": ctx.level})
            r = self.results.get(n)
            if callable(r):
                r = r(ctx)
            return r if r is not None else GateResult.passed(note="stub")
        m = types.SimpleNamespace(NAME=n, OWNER_METRIC="stub", run=run, __name__="stub_" + n)
        for k, v in self.attrs.get(n, {}).items():
            setattr(m, k, v)
        return m


class DriverRepo(LoopRepo):
    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, cfg: Optional[Dict[str, Any]] = None, scenario: Optional[Dict[str, Any]] = None,
                 agents: Sequence[str] = AGENT_NAMES, milestones: Optional[List[str]] = None, timing: Optional[Dict[str, float]] = None) -> None:
        super(DriverRepo, self).__init__(items=items, tier2=True, milestones=milestones, commit=False)       # one commit for everything, below
        self.cfg_text = render_config(cfg)
        self.write_plan(self.items)
        self.repo.write(".claude/loop.config.md", self.cfg_text)
        self.repo.write(".gitignore", "")
        for a in agents:
            self.repo.write(".claude/agents/%s.md" % a, agent_file(a))
        self.commit_all("plan, config and agents")
        self.base = self.repo.head()
        self.rs = runstate.RunState(self.repo.common_dir())
        self.claude = bins.install_fake_claude(os.path.join(self.home, "bin"), {})
        self.calls_log = os.path.join(self.home, "calls.jsonl")
        self.env["KIT_CLAUDE_BIN"] = self.claude
        self.env["KIT_CLAUDE_VERSION"] = "2.1.302"
        self.timing = dict(TIMING, **(timing or {}))
        self.env["KIT_DRIVER_TEST_TIMING"] = json.dumps(self.timing)
        self.scenario_base = {"nonce_dir": self.rs.nonces_dir, "events_dir": self.rs.events_dir, "log": self.calls_log, "read_stdin": True}
        self.set_scenario(scenario or {})
        self.stubs = GateStubs()
        self.sleeps = []  # type: List[float]

    # -- scenarios -------------------------------------------------------------------------------------------------
    def set_scenario(self, scenario: Dict[str, Any]) -> None:
        """Replace the fake's scenario and reset its counters (the global one and the per-role ones)."""
        bins.set_scenario(self.claude, dict(self.scenario_base, **scenario))
        for p in glob.glob(self.claude + ".state.role.*"):
            os.unlink(p)

    def calls(self) -> List[Dict[str, Any]]:
        return bins.read_jsonl(self.calls_log)

    def calls_for(self, role: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls() if c.get("role") == role]

    def slug(self, iid: str) -> str:
        return [i for i in self.items if i["id"] == iid][0]["slug"]

    def eng_run(self, iid: str, domain: str = "backend", status: str = "APPROVED", cost: Optional[float] = 0.05, iteration: int = 1,
                files: Optional[Dict[str, str]] = None, result: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        rel = "docs/features/%s/reports/%s.md" % (self.slug(iid), domain)
        wf = [{"path": rel, "content": report_text(status, iteration)}]
        for p, c in (files if files is not None else {"src/%s.py" % iid.lower(): "x = 1\n"}).items():
            wf.append({"path": p, "content": c})
        trailer = json.dumps({"status": status, "report_path": rel, "iteration": iteration})
        run = {"behavior": "succeed", "total_cost_usd": cost, "write_files": wf, "result": result if result is not None else "Done.\n" + trailer}
        run.update(kw)
        return run

    def review_run(self, verdict: str = "CODE_REVIEW_APPROVED", cost: Optional[float] = 0.03, kind: str = "Code Review", **kw: Any) -> Dict[str, Any]:
        run = {"behavior": "succeed", "total_cost_usd": cost, "result": review_text(verdict, kind)}
        run.update(kw)
        return run

    def happy(self, iids: Optional[Sequence[str]] = None, **more: Any) -> Dict[str, Any]:
        """A scenario in which every item's backend engineer and the code-reviewer succeed (the reviewer's last run repeats)."""
        ids = list(iids or [i["id"] for i in self.items])
        by_role = {"backend-engineer": [self.eng_run(i) for i in ids], "code-reviewer": [self.review_run()]}
        by_role.update(more.pop("by_role", {}))
        return dict({"by_role": by_role}, **more)

    # -- running the driver --------------------------------------------------------------------------------------------
    def approve(self) -> None:
        assert self.ctl("approve", "plan", "--by", "Pat").rc == 0

    def run(self, *extra: str, env: Optional[Dict[str, str]] = None) -> Result:
        """`loopctl run`. The driver writes to sys.stdout (it has no handle on the CLI's own stream), so capture that too."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = self.ctl("run", *extra, env=env)
        return Result(r.rc, buf.getvalue() + r.out, r.err)

    def driver_at(self, iid: str, *stages: str, begin: bool = True) -> "driver.Driver":
        """A Driver (not started) on a held lock, with `iid` begun on its branch and walked through `stages` (for tests of one step)."""
        loop = self.fast_start()
        run = self.rs.read_run()
        drv = driver.Driver(loop.ctx, loop, run, False, out=io.StringIO())              # the narration goes nowhere
        drv.ib = "main"
        drv.anchor = self.base
        drv.binary = self.claude
        drv.fake = True
        drv.timing = launch.Timing(**self.timing)
        if begin:
            drv.begin(iid)
            for st in stages:
                loop.advance(iid, st, self.session)
        return drv

    def gates_off(self) -> Dict[str, str]:
        return {n: "off" for n in gates.KNOWN_GATES}

    def spawn_run(self, procs: Any, *extra: str, env: Optional[Dict[str, str]] = None) -> Any:
        """`python3 .claude/scripts/loopctl run` as a real process (own session), for the tests that kill it. No gate is registered in
        another process, and the real ones (Phase 4b) must not run here: use `cfg={"gates": lr.gates_off()}`."""
        import sys
        full = dict(self.env)
        full.update(env or {})
        import subprocess
        return procs.spawn([sys.executable, os.path.join(SCRIPTS_DIR, "loopctl"), "run", "--repo", self.path] + list(extra), env=full,
                           stdin=subprocess.DEVNULL, cwd=self.path)

    def counters(self, iid: str) -> Dict[str, int]:
        return self.item(iid)["counters"]

    def children_left(self) -> List[str]:
        d = os.path.join(self.rs.root, "children")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def branch_names(self) -> List[str]:
        return [ln.strip().lstrip("* ") for ln in self.repo.git("branch", "--list").splitlines()]


class DriverTestCase(LoopTestCase):
    """Installs the stub gates and a no-op backoff sleep for every test, and removes them afterwards."""

    def setUp(self) -> None:
        super(DriverTestCase, self).setUp()
        self._saved = (driver.SLEEP, driver.Driver.compute_level)
        driver.Driver.compute_level = False
        self.slept = []  # type: List[float]
        self.skew = 0.0

        def fake_sleep(seconds: float, tick: Any) -> None:       # a backoff "passes" instantly: the loopctl clock jumps forward by it
            self.slept.append(seconds)
            self.skew += seconds
        driver.SLEEP = fake_sleep
        common.set_clock(lambda: time.time() + self.skew)
        self.addCleanup(self._restore)
        for n in list(gates._registered):
            gates.unregister(n)

    def _restore(self) -> None:
        common.set_clock(None)
        driver.SLEEP, driver.Driver.compute_level = self._saved
        for n in list(gates._registered):
            gates.unregister(n)

    def dr(self, **kw: Any) -> DriverRepo:
        r = DriverRepo(**kw)
        self.addCleanup(r.cleanup)
        r.stubs.install()
        return r
