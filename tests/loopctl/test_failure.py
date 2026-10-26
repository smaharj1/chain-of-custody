"""failure.py and Loop.fail: the taxonomy, the caps, discard and the second discard (report section 10.4, RC-19, L-8)."""
from __future__ import annotations

import os
import unittest

from loopctl import common, failure, branches
from loopctl.common import Refused, UsageError

from . import LoopTestCase, item_doc


class Cfg(object):
    def __init__(self, **kw):
        self.v = {"max_retries_per_item": 2, "max_design_rounds": 2, "max_ci_fix_attempts": 2, "max_transient_retries": 2,
                  "max_platform_retries": 3, "platform_backoff_base_s": 30}
        self.v.update(kw)

    def get(self, name, default=None):
        return self.v.get(name, default)


def rec(**kw):
    r = {"attempt": 1, "counters": {}, "lastFailure": None}
    r.update(kw)
    return r


class Decide(unittest.TestCase):
    def test_a_known_reason_keeps_its_class(self):
        for reason, cls in failure.KNOWN_REASONS.items():
            for other in failure.CLASSES:
                if other == cls:
                    continue
                with self.subTest(reason=reason, other=other):
                    with self.assertRaises(Refused):
                        failure.decide(rec(), "verifying", Cfg(), other, reason)

    def test_an_unknown_reason_is_accepted_with_its_class_and_a_bad_one_is_not(self):
        d = failure.decide(rec(), "verifying", Cfg(), "platform", "http_503_from_proxy")
        self.assertEqual(d.action, "retry")
        for bad in ("", "A", "Has Space", "x" * 50, "semi;colon", "../x"):
            with self.assertRaises(UsageError):
                failure.check_reason("item", bad)
        with self.assertRaises(UsageError):
            failure.check_reason("magic", "tests_failed")

    def test_platform_retries_with_backoff_and_uses_no_budget_until_the_outage_cap(self):
        r = rec()
        seen = []
        for n in range(1, 4):
            d = failure.decide(r, "building", Cfg(), "platform", "overloaded", seed="A:1")
            self.assertEqual((d.action, d.consumes, d.breaker), ("retry", False, False))
            self.assertGreater(d.backoff_s, 0)
            seen.append(d.backoff_s)
            r["counters"] = d.counters
        self.assertEqual(r["counters"]["platform"], 3)
        self.assertEqual(r["counters"].get("verify", 0), 0)
        self.assertTrue(all(b <= 900 for b in seen))
        d = failure.decide(r, "building", Cfg(), "platform", "overloaded")
        self.assertEqual((d.action, d.status_kind, d.block_reason), ("park", "needs_human", "platform_outage"))

    def test_backoff_grows_has_jitter_and_is_deterministic(self):
        a = [failure.backoff_s(30, n, "A:1") for n in range(1, 7)]
        self.assertEqual(a, [failure.backoff_s(30, n, "A:1") for n in range(1, 7)])
        self.assertNotEqual(a, [failure.backoff_s(30, n, "B:1") for n in range(1, 7)])
        for n, v in enumerate(a, 1):
            step = min(900, 30 * 2 ** (n - 1))
            self.assertTrue(step * 0.5 <= v <= step, (n, v, step))

    def test_env_failures_park_demote_and_do_not_count(self):
        d = failure.decide(rec(), "verifying", Cfg(), "env", "db_down")
        self.assertEqual((d.action, d.status_kind, d.demote, d.consumes, d.breaker), ("park", "blocked", True, False, False))
        self.assertEqual(d.block_reason, "env:db_down")
        self.assertEqual(d.counters.get("verify", 0), 0)

    def test_transient_failures_retry_twice_then_become_item_failures(self):
        r = rec()
        for n in (1, 2):
            d = failure.decide(r, "verifying", Cfg(), "transient", "flaky_test")
            self.assertEqual((d.action, d.consumes), ("retry", True))
            r["counters"] = d.counters
        self.assertEqual((r["counters"]["transient"], r["counters"]["verify"]), (2, 2))
        d = failure.decide(r, "verifying", Cfg(), "transient", "flaky_test")
        self.assertEqual(d.action, "discard")            # third: treated as an item failure, and the verify cap is spent too

    def test_item_failures_use_their_own_counter_per_loop(self):
        r = rec()
        d = failure.decide(r, "verifying", Cfg(), "item", "tests_failed", ["AC-A-1"])
        r["counters"] = d.counters
        d = failure.decide(r, "reviewing", Cfg(), "item", "review_blocked")
        r["counters"] = d.counters
        d = failure.decide(r, "building", Cfg(), "item", "partial", stage="partial")
        r["counters"] = d.counters
        self.assertEqual({k: r["counters"][k] for k in ("verify", "review", "partial")}, {"verify": 1, "review": 1, "partial": 1})
        self.assertEqual(d.action, "retry")               # three loops, each under its own cap: not multiplicative (L-8)

    def test_exhaustion_discards_once_then_hard_stops_and_trips_the_breaker(self):
        r = rec(counters={"verify": 2})
        d = failure.decide(r, "verifying", Cfg(), "item", "tests_failed", ["AC-A-1"])
        self.assertEqual((d.action, d.new_attempt, d.to_status, d.breaker), ("discard", True, "pending", False))
        r2 = rec(counters={"verify": 2, "discards": 1}, attempt=2)
        d = failure.decide(r2, "verifying", Cfg(), "item", "tests_failed", ["AC-A-1"])
        self.assertEqual((d.action, d.block_reason, d.breaker, d.status_kind), ("hard_stop", "retries_exhausted", True, "blocked"))

    def test_no_progress_on_the_same_failing_acs_discards_before_the_cap(self):
        r = rec(lastFailure={"class": "item", "failedAcs": ["AC-A-1", "AC-A-2"]})
        d = failure.decide(r, "verifying", Cfg(max_retries_per_item=5), "item", "tests_failed", ["AC-A-2", "AC-A-1"])
        self.assertTrue(d.no_progress)
        self.assertEqual(d.action, "discard")
        d = failure.decide(r, "verifying", Cfg(max_retries_per_item=5), "item", "tests_failed", ["AC-A-1"])
        self.assertFalse(d.no_progress)
        self.assertEqual(d.action, "retry")

    def test_a_reward_hack_signature_discards_at_once(self):
        d = failure.decide(rec(), "reviewing", Cfg(), "item", "reward_hack")
        self.assertEqual(d.action, "discard")
        d = failure.decide(rec(counters={"discards": 1}), "reviewing", Cfg(), "item", "reward_hack")
        self.assertEqual(d.action, "hard_stop")

    def test_the_critic_cap_and_the_ci_fix_cap_are_hard_stops_without_a_discard(self):
        r = rec()
        for n in (1, 2):
            d = failure.decide(r, "design_review", Cfg(), "item", "design_blocked")
            self.assertEqual((d.action, d.to_status), ("retry", "designing"))
            r["counters"] = d.counters
        d = failure.decide(r, "design_review", Cfg(), "item", "design_blocked")
        self.assertEqual((d.action, d.block_reason, d.breaker), ("hard_stop", "design_cap", False))
        r = rec()
        for n in (1, 2):
            d = failure.decide(r, "pr_open", Cfg(), "item", "ci_failed")
            self.assertEqual((d.action, d.to_status), ("retry", "building"))
            r["counters"] = d.counters
        d = failure.decide(r, "pr_open", Cfg(), "item", "ci_failed")
        self.assertEqual((d.action, d.block_reason), ("hard_stop", "ci_fix_cap"))

    def test_a_retry_goes_back_to_the_right_stage(self):
        for frm, back in (("verifying", "building"), ("reviewing", "building"), ("design_review", "designing"), ("pr_open", "building")):
            d = failure.decide(rec(), frm, Cfg(), "item", "tests_failed")
            self.assertEqual(d.to_status, back, frm)

    def test_classify_exit(self):
        c = failure.classify_exit
        self.assertEqual(c("overloaded_error 529")["failure_class"], "platform")
        self.assertEqual(c(http_status=503)["failure_class"], "platform")
        self.assertEqual(c(http_status=429)["reason"], "rate_limit")
        self.assertEqual(c(timed_out=True), {"failure_class": "transient", "reason": "stall"})
        self.assertEqual(c(signal_name="SIGKILL")["reason"], "cli_crash")
        self.assertEqual(c(exit_code=137)["failure_class"], "platform")
        self.assertEqual(c("OSError: [Errno 98] Address already in use")["reason"], "port_in_use")
        self.assertEqual(c("assertion failed", exit_code=1)["failure_class"], "item")
        for out in (c("x"), c(timed_out=True), c(signal_name="SIGTERM")):
            failure.check_reason(out["failure_class"], out["reason"])


class LoopFail(LoopTestCase):
    def start(self, **kw):
        lr = self.lr(**kw)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        return lr, loop

    def test_a_verify_failure_goes_back_to_building_and_counts(self):
        lr, loop = self.start()
        d = loop.fail("A", "item", "tests_failed", lr.session, failed_acs=["AC-A-1"])
        self.assertEqual(d.action, "retry")
        rec_ = lr.item("A")
        self.assertEqual((rec_["status"], rec_["counters"]["verify"], rec_["lastFailure"]["failedAcs"]), ("building", 1, ["AC-A-1"]))

    def test_an_item_that_is_not_in_progress_cannot_fail(self):
        lr = self.lr()
        loop = lr.fast_start()
        with self.assertRaises(Refused):
            loop.fail("A", "item", "tests_failed", lr.session)

    def test_failed_ac_ids_are_validated(self):
        lr, loop = self.start()
        with self.assertRaises(UsageError):
            loop.fail("A", "item", "tests_failed", lr.session, failed_acs=["not an ac; rm -rf"])

    def test_platform_failures_set_a_back_off_that_begin_respects(self):
        lr, loop = self.start()
        d = loop.fail("A", "platform", "overloaded", lr.session)
        self.assertGreater(d.backoff_s, 0)
        self.assertIsNotNone(lr.item("A")["notBefore"])
        self.assertEqual(lr.item("A")["status"], "verifying")           # stays where it was: the stage is re-run
        self.assertEqual(lr.item("A")["counters"]["verify"], 0)

    def test_discard_renames_the_branch_resets_the_attempt_and_the_second_discard_blocks(self):
        lr, loop = self.start(extra_cfg="max_retries_per_item: 0")
        br = lr.item("A")["branch"]
        lr.repo.git("branch", br, "main")
        d = loop.fail("A", "item", "tests_failed", lr.session)
        self.assertEqual(d.action, "discard")
        self.assertEqual(d.git["dead"], "dead/slice-a-a1")
        self.assertTrue(branches.exists(lr.path, "dead/slice-a-a1"))
        self.assertFalse(branches.exists(lr.path, br))
        rec_ = lr.item("A")
        self.assertEqual((rec_["status"], rec_["attempt"], rec_["branch"], rec_["counters"]["discards"]), ("pending", 2, None, 1))
        loop.begin("A", lr.session)
        self.assertEqual(lr.item("A")["branch"], "feat/slice-a-a2")
        lr.repo.git("branch", "feat/slice-a-a2", "main")
        lr.walk("A", "design_review", "building", "verifying")
        d = loop.fail("A", "item", "tests_failed", lr.session)
        self.assertEqual((d.action, d.block_reason), ("hard_stop", "retries_exhausted"))
        self.assertEqual(d.git["blocked"], "blocked/slice-a-a2")
        self.assertTrue(branches.exists(lr.path, "blocked/slice-a-a2"))
        self.assertEqual(lr.item("A")["parkedBranch"], "blocked/slice-a-a2")

    def test_discard_will_not_touch_a_checkout_that_is_not_the_registered_loop_worktree(self):
        lr, loop = self.start(extra_cfg="max_retries_per_item: 0")
        br = lr.item("A")["branch"]
        lr.repo.git("branch", br, "main")
        rec_ = lr.context().rs.read_run()
        rec_["worktree"] = "/somewhere/else"
        lr.context().rs.write_run(rec_)
        d = loop.fail("A", "item", "tests_failed", lr.session)
        self.assertEqual(d.action, "discard")
        self.assertIsNone(d.git)
        self.assertTrue(branches.exists(lr.path, br))                  # untouched

    def test_discard_resets_a_dirty_item_branch_it_is_standing_on(self):
        lr, loop = self.start(extra_cfg="max_retries_per_item: 0")
        br = lr.item("A")["branch"]
        lr.repo.git("switch", "-q", "-c", br, "main")
        lr.repo.write("scratch.txt", "half done")
        lr.repo.git("add", "scratch.txt")
        d = loop.fail("A", "item", "tests_failed", lr.session)
        self.assertTrue(d.git["reset"])
        self.assertEqual(lr.repo.git("branch", "--show-current"), "main")
        self.assertFalse(os.path.exists(os.path.join(lr.path, "scratch.txt")))

    def test_discard_refuses_a_protected_branch(self):
        lr = self.lr()
        loop = lr.fast_start()
        with self.assertRaises(Refused):
            branches.discard(lr.context(), "main", "main")
        with self.assertRaises(Refused):
            branches.discard(lr.context(), "feat/x-a1", "feat/x-a1")

    def test_env_failure_parks_the_branch_and_caps_the_level(self):
        lr, loop = self.start()
        lr.repo.git("branch", lr.item("A")["branch"], "main")
        d = loop.fail("A", "env", "toolchain_mismatch", lr.session)
        self.assertEqual(lr.item("A")["status"], "blocked")
        self.assertEqual(lr.item("A")["blockReason"], "env:toolchain_mismatch")
        self.assertEqual(d.git["blocked"], "blocked/slice-a-a1")
        self.assertEqual(lr.state().header["levelCap"]["level"], "L1")

    def test_branch_names_are_validated(self):
        for bad in ("feat/x", "feat/X-a1", "dead/../x-a1", "feat/a b-a1", "main", "feat/-a1", "feat/x-a1/y"):
            self.assertIsNone(branches.parse(bad), bad)
        self.assertEqual(branches.parse("blocked/some-slice-a12"), ("blocked", "some-slice", 12))
        with self.assertRaises(UsageError):
            branches.feat_name("Bad Slug", 1)
        with self.assertRaises(UsageError):
            branches.feat_name("ok", 0)


if __name__ == "__main__":
    unittest.main()
