"""Phase 8 tests: parallel items (report 11.5 item 3, 10.3, 16.1 row 8; DEC-9, DEC-34; D-6, D-29, L-6).

The pure parts (footprints, leases, ports, worktrees, the hook) are tested directly. The scheduler is tested through `loopctl run` with
`parallel_mode: worktree`, a temp repo, a temp HOME and the fake `claude`: no test starts the real binary, touches the network or sleeps for real
(the driver's waits are shortened through KIT_DRIVER_TEST_TIMING; a test waits on events, never on a clock).

`ParRepo` is the 4c `DriverRepo` with `parallel_mode: worktree`, a committed evidence file, and a fake `claude` that plays the run written for the
ITEM named in the prompt (`par_claude.py`): in a parallel run nobody controls which child starts first, so a script by call order would be a lottery.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any, Dict, List, Optional, Sequence

from ... import REPO_ROOT, SCRIPTS_DIR  # noqa: F401
from ..fault_injection import DriverRepo, DriverTestCase, item_doc, report_text, review_text  # noqa: F401
from ...helpers import bins

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = {"schema": "evidence.v1", "fixture_runs": [{"id": "r1", "clean": True}, {"id": "r2", "clean": True}, {"id": "r3", "clean": True}],
            "real_feature": {"id": "f1", "completed": True}, "attested_by": "Pat", "attested_at": "2026-10-20"}
ENG = {"loc": 100, "engineers": ["backend-engineer"]}


def put(path: str, text: str = "") -> str:
    """Write a small file (and close it)."""
    with open(path, "w") as fh:
        fh.write(text)
    return path


def read(path: str) -> str:
    with open(path, "r") as fh:
        return fh.read()


def pitem(iid: str, deps: Sequence[str] = (), paths: Optional[Sequence[str]] = None, schema: bool = False, deps_change: bool = False, **kw: Any) -> Dict[str, Any]:
    """A plan item that declares where it works: `paths` defaults to `src/<id>/**`."""
    paths = list(paths) if paths is not None else ["src/%s/**" % iid.lower()]
    kw.setdefault("estimate", ENG)
    kw.setdefault("touches", {"domains": ["backend"], "paths": paths, "schema": schema, "deps_change": deps_change})
    return item_doc(iid, list(deps), **kw)


class ParRepo(DriverRepo):
    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, cfg: Optional[Dict[str, Any]] = None, **kw: Any) -> None:
        full = {"parallel_mode": "worktree", "max_parallel": "2"}
        full.update(cfg or {})
        super(ParRepo, self).__init__(items=items, cfg=full, **kw)
        self.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
        self.commit_all("evidence")
        self.base = self.repo.head()
        # the per-item fake in place of the shared one
        d = os.path.join(self.home, "bin")
        self.claude = os.path.join(d, "claude")
        with open(os.path.join(HERE, "par_claude.py"), "r") as fh:
            lines = fh.read().split("\n")
        lines[0] = "#!" + sys.executable
        with open(self.claude, "w") as fh:
            fh.write("\n".join(lines))
        os.chmod(self.claude, os.stat(self.claude).st_mode | stat.S_IXUSR)
        self.shared = os.path.join(d, "fake_claude.py")                      # par_claude imports the shared fake from its own directory
        with open(os.path.join(REPO_ROOT, "tests", "helpers", "fake_claude.py"), "r") as src, open(self.shared, "w") as dst:
            dst.write(src.read())
        self.env["KIT_CLAUDE_BIN"] = self.claude
        self.par_scenario = {}  # type: Dict[str, Any]
        self.set_scenario({})

    # -- scenarios ------------------------------------------------------------------------------------------------------------------------
    def set_scenario(self, scenario: Dict[str, Any]) -> None:
        """`scenario` is {"items": {id: {role: [run, ...]}}, "roles": {role: [run]}, "default": run}. Counters and logs are reset."""
        for suffix in (".log", ".fakestate"):
            try:
                os.unlink(self.claude + ".par" + suffix if suffix == ".log" else self.claude + suffix)
            except OSError:
                pass
        import glob
        for p in glob.glob(self.claude + ".state.*"):
            os.unlink(p)
        doc = dict(scenario)
        doc["base"] = dict(self.scenario_base, **scenario.get("base", {}))
        with open(self.claude + ".par.json", "w") as fh:
            json.dump(doc, fh)

    def play(self, iids: Optional[Sequence[str]] = None, delay: float = 0.0, per_item: Optional[Dict[str, Dict[str, Any]]] = None,
             files: Optional[Dict[str, Dict[str, str]]] = None, **more: Any) -> Dict[str, Any]:
        """Every listed item's engineer succeeds (writing `files[id]`, default `src/<id>/m.py`) and the code-reviewer approves. `delay` is how long each
        engineer child takes, in the child's own process (the test never sleeps). `per_item[id]` replaces an item's roles."""
        ids = list(iids or [i["id"] for i in self.items])
        items = {}
        for i in ids:
            fs = (files or {}).get(i) or {"src/%s/m.py" % i.lower(): "x = 1\n"}
            eng = self.eng_run(i, files=fs)
            if delay:
                eng["delay"] = delay
            items[i] = {"backend-engineer": [eng], "code-reviewer": [self.review_run()]}
        items.update(per_item or {})
        return dict({"items": items, "roles": {"code-reviewer": [self.review_run()]}}, **more)

    def child_log(self) -> List[Dict[str, Any]]:
        return bins.read_jsonl(self.claude + ".par.log")

    def overlapped(self, a: str, b: str) -> bool:
        """Did a child of item `a` and a child of item `b` run at the same moment?"""
        log = self.child_log()
        for x in [r for r in log if r["item"] == a]:
            for y in [r for r in log if r["item"] == b]:
                if x["start"] < y["end"] and y["start"] < x["end"]:
                    return True
        return False

    def order_merged(self, since: Optional[str] = None) -> List[str]:
        """Item ids in the order their merge commits sit on main (oldest first), after `since` if given."""
        out = []  # type: List[str]
        for ln in self.repo.git("log", "--merges", "--reverse", "--format=%B%x1e", "%s..main" % since if since else "main").split("\x1e"):
            for l in ln.splitlines():
                if l.startswith("Loop-Item: "):
                    out.append(l.split(": ", 1)[1].strip())
        return out

    def leases_left(self) -> List[str]:
        d = os.path.join(self.rs.root, "leases")
        return sorted(n for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []

    def worktrees_left(self) -> List[str]:
        d = os.path.join(self.path, ".claude", "worktrees")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def events(self, name: str = "loopctl") -> List[Dict[str, Any]]:
        return self.rs.read_events(name)


class ParTestCase(DriverTestCase):
    def pr(self, **kw: Any) -> ParRepo:
        r = ParRepo(**kw)
        self.addCleanup(r.cleanup)
        r.stubs.install()
        return r


class DriverCase(ParTestCase):
    """One `ParRepo` for the whole class and a `ParallelDriver` built over it for each test (not started: the tests call its methods).

    A test that stages items in the repo must use ids no other test uses, and takes the ids it needs from the class's plan: the plan is committed once.
    """
    ITEMS = []  # type: List[Dict[str, Any]]
    CFG = {}  # type: Dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        super(DriverCase, cls).setUpClass()
        cls.lr = ParRepo(items=cls.ITEMS, cfg=cls.CFG)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.lr.cleanup()

    def setUp(self) -> None:
        super(DriverCase, self).setUp()
        self.lr.stubs.calls = []
        self.lr.stubs.results = {}
        self.lr.stubs.install()
        self.start_tip = self.lr.repo.git("rev-parse", "main")
        self.drv = self.make_driver()
        self.addCleanup(self.clean_up)

    def make_driver(self) -> Any:
        import io
        from loopctl import launch
        from loopctl.parallel import scheduler
        lr = self.lr
        loop = lr.fast_start()
        run = lr.rs.read_run()
        drv = scheduler.ParallelDriver(loop.ctx, loop, run, False, out=io.StringIO())
        drv.ib = "main"
        drv.anchor = lr.base
        drv.binary = lr.claude
        drv.fake = True
        drv.timing = launch.Timing(**lr.timing)
        drv.index = {i: k for k, i in enumerate(loop._order())}
        drv.include = []
        return drv

    def clean_up(self) -> None:
        from loopctl.parallel import worktrees
        for p in worktrees.children(self.lr.path):
            try:
                worktrees.remove(self.lr.path, p, save=False)
            except Exception:  # noqa: BLE001
                pass
        for rec in self.drv.leases.list():
            self.drv.leases.remove(rec["name"])

    def stage(self, iid: str, files: Optional[Dict[str, str]] = None, ready: bool = True, upto: str = "reviewing") -> Any:
        """Put an item where a finished worker leaves it: begun, on its branch in its own worktree with `files` committed, walked to `upto`, a slot that is
        READY. No child session is started."""
        from loopctl import branches
        from loopctl.parallel import mutex, scheduler, worktrees
        from kitlib import gitutil
        drv = self.drv
        item = drv.item_def(iid)
        rec = drv.loop.begin(iid, drv.sid, "driver", max_active=10)
        tip = gitutil.rev_parse("refs/heads/main^{commit}", drv.repo, isolated=True)
        drv.item_base[iid] = tip
        branches.create(drv.repo, rec["branch"], tip)
        path = worktrees.create(drv.repo, worktrees.loop_name(item["slug"], rec["attempt"]), rec["branch"])
        lease = drv.leases.acquire(mutex.item_lease(iid), "worker:" + iid, item=iid)
        block = drv.ports.allocate("worker:" + iid, item=iid)
        slot = scheduler.Slot(iid, rec["attempt"], rec["branch"], path, [lease], block, [])
        slot.worker = scheduler.Worker(drv, slot)
        drv.slots[iid] = slot
        default = {"src/%s/m.py" % iid.lower(): "x = 1\n"}
        for rel, text in (files if files is not None else default).items():
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            put(full, text)
        if files != {}:
            self.lr.repo.git("-C", path, "add", "-A")
            self.lr.repo.git("-C", path, "commit", "-q", "-m", "WIP(loopctl): %s" % iid)
        for st in ("design_review", "building", "verifying", "reviewing"):
            drv.loop.advance(iid, st, drv.sid, "driver", head=self.lr.repo.git("-C", path, "rev-parse", "HEAD"))
            if st == upto:
                break
        slot.done.set()
        slot.harvested = True
        slot.ready = ready
        return slot

    def main_files(self) -> List[str]:
        return self.lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines()
