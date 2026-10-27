"""The reject cascade (report 19.7 L-6, 10.7, 11.5 item 3; RC-14, CO-18): revert what was built on a rejected item, never what has reached main, never loop."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import shutil
import unittest

from loopctl import branches, plan as plan_mod, state as sm
from loopctl.parallel import cascade
from loopctl import common

from .. import LoopTestCase
from . import item_doc, put, read

MIG = [{"name": "m", "up": "db/m/%s.sql", "down": "db/m/%s.down.sql", "dataLossClass": "none"}]


def items():
    return [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"]), item_doc("D"),
            item_doc("MA", migrations=[dict(MIG[0], name="ma", up="db/m/ma.sql", down="db/m/ma.down.sql")]),
            item_doc("MB", ["MA"], migrations=[dict(MIG[0], name="mb", up="db/m/mb.sql", down="db/m/mb.down.sql", dataLossClass="lossy", noDownReason="drops a column")]),
            item_doc("MC", ["MB"], migrations=[dict(MIG[0], name="mc", up="db/m/mc.sql", down="db/m/mc.down.sql")])]


class Cascade(LoopTestCase):
    def setUp(self):
        super(Cascade, self).setUp()
        self.tmp = tempfile.mkdtemp(prefix="cascade-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "down.log")
        script = os.path.join(self.tmp, "down.py")
        put(script, "#!%s\nimport os\nwith open(%r, 'a') as f:\n    f.write(os.environ['LOOPCTL_ITEM'] + ':' + os.environ['LOOPCTL_MIGRATION'] + '\\n')\n" % (sys.executable, self.log))
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        self.lr = self.lr_(extra_cfg="db_migrate_down: %s" % script)
        self.loop = self.lr.fast_start()

    def lr_(self, **kw):
        r = self.lr_factory(**kw)
        return r

    def lr_factory(self, **kw):
        from .. import LoopRepo
        r = LoopRepo(items=items(), **kw)
        self.addCleanup(r.cleanup)
        return r

    def plan_for(self, root):
        st, _ = self.loop.store.read()
        return cascade.plan_cascade(self.loop.ctx, st, self.loop.plan, root), st

    def test_the_merged_dependents_are_reverted_newest_first_and_everything_touched_starts_over(self):
        for i in "ABCD":
            self.lr.quick_merge(i)
        cp, _ = self.plan_for("A")
        self.assertEqual([m["item"] for m in cp.merged], ["C", "B", "A"])                      # reverse merge order; D does not depend on A
        self.assertFalse(cp.plan_only)
        before = self.lr.repo.git("rev-parse", "main")
        rec = cascade.execute(self.loop, cp, "Pat", "wrong approach")
        self.assertEqual([r["item"] for r in rec["reverted"]], ["C", "B", "A"])
        files = self.lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines()
        self.assertEqual([f for f in files if f.startswith("src/")], ["src/D.txt"])
        self.assertEqual(len(self.lr.repo.git("log", "--format=%s", "%s..main" % before).splitlines()), 3)      # three revert commits
        st = self.lr.state()
        for i in "ABC":
            r = st.items[i]
            self.assertEqual((r["status"], r["attempt"], r["mergeCommit"], r["branch"], r["gates"]), ("pending", 2, None, None, {}))
        self.assertEqual(st.items["D"]["status"], "merged")
        self.assertEqual(st.header["cascades"], {"A": 1})
        self.assertEqual(st.items["A"]["rework"][-1]["note"], "wrong approach")
        self.assertEqual(self.lr.repo.git("status", "--porcelain"), "")

    def test_an_item_still_being_built_on_it_is_invalidated_and_its_work_kept_on_a_dead_branch(self):
        self.lr.quick_merge("A")
        self.loop.begin("B", self.lr.session)                                                   # B is designing on feat/slice-b-a1
        self.lr.repo.git("branch", "feat/slice-b-a1", "main")
        cp, st = self.plan_for("A")
        self.assertEqual(cp.in_flight, ["B"])
        cascade.execute(self.loop, cp, "Pat", "")
        st = self.lr.state()
        self.assertEqual((st.items["B"]["status"], st.items["B"]["attempt"], st.items["B"]["branch"]), ("pending", 2, None))
        self.assertEqual((st.items["A"]["status"], st.items["A"]["attempt"]), ("pending", 2))
        names = [b["name"] for b in branches.list_branches(self.lr.path)]
        self.assertIn("dead/slice-b-a1", names)
        self.assertNotIn("feat/slice-b-a1", names)

    def test_what_has_reached_main_is_never_reverted_a_human_gets_a_plan(self):
        for i in "AB":
            self.lr.quick_merge(i)
        self.lr.repo.git("update-ref", "refs/remotes/origin/main", self.lr.repo.git("rev-parse", "main"))      # pushed and merged by the host
        cp, _ = self.plan_for("A")
        self.assertTrue(cp.plan_only)
        self.assertIn("already on origin/main", cp.plan_only_reason)
        tip = self.lr.repo.git("rev-parse", "main")
        rec = cascade.execute(self.loop, cp, "Pat", "")
        self.assertEqual((rec["reverted"], self.lr.repo.git("rev-parse", "main")), ([], tip))
        st = self.lr.state()
        self.assertEqual([(st.items[i]["status"], st.items[i]["blockReason"]) for i in "AB"], [("needs_human", "human_revert")] * 2)
        text = read(rec["planFile"])
        self.assertIn("git revert -m 1 %s" % st.items["B"]["mergeCommit"], text)
        self.assertLess(text.index(st.items["B"]["mergeCommit"]), text.index(st.items["A"]["mergeCommit"]))           # newest first
        self.assertEqual(open(self.log).read() if os.path.exists(self.log) else "", "")                                    # and no down-migration ran

    def test_down_migrations_run_newest_first_and_a_lossy_one_never_runs(self):
        for i in ("MA", "MB", "MC"):
            self.lr.quick_merge(i)
        cp, _ = self.plan_for("MA")
        rec = cascade.execute(self.loop, cp, "Pat", "")
        self.assertEqual(read(self.log).split(), ["MC:mc", "MA:ma"])                           # MB's down is lossy: never run, and the item is still reverted
        self.assertEqual([d["ok"] for d in rec["downMigrations"]], [True, True])
        self.assertEqual([r["item"] for r in rec["reverted"]], ["MC", "MB", "MA"])

    def test_a_revert_that_conflicts_is_aborted_and_everything_older_waits_for_a_human(self):
        for i in "ABC":
            self.lr.quick_merge(i)
        self.lr.repo.commit_file("src/B.txt", "someone else changed B's file afterwards\n", "unrelated later work")
        cp, _ = self.plan_for("A")
        rec = cascade.execute(self.loop, cp, "Pat", "")
        self.assertEqual([r["item"] for r in rec["reverted"]], ["C"])
        self.assertTrue(any("conflicted" in p for p in rec["problems"]))
        st = self.lr.state()
        self.assertEqual(st.items["C"]["status"], "pending")
        self.assertEqual([(st.items[i]["status"], st.items[i]["blockReason"]) for i in "BA"], [("needs_human", "human_revert")] * 2)
        self.assertEqual(self.lr.repo.git("status", "--porcelain"), "")
        self.assertTrue(os.path.exists(rec["planFile"]))

    def test_a_root_is_cascaded_three_times_and_then_a_human_looks(self):
        self.lr.quick_merge("D")
        for n in (1, 2, 3):
            cp, _ = self.plan_for("D")
            self.assertEqual(cp.loops, n - 1)
            cascade.execute(self.loop, cp, "Pat", "again")
            self.lr.quick_merge("D")
        cp, _ = self.plan_for("D")
        rec = cascade.execute(self.loop, cp, "Pat", "again")
        self.assertEqual(rec["refused"], "cascade_loop")
        r = self.lr.item("D")
        self.assertEqual((r["status"], r["blockReason"]), ("merged", None))                      # nothing was reverted
        self.assertEqual(self.lr.state().header["cascade"]["refused"], "cascade_loop")

    def test_a_cyclic_dependency_graph_terminates(self):
        cyc = {"X": {"dependsOn": ["Y"]}, "Y": {"dependsOn": ["X", "Z"]}, "Z": {"dependsOn": []}}
        self.assertEqual(plan_mod.descendants(cyc, "X"), {"X", "Y"})
        self.assertEqual(plan_mod.ancestors(cyc, "X"), {"X", "Y", "Z"})
        st = sm.State.new("demo")
        for i in cyc:
            st.items[i] = sm.new_item_record()
        st.items["X"]["status"] = "building"
        self.assertEqual(cascade.invalid_inflight(st, cyc), ["X"])

    def test_invalid_inflight_is_an_item_in_progress_whose_ancestor_is_not_merged(self):
        st = sm.State.new("demo")
        its = {"A": {"dependsOn": []}, "B": {"dependsOn": ["A"]}, "C": {"dependsOn": ["B"]}, "D": {"dependsOn": []}}
        for i in its:
            st.items[i] = sm.new_item_record()
        st.items["A"]["status"] = "merged"
        st.items["A"]["mergeCommit"] = "0" * 40
        st.items["B"]["status"] = "reviewing"
        st.items["C"]["status"] = "pending"
        st.items["D"]["status"] = "building"
        self.assertEqual(cascade.invalid_inflight(st, its), [])
        st.items["C"]["status"] = "awaiting_human"
        self.assertEqual(cascade.invalid_inflight(st, its), ["C"])                                 # waiting for a human on a B that has not merged
        st.items["A"]["status"] = "needs_human"                                                   # a human reverted A
        self.assertEqual(cascade.invalid_inflight(st, its), ["B", "C"])

    def test_the_wrong_item_or_no_name_is_refused(self):
        st, _ = self.loop.store.read()
        with self.assertRaises(common.UsageError):
            cascade.plan_cascade(self.loop.ctx, st, self.loop.plan, "NOPE")
        with self.assertRaises(common.UsageError):
            cascade.plan_cascade(self.loop.ctx, st, self.loop.plan, "../x")
        cp, _ = self.plan_for("D")
        with self.assertRaises(common.UsageError):
            cascade.execute(self.loop, cp, "  ", "")


if __name__ == "__main__":
    unittest.main()
