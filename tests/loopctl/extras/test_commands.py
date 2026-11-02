"""`remote-lock` and `claim` as commands, the `run` preflight rows, and a driver run that holds the remote lock and leaves a claimed item alone."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import runstate

from loopctl import claims as claims_mod, remote_lock as rl

from .. import LoopTestCase, item_doc
from ..fault_injection import DriverTestCase, render_config
from ...helpers import make_bare_remote
from . import EVIDENCE

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Commands(LoopTestCase):
    def repo(self, cfg="remote_lock: origin\n", evidence=True):
        lr = self.lr(items=[item_doc("A"), item_doc("B")], extra_cfg=cfg)
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        if evidence:
            lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
            lr.commit_all("evidence")
        return lr, bare

    def other_machine(self, lr, bare):
        """A second clone of the repository: another person's machine."""
        from ...helpers import TempRepo
        m = TempRepo()
        m.add_remote("origin", bare.path)
        self.addCleanup(m.cleanup)
        return m

    def test_the_lock_commands_hold_renew_and_release_and_never_print_the_token(self):
        lr, bare = self.repo()
        self.assertIn("free", lr.ctl("remote-lock", "status").out)
        r = lr.ctl("remote-lock", "acquire", "--owner", "alice", "--json")
        self.assertEqual(r.rc, 0, r)
        self.assertEqual((r.json["action"], r.json["owner"], r.json["remote"]), ("acquired", "alice", "origin"))
        with open(rl.state_path(runstate.RunState.for_path(lr.path))) as fh:
            state = json.load(fh)
        self.assertNotIn(state["token"], r.out)
        self.assertEqual(bare.ref("refs/loopctl/lock/demo"), r.json["oid"])
        st = lr.ctl("remote-lock", "status", "--json").json
        self.assertEqual((st["state"], st["record"]["owner"]), ("ok", "alice"))
        self.assertNotIn(state["token"], json.dumps(st))
        self.assertIn("held by alice", lr.ctl("remote-lock", "status").out)
        self.assertEqual(lr.ctl("remote-lock", "renew").rc, 0)
        self.assertEqual(lr.ctl("remote-lock", "acquire", "--owner", "alice").rc, 0)           # this machine asking again gets its own lock back
        rel = lr.ctl("remote-lock", "release")
        self.assertEqual(rel.rc, 0, rel)
        self.assertIsNone(bare.ref("refs/loopctl/lock/demo"))
        again = lr.ctl("remote-lock", "release")
        self.assertEqual(again.rc, 3, again)                                                   # nothing to release: no record of holding it

    def test_another_machine_is_refused_and_a_stale_lock_needs_its_say_so(self):
        lr, bare = self.repo()
        lr.ctl("remote-lock", "acquire", "--owner", "alice")
        m = self.other_machine(lr, bare)
        theirs = rl.RemoteLock(m.path, "origin", "demo", clock=lambda: 1_900_000_000.0, host="elsewhere", alive=lambda p: False)
        with self.assertRaises(Exception) as cm:
            theirs.acquire("bob")
        self.assertIn("alice", str(cm.exception))
        r = lr.ctl("remote-lock", "steal", "--owner", "me")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("not stale", r.err)
        self.assertEqual(lr.ctl("remote-lock", "steal", "--owner", "me", "--corrupt").rc, 3)

    def test_the_opt_in_and_the_evidence_gate_are_enforced_and_halt_stops_a_hand_out(self):
        lr, _ = self.repo(cfg="")
        r = lr.ctl("remote-lock", "acquire")
        self.assertEqual(r.rc, 3)
        self.assertIn("remote_lock is not set", r.err)
        lr, _ = self.repo(cfg="remote_lock: nowhere\n")                                       # a name this repository does not have (a URL never gets this far)
        r = lr.ctl("remote-lock", "status")
        self.assertEqual(r.rc, 3, r)
        lr, _ = self.repo(evidence=False)
        r = lr.ctl("remote-lock", "status")
        self.assertEqual(r.rc, 3)
        self.assertIn("evidence-gated", r.err)
        self.assertEqual(lr.ctl("claim", "list").rc, 3)
        lr, _ = self.repo()
        lr.ctl("halt", "--reason", "test", "--by", "Pat")
        self.assertEqual(lr.ctl("remote-lock", "acquire").rc, 3)
        self.assertEqual(lr.ctl("claim", "take", "A", "--by", "pat").rc, 3)
        self.assertEqual(lr.ctl("remote-lock", "status").rc, 0)                                # reading is always allowed

    def test_claims_two_humans_one_item(self):
        lr, bare = self.repo()
        self.assertEqual(lr.ctl("claim", "take", "A", "--by", "alice", "--note", "schema first").rc, 0)
        r = lr.ctl("claim", "take", "A", "--by", "bob")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("alice", r.err)
        self.assertEqual(lr.ctl("claim", "show", "A").out.strip(), "A: live by alice")
        lst = lr.ctl("claim", "list", "--json").json
        self.assertEqual([(c["item"], c["state"], c["claim"]["by"]) for c in lst["claims"]], [("A", "live", "alice")])
        self.assertNotIn("token", json.dumps(lst))
        self.assertIn("A", lr.ctl("claim", "list").out)
        self.assertEqual(lr.ctl("claim", "take", "A", "--by", "bob", "--override").rc, 0)
        self.assertEqual(lr.ctl("claim", "release", "A", "--by", "alice").rc, 3)               # no longer hers
        self.assertEqual(lr.ctl("claim", "release", "A", "--by", "bob").rc, 0)
        self.assertEqual(lr.ctl("claim", "take", "ZZ", "--by", "bob").rc, 2)                   # not an item of the plan
        self.assertEqual(lr.ctl("claim", "take", "A", "--by", "bad name;").rc, 2)
        self.assertEqual(lr.ctl("claim", "take", "--by", "bob").rc, 2)
        self.assertEqual(lr.ctl("claim", "list").out.strip(), "no claims on demo")

    def test_the_run_preflight_says_what_the_remote_lock_and_the_claims_look_like(self):
        lr, bare = self.repo(cfg="remote_lock: origin\nrunner: driver\nrun_budget_usd: 10\n")
        lr.fast_start()
        rows = {r["id"]: r for r in lr.ctl("run", "--dry-run", "--json").json["preflight"]}
        self.assertTrue(rows["remote-lock"]["ok"], rows["remote-lock"])
        self.assertTrue(rows["claims"]["ok"])
        self.assertIn("free", rows["remote-lock"]["message"])
        m = self.other_machine(lr, bare)
        claims_mod.Claims(m.path, "origin", "demo").take("B", "bob")
        rl.RemoteLock(m.path, "origin", "demo", host="elsewhere").acquire("bob")
        rows = {r["id"]: r for r in lr.ctl("run", "--dry-run", "--json").json["preflight"]}
        self.assertFalse(rows["remote-lock"]["ok"])
        self.assertIn("held by bob", rows["remote-lock"]["message"])
        self.assertIn("B", rows["claims"]["message"])
        self.assertTrue(rows["claims"]["ok"])                                                  # a claimed item is skipped, not a refusal
        before = lr.state().d["seq"]
        r = lr.ctl("run", "--session", lr.session, "--nonce", "x")
        self.assertEqual(r.rc, 3, r)                                                           # a run whose lock another machine holds does not start
        self.assertIn("remote-lock", r.err)
        self.assertEqual(lr.state().d["seq"], before)


class DriverRun(DriverTestCase):
    def test_a_driver_run_holds_the_remote_lock_while_it_runs_and_leaves_a_claimed_item_alone(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG), item_doc("B", estimate=ENG)], cfg={"remote_lock": "origin"})
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
        lr.commit_all("evidence")
        lr.base = lr.repo.head()
        claims_mod.Claims(lr.path, "origin", "demo").take("B", "bob")                        # bob has B
        held = []
        orig = rl.Keeper.release

        def spy(keeper):
            held.append(keeper.lock.holds(keeper.held))                                      # at the end of the run the lock was still ours
            return orig(keeper)
        rl.Keeper.release = spy
        self.addCleanup(setattr, rl.Keeper, "release", orig)
        lr.set_scenario(lr.happy(["A"]))
        lr.approve()
        r = lr.run("--json")
        self.assertEqual(r.rc, 1, r)                                                           # A merged; B waits on bob, so the plan is not complete
        doc = json.loads(r.out)
        self.assertEqual((doc["outcome"], doc["merged_this_run"]), ("claimed", ["A"]))
        self.assertEqual({i: x["status"] for i, x in lr.state().items.items()}, {"A": "merged", "B": "pending"})
        self.assertEqual(held, [True])
        self.assertIsNone(bare.ref("refs/loopctl/lock/demo"))                                  # given back when the run ended
        self.assertFalse(os.path.exists(rl.state_path(lr.rs)))
        self.assertEqual(len(lr.calls_for("backend-engineer")), 1)                             # no child was ever started for B


if __name__ == "__main__":
    unittest.main()
