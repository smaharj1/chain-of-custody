"""Phase 11b tests: the remote ref lock, claims (F-25), the L5 eligibility classifier, the reproducibility manifest (F-26), G10 and the extras' commands.

Everything runs against temp repositories and a LOCAL BARE REMOTE built once per test class: nothing here touches a real remote, `gh`, the network or the real
`claude` (IMP-14). `Fleet` is one bare remote and two "machines" (clones) that share it, which is what two people racing for one lock look like.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, Optional

from ... import REPO_ROOT, SCRIPTS_DIR  # noqa: F401  (puts .claude/scripts on sys.path)
from ...helpers import TempRepo, make_bare_remote

EVIDENCE = {"schema": "evidence.v1", "fixture_runs": [{"id": "r1", "clean": True}, {"id": "r2", "clean": True}, {"id": "r3", "clean": True}],
            "real_feature": {"id": "f1", "completed": True}, "attested_by": "Pat", "attested_at": "2026-10-20"}


class Clock(object):
    """A clock a test moves by hand (no sleeping)."""

    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Fleet(object):
    """One bare remote and two machines (clones with `origin` pointing at it). Built once per class; a test uses its own plan name."""

    def __init__(self) -> None:
        self.bare = make_bare_remote()
        self.a, self.b = TempRepo(), TempRepo()
        for r in (self.a, self.b):
            r.add_remote("origin", self.bare.path)
        self.a.git("push", "-q", "origin", "main")
        self.n = 0

    def plan(self) -> str:
        self.n += 1
        return "plan%d" % self.n

    def forge(self, machine: Any, ref: str, files: Optional[Dict[str, str]] = None, blob: Optional[str] = None, parent: Optional[str] = None) -> str:
        """Put an arbitrary object at `ref` on the remote, the way someone with contents: write could. `blob` pushes a bare blob; `files` a commit whose tree holds those files."""
        if blob is not None:
            oid = machine.git("hash-object", "-w", "--stdin", input_text=blob)
        else:
            rows = []
            for name, text in (files or {}).items():
                rows.append("100644 blob %s\t%s" % (machine.git("hash-object", "-w", "--stdin", input_text=text), name))
            tree = machine.git("mktree", input_text="\n".join(rows) + "\n")
            args = ["commit-tree", tree, "-m", "forged"] + (["-p", parent] if parent else [])
            oid = machine.git(*args)
        machine.git("push", "-q", "--force", "origin", "%s:%s" % (oid, ref))
        return oid

    def cleanup(self) -> None:
        for r in (self.a, self.b):
            r.cleanup()
        self.bare.cleanup()


def good_lock(plan: str, **over: Any) -> Dict[str, Any]:
    from loopctl import remote_lock as rl
    d = {"schema": rl.SCHEMA, "plan": plan, "name": "lock/" + plan, "token": "a" * 32, "owner": "mallory", "pid": 4242, "host": "elsewhere", "acquired": "2026-10-26T10:00:00Z",
         "hb": 1_800_000_000.0, "ttl_s": 3600.0, "meta": {}}
    d.update(over)
    return d


class MemRemote(object):
    """An in-memory remote for the tests whose subject is the lock's LOGIC (who may take what, when): a ref holds (commit id, document, why-unreadable).
    `MemDoc.push` is a real compare-and-swap on it. What git itself does (create-only leases, forged refs, a remote that refuses) is tested against a real bare remote."""

    def __init__(self) -> None:
        self.refs = {}      # type: Dict[str, Any]
        self.objects = {}   # type: Dict[str, Any]
        self.n = 0


def mem_doc(remote: MemRemote, real: Any) -> Any:
    from loopctl import remote_lock as rl

    class MemDoc(rl.RefDoc):
        def check_remote(self) -> None:
            return None

        def observe(self) -> Any:
            cur = remote.refs.get(self.ref)
            if cur is None:
                return rl.Observed("none")
            oid, doc, bad = cur
            if bad:
                return rl.Observed("corrupt", oid, None, bad)
            problem = self.check(doc)
            return rl.Observed("corrupt", oid, None, problem) if problem else rl.Observed("ok", oid, json.loads(json.dumps(doc)))

        def make_commit(self, doc: Any, parent: Optional[str], message: str) -> str:
            remote.n += 1
            oid = "%040x" % remote.n
            remote.objects[oid] = json.loads(json.dumps(doc))
            return oid

        def push(self, new: str, old: Optional[str]) -> bool:
            if self.before_push is not None:
                self.before_push()
            cur = remote.refs.get(self.ref)
            if (cur[0] if cur else None) != old:
                return False
            remote.refs[self.ref] = (new, remote.objects[new], None)
            return True

        def delete(self, old: str) -> bool:
            cur = remote.refs.get(self.ref)
            if cur is None or cur[0] != old:
                return False
            del remote.refs[self.ref]
            return True
    return MemDoc(real.repo, real.remote, real.ref, real.filename, real.check)


def mem_claims(remote: MemRemote, plan: str, clock: Any, ttl_h: float = 72.0) -> Any:
    """`claims.Claims` over the in-memory remote (the rules; git's part is tested against a bare remote)."""
    from loopctl import claims as cm_mod

    class MemClaims(cm_mod.Claims):
        def _doc(self, item: str) -> Any:
            d = mem_doc(remote, super(MemClaims, self)._doc(item))
            d.before_push = self.before_push
            return d

        def listing(self) -> Any:
            prefix = "refs/loopctl/claim/%s/" % self.plan
            return [self._summary(ref[len(prefix):], self._doc(ref[len(prefix):]).observe()) for ref in sorted(remote.refs) if ref.startswith(prefix)]
    return MemClaims("/mem", "origin", plan, ttl_h=ttl_h, clock=clock, host="h-mem")
