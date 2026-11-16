"""plan.py: schema, AC ids, the DAG, critical path, width, slicing, data-loss classes, hostile input (report sections 10.3, 19.7)."""
from __future__ import annotations

import json
import os
import unittest

from loopctl import plan

from . import LoopRepo, LoopTestCase, item_doc


def lp_of(items, name="demo", **plan_extra):
    lp = plan.LoadedPlan("test")
    lp.plan = {"schema": plan.PLAN_SCHEMA, "name": name, "goal": "g"}
    lp.plan.update(plan_extra)
    lp.items = {i["id"]: i for i in items}
    return lp


def ids(rep, severity=None):
    return sorted({f.id for f in rep.findings if severity is None or f.severity == severity})


class Schema(unittest.TestCase):
    def test_a_good_plan_validates_clean(self):
        rep = plan.validate(lp_of([item_doc("A"), item_doc("B", ["A"])]))
        self.assertTrue(rep.ok, rep.findings)
        self.assertEqual(rep.order, ["A", "B"])

    def test_unknown_fields_fail_loudly(self):
        it = item_doc("A", mystery=1)
        self.assertIn("schema.unknown-field", ids(plan.validate(lp_of([it])), "error"))
        lp = lp_of([item_doc("A")], surprise=True)
        self.assertIn("schema.unknown-field", ids(plan.validate(lp), "error"))

    def test_wrong_types_and_missing_fields(self):
        for bad in ({"title": 5}, {"slug": "Bad Slug"}, {"size": "XL"}, {"dependsOn": "A"}, {"acceptance": "x"}, {"requiresEnv": ["lower"]},
                    {"riskClass": "huge"}, {"schema": "v0"}, {"milestone": ""}):
            with self.subTest(bad):
                rep = plan.validate(lp_of([item_doc("A", **bad)]))
                self.assertFalse(rep.ok, bad)
        it = item_doc("A")
        del it["intent"]
        self.assertIn("schema.missing", ids(plan.validate(lp_of([it]))))

    def test_milestone_must_be_declared_when_the_plan_lists_milestones(self):
        rep = plan.validate(lp_of([item_doc("A", milestone="Other")], milestones=["M1"]))
        self.assertIn("plan.milestone", ids(rep, "error"))

    def test_item_ids_are_alphanumeric_so_ac_ids_split_cleanly(self):
        for bad in ("F-1", "F_1", "1F", "F1.2", "a" * 25, "../x", "F/1"):
            with self.subTest(bad):
                self.assertFalse(plan.common.valid_item_id(bad))

    def test_ids_that_differ_only_by_case_are_refused(self):
        rep = plan.validate(lp_of([item_doc("Ab"), item_doc("aB", slug="other")]))
        self.assertIn("plan.item-id", ids(rep, "error"))


class AcceptanceCriteria(unittest.TestCase):
    def test_ac_ids_must_carry_the_item_id(self):
        it = item_doc("A")
        it["acceptance"][0]["id"] = "AC-B-1"
        self.assertIn("ac.id", ids(plan.validate(lp_of([it])), "error"))

    def test_duplicate_and_malformed_acs(self):
        it = item_doc("A")
        it["acceptance"].append(dict(it["acceptance"][0]))
        self.assertIn("ac.dup", ids(plan.validate(lp_of([it])), "error"))
        it = item_doc("A")
        it["acceptance"] = ["AC-A-1: a string, not an object"]
        self.assertIn("ac.shape", ids(plan.validate(lp_of([it])), "error"))
        it = item_doc("A")
        it["acceptance"] = []
        self.assertIn("ac.none", ids(plan.validate(lp_of([it])), "error"))

    def test_the_app_boots_is_not_falsifiable(self):
        it = item_doc("A")
        it["acceptance"] = [{"id": "AC-A-1", "text": "The app boots and works well"}]
        self.assertIn("ac.not-falsifiable", ids(plan.validate(lp_of([it])), "error"))
        for ok in ("GET /health returns the JSON", "the endpoint answers 404 for an unknown id", "the command exits 0", "the test test_x must pass",
                   "`foo --bar` prints usage"):
            with self.subTest(ok):
                self.assertTrue(plan.falsifiable(ok), ok)

    def test_the_ac_digest_changes_with_any_edit(self):
        a = item_doc("A")
        d0 = plan.ac_digest(a)
        b = json.loads(json.dumps(a))
        b["acceptance"][0]["text"] += " "
        self.assertNotEqual(d0, plan.ac_digest(b))
        c = json.loads(json.dumps(a))
        c["acceptance"].append({"id": "AC-A-3", "text": "`x` returns 200"})
        self.assertNotEqual(d0, plan.ac_digest(c))
        d = json.loads(json.dumps(a))
        d["title"] = "different title"
        self.assertEqual(d0, plan.ac_digest(d))


class Graph(unittest.TestCase):
    def test_missing_self_and_cycle(self):
        self.assertIn("dag.missing", ids(plan.validate(lp_of([item_doc("A", ["Z"])])), "error"))
        self.assertIn("dag.self", ids(plan.validate(lp_of([item_doc("A", ["A"])])), "error"))
        rep = plan.validate(lp_of([item_doc("A", ["C"]), item_doc("B", ["A"]), item_doc("C", ["B"])]))
        cyc = [f for f in rep.findings if f.id == "dag.cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertIn(" -> ", cyc[0].message)
        self.assertEqual(rep.critical, [])

    def test_topological_order_breaks_ties_by_milestone_then_natural_id(self):
        items = [item_doc("F10", milestone="M2"), item_doc("F2", milestone="M2"), item_doc("F1", milestone="M1"), item_doc("F3", ["F1"], milestone="M1")]
        lp = lp_of(items, milestones=["M1", "M2"])
        rep = plan.validate(lp)
        self.assertEqual(rep.order, ["F1", "F3", "F2", "F10"])

    def test_critical_path_and_width(self):
        # A -> B -> D, A -> C -> D, and a lone E: longest chain 3, widest antichain {B, C, E}
        items = [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["A"]), item_doc("D", ["B", "C"]), item_doc("E")]
        rep = plan.validate(lp_of(items))
        self.assertEqual(len(rep.critical), 3)
        self.assertEqual(rep.critical[0], "A")
        self.assertEqual(rep.critical[-1], "D")
        self.assertEqual(rep.width, 3)
        self.assertEqual(sorted(rep.witness), ["B", "C", "E"])

    def test_width_is_the_true_antichain_not_the_widest_level(self):
        # levels: [A, X] [B, Y?]... X (level 1) and B (level 2): B depends on A only, X independent of both, so {X, B} is an
        # antichain of 2 that spans levels; two long chains A->B->C and X->Y->Z give width 2 with level width 2 as well.
        items = [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"]), item_doc("X"), item_doc("Y", ["X"]), item_doc("Z", ["Y"])]
        self.assertEqual(plan.validate(lp_of(items)).width, 2)
        # a chain with a side branch: A->B->C->D and A->E. Antichain {E, B} or {E, C} or {E, D}: width 2, level widths 1,2,1,1
        items = [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"]), item_doc("D", ["C"]), item_doc("E", ["A"])]
        self.assertEqual(plan.validate(lp_of(items)).width, 2)
        # fully independent: width = n
        self.assertEqual(plan.validate(lp_of([item_doc("A"), item_doc("B"), item_doc("C"), item_doc("D")])).width, 4)

    def test_a_single_item(self):
        rep = plan.validate(lp_of([item_doc("A")]))
        self.assertEqual((rep.width, rep.critical), (1, ["A"]))

    def test_ancestors_and_descendants(self):
        items = {i["id"]: i for i in [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"]), item_doc("D")]}
        self.assertEqual(plan.ancestors(items, "C"), {"A", "B"})
        self.assertEqual(plan.descendants(items, "A"), {"B", "C"})


class Slicing(unittest.TestCase):
    def test_size_l_must_be_split(self):
        self.assertIn("slice.size-l", ids(plan.validate(lp_of([item_doc("A", size="L")])), "error"))

    def test_estimated_loc_over_the_limit(self):
        it = item_doc("A", estimate={"loc": 401, "engineers": ["backend-engineer"]})
        self.assertIn("slice.loc", ids(plan.validate(lp_of([it]), max_item_loc=400), "error"))
        self.assertNotIn("slice.loc", ids(plan.validate(lp_of([it]), max_item_loc=500)))

    def test_engineers_and_migrations_and_acceptance_counts(self):
        it = item_doc("A", estimate={"loc": 10, "engineers": ["database-engineer", "backend-engineer", "app-engineer", "infra-engineer"]})
        self.assertIn("slice.engineers", ids(plan.validate(lp_of([it])), "error"))
        it = item_doc("A", estimate={"loc": 10, "engineers": ["database-engineer", "backend-engineer", "app-engineer"]})
        self.assertIn("slice.engineers", ids(plan.validate(lp_of([it])), "warn"))
        mig = {"name": "m1", "up": "db/m1.up.sql", "down": "db/m1.down.sql", "dataLossClass": "none"}
        it = item_doc("A", migrations=[mig, dict(mig, name="m2")])
        self.assertIn("slice.migrations", ids(plan.validate(lp_of([it])), "error"))
        self.assertIn("slice.acceptance", ids(plan.validate(lp_of([item_doc("A", acs=4)])), "warn"))

    def test_unknown_engineer_is_a_warning(self):
        it = item_doc("A", estimate={"loc": 10, "engineers": ["ghost-engineer"]})
        lp = lp_of([it])
        lp.agents = ["backend-engineer"]
        self.assertIn("plan.unknown-engineer", ids(plan.validate(lp), "warn"))


class Migrations(unittest.TestCase):
    def m(self, **kw):
        base = {"name": "0001_x", "up": "db/0001.up.sql", "down": "db/0001.down.sql", "dataLossClass": "none"}
        base.update(kw)
        return base

    def test_data_loss_class_is_required_and_closed(self):
        rep = plan.validate(lp_of([item_doc("A", migrations=[self.m(dataLossClass="maybe")])]))
        self.assertIn("migration.data-loss-class", ids(rep, "error"))
        bad = self.m()
        del bad["dataLossClass"]
        self.assertIn("migration.data-loss-class", ids(plan.validate(lp_of([item_doc("A", migrations=[bad])])), "error"))

    def test_a_down_is_required_except_for_a_lossy_one_with_a_reason(self):
        no_down = self.m(down=None)
        self.assertIn("migration.no-down", ids(plan.validate(lp_of([item_doc("A", migrations=[no_down])])), "error"))
        lossy = self.m(down=None, dataLossClass="lossy")
        self.assertIn("migration.no-down", ids(plan.validate(lp_of([item_doc("A", migrations=[lossy])])), "error"))
        lossy["noDownReason"] = "drops the column; a snapshot is in the item's acceptance"
        rep = plan.validate(lp_of([item_doc("A", migrations=[lossy])]))
        self.assertTrue(rep.ok, rep.findings)
        self.assertIn("migration.lossy", ids(rep, "warn"))

    def test_unsafe_migration_paths(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "db/../../x", "-rf", "a\\b"):
            with self.subTest(bad):
                rep = plan.validate(lp_of([item_doc("A", migrations=[self.m(up=bad)])]))
                self.assertIn("path.unsafe", ids(rep, "error"))

    def test_human_gate_reasons(self):
        lossy = self.m(dataLossClass="lossy")
        self.assertTrue(any("lossy" in r for r in plan.item_human_gate(item_doc("A", migrations=[lossy]))))
        self.assertTrue(plan.item_human_gate(item_doc("A", riskClass="high")))
        self.assertTrue(plan.item_human_gate(item_doc("A", touches={"domains": [], "paths": ["infra/**"], "schema": False, "deps_change": False})))
        self.assertTrue(plan.item_human_gate(item_doc("A", touches={"domains": [], "paths": ["src/**"], "schema": False, "deps_change": True})))
        self.assertEqual(plan.item_human_gate(item_doc("A", touches={"domains": [], "paths": ["src/**"], "schema": False, "deps_change": False})), [])
        self.assertTrue(plan.item_human_gate(item_doc("A", touches={"domains": [], "paths": ["billing/**"], "schema": False, "deps_change": False}), ["billing/**"]))


class Waivers(unittest.TestCase):
    def test_waivers_need_an_approver_an_expiry_and_findings(self):
        good = {"approver": "Pat", "expiry": "2999-01-01", "finding_ids": ["CWE-79:src/a.py"], "reason": "accepted risk"}
        self.assertTrue(plan.validate(lp_of([item_doc("A", waivers=[good])])).ok)
        for k in ("approver", "expiry", "finding_ids", "reason"):
            bad = dict(good)
            del bad[k]
            with self.subTest(k):
                self.assertFalse(plan.validate(lp_of([item_doc("A", waivers=[bad])])).ok)
        self.assertFalse(plan.validate(lp_of([item_doc("A", waivers=[dict(good, expiry="tomorrow")])])).ok)
        self.assertTrue(plan.waiver_active(good, "2026-10-26"))
        self.assertFalse(plan.waiver_active(dict(good, expiry="2020-01-01"), "2026-10-26"))


class Footprints(unittest.TestCase):
    def test_unsafe_globs_are_errors(self):
        for bad in ("../x/**", "/abs/**", ".git/hooks/*", "a/../b", "src/[a-z]*", "~/x", "-r"):
            with self.subTest(bad):
                it = item_doc("A", touches={"domains": [], "paths": [bad], "schema": False, "deps_change": False})
                self.assertIn("path.unsafe", ids(plan.validate(lp_of([it])), "error"))

    def test_verify_items_file_matches_what_verify_py_reads(self):
        it = item_doc("A", verify={"tests": "python3 -m unittest tests.test_a", "demoRoute": "/a"})
        out = plan.verify_items_file(lp_of([it]))
        self.assertEqual(out["items"]["A"], {"tests": "python3 -m unittest tests.test_a", "demo_route": "/a", "ac_ids": ["AC-A-1", "AC-A-2"]})
        bad = item_doc("A", verify={"tests": "x\ny", "demoRoute": "no-slash"})
        self.assertFalse(plan.validate(lp_of([bad])).ok)


class Loading(LoopTestCase):
    def test_load_at_rev_and_worktree_agree_on_a_committed_plan(self):
        lr = self.lr()
        a = plan.load_worktree(lr.path)
        b = plan.load_at_rev(lr.path, lr.base)
        self.assertEqual(plan.plan_digest(a), plan.plan_digest(b))
        self.assertEqual(b.ids(), ["A", "B", "C"])
        self.assertFalse(b.fatal)

    def test_the_committed_plan_is_not_the_working_tree_draft(self):
        lr = self.lr()
        lr.repo.write("plan/items/A.json", json.dumps(item_doc("A", title="EDITED")))
        self.assertEqual(plan.load_at_rev(lr.path, lr.base).items["A"]["title"], "Item A")
        self.assertEqual(plan.load_worktree(lr.path).items["A"]["title"], "EDITED")

    def test_stray_files_and_mismatched_ids(self):
        lr = self.lr()
        lr.repo.write("plan/items/notes.txt", "hi")
        lr.repo.write("plan/items/Z.json", json.dumps(item_doc("Q")))
        lr.repo.write("plan/items/not an id.json", "{}")
        lr.repo.write("plan/extra/other.json", "{}")
        sha = lr.commit_all()
        for lp in (plan.load_worktree(lr.path), plan.load_at_rev(lr.path, sha)):
            kinds = {f.id for f in lp.findings}
            self.assertIn("plan.stray-file", kinds)
            self.assertIn("plan.item-id", kinds)

    def test_symlinks_are_refused_in_the_worktree_and_in_git(self):
        lr = self.lr()
        outside = os.path.join(lr.home, "outside.json")
        with open(outside, "w") as fh:
            json.dump(item_doc("D"), fh)
        os.symlink(outside, os.path.join(lr.path, "plan", "items", "D.json"))
        lp = plan.load_worktree(lr.path)
        self.assertNotIn("D", lp.items)
        self.assertIn("plan.not-regular", {f.id for f in lp.findings})
        sha = lr.commit_all("add a symlinked item")
        lp = plan.load_at_rev(lr.path, sha)
        self.assertNotIn("D", lp.items)
        self.assertIn("plan.not-regular", {f.id for f in lp.findings})

    def test_hostile_json(self):
        lr = self.lr()
        hostile = {
            "dup": '{"schema": "loopctl.item.v1", "id": "D", "id": "D"}',
            "nan": '{"schema": "loopctl.item.v1", "id": "N", "estimate": {"loc": NaN}}',
            "deep": '{"id": "P", "x": ' + "[" * 40 + "]" * 40 + "}",
            "array": "[1, 2]",
            "empty": "",
            "binary": "\x00\x01\x02",
        }
        for name, text in hostile.items():
            iid = {"dup": "H1", "nan": "H2", "deep": "H3", "array": "H4", "empty": "H5", "binary": "H6"}[name]
            lr.repo.write("plan/items/%s.json" % iid, text)
        big = "x" * (plan.MAX_FILE_BYTES + 10)
        lr.repo.write("plan/items/Big.json", json.dumps({"id": "Big", "pad": big}))
        lp = plan.load_worktree(lr.path)
        self.assertEqual(sorted(lp.items), ["A", "B", "C"])
        self.assertGreaterEqual(len([f for f in lp.findings if f.severity == "error"]), 7)

    def test_a_missing_plan_is_fatal_not_empty(self):
        lr = self.lr(commit=False)
        lp = plan.load_worktree(lr.path)
        self.assertTrue(lp.fatal)
        self.assertEqual(lp.findings[0].id, "plan.missing")


if __name__ == "__main__":
    unittest.main()
