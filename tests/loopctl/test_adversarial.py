"""Adversarial self-review of everything that writes state or decides a gate (IMP-24 item 8). One row per attempt.

Attempts made against loopctl, and the test that pins the refusal:

    forged state ref ................ test_store.ForgedRef (ordinary commit, blob, tree, wrong shape, hostile JSON, other plan, broken
                                      sequence, a plausible forged `merged`) and here: ForgedAuxFiles
    stale lock ...................... test_lock (steal conditions, unreadable lock, foreign host, live driver pid)
    concurrent CAS .................. test_store.Store (injected race, four threads, exhausted retries)
    hostile item JSON ............... test_plan.Loading and here: HostilePlan
    path traversal in item ids ...... test_plan (ids, files, globs, migration paths), test_report (report paths), test_migrate (--from), here: Injection
    config change on the head branch  test_run, test_gates and here: HeadBranchChanges
    unknown cost .................... test_ledger, test_cli, here: ForgedAuxFiles.test_a_ledger_line_cannot_lower_the_total
    option and shell injection ...... here: Injection
    base rollback with --base ....... here: HeadBranchChanges
"""
from __future__ import annotations

import json
import os
import unittest

from loopctl import common, locked, plan, state as sm
from loopctl.common import Refused, UsageError

from . import LoopRepo, LoopTestCase, item_doc

SHA = "0123456789abcdef0123456789abcdef01234567"


class Injection(LoopTestCase):
    """Text from a plan, a model or a command line must never become an option, a path, a ref or a shell word."""

    def test_option_like_and_malformed_arguments_are_refused_with_no_side_effect(self):
        lr = self.lr()
        loop = lr.fast_start()
        before = lr.repo.git("rev-parse", "refs/loopctl/state/demo")
        attempts = [
            ["begin", "../x", "--session", lr.session], ["begin", "A;rm -rf /", "--session", lr.session],
            ["begin", "A", "--session", "../../x"], ["begin", "A", "--session", "a b"],
            ["advance", "A", "merged; touch /tmp/pwn", "--session", lr.session],
            ["advance", "A", "merged", "--merge-commit", "--upload-pack=x", "--session", lr.session],
            ["advance", "A", "merged", "--merge-commit", "-n", "--session", lr.session],
            ["advance", "A", "merged", "--merge-commit", "HEAD", "--session", lr.session],
            ["advance", "A", "design_review", "--pr-url", "javascript:alert(1)", "--session", lr.session],
            ["advance", "A", "design_review", "--head", "main", "--session", lr.session],
            ["fail", "A", "--class", "item", "--reason", "x; rm -rf", "--session", lr.session],
            ["fail", "A", "--class", "item; echo", "--reason", "tests_failed", "--session", lr.session],
            ["fail", "A", "--class", "item", "--reason", "tests_failed", "--ac", "AC-A-1; rm", "--session", lr.session],
            ["approve", "item", "A;B", "--by", "Pat"], ["approve", "plan", "--by", "Pat", "--seed-v1", "../../etc/passwd"],
            ["approve", "item", "A", "--by", "Pat", "--choice", "yolo"],
            ["park", "A", "--code", "x" * 100, "--session", lr.session], ["gate", "../g6", "A", "--session", lr.session],
            ["abort", "--dry-run", "--rollback-to", "--hard"], ["abort", "--dry-run", "--rollback-to", "main"], ["abort", "--dry-run", "--rollback-to", "$(id)"],
            ["status", "--base", "--output=/tmp/pwn"], ["validate", "--base", "-1"], ["render", "--repo", "/nonexistent/path"],
            ["audit", "render", "../A"], ["report", "check", "../../etc/passwd"], ["report", "check", "docs/features/../../x.md"],
            ["spend", "record", "--item", "A/../B", "--result", "-"],
            ["lock", "acquire", "--session", "../x", "--nonce", "n"], ["reconcile", "--dry-run", "--prs", "/etc/passwd"],
        ]
        for args in attempts:
            with self.subTest(args):
                r = lr.ctl(*args)
                self.assertNotEqual(r.rc, 0, (args, r))
        self.assertEqual(lr.repo.git("rev-parse", "refs/loopctl/state/demo"), before)
        self.assertEqual(lr.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists("/tmp/pwn"))
        self.assertEqual(lr.item("A")["status"], "pending")

    def test_a_hostile_slug_or_branch_never_reaches_git(self):
        lr = self.lr(items=[item_doc("A", slug="ok-slug")])
        loop = lr.fast_start()
        rec = loop.begin("A", lr.session)
        self.assertEqual(rec["branch"], "feat/ok-slug-a1")
        for bad_branch in ("--delete", "feat/../main", "-b", "feat/x y"):
            doc = lr.state().d
            with self.assertRaises(sm.StateCorrupt if not bad_branch.startswith("feat/x") else sm.StateCorrupt):
                doc["items"]["A"]["branch"] = bad_branch
                sm.validate_state_doc(doc, "demo")

    def test_a_forged_session_cannot_hold_or_use_the_lock(self):
        lr = self.lr()
        loop = lr.fast_start()
        for sid in ("sess-1x", "SESS-1", "sess-1 ", "", "sess-2"):
            with self.assertRaises((Refused, UsageError)):
                loop.begin("A", sid)

    def test_text_that_looks_like_an_escape_or_markup_is_cleaned_before_it_is_stored_or_printed(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.approve_plan("Pat\x1b[2J‮")
        loop.begin("A", lr.session, by="bot\x1b]0;pwn\x07")
        d = lr.state().d
        blob = json.dumps(d)
        self.assertNotIn("\\u001b", blob)
        self.assertNotIn("\\u202e", blob)
        self.assertNotIn("\\u0007", blob)
        self.assertNotIn("\x1b", lr.ctl("status", "--pending-human").out + lr.ctl("status").out)

    def test_a_ref_name_cannot_be_built_from_a_plan_name(self):
        for bad in ("../x", "a/b", "x.lock", "refs", "A", "a b", "", "-x", "x" * 60, "a..b", "x-"):
            if bad in ("refs",):
                continue
            with self.assertRaises(UsageError, msg=bad):
                sm.ref_for(bad)
        self.assertEqual(sm.ref_for("ok-name"), "refs/loopctl/state/ok-name")


class HostilePlan(LoopTestCase):
    def locked(self, lr):
        with open(os.path.join(lr.context().rs.root, "locked_paths.json")) as fh:
            return json.load(fh)

    def test_a_footprint_that_would_lock_the_whole_tree_is_refused(self):
        for glob in ("*", "**", "**/*", "/**", "?", "*/*"):
            it = item_doc("A", touches={"domains": [], "paths": [glob], "schema": False, "deps_change": False})
            lp = plan.LoadedPlan("t")
            lp.plan, lp.items = {"schema": plan.PLAN_SCHEMA, "name": "t", "goal": "g"}, {"A": it}
            self.assertFalse(plan.validate(lp).ok, glob)

    def test_locked_paths_hold_only_safe_globs_and_cannot_be_widened_by_a_hostile_item(self):
        lr = self.lr(items=[item_doc("A", touches={"domains": [], "paths": ["src/a/**"], "schema": False, "deps_change": False}),
                            item_doc("B", touches={"domains": [], "paths": ["src/b/**", "src/a/**"], "schema": False, "deps_change": False})])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        d = self.locked(lr)
        self.assertEqual(sorted(d["globs"]), ["plan/**", "src/b/**"])             # src/a/** is A's own lane, shared with B
        self.assertEqual(sorted(d["sources"]), ["footprints", "plan"])
        rs = lr.context().rs
        locked.add_gate_globs(rs, "g3", ["tests/acceptance/**", "../../etc/**", "/abs/**", "*", ".git/hooks/x"])
        d = self.locked(lr)
        self.assertEqual(sorted(d["globs"]), ["plan/**", "src/b/**", "tests/acceptance/**"])
        self.assertEqual(oct(os.stat(os.path.join(rs.root, "locked_paths.json")).st_mode & 0o777), "0o600")

    def test_a_merged_item_releases_its_footprint_lock(self):
        lr = self.lr(items=[item_doc("A", touches={"domains": [], "paths": ["src/a/**"], "schema": False, "deps_change": False}),
                            item_doc("B", touches={"domains": [], "paths": ["src/b/**"], "schema": False, "deps_change": False})])
        lr.fast_start()
        lr.quick_merge("A")
        d = self.locked(lr)
        self.assertEqual(d["globs"], ["plan/**"])

    def test_item_text_cannot_smuggle_a_second_audit_block_or_a_comment(self):
        from loopctl import audit
        lr = self.lr(items=[item_doc("A", intent="<!-- audit:end --> and <!-- audit:begin v1 --> `x` returns 200")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        text = audit.render_body(lr.context(), lr.state(), "A")
        self.assertEqual(text.count("<!-- audit:begin"), 1)
        self.assertEqual(text.count("<!-- audit:end"), 1)

    def test_an_enormous_plan_is_bounded(self):
        lr = self.lr(commit=False)
        many = [item_doc("I%d" % n) for n in range(plan.MAX_ITEMS + 5)]
        lr.items = many
        lr.write_plan(many)
        lp = plan.load_worktree(lr.path)
        self.assertEqual(len(lp.items), plan.MAX_ITEMS)
        self.assertIn("plan.too-many-items", {f.id for f in lp.findings})

    def test_a_dependency_chain_of_hundreds_does_not_blow_the_stack(self):
        items = [item_doc("N0")] + [item_doc("N%d" % n, ["N%d" % (n - 1)]) for n in range(1, 250)]
        lp = plan.LoadedPlan("t")
        lp.plan, lp.items = {"schema": plan.PLAN_SCHEMA, "name": "t", "goal": "g"}, {i["id"]: i for i in items}
        rep = plan.validate(lp)
        self.assertTrue(rep.ok)
        self.assertEqual((len(rep.critical), rep.width), (250, 1))


class HeadBranchChanges(LoopTestCase):
    """A change set must not be able to loosen the rules it is judged by (CO-11)."""

    def started(self, **kw):
        lr = self.lr(**kw)
        lr.fast_start()
        lr.repo.git("switch", "-q", "-c", "feat/attack", "main")
        return lr

    def test_budgets_retry_caps_and_loc_limits_raised_on_the_branch_are_ignored(self):
        lr = self.started(extra_cfg="run_budget_usd: 1\nmax_retries_per_item: 0\nmax_item_loc: 100")
        lr.repo.write(".claude/loop.config.md", lr.cfg_text.replace("run_budget_usd: 1", "run_budget_usd: 1000").replace("max_retries_per_item: 0", "max_retries_per_item: 50")
                      .replace("max_item_loc: 100", "max_item_loc: 100000"))
        lr.commit_all("loosen everything")
        ctx = lr.context()
        self.assertEqual((ctx.cfg.get("run_budget_usd"), ctx.cfg.get("max_retries_per_item"), ctx.cfg.get("max_item_loc")), (1, 0, 100))
        self.assertEqual({n for n, _b, _h in ctx.config_drift}, {"run_budget_usd", "max_retries_per_item", "max_item_loc"})
        lr.write_plan([item_doc("A", estimate={"loc": 5000, "engineers": ["backend-engineer"]}), item_doc("B", ["A"]), item_doc("C", ["B"])])
        lr.commit_all("big item")
        # Tier 2 reads the plan at the base sha, where the big item does not exist
        loop = lr.loop(fresh=True)
        self.assertNotIn("slice.loc", {f.id for f in plan.validate(loop.ctx.plan, max_item_loc=loop.ctx.cfg.get_int("max_item_loc")).findings})

    def test_acceptance_criteria_edited_on_the_branch_do_not_change_what_the_item_is_judged_against(self):
        lr = self.started()
        edited = [item_doc("A"), item_doc("B", ["A"]), item_doc("C", ["B"])]
        edited[0]["acceptance"] = [{"id": "AC-A-1", "text": "`GET /a/1` returns 200"}]       # quietly weaker
        lr.write_plan(edited)
        lr.commit_all("weaken my own acceptance criteria")
        loop = lr.loop(fresh=True)
        self.assertEqual(len(loop.ctx.plan.items["A"]["acceptance"]), 2)
        self.assertEqual(plan.ac_digest(loop.ctx.plan.items["A"]), lr.state().header["approvedPlan"]["acDigests"]["A"])

    def test_an_item_cannot_opt_itself_into_tier2_or_turn_a_gate_off(self):
        lr = self.lr(tier2=False)
        lr.repo.git("switch", "-q", "-c", "feat/attack", "main")
        lr.repo.write(".claude/loop.config.md", lr.cfg_text.replace("loopctl_tier2: false", "loopctl_tier2: true\ngates:\n  g6: off"))
        lr.commit_all("opt in")
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 3)

    def test_base_cannot_be_rolled_back_to_a_commit_with_a_weaker_config(self):
        lr = self.lr(extra_cfg="run_budget_usd: 1")
        old = lr.repo.head()
        lr.repo.write(".claude/loop.config.md", lr.cfg_text.replace("run_budget_usd: 1", "run_budget_usd: 2"))
        lr.commit_all("raise the budget through a reviewed merge")
        # a run starts at the new base
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 0, r)
        lr.context().rs.write_run({"runId": "run-1", "baseSha": lr.repo.head(), "worktree": lr.path, "plan": "demo", "runner": "sequential", "conductor_session_id": "s"})
        r = lr.ctl("approve", "item", "A", "--by", "Pat", "--base", old)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("older than the base this run started from", r.err)

    def test_base_must_be_on_the_integration_branch(self):
        lr = self.lr()
        lr.repo.git("switch", "-q", "-c", "feat/attack", "main")
        tip = lr.repo.commit_file(".claude/loop.config.md", lr.cfg_text.replace("delivery: local", "delivery: local\nrun_budget_usd: 9999"), "my own config")
        lr.repo.git("switch", "-q", "main")
        r = lr.ctl("approve", "plan", "--by", "Pat", "--base", tip)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("not on the integration branch", r.err)

    def test_reading_the_working_tree_is_for_drafts_only(self):
        lr = self.lr()
        lr.fast_start()
        lr.repo.write("plan/items/A.json", json.dumps(item_doc("A", title="draft")))
        self.assertEqual(lr.context().plan.items["A"]["title"], "Item A")                      # Tier 2 context: the base
        self.assertEqual(lr.ctl("validate").rc, 0)                                             # the draft is validated, not used


class HostileGitConfig(LoopTestCase):
    """loopctl's git calls ignore the user's config and the repository's hooks and fsmonitor (IMP-25)."""

    def arm(self, lr):
        marker = os.path.join(lr.home, "executed")
        hook = os.path.join(lr.home, "evil.sh")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\nexit 0\n" % marker)
        os.chmod(hook, 0o755)
        hooks = os.path.join(lr.path, ".git", "hooks")
        for name in ("reference-transaction", "post-commit", "post-merge", "pre-push", "post-checkout", "post-rewrite", "pre-commit"):
            with open(os.path.join(hooks, name), "w") as fh:
                fh.write("#!/bin/sh\ntouch %s\nexit 0\n" % marker)
            os.chmod(os.path.join(hooks, name), 0o755)
        with open(os.path.join(lr.path, ".git", "config"), "a") as fh:
            fh.write("[core]\n\tfsmonitor = %s\n\teditor = %s\n\tpager = %s\n[diff]\n\texternal = %s\n[alias]\n\tstatus = !%s\n" % (hook, hook, hook, hook, hook))
        gcfg = os.path.join(lr.home, "gitconfig")
        with open(gcfg, "w") as fh:
            fh.write("[core]\n\thooksPath = %s\n\tfsmonitor = %s\n[alias]\n\tlog = !%s\n" % (os.path.dirname(hook), hook, hook))
        return marker, gcfg

    def test_hooks_fsmonitor_pagers_aliases_and_global_config_never_run(self):
        lr = self.lr()
        loop = lr.fast_start()
        before = lr.repo.head()
        lr.quick_merge("A")
        lr.quick_merge("B")
        marker, gcfg = self.arm(lr)
        env = {"GIT_CONFIG_GLOBAL": gcfg, "GIT_CONFIG_SYSTEM": gcfg, "HOME": lr.home}
        # control: plain git on this repo does run the planted program, so the rest of the test proves something
        import subprocess
        subprocess.run(["git", "-C", lr.path, "commit", "--allow-empty", "-q", "-m", "control"], env=dict(lr.env, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        controlled = os.path.exists(marker)
        self.assertTrue(controlled, "control: plain git should have run the planted program, or this test proves nothing")
        os.unlink(marker)
        loop = lr.loop(fresh=True)
        loop.ctx.env = dict(lr.env, **env)
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in env])
        for args in (["status"], ["validate"], ["lock", "status"], ["approve", "plan", "--by", "Pat"], ["reconcile", "--dry-run"], ["reconcile", "--apply"],
                     ["run", "--dry-run"], ["abort", "--dry-run", "--rollback-to", before]):
            lr.ctl(*args, env=env)
            self.assertFalse(os.path.exists(marker), "%s ran a planted program" % args)
        r = lr.ctl("abort", "--rollback-to", before, "--by", "Pat", "--session", lr.session, env=env)
        self.assertEqual(r.rc, 0, r)
        self.assertFalse(os.path.exists(marker), "abort (git revert, status) ran a planted program")
        self.assertEqual(lr.state().header["runStatus"], "aborted")


class ForgedAuxFiles(LoopTestCase):
    def test_a_ledger_line_cannot_lower_the_total_or_hide_an_unknown(self):
        lr = self.lr()
        led = lr.context().ledger()
        led.record("run-1", "A", 1, None, cost=2.0)
        with open(led.path, "a") as fh:
            fh.write(json.dumps({"type": "spend", "runId": "run-1", "item": "A", "day": common.iso()[:10], "total_cost_usd": -1000.0, "unknown": False}) + "\n")
            fh.write(json.dumps({"type": "spend", "runId": "run-1", "item": "A", "day": common.iso()[:10], "total_cost_usd": 0.1, "unknown": False, "extra": "x"}) + "\n")
            fh.write(json.dumps({"type": "ack", "runId": "run-OTHER", "by": "x"}) + "\n")
        t = led.totals(run_id="run-1")
        self.assertEqual(t["usd"], 2.1)                                   # the negative line counted for nothing, the other for its face value
        self.assertGreaterEqual(t["unknown"], 1)                          # and the negative one is an unknown, so the run is parked
        self.assertFalse(led.verdict(type("C", (), {"get": lambda self, n, d=None: 0})(), "run-1").ok)

    def test_a_forged_session_registration_does_not_make_a_conductor_of_the_wrong_run(self):
        lr = self.lr()
        lr.fast_start()
        rs = lr.context().rs
        rs.register_session("rogue", "conductor", registered_by="forged")
        with self.assertRaises(Refused):
            lr.loop(fresh=True).begin("A", "rogue")                       # registration is not the lock: the lock holder is who writes

    def test_the_run_record_cannot_move_the_loop_worktree_somewhere_else_to_unlock_a_discard(self):
        lr = self.lr(extra_cfg="max_retries_per_item: 0")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        lr.repo.git("branch", lr.item("A")["branch"], "main")
        run = lr.context().rs.read_run()
        run["worktree"] = lr.path + "/../" + os.path.basename(lr.path)           # same directory spelled differently
        lr.context().rs.write_run(run)
        d = loop.fail("A", "item", "tests_failed", lr.session)
        self.assertEqual(d.action, "discard")
        self.assertIsNotNone(d.git)                                       # realpath comparison: it is the same checkout

    def test_status_survives_every_kind_of_junk_in_the_run_state_directory(self):
        lr = self.lr()
        lr.fast_start()
        rs = lr.context().rs
        junk = {"run.json": "[]", "lock": "\x00\x01", "spend.jsonl": "}{\n" * 50, "locked_paths.json": "null", "dead-branches.json": "[1]",
                "HALT": "not json"}
        for name, text in junk.items():
            with open(os.path.join(rs.root, name), "w") as fh:
                fh.write(text)
        for args in (["status"], ["status", "--json"], ["status", "--pending-human"], ["lock", "status"], ["render"], ["cost", "--projection"], ["run", "--dry-run"]):
            r = lr.ctl(*args)
            self.assertIn(r.rc, (0, 1), (args, r))


if __name__ == "__main__":
    unittest.main()
