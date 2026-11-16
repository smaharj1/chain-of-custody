"""The driver's pure decisions: what a child's exit means, what a gate's failure is called, who builds an item, what counts as sensitive,
and the checkpoint rule (report 10.4, 11.6, FI-05 and FI-14 classification, FI-16 gate reasons)."""
from __future__ import annotations

import json
import signal
import unittest

from ... import SCRIPTS_DIR  # noqa: F401

from loopctl import driver, failure, launch

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


def child(rc=0, parsed="ok", sig=None, reason=None, stderr="", stdout=""):
    r = launch.ChildResult()
    r.returncode = rc
    r.signal_name = sig
    r.reason = reason
    r.stderr_tail = stderr
    r.stdout = stdout
    if parsed == "ok":
        r.parsed = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.1}
    elif isinstance(parsed, dict):
        r.parsed = parsed
    return r


class ClassifyChild(unittest.TestCase):
    def test_a_clean_success_is_not_a_failure(self):
        self.assertIsNone(driver.classify_child(child()))

    def test_overload_and_rate_limit_are_platform_failures_with_their_own_reasons(self):
        over = child(1, {"subtype": "error_during_execution", "is_error": True, "result": 'API Error: 529 {"error":{"type":"overloaded_error"}}'})
        self.assertEqual(driver.classify_child(over), {"failure_class": "platform", "reason": "overloaded"})
        limit = child(1, {"subtype": "error_during_execution", "is_error": True, "result": "API Error: 429 rate limit reached"})
        self.assertEqual(driver.classify_child(limit), {"failure_class": "platform", "reason": "rate_limit"})
        five = child(1, {"subtype": "error_during_execution", "is_error": True, "result": "API Error: 529 overloaded"}, stderr="529")
        self.assertEqual(driver.classify_child(five)["failure_class"], "platform")

    def test_a_stall_is_transient_and_anything_the_cli_did_wrong_is_platform_never_the_items_fault(self):
        self.assertEqual(driver.classify_child(child(-15, None, "SIGTERM", "stall")), {"failure_class": "transient", "reason": "stall"})
        for kw in (dict(rc=-9, parsed=None, sig="SIGKILL"), dict(rc=139, parsed=None), dict(rc=1, parsed=None, stderr="TypeError: x"),
                   dict(rc=1, parsed={"is_error": True, "subtype": "error_during_execution", "result": "something odd"}), dict(rc=0, parsed=None, stdout='{"type":"res'),
                   dict(rc=0, parsed={"is_error": True, "result": "boom"}), dict(rc=0, parsed={"subtype": "error_during_execution"})):
            with self.subTest(kw):
                self.assertEqual(driver.classify_child(child(**kw)), {"failure_class": "platform", "reason": "cli_crash"})

    def test_a_missing_toolchain_is_env_and_a_port_in_use_is_transient(self):
        self.assertEqual(driver.classify_child(child(1, None, stderr="node: command not found")), {"failure_class": "env", "reason": "toolchain_mismatch"})
        self.assertEqual(driver.classify_child(child(127, None, stderr="node: command not found"))["failure_class"], "platform")     # the CLI itself missing
        self.assertEqual(driver.classify_child(child(1, None, stderr="Error: listen EADDRINUSE :::3000"))["failure_class"], "transient")

    def test_budget_and_turn_limits(self):
        self.assertEqual(driver.classify_child(child(1, {"subtype": "error_max_budget_usd", "is_error": True}))["failure_class"], "budget")
        self.assertEqual(driver.classify_child(child(1, {"subtype": "error_max_turns", "is_error": True})), {"failure_class": "item", "reason": "partial"})

    def test_a_child_ended_by_the_driver_is_never_a_success(self):
        for reason in ("halt", "lock_lost", "canary", "interrupted"):
            self.assertIsNotNone(driver.classify_child(child(0, "ok", reason=reason)), reason)


class ReasonFor(unittest.TestCase):
    def res(self, fclass, status="fail", findings=()):
        return {"status": status, "failure_class": fclass, "findings": list(findings)}

    def test_defaults_by_gate_and_class(self):
        self.assertEqual(driver.reason_for("g6", self.res("item")), ("item", "tests_failed"))
        self.assertEqual(driver.reason_for("g3", self.res("item")), ("item", "tests_failed"))
        self.assertEqual(driver.reason_for("g5", self.res("item")), ("item", "reward_hack"))
        self.assertEqual(driver.reason_for("g7", self.res("item")), ("item", "review_blocked"))
        self.assertEqual(driver.reason_for("g1", self.res("item")), ("item", "ac_unclear"))
        self.assertEqual(driver.reason_for("g6", self.res("item", findings=[{"id": "g6.lint"}])), ("item", "lint_failed"))
        self.assertEqual(driver.reason_for("g6", self.res("item", findings=[{"id": "g6.build-failed"}])), ("item", "build_failed"))
        self.assertEqual(driver.reason_for("g0", self.res("env", "error")), ("env", "doctor_failed"))
        self.assertEqual(driver.reason_for("g6", self.res("platform", "error")), ("platform", "cli_crash"))
        self.assertEqual(driver.reason_for("g6", self.res("transient")), ("transient", "flaky_test"))

    def test_an_error_without_a_class_is_env_and_a_fail_without_one_is_item(self):
        self.assertEqual(driver.reason_for("g9", {"status": "error", "failure_class": "none", "findings": []})[0], "env")
        self.assertEqual(driver.reason_for("g9", {"status": "fail", "failure_class": "none", "findings": []})[0], "item")
        self.assertEqual(driver.reason_for("g9", {"status": "fail", "failure_class": "bogus", "findings": []})[0], "item")

    def test_a_gate_may_name_its_reason_but_not_launder_one(self):
        self.assertEqual(driver.reason_for("g6", self.res("env", "error", [{"reason": "db_down"}])), ("env", "db_down"))
        self.assertEqual(driver.reason_for("g6", self.res("env", "error", [{"reason": "port_in_use"}])), ("env", "doctor_failed"))     # that one is transient
        self.assertEqual(driver.reason_for("g6", self.res("item", "fail", [{"reason": "db_down"}])), ("item", "tests_failed"))          # an item cannot claim env
        self.assertEqual(driver.reason_for("g6", self.res("item", "fail", [{"reason": "Not A Reason"}]))[1], "tests_failed")
        self.assertEqual(driver.reason_for("g9", self.res("item", "fail", [{"reason": "new_dependency"}])), ("item", "new_dependency"))      # an unknown reason keeps its class
        for fclass, reason in (driver.reason_for(g, self.res(c, "fail", [{"reason": r}])) for g in ("g6", "g5") for c in failure.CLASSES for r in failure.KNOWN_REASONS):
            self.assertIn(fclass, failure.CLASSES)
            failure.check_reason(fclass, reason)                                                       # whatever comes out is accepted by the taxonomy


class Roles(unittest.TestCase):
    ROSTER = ["backend-engineer", "database-engineer", "app-engineer", "infra-engineer", "admin-app-engineer", "code-reviewer"]

    def test_engineers_come_from_the_estimate_in_dependency_order(self):
        it = {"estimate": {"engineers": ["app-engineer", "backend-engineer", "database-engineer", "infra-engineer"]}}
        self.assertEqual(driver.engineers_for(it, self.ROSTER), ["infra-engineer", "database-engineer", "backend-engineer", "app-engineer"])
        self.assertEqual(driver.engineers_for({"estimate": {"engineers": ["zeta-engineer", "backend-engineer", "backend-engineer"]}}, self.ROSTER), ["backend-engineer", "zeta-engineer"])

    def test_without_an_estimate_the_footprint_domains_name_them(self):
        it = {"touches": {"domains": ["frontend", "backend", "database"]}}
        self.assertEqual(driver.engineers_for(it, self.ROSTER), ["database-engineer", "backend-engineer"])
        self.assertEqual(driver.engineers_for({}, self.ROSTER), [])
        self.assertEqual(driver.engineers_for({"estimate": "x", "touches": 5}, self.ROSTER), [])

    def test_domain_names(self):
        self.assertEqual([driver.domain_of(r) for r in ("backend-engineer", "admin-app-engineer", "acceptance-author")], ["backend", "admin-app", "acceptance-author"])


class Sensitive(unittest.TestCase):
    def test_sensitive_means_auth_payments_uploads_user_input_deps_schema_or_high_risk(self):
        for kw in ({"riskClass": "high"}, {"intent": "Add login with a password."}, {"title": "Stripe checkout"}, {"intent": "Accept file uploads."},
                   {"intent": "Rotate the session token."}, {"touches": {"domains": ["backend"], "paths": ["src/auth/**"], "schema": False, "deps_change": False}},
                   {"touches": {"domains": ["backend"], "paths": ["src/a.py"], "schema": True, "deps_change": False}},
                   {"touches": {"domains": ["backend"], "paths": ["src/a.py"], "schema": False, "deps_change": True}}):
            self.assertTrue(driver.is_sensitive(item_doc("A", estimate=ENG, **kw)), kw)
        for kw in ({"intent": "Render the home page."}, {"title": "Bookmarks list"}, {"riskClass": "low"}):
            self.assertFalse(driver.is_sensitive(item_doc("A", estimate=ENG, **kw)), kw)


class Verdicts(unittest.TestCase):
    def test_verdict_parsing(self):
        ok = "# Review\n\n## Verdict\nCODE_REVIEW_APPROVED\n\n## Summary\n"
        self.assertEqual(driver.parse_verdict(ok, "code-reviewer"), "CODE_REVIEW_APPROVED")
        self.assertEqual(driver.parse_verdict("## Verdict\n\n**CHANGES_REQUESTED**\n", "code-reviewer"), "CHANGES_REQUESTED")
        self.assertEqual(driver.parse_verdict("### verdict\n`SECURITY_BLOCKED`\n", "security-reviewer"), "SECURITY_BLOCKED")
        self.assertEqual(driver.parse_verdict("## Verdict\nDESIGN_NEEDS_REVISION", "design-critic"), "DESIGN_NEEDS_REVISION")
        for bad in ("", "no verdict", "## Verdict\nCODE_REVIEW_APPROVED | CHANGES_REQUESTED\n", "## Verdict\nmaybe\n", "CODE_REVIEW_APPROVED",
                    "## Verdict\n\n\n\n\n\n\nCODE_REVIEW_APPROVED", "## Summary\nCODE_REVIEW_APPROVED\n", "## Verdict\nSECURITY_APPROVED\n"):
            self.assertIsNone(driver.parse_verdict(bad, "code-reviewer"), bad)
        self.assertIsNone(driver.parse_verdict(ok, "unknown-role"))
        self.assertIsNone(driver.parse_verdict(None, "code-reviewer"))

    def test_a_reviewer_cannot_approve_by_quoting_the_template(self):
        quoted = "## Summary\nthe format is:\n```\n## Verdict\nCODE_REVIEW_APPROVED | CHANGES_REQUESTED\n```\n## Verdict\nCODE_REVIEW_APPROVED\n"
        self.assertIsNone(driver.parse_verdict(quoted, "code-reviewer"))                     # the first heading is the template line: fail closed


class Checkpoints(DriverTestCase):
    def merged_a(self, cfg, items=None, milestones=None):
        its = items or [item_doc("A", estimate=ENG, milestone="M1"), item_doc("B", ["A"], estimate=ENG, milestone="M1"), item_doc("C", ["B"], estimate=ENG, milestone="M2")]
        lr = self.dr(items=its, cfg=cfg, milestones=milestones or ["M1", "M2"])
        drv = lr.driver_at("A", begin=False)
        lr.quick_merge("A")
        return lr, drv

    def parked(self, lr):
        return lr.state().header.get("parked")

    def test_per_feature_pauses_after_every_item_per_milestone_only_at_the_boundary_and_unattended_never(self):
        lr, drv = self.merged_a({"autonomy": "per-feature"})
        self.assertTrue(drv.checkpoint_due("A"))
        self.assertEqual(self.parked(lr)["reason"], "checkpoint")
        lr, drv = self.merged_a({"autonomy": "per-milestone"})
        self.assertFalse(drv.checkpoint_due("A"))                                      # B is still in M1
        self.assertIsNone(self.parked(lr))
        lr.quick_merge("B")
        self.assertTrue(drv.checkpoint_due("B"))                                        # M1 is done; C starts M2
        self.assertEqual(self.parked(lr)["item"], "B")

    def test_no_checkpoint_after_the_last_item_and_none_for_a_plain_item_when_unattended(self):
        lr, drv = self.merged_a({"autonomy": "per-feature"}, items=[item_doc("A", estimate=ENG)], milestones=["M1"])
        self.assertFalse(drv.checkpoint_due("A"))                                       # nothing remains: the run just completes

    def test_an_infra_item_pauses_whatever_the_dial_says(self):
        its = [item_doc("A", estimate={"loc": 100, "engineers": ["infra-engineer"]}, touches={"domains": ["infra"], "paths": ["infra/**"], "schema": False, "deps_change": False}),
               item_doc("B", ["A"], estimate=ENG)]
        lr, drv = self.merged_a({"autonomy": "per-milestone"}, items=its, milestones=["M1"])
        self.assertTrue(drv.checkpoint_due("A"))

    def test_the_answer_is_consumed_once_and_stop_ends_the_run(self):
        lr, drv = self.merged_a({"autonomy": "per-feature"})
        drv.checkpoint_due("A")
        st = lr.state()
        self.assertEqual(drv.pending_checkpoint(st), "wait")
        lr.ctl("approve", "checkpoint", "A", "--by", "Pat")
        self.assertEqual(drv.pending_checkpoint(lr.state()), "go")
        self.assertIsNone(self.parked(lr))
        self.assertIsNone(drv.pending_checkpoint(lr.state()))                           # consumed: no longer pending
        drv.checkpoint_due("A")
        lr.ctl("approve", "checkpoint", "A", "--by", "Pat", "--choice", "stop")
        self.assertEqual(drv.pending_checkpoint(lr.state()), "stop")
        drv.checkpoint_due("A")
        self.assertEqual(drv.pending_checkpoint(lr.state()), "wait")                    # the earlier answers were used: a new pause needs a new one


if __name__ == "__main__":
    unittest.main()


class StoreExtras(DriverTestCase):
    """The two small additions to 4a's state module that the driver needs (Loop.record_gates, the parsed-commit cache)."""

    def test_record_gates_is_one_write_with_the_same_slim_result_as_record_gate(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        seq0 = lr.state().seq
        res = {"status": "pass", "mode": "on", "blocking": False, "head": "a" * 40, "failure_class": "none", "findings": [],
               "evidence": {"ac_results": {"AC-A-1": {"result": "pass", "evidence": "t"}, "AC-ZZ-9": {"result": "pass", "evidence": "x"}}, "review": {"code": "approved"}}}
        slims = loop.record_gates("A", [("g6", res), ("g7", dict(res, status="fail", blocking=True))], lr.session)
        self.assertEqual(lr.state().seq, seq0 + 1)                                           # a round is one compare-and-swap
        self.assertEqual([s["status"] for s in slims], ["pass", "fail"])
        it = lr.item("A")
        self.assertEqual(set(it["gates"]), {"g6", "g7"})
        self.assertEqual(set(it["ac"]), {"AC-A-1"})                                          # a criterion of another item is not folded in
        self.assertEqual(it["review"]["code"], "approved")
        one = loop.record_gate("A", "g9", res, lr.session)
        again = loop.record_gates("A", [("g9", res)], lr.session)[0]
        self.assertEqual({k: v for k, v in one.items() if k != "at"}, {k: v for k, v in again.items() if k != "at"})
        for bad in ("G6", "6g", "x" * 40, "a b", ""):
            with self.assertRaises(Exception):
                loop.record_gates("A", [(bad, res)], lr.session)
        with self.assertRaises(Exception):
            loop.record_gates("A", [("g6", res)], "not-the-lock-holder")                      # and only the lock holder writes

    def test_the_parsed_state_cache_is_by_commit_and_hands_out_copies(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        loop = lr.fast_start()
        store = loop.store
        a, sha = store.read()
        a.items["A"]["status"] = "merged"                                                    # a caller scribbling on what it was given ...
        a.header["breaker"] = 99
        b, sha2 = store.read()
        self.assertEqual(sha, sha2)
        self.assertEqual((b.items["A"]["status"], b.header["breaker"]), ("pending", 0))      # ... does not reach the next reader
        lr.loop(fresh=True).approve("cost", "cost", "Pat")                                   # a write by someone else moves the ref ...
        c, sha3 = store.read()
        self.assertNotEqual(sha, sha3)
        self.assertEqual(len(c.d["approvals"]), len(b.d["approvals"]) + 1)                   # ... and the next read sees it
        d, _ = store.update(lambda st: st.header.update(breaker=1), "x")
        e, sha4 = store.read()
        self.assertEqual(e.header["breaker"], 1)
        self.assertEqual(sha4, store.head())
