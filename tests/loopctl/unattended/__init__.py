"""Phase 7 tests: unattended L4, notifications, the stall watchdog, demotion and promotion, two-stage intake (report 5.3, 6.3, 10.4, 16.1 row 7).

Everything runs against a temp repo, a temp HOME, the fake `claude` and a fake `notify_command` (a script that records what it was given). No test
sends anything, starts the real binary, touches the network or waits for real: the driver's waits are shortened through KIT_DRIVER_TEST_TIMING.

`UnattendedRepo` is the 4c `DriverRepo` with `autonomy: unattended` / `autonomy_level: L4`, a committed evidence file and kit version, spend ceilings
and a fake notifier outside the repository.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from ... import REPO_ROOT, SCRIPTS_DIR  # noqa: F401
from ..fault_injection import DriverRepo, DriverTestCase, item_doc, report_text, review_text  # noqa: F401

EVIDENCE = {"schema": "evidence.v1", "fixture_runs": [{"id": "r1", "clean": True}, {"id": "r2", "clean": True}, {"id": "r3", "clean": True}],
            "real_feature": {"id": "f1", "completed": True}, "attested_by": "Pat", "attested_at": "2026-10-20"}
KIT_VERSION = "2.0.0 (2026-10-01)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.400\n"
ENG = {"loc": 100, "engineers": ["backend-engineer"]}

_NOTIFY = '''#!%(python)s
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
try:
    behaviour = open(os.path.join(here, "behaviour")).read().strip()
except OSError:
    behaviour = "ok"
rec = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(), "env_names": sorted(os.environ),
       "env": {k: v for k, v in os.environ.items() if k.startswith("KIT_NOTIFY_") or k in ("TOPIC", "HOME")}}
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(rec, sort_keys=True) + "\\n")
if behaviour == "fail":
    sys.stderr.write("the channel is down")
    sys.exit(3)
if behaviour == "hang":
    time.sleep(60)
sys.exit(0)
'''


class FakeNotify(object):
    """A `notify_command` that records every call (argv, stdin, the environment it got) and can be told to fail or hang."""

    def __init__(self) -> None:
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="notify-fake-"))
        self.path = os.path.join(self.dir, "notify-fake")
        with open(self.path, "w") as fh:
            fh.write(_NOTIFY % {"python": sys.executable})
        os.chmod(self.path, os.stat(self.path).st_mode | stat.S_IXUSR)

    def behave(self, how: str) -> None:
        with open(os.path.join(self.dir, "behaviour"), "w") as fh:
            fh.write(how)

    def calls(self) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as fh:
                for ln in fh:
                    if ln.strip():
                        rec = json.loads(ln)
                        rec["msg"] = json.loads(rec["stdin"]) if rec["stdin"].strip() else {}
                        out.append(rec)
        except OSError:
            pass
        return out

    def kinds(self) -> List[str]:
        return [c["msg"].get("kind") for c in self.calls()]

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


UNATTENDED_CFG = {"autonomy": "unattended", "autonomy_level": "L4", "day_budget_usd": "50", "notify_timeout_s": "5"}


class UnattendedRepo(DriverRepo):
    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, cfg: Optional[Dict[str, Any]] = None, scenario: Optional[Dict[str, Any]] = None,
                 evidence: bool = True, notify: bool = True, **kw: Any) -> None:
        self.notifier = FakeNotify()
        full = dict(UNATTENDED_CFG)
        if notify:
            full["notify_command"] = self.notifier.path
        full.update(cfg or {})
        super(UnattendedRepo, self).__init__(items=items if items is not None else [item_doc("A", estimate=ENG)], cfg=full, scenario=scenario, **kw)
        self.repo.write(".claude/KIT_VERSION", KIT_VERSION)
        if evidence:
            self.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
        self.commit_all("kit version and evidence")
        self.base = self.repo.head()
        self.checkin()

    def checkin(self) -> None:
        """The external watchdog has checked in (an unattended start needs that): what `loopctl unattended watch` leaves behind."""
        from kitlib import jsonio
        from loopctl import unattended
        self.rs.ensure()
        jsonio.atomic_write_json(unattended.watch_path(self.rs), {"ts": time.time(), "lastStatus": "idle", "notified": {}})

    def cleanup(self) -> None:
        self.notifier.cleanup()
        super(UnattendedRepo, self).cleanup()

    def events(self, name: str = "loopctl") -> List[Dict[str, Any]]:
        return self.rs.read_events(name)


class UnattendedTestCase(DriverTestCase):
    def un(self, **kw: Any) -> UnattendedRepo:
        r = UnattendedRepo(**kw)
        self.addCleanup(r.cleanup)
        r.stubs.install()
        return r
