"""abort and rollback before merge to main; after merge a written plan and nothing executed (report section 10.7; CO-9, L-6, RC-14)."""
from __future__ import annotations

import json
import os
import unittest

from loopctl import abort, branches
from loopctl.common import Refused, UsageError

from . import LoopTestCase, item_doc


class Fake(object):
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw.get("env", {})))
        return type("P", (), {"returncode": self.rc, "stdout": b"", "stderr": b""})()


class Abort(LoopTestCase):
    def merged_chain(self, extra_cfg="", items=None):
        lr = self.lr(items=items, extra_cfg=extra_cfg)
        loop = lr.fast_start()
        before = lr.repo.head()
        mcs = {}
        for iid in (i["id"] for i in lr.items):
            mcs[iid] = lr.complete(iid, approve=bool(loop.ctx.plan.items[iid].get("migrations") and any(m.get("dataLossClass") == "lossy" for m in loop.ctx.plan.items[iid]["migrations"])))
        return lr, loop, before, mcs

    def test_dry_run_lists_merged_items_in_reverse_merge_order_and_changes_nothing(self):
        lr, loop, before, mcs = self.merged_chain()
        head = lr.repo.head()
        r = lr.ctl("abort", "--dry-run", "--rollback-to", before)
        self.assertEqual(r.rc, 0, r)
        j = lr.ctl("abort", "--dry-run", "--rollback-to", before, "--json").json
        self.assertEqual([i["item"] for i in j["items"]], ["C", "B", "A"])
        self.assertTrue(all(i["executable"] for i in j["items"]))
        self.assertEqual(lr.repo.head(), head)
        self.assertFalse(lr.context().rs.is_halted())
        self.assertEqual(lr.item("C")["status"], "merged")

    def test_rollback_reverts_in_reverse_order_halts_resets_items_and_releases_the_lock(self):
        lr, loop, before, mcs = self.merged_chain()
        db = Fake()
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        rec = abort.execute(loop, ap, "Pat", "wrong direction", lr.session, runner=db)
        self.assertEqual([r["item"] for r in rec["reverted"]], ["C", "B", "A"])
        self.assertEqual(rec["revertPlan"], [])
        self.assertTrue(lr.context().rs.is_halted())
        self.assertEqual({lr.item(i)["status"] for i in "ABC"}, {"pending"})
        self.assertEqual(lr.state().header["runStatus"], "aborted")
        self.assertFalse(os.path.exists(os.path.join(lr.path, "src", "A.txt")))              # the code is gone from the integration branch
        self.assertEqual(lr.repo.git("diff", "--stat", before, "HEAD"), "")
        self.assertEqual(lr.repo.git("log", "-3", "--format=%s").count("Revert"), 3)
        self.assertFalse(lr.context().lock().status()["held"])
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")

    def test_down_migrations_run_first_never_a_lossy_one_and_never_with_the_callers_environment(self):
        mig = {"name": "m1", "up": "db/m1.up.sql", "down": "db/m1.down.sql", "dataLossClass": "none"}
        lossy = {"name": "m2", "up": "db/m2.up.sql", "down": "db/m2.down.sql", "dataLossClass": "lossy"}
        lr, loop, before, mcs = self.merged_chain(
            extra_cfg="db_migrate_down: python3 down.py",
            items=[item_doc("A", migrations=[mig]), item_doc("B", ["A"], migrations=[lossy])])
        db = Fake()
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        env_before = dict(os.environ)
        os.environ["DATABASE_URL"] = "postgres://prod.example.invalid/app"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "not-a-real-secret"
        self.addCleanup(lambda: (os.environ.pop("DATABASE_URL", None), os.environ.pop("AWS_SECRET_ACCESS_KEY", None)))
        rec = abort.execute(loop, ap, "Pat", "x", lr.session, runner=db)
        self.assertEqual([c[0] for c in db.calls], [["python3", "down.py"]])                    # only m1: m2 is lossy
        env = db.calls[0][1]
        self.assertEqual((env["LOOPCTL_ITEM"], env["LOOPCTL_MIGRATION"], env["LOOPCTL_MIGRATION_DOWN"]), ("A", "m1", "db/m1.down.sql"))
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(rec["downMigrations"], [{"item": "A", "migration": "m1", "ok": True}])
        self.assertEqual([r["item"] for r in rec["reverted"]], ["B", "A"])

    def test_a_failed_down_migration_stops_the_rollback_and_writes_a_plan_for_the_rest(self):
        mig = {"name": "m1", "up": "db/m1.up.sql", "down": "db/m1.down.sql", "dataLossClass": "none"}
        lr, loop, before, mcs = self.merged_chain(extra_cfg="db_migrate_down: python3 down.py",
                                                  items=[item_doc("A"), item_doc("B", ["A"], migrations=[mig])])
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        rec = abort.execute(loop, ap, "Pat", "x", lr.session, runner=Fake(rc=1))
        self.assertEqual(rec["reverted"], [])
        self.assertEqual({r["item"] for r in rec["revertPlan"]}, {"A", "B"})
        self.assertTrue(any("failed" in p for p in rec["problems"]))
        self.assertEqual(lr.item("B")["status"], "needs_human")
        self.assertEqual(lr.item("B")["blockReason"], "aborted")
        self.assertTrue(os.path.exists(os.path.join(lr.path, "src", "B.txt")))                   # nothing was reverted

    def test_a_revert_that_conflicts_is_aborted_and_the_rest_become_plan_only(self):
        lr, loop, before, mcs = self.merged_chain()
        # a later human commit touches the same file C added, so reverting C... C comes first: make C's revert conflict
        lr.repo.commit_file("src/C.txt", "changed by a human\n", "human edit")
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        rec = abort.execute(loop, ap, "Pat", "x", lr.session, runner=Fake())
        self.assertEqual(rec["reverted"], [])
        self.assertEqual({r["item"] for r in rec["revertPlan"]}, {"A", "B", "C"})
        self.assertTrue(any("conflicted" in p for p in rec["problems"]))
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")                                # the failed revert was aborted cleanly

    def test_items_already_on_a_remote_main_get_a_plan_and_nothing_is_executed(self):
        from tests.helpers import make_bare_remote
        lr = self.lr()
        loop = lr.fast_start()
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        before = lr.repo.head()
        lr.complete("A")
        lr.repo.git("push", "-q", "origin", "main")
        lr.repo.git("fetch", "-q", "origin")
        lr.complete("B")                                                                           # B is local only
        head = lr.repo.head()
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        by_item = {i["item"]: i for i in ap.items}
        self.assertTrue(by_item["B"]["executable"])
        self.assertFalse(by_item["A"]["executable"])
        self.assertIn("already on", by_item["A"]["plan_only_reason"])
        rec = abort.execute(loop, ap, "Pat", "x", lr.session, runner=Fake())
        self.assertEqual([r["item"] for r in rec["reverted"]], ["B"])
        self.assertEqual([r["item"] for r in rec["revertPlan"]], ["A"])
        self.assertEqual(lr.item("A")["status"], "needs_human")                                    # merged to a published branch: a human decides
        self.assertEqual(lr.item("B")["status"], "pending")
        with open(rec["planFile"]) as fh:
            plan_text = fh.read()
        self.assertIn("git revert -m 1 %s" % by_item["A"]["commit"], plan_text)
        self.assertIn("NOT executed", plan_text)
        self.assertIn("revert-plan", lr.ctl("status", "--pending-human").out)

    def test_delivery_other_than_local_never_executes(self):
        lr = self.lr(extra_cfg="delivery: draft-pr")
        # the shared template already sets delivery: local, so build the config without it
        lr.repo.write(".claude/loop.config.md", "```yaml\nloopctl_tier2: true\ndelivery: draft-pr\n```\n")
        lr.commit_all("draft-pr")
        loop = lr.fast_start()
        before = lr.repo.head()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "pr_open", "awaiting_human")
        mc = lr.do_merge("A", trailer=True)
        loop.approve("item", "A", "Pat")
        loop.advance("A", "merged", lr.session, merge_commit=mc)
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        self.assertFalse(ap.items[0]["executable"])
        self.assertIn("delivery is draft-pr", ap.items[0]["plan_only_reason"])

    def test_in_flight_work_is_parked_and_dependents_of_a_rolled_back_item_are_abandoned(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B", ["A"]), item_doc("C")])
        loop = lr.fast_start()
        before = lr.repo.head()
        lr.complete("A")
        loop.begin("B", lr.session)
        lr.repo.git("branch", lr.item("B")["branch"], "main")
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        self.assertEqual(ap.cascade, ["B"])
        abort.execute(loop, ap, "Pat", "x", lr.session, runner=Fake())
        self.assertEqual(lr.item("B")["status"], "abandoned")
        self.assertEqual(lr.item("A")["status"], "pending")
        self.assertEqual(lr.item("C")["status"], "pending")
        self.assertTrue(any(b["kind"] == "dead" for b in branches.list_branches(lr.path)))        # abandoned work is kept as dead/*, not deleted

    def test_abort_without_rollback_parks_in_flight_items_and_keeps_their_branches(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        br = lr.item("A")["branch"]
        lr.repo.git("branch", br, "main")
        rec = abort.execute(loop, abort.plan_abort(loop.ctx, lr.state(), loop.plan), "Pat", "stop", lr.session)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("blocked", "aborted"))
        self.assertTrue(branches.exists(lr.path, "blocked/slice-a-a1"))
        self.assertFalse(branches.exists(lr.path, br))
        self.assertEqual(rec["reverted"], [])

    def test_open_prs_are_closed_by_the_supplied_closer_else_listed(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        loop.advance("A", "pr_open", lr.session, pr_url="https://github.example/acme/app/pull/7")
        closed = []
        rec = abort.execute(loop, abort.plan_abort(loop.ctx, lr.state(), loop.plan), "Pat", "x", lr.session, pr_closer=lambda u: closed.append(u) or True)
        self.assertEqual(closed, ["https://github.example/acme/app/pull/7"])
        self.assertEqual(rec["manualActions"], [])
        lr2 = self.lr()
        loop2 = lr2.fast_start()
        loop2.begin("A", lr2.session)
        lr2.walk("A", "design_review", "building", "verifying", "reviewing")
        loop2.advance("A", "pr_open", lr2.session, pr_url="https://github.example/acme/app/pull/8")
        rec = abort.execute(loop2, abort.plan_abort(loop2.ctx, lr2.state(), loop2.plan), "Pat", "x", lr2.session)
        self.assertIn("pull/8", rec["manualActions"][0])

    def test_validation_of_the_rollback_target(self):
        lr = self.lr()
        loop = lr.fast_start()
        for bad in ("HEAD", "main", "--hard", "0" * 40, "zz", "../x"):
            with self.assertRaises((UsageError, Refused)):
                abort.plan_abort(loop.ctx, lr.state(), loop.plan, bad)
        lr.repo.git("switch", "-q", "-c", "side", "main")
        side = lr.repo.commit_file("s.txt", "s", "side")
        lr.repo.git("switch", "-q", "main")
        with self.assertRaises(Refused):
            abort.plan_abort(loop.ctx, lr.state(), loop.plan, side)                                # not an ancestor of the integration branch

    def test_an_abort_needs_a_named_human_and_a_loop_worktree_to_revert_in(self):
        lr, loop, before, mcs = self.merged_chain()
        ap = abort.plan_abort(loop.ctx, lr.state(), loop.plan, before)
        with self.assertRaises(UsageError):
            abort.execute(loop, ap, "  ", "x", lr.session)
        rs = loop.ctx.rs
        run = rs.read_run()
        run["worktree"] = "/not/this/checkout"
        rs.write_run(run)
        rec = abort.execute(loop, ap, "Pat", "x", lr.session, runner=Fake())
        self.assertEqual(rec["reverted"], [])
        self.assertEqual(len(rec["revertPlan"]), 3)
        self.assertTrue(any("registered loop worktree" in p for p in rec["problems"]))

    def test_the_cli_path(self):
        lr, loop, before, mcs = self.merged_chain()
        r = lr.ctl("abort", "--rollback-to", before)
        self.assertEqual(r.rc, 2, r)                                                               # --by is required
        r = lr.ctl("abort", "--rollback-to", before, "--by", "Pat", "--reason", "cli test", "--session", lr.session)
        self.assertEqual(r.rc, 0, r)
        self.assertIn("3 reverted", r.out)
        self.assertEqual(lr.state().header["abort"]["by"], "Pat")


if __name__ == "__main__":
    unittest.main()
