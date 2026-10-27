"""Reviews: the reviewer's verdict (missing, changes requested, approved), model escalation on a later round, and a security block
(report 9 G7, 11.4; FI-19).
"""
from __future__ import annotations

import unittest

from ... import SCRIPTS_DIR  # noqa: F401

import json

from loopctl import driver

from . import DriverTestCase, item_doc

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


class Reviews(DriverTestCase):
    def test_a_review_without_a_verdict_then_changes_requested_then_approval(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy(by_role={
            "backend-engineer": [lr.eng_run("A"), lr.eng_run("A", iteration=2), lr.eng_run("A", iteration=3)],
            "code-reviewer": [{"behavior": "succeed", "total_cost_usd": 0.01, "result": "Looks fine to me!"}, lr.review_run("CHANGES_REQUESTED"), lr.review_run()]}))
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        it = lr.item("A")
        self.assertEqual((it["status"], it["counters"]["review"], it["lastFailure"]["reason"]), ("merged", 2, "review_blocked"))
        eng, rev = lr.calls_for("backend-engineer"), lr.calls_for("code-reviewer")
        self.assertEqual((len(eng), len(rev)), (3, 3))
        self.assertIn("docs/features/slice-a/reviews/code-reviewer-r2.md", eng[2]["stdin"])      # the engineer is pointed at the review that blocked
        self.assertIn("Review round 1", rev[0]["stdin"])
        self.assertIn("Review round 3", rev[2]["stdin"])
        files = lr.repo.git("ls-tree", "-r", "--name-only", "main").splitlines()
        for n in (1, 2, 3):
            self.assertIn("docs/features/slice-a/reviews/code-reviewer-r%d.md" % n, files)
        # round 3 of an engineer retry runs on opus at xhigh whatever the profile says (model-profiles.md)
        agents = [json.loads(c["argv"][c["argv"].index("--agents") + 1])["backend-engineer"] for c in eng]
        self.assertEqual([(a.get("model"), a.get("effort")) for a in agents], [("opus", "high"), ("opus", "xhigh"), ("opus", "xhigh")])

    def test_the_security_reviewer_runs_on_sensitive_items_and_a_block_parks_the_item(self):
        lr = self.dr(items=[item_doc("A", estimate=ENG, intent="Add password reset with a token emailed to the user.")], cfg={"security_review": "sensitive"})
        lr.set_scenario(lr.happy(by_role={"code-reviewer": [lr.review_run()], "security-reviewer": [lr.review_run("SECURITY_BLOCKED", kind="Security Review")]}))
        drv = lr.driver_at("A", "design_review", "building", "verifying", "reviewing")
        drv.write_briefs("A")
        lr.repo.commit_file("src/a.py", "x = 1\n", "the work")
        with self.assertRaises(driver._StageDone):
            drv.stage_reviewing("A")
        it = lr.item("A")
        self.assertEqual((it["status"], it["blockReason"], it["review"]["security"], it["review"]["code"]), ("needs_human", "security_blocked", "blocked", "approved"))
        self.assertEqual([c["role"] for c in lr.calls()], ["code-reviewer", "security-reviewer"])
        self.assertEqual(lr.branch_names(), ["blocked/slice-a-a1", "main"])
        self.assertIn("SECURITY_APPROVED or SECURITY_BLOCKED", lr.calls_for("security-reviewer")[0]["stdin"])
        self.assertEqual(lr.state().header["runStatus"], "parked")
        # a critic gets the critic profile: no Edit, no Write, no installs
        sec = lr.calls_for("security-reviewer")[0]
        a = sec["argv"]
        allowed = a[a.index("--allowedTools") + 1:a.index("--permission-mode")]
        self.assertNotIn("Edit", allowed)
        self.assertNotIn("Write", allowed)

    def test_which_reviewers_run(self):
        plain, risky = item_doc("A", estimate=ENG), item_doc("A", estimate=ENG, riskClass="high")
        for mode, item, want in (("off", plain, ["code-reviewer"]), ("off", risky, ["code-reviewer"]), ("sensitive", plain, ["code-reviewer"]),
                                 ("sensitive", risky, ["code-reviewer", "security-reviewer"]), ("every_item", plain, ["code-reviewer", "security-reviewer"])):
            with self.subTest(mode=mode, risk=item.get("riskClass")):
                lr = self.dr(items=[item], cfg={"security_review": mode})
                drv = lr.driver_at("A", begin=False)
                self.assertEqual(drv.review_roles(item), want)


if __name__ == "__main__":
    unittest.main()
