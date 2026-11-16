"""DEC-1: `delivery.suggest-draft-pr`, an information row that appears once the ruleset checks pass and `delivery` is `local` or
`push-branch`; and `delivery.push-tier`, which reports `git_autonomy` and `delivery` at the base sha."""
from __future__ import annotations

import unittest

from doctorlib import github
from doctorlib.model import is_known_id

from tests.doctor import ghfixtures as gf
from tests.doctor.helpers import BoundaryCase, config_text

ID = "delivery.suggest-draft-pr"


class SuggestDraftPr(BoundaryCase):
    def row(self, config=None, fx=None, no_config=False):
        repo = self.kit_repo(config=config)
        if no_config:
            import os
            os.unlink(os.path.join(repo.path, ".claude", "loop.config.md"))
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(fx) if fx is not None else None)
        rows = self.check_one(ctx, ID)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_check_is_registered_and_discovered(self):
        from doctorlib.model import CHECKS
        self.assertIn(ID, [c["id"] for c in CHECKS])
        self.assertTrue(is_known_id(ID))

    def test_it_suggests_draft_pr_when_the_rulesets_are_sound_and_delivery_is_local(self):
        r = self.row(fx=gf.build())
        self.assertEqual((r.status, r.severity), ("pass", "info"))
        self.assertIn("delivery: draft-pr", r.message)
        self.assertIn("never pushes", r.message)
        self.assertNotIn("default stays", r.message)

    def test_it_also_suggests_draft_pr_at_push_branch(self):
        r = self.row(config={"delivery": "push-branch", "git_autonomy": "branch+commit+push"}, fx=gf.build())
        self.assertEqual((r.status, r.severity), ("pass", "info"))
        self.assertIn("delivery: draft-pr", r.message)
        self.assertIn("push-branch", r.message)

    def test_nothing_is_suggested_when_a_ruleset_check_fails(self):
        for rules in ([gf.pr_rule(approvals=0), gf.checks_rule()], [gf.pr_rule(code_owner=False), gf.checks_rule()], [gf.pr_rule()]):
            with self.subTest(rules=len(rules)):
                r = self.row(fx=gf.build(rulesets=[gf.ruleset(rules=rules)]))
                self.assertEqual(r.status, "skipped")
                self.assertIn("stays local", r.message)
        r = self.row(config={"delivery": "push-branch"}, fx=gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(approvals=0)])]))
        self.assertEqual(r.status, "skipped")
        self.assertIn("stays push-branch", r.message)

    def test_a_bypass_for_the_bot_stops_the_suggestion(self):
        bot = {"actor_id": 99, "actor_type": "Integration", "bypass_mode": "always"}
        r = self.row(config={"bot": {"kind": "github-app", "login": "acme-bot[bot]", "id": "99"}}, fx=gf.build(rulesets=[gf.ruleset(bypass=[bot])]))
        self.assertEqual(r.status, "skipped")

    def test_no_github_data_is_never_a_suggestion(self):
        self.assertEqual(self.row(fx=None).status, "skipped")

    def test_it_is_silent_when_delivery_is_already_chosen(self):
        for value in ("draft-pr", "pr-automerge"):
            with self.subTest(value):
                r = self.row(config={"delivery": value}, fx=gf.build())
                self.assertEqual(r.status, "skipped")
                self.assertIn(value, r.message)

    def test_no_config_is_skipped(self):
        self.assertEqual(self.row(fx=gf.build(), no_config=True).status, "skipped")

    def test_it_never_fails_and_never_asks_for_anything(self):
        for fx in (gf.build(), None, gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(approvals=0)])])):
            r = self.row(fx=fx)
            self.assertNotEqual(r.status, "fail")
            self.assertEqual(r.remediation, "")


PUSH = {"git_autonomy": "branch+commit+push", "delivery": "push-branch"}


class PushTier(BoundaryCase):
    ID = "delivery.push-tier"

    def row(self, repo):
        rows = self.check_one(self.ctx(repo), self.ID)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_check_is_registered(self):
        self.assertTrue(is_known_id(self.ID))

    def test_below_the_push_tier_it_is_information(self):
        for config in (None, {"git_autonomy": "branch+commit", "delivery": "draft-pr"}, {"git_autonomy": "branch+commit+push", "delivery": "local"}):
            with self.subTest(config=config):
                r = self.row(self.kit_repo(config=config, commit=True))
                self.assertEqual((r.status, r.severity), ("pass", "info"))
                self.assertIn("no agent pushes", r.message)

    def test_the_push_tier_without_a_bot_identity_warns(self):
        r = self.row(self.kit_repo(config=PUSH, commit=True))
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn("git_autonomy branch+commit+push, delivery push-branch", r.message)
        self.assertIn("bot.kind is none", r.message)
        self.assertIn("bot-identity.md", r.remediation)

    def test_the_push_tier_with_a_bot_identity_is_information(self):
        config = dict(PUSH, delivery="draft-pr", bot={"kind": "github-app", "login": "acme-bot[bot]", "id": "99"})
        r = self.row(self.kit_repo(config=config, commit=True))
        self.assertEqual((r.status, r.severity), ("pass", "info"))
        self.assertIn("opens the draft PR", r.message)
        self.assertIn("github-app", r.message)

    def test_it_reads_the_base_sha_not_the_working_tree(self):
        # the push tier set on a feature branch does not count until it is merged
        repo = self.kit_repo(commit=True)
        repo.git("switch", "-q", "-c", "feat/x")
        repo.write(".claude/loop.config.md", config_text(PUSH))
        repo.git("commit", "-q", "-am", "push tier")
        r = self.row(repo)
        self.assertEqual((r.status, r.severity), ("pass", "info"))
        self.assertIn("git_autonomy none, delivery local", r.message)
        self.assertIn("the working tree says git_autonomy branch+commit+push, delivery push-branch", r.message)

    def test_no_base_sha_is_skipped(self):
        r = self.row(self.kit_repo(config=PUSH))
        self.assertEqual(r.status, "skipped")
        self.assertIn("no agent pushes", r.message)


if __name__ == "__main__":
    unittest.main()
