"""migrate-plan (v1 build-plan.json -> plan/) and the hello-loop example plan (report section 10.3 field mapping, CO-19)."""
from __future__ import annotations

import json
import os
import shutil
import unittest

from loopctl import migrate, plan

from tests import REPO_ROOT
from tests.baseline import FIXTURE, load_tool

from . import LoopTestCase

V1 = {
    "goal": "Build a thing.", "createdAt": "2026-10-26",
    "items": [
        {"id": "F1", "milestone": "M1", "title": "T1", "slug": "t-one", "size": "S", "dependsOn": [], "intent": "i1",
         "acceptance": ["AC-F1-1: `GET /` returns 200", "AC-F1-2: the test `tests/test_one.py` passes"], "requiresEnv": ["API_KEY"],
         "status": "done", "blockReason": None, "branch": "feat/t-one-a1", "mergeCommit": "a" * 40, "verifyEvidence": {"testsExit": 0, "runExit": 0, "logRef": "x"},
         "downMigrationRef": "db/0001.down.sql (class: lossy-recoverable)", "reworkNotes": "make the button blue", "securityWaiver": None},
        {"id": "F2", "milestone": "M1", "title": "T2", "slug": "t-two", "size": "M", "dependsOn": ["F1"], "intent": "i2",
         "acceptance": ["`GET /two` returns 404 for an unknown id", "AC-F2-2: exits 0"], "requiresEnv": [],
         "status": "blocked", "blockReason": "security_blocked", "branch": None, "mergeCommit": None, "verifyEvidence": None, "downMigrationRef": None,
         "reworkNotes": None, "securityWaiver": {"by": "user", "reason": "accepted for the demo", "findings": ["src/a.py:12 CWE-79"], "commit": "b" * 40, "ts": "2026-10-01"}},
        {"id": "F3", "milestone": "M2", "title": "T3", "slug": "t-three", "dependsOn": ["F2"], "intent": "i3", "acceptance": ["`x` returns 200"],
         "requiresEnv": [], "status": "in_progress", "blockReason": None, "branch": "feat/t-three-a1", "mergeCommit": None, "verifyEvidence": None,
         "downMigrationRef": None, "reworkNotes": None, "securityWaiver": None},
    ],
}


def v1(**over):
    d = json.loads(json.dumps(V1))
    d.update(over)
    return d


class Migrate(LoopTestCase):
    def test_field_mapping(self):
        res = migrate.migrate_v1(v1(), "demo", today="2026-10-26")
        self.assertTrue(res.ok, [f.to_dict() for f in res.findings if f.severity == "error"])
        f1, f2, f3 = (res.item_docs[i] for i in ("F1", "F2", "F3"))
        self.assertEqual(f1["acceptance"], [{"id": "AC-F1-1", "text": "`GET /` returns 200"}, {"id": "AC-F1-2", "text": "the test `tests/test_one.py` passes"}])
        self.assertEqual(f1["rework"], [{"ts": "2026-10-26", "note": "make the button blue", "by": "v1"}])
        self.assertEqual(f1["migrations"], [{"name": "0001.down.sql", "up": None, "down": "db/0001.down.sql", "dataLossClass": "lossy-recoverable"}])
        self.assertEqual(f1["requiresEnv"], ["API_KEY"])
        self.assertEqual(f2["acceptance"][0]["id"], "AC-F2-1")                     # generated from its position
        self.assertIn("migrate.ac-id-generated", {f.id for f in res.findings})
        w = f2["waivers"][0]
        self.assertEqual((w["approver"], w["expiry"], w["finding_ids"], w["commit"]), ("user", "2026-10-26", ["src_a.py:12_CWE-79"], "b" * 40))
        self.assertIn("migrate.waiver-expiry", {f.id for f in res.findings})
        self.assertEqual(f3["size"], "M")                                          # defaulted, with a warning
        self.assertIn("migrate.size-defaulted", {f.id for f in res.findings})
        self.assertNotIn("status", f1)
        self.assertEqual(res.plan_doc["milestones"], ["M1", "M2"])
        self.assertEqual(res.plan_doc["name"], "demo")

    def test_the_migrated_plan_validates(self):
        res = migrate.migrate_v1(v1(), "demo")
        lp = plan.LoadedPlan("m")
        lp.plan, lp.items = res.plan_doc, res.item_docs
        rep = plan.validate(lp)
        self.assertTrue(rep.ok, rep.findings)

    def test_the_state_seed(self):
        seed = migrate.state_seed(v1())
        self.assertEqual(seed["F1"]["status"], "merged")
        self.assertEqual(seed["F1"]["mergeCommit"], "a" * 40)
        self.assertEqual((seed["F2"]["status"], seed["F2"]["blockReason"]), ("blocked", "security_blocked"))
        self.assertEqual(seed["F3"]["status"], "pending")                            # in_progress: v1 discards partial work
        self.assertEqual(seed["F3"]["v1Status"], "in_progress")
        d = v1()
        d["items"][0]["mergeCommit"] = None
        res = migrate.migrate_v1(d, "demo")
        self.assertEqual((res.seed["F1"]["status"], res.seed["F1"]["blockReason"]), ("needs_human", "merge_missing"))

    def test_unknown_fields_fail_loudly_and_nothing_is_written(self):
        d = v1()
        d["items"][0]["surprise"] = 1
        res = migrate.migrate_v1(d, "demo")
        self.assertFalse(res.ok)
        self.assertIn("migrate.unknown-field", {f.id for f in res.findings})
        d = v1(extra_top="x")
        self.assertIn("migrate.unknown-field", {f.id for f in migrate.migrate_v1(d, "demo").findings})
        lr = self.lr(commit=False)
        with self.assertRaises(Exception):
            migrate.write_plan(lr.path, res)
        self.assertFalse(os.path.exists(os.path.join(lr.path, "plan")))

    def test_bad_input_is_refused(self):
        for bad in ({"goal": "g", "items": []}, {"items": [{"id": "F1"}]}, {"goal": "g", "items": "x"}, {"goal": "", "items": [{}]}):
            self.assertFalse(migrate.migrate_v1(bad, "demo").ok)
        d = v1()
        d["items"][0]["id"] = "../etc"
        self.assertFalse(migrate.migrate_v1(d, "demo").ok)
        d = v1()
        d["items"][1]["acceptance"] = ["AC-F9-1: belongs to another item"]
        self.assertIn("migrate.ac-id", {f.id for f in migrate.migrate_v1(d, "demo").findings})
        d = v1()
        d["items"][1]["status"] = "weird"
        self.assertIn("migrate.status", {f.id for f in migrate.migrate_v1(d, "demo").findings})
        d = v1()
        d["items"].append(dict(d["items"][0]))
        self.assertIn("migrate.dup", {f.id for f in migrate.migrate_v1(d, "demo").findings})
        self.assertFalse(migrate.migrate_v1(v1(), "Bad Name").ok)
        for text in ("[]", "NaN", '{"a": 1, "a": 2}', "x"):
            with self.assertRaises(ValueError):
                migrate.read_v1(text)

    def test_a_down_migration_ref_that_is_not_a_safe_path_becomes_a_lossy_no_down(self):
        d = v1()
        d["items"][0]["downMigrationRef"] = "../../etc/passwd lossy"
        res = migrate.migrate_v1(d, "demo")
        m = res.item_docs["F1"]["migrations"][0]
        self.assertEqual((m["down"], m["dataLossClass"]), (None, "lossy"))
        self.assertTrue(res.ok)

    def test_cli_check_dry_run_write_and_force(self):
        lr = self.lr(commit=False)
        lr.repo.write("build-plan.json", json.dumps(v1()))
        r = lr.ctl("migrate-plan")
        self.assertEqual(r.rc, 2)                                                   # one of --check, --dry-run, --write is required
        for flag in ("--check", "--dry-run"):
            r = lr.ctl("migrate-plan", flag, "--name", "demo")
            self.assertEqual(r.rc, 0, r)
            self.assertFalse(os.path.exists(os.path.join(lr.path, "plan")))
        self.assertEqual(lr.ctl("migrate-plan", "--check", "--write").rc, 2)
        r = lr.ctl("migrate-plan", "--write", "--name", "demo")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(os.path.isfile(os.path.join(lr.path, "plan", "items", "F2.json")))
        self.assertEqual(lr.ctl("migrate-plan", "--write", "--name", "demo").rc, 3)       # refuses to overwrite
        self.assertEqual(lr.ctl("migrate-plan", "--write", "--name", "demo", "--force").rc, 0)
        self.assertEqual(lr.ctl("migrate-plan", "--check", "--force").rc, 2)
        self.assertEqual(lr.ctl("validate").rc, 0)

    def test_cli_path_confinement(self):
        lr = self.lr(commit=False)
        outside = os.path.join(lr.home, "build-plan.json")
        with open(outside, "w") as fh:
            json.dump(v1(), fh)
        for src in (outside, "../outside.json"):
            self.assertEqual(lr.ctl("migrate-plan", "--check", "--from", src).rc, 2, src)
        os.symlink(outside, os.path.join(lr.path, "linked.json"))
        self.assertEqual(lr.ctl("migrate-plan", "--check", "--from", "linked.json").rc, 2)

    def test_the_migrate_then_approve_with_seed_path_end_to_end(self):
        lr = self.lr(commit=False)
        mc = lr.repo.head()
        d = v1()
        d["items"][0]["mergeCommit"] = mc
        lr.repo.write("build-plan.json", json.dumps(d))
        lr.repo.write(".claude/loop.config.md", lr.cfg_text)
        lr.ctl("migrate-plan", "--write", "--name", "demo")
        lr.commit_all("migrate")
        r = lr.ctl("approve", "plan", "--by", "Pat", "--seed-v1", "build-plan.json")
        self.assertEqual(r.rc, 0, r)
        st = lr.state()
        self.assertEqual((st.items["F1"]["status"], st.items["F1"]["mergeCommit"]), ("merged", mc))
        self.assertEqual((st.items["F2"]["status"], st.items["F2"]["blockReason"]), ("blocked", "security_blocked"))
        self.assertEqual(st.items["F3"]["status"], "pending")


class ExamplePlan(LoopTestCase):
    def test_the_hello_loop_plan_validates_and_matches_its_v1_source(self):
        with open(os.path.join(FIXTURE, "build-plan.json")) as fh:
            doc = json.load(fh)
        res = migrate.migrate_v1(doc, "hello-loop")
        lp = plan.load_worktree(FIXTURE)
        self.assertFalse(lp.fatal, lp.findings)
        rep = plan.validate(lp, roster=["database-engineer", "backend-engineer", "app-engineer"])
        self.assertTrue(rep.ok, [f.to_dict() for f in rep.findings if f.severity == "error"])
        self.assertEqual(rep.order, ["F1", "F2", "F3"])
        self.assertEqual(rep.critical, ["F1", "F2", "F3"])
        for iid, mig in res.item_docs.items():
            ex = lp.items[iid]
            for k in ("id", "milestone", "title", "slug", "dependsOn", "intent", "acceptance", "requiresEnv"):
                self.assertEqual(ex[k], mig[k], (iid, k))
        self.assertEqual(lp.plan["goal"], doc["goal"])
        self.assertEqual(lp.plan["name"], "hello-loop")

    def test_the_example_items_agree_with_the_fixture_verify_json(self):
        with open(os.path.join(FIXTURE, "verify.json")) as fh:
            vj = json.load(fh)["items"]
        lp = plan.load_worktree(FIXTURE)
        for iid, spec in vj.items():
            it = lp.items[iid]
            self.assertEqual(plan.ac_ids(it), spec["ac_ids"])
            self.assertEqual(it["verify"]["tests"], spec["tests"])
            self.assertEqual(it["verify"]["demoRoute"], spec["demo_route"])

    def test_the_example_is_human_gated_where_the_report_says_it_must_be(self):
        lp = plan.load_worktree(FIXTURE)
        self.assertTrue(plan.item_human_gate(lp.items["F1"]))        # a migration (H3)
        self.assertEqual(plan.item_human_gate(lp.items["F3"]), [])

    def test_the_example_plan_runs_end_to_end_in_a_temp_repo(self):
        lr = self.lr(commit=False)
        shutil.copytree(os.path.join(FIXTURE, "plan"), os.path.join(lr.path, "plan"))
        lr.items = list(plan.load_worktree(lr.path).items.values())
        lr.plan_name = "hello-loop"
        lr.repo.write(".claude/loop.config.md", lr.cfg_text)
        lr.commit_all("hello-loop plan")
        self.assertEqual(lr.ctl("validate", "--committed").rc, 0)
        loop = lr.fast_start()
        self.assertIn("footprint hello_loop/migrations/**", lr.state().items["F1"]["humanGate"][0])
        lr.complete("F1", approve=True)
        lr.complete("F2")
        lr.complete("F3")
        self.assertEqual(lr.state().counts()["merged"], 3)

    def test_the_v1_baseline_run_does_not_receive_the_v2_plan(self):
        prepare = load_tool("prepare")
        files = prepare.fixture_files(FIXTURE)
        self.assertIn("build-plan.json", files)
        self.assertFalse([f for f in files if f.startswith("plan" + os.sep) or f == "plan"])


class Template(unittest.TestCase):
    def test_the_plan_item_template_is_a_valid_item(self):
        with open(os.path.join(REPO_ROOT, ".claude", "templates", "plan-item.json")) as fh:
            it = json.load(fh)
        lp = plan.LoadedPlan("t")
        lp.plan = {"schema": plan.PLAN_SCHEMA, "name": "t", "goal": "g"}
        lp.items = {it["id"]: it}
        rep = plan.validate(lp)
        self.assertTrue(rep.ok, rep.findings)
        self.assertEqual(set(it), plan.KNOWN_ITEM_KEYS - {"ext"})


if __name__ == "__main__":
    unittest.main()
