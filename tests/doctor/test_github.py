"""B1 / B4: rulesets, bypass actors, plan features, squash setting, bot identity (RC-9, RC-11, RC-2, DEC-40, DEC-41)."""
from __future__ import annotations

import json
import unittest

from doctorlib import github, levelcalc

from . import ghfixtures as gf
from .helpers import BoundaryCase, run_doctor


class GhCase(BoundaryCase):
    GROUPS = {"github"}

    def gh_ctx(self, fx, config=None, **kw):
        repo = self.kit_repo(config=config)
        return self.ctx(repo, ghapi=github.fixture_ghapi(fx) if fx is not None else None, **kw)

    def run_gh(self, fx, config=None, **kw):
        ctx = self.gh_ctx(fx, config, **kw)
        return ctx, self.results(ctx)


class NoData(GhCase):
    def test_everything_is_unknown_and_unknown_means_unavailable(self):
        ctx, res = self.run_gh(None)
        for rid in ("gh.main-protection", "gh.codeowner-review", "gh.required-checks", "gh.bypass"):
            self.assertEqual(self.by_id(res, rid)[0].status, "unknown", rid)
        cap = levelcalc.cap_platform(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, 1)
        self.assertIn("unknown means unavailable", cap.reason)

    def test_human_attestation_of_features_is_accepted(self):
        cfg = {"platform_attested": ["rulesets", "code_owner_review", "required_status_checks", "bot_no_bypass"]}
        ctx, res = self.run_gh(None, cfg)
        for rid in ("gh.main-protection", "gh.codeowner-review", "gh.required-checks", "gh.bypass"):
            self.assertEqual(self.by_id(res, rid)[0].status, "pass", rid)
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(res)).level, 5)

    def test_non_github_origin_caps_at_l1(self):
        repo = self.kit_repo(origin="https://gitlab.com/acme/widgets.git", config={"platform_attested": ["rulesets", "code_owner_review"]})
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(self.by_id(res, "gh.forge")[0].status, "fail")
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(res)).level, 1)

    def test_no_origin_at_all(self):
        repo = self.kit_repo(origin="")
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "gh.forge")[0].status, "fail")

    def test_origin_url_forms(self):
        for url, ok in (("git@github.com:acme/widgets.git", True), ("ssh://git@github.com/acme/widgets.git", True), ("https://github.com/acme/widgets", True),
                        ("https://x:tok@github.com/acme/widgets.git", True), ("https://github.example.com/acme/widgets.git", False), ("git@gitlab.com:a/b.git", False)):
            with self.subTest(url=url):
                self.assertEqual(self.ctx(self.kit_repo(origin=url)).fact("origin")["is_github"], ok)

    def test_origin_url_credentials_are_stripped_from_the_stored_url(self):
        o = self.ctx(self.kit_repo(origin="https://x:tok123456@github.com/acme/widgets.git")).fact("origin")
        self.assertNotIn("tok123456", o["url"])


class Protection(GhCase):
    def test_good_team_setup_passes_everything(self):
        ctx, res = self.run_gh(gf.build())
        for rid in ("gh.main-protection", "gh.required-review", "gh.last-push-approval", "gh.codeowner-review", "gh.required-checks", "gh.bypass", "gh.actions-permissions", "gh.squash-setting"):
            self.assertClean(res, rid)
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(res)).level, 5)

    def test_summaries_in_the_list_are_followed_to_the_detail_endpoint(self):
        ctx, res = self.run_gh(gf.build(list_summaries_only=True))
        self.assertClean(res, "gh.main-protection")
        self.assertClean(res, "gh.bypass")

    def test_no_ruleset_and_no_protection(self):
        ctx, res = self.run_gh(gf.build(rulesets=[]))
        hit = self.assertFails(res, "gh.main-protection")
        self.assertIn("requires a pull request", hit[0].message)
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(res)).level, 1)

    def test_ruleset_not_active_or_for_other_branches_does_not_count(self):
        for rs in (gf.ruleset(enforcement="evaluate"), gf.ruleset(enforcement="disabled"), gf.ruleset(include=("refs/heads/release/**",)),
                   gf.ruleset(target="tag"), gf.ruleset(exclude=("~DEFAULT_BRANCH",))):
            with self.subTest(rs=(rs["enforcement"], rs["target"], rs["conditions"]["ref_name"])):
                ctx, res = self.run_gh(gf.build(rulesets=[rs]))
                self.assertFails(res, "gh.main-protection")

    def test_include_patterns_that_match_main(self):
        for inc in (("refs/heads/main",), ("refs/heads/ma*",), ("~ALL",), ("refs/heads/**",)):
            with self.subTest(include=inc):
                ctx, res = self.run_gh(gf.build(rulesets=[gf.ruleset(include=inc)]))
                self.assertClean(res, "gh.main-protection")

    def test_rulesets_unavailable_on_the_plan(self):
        fx = gf.build(rulesets_status={"__status": 403, "message": "Upgrade to GitHub Pro or make this repository public to enable this feature."})
        ctx, res = self.run_gh(fx)
        hit = self.assertFails(res, "gh.main-protection")
        self.assertIn("Upgrade", hit[0].message)
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(res)).level, 1)

    def test_classic_branch_protection_is_read_too(self):
        prot = {"required_pull_request_reviews": {"required_approving_review_count": 2, "require_code_owner_reviews": True, "require_last_push_approval": True},
                "required_status_checks": {"contexts": ["verify", "evidence", "gitleaks"]}, "enforce_admins": {"enabled": True},
                "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}}
        ctx, res = self.run_gh(gf.build(rulesets=[], protection=prot))
        for rid in ("gh.main-protection", "gh.codeowner-review", "gh.required-checks", "gh.bypass"):
            self.assertClean(res, rid)

    def test_classic_protection_without_enforce_admins(self):
        prot = {"required_pull_request_reviews": {"required_approving_review_count": 1}, "enforce_admins": {"enabled": False}}
        ctx, res = self.run_gh(gf.build(rulesets=[], protection=prot))
        hit = self.assertFails(res, "gh.bypass")
        self.assertEqual(hit[0].severity, "error")
        ctx, res = self.run_gh(gf.build(rulesets=[], protection=prot), {"solo_maintainer": True})
        self.assertEqual(self.assertFails(res, "gh.bypass")[0].severity, "warn")

    def test_classic_protection_restricting_pushes_to_the_bot(self):
        prot = {"required_pull_request_reviews": {"required_approving_review_count": 1}, "enforce_admins": {"enabled": True},
                "restrictions": {"users": [{"id": 777, "login": "acme-bot"}], "teams": [], "apps": []}}
        ctx, res = self.run_gh(gf.build(rulesets=[], protection=prot), {"bot": {"kind": "machine-user-pat", "login": "acme-bot", "id": "777"}})
        self.assertIn("lets the bot push", self.assertFails(res, "gh.bypass")[0].message)

    def test_missing_pieces_are_reported_individually(self):
        rs = gf.ruleset(rules=[pr for pr in [gf.pr_rule(approvals=0, code_owner=False, last_push=False)]])
        ctx, res = self.run_gh(gf.build(rulesets=[rs]))
        self.assertFails(res, "gh.required-review")
        self.assertFails(res, "gh.codeowner-review")
        self.assertFails(res, "gh.last-push-approval")
        self.assertFails(res, "gh.force-push")
        cap = levelcalc.cap_platform(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, 2)

    def test_required_checks_must_include_verify_evidence_and_a_secrets_scan(self):
        for contexts, missing in ((("verify",), "evidence"), (("verify", "evidence"), "secrets scan"), (("lint",), "verify")):
            with self.subTest(contexts=contexts):
                ctx, res = self.run_gh(gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(), gf.checks_rule(contexts)])]))
                self.assertIn(missing, self.assertFails(res, "gh.required-checks")[0].message)

    def test_required_checks_without_a_pinned_app_are_forgeable(self):
        ctx, res = self.run_gh(gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(), gf.checks_rule(integration_id=None)])]))
        hit = self.assertFails(res, "gh.required-checks-source")
        self.assertEqual(hit[0].severity, "warn")
        self.assertIn("checks: write", hit[0].message)


class BypassActors(GhCase):
    """RC-9 / DEC-41: the bot never bypasses; a solo human admin only in pull_request mode; teams none."""

    def bypass(self, actors, config=None):
        ctx, res = self.run_gh(gf.build(rulesets=[gf.ruleset(bypass=actors)]), config)
        return res

    def test_team_with_no_bypass_is_clean(self):
        self.assertClean(self.bypass([]), "gh.bypass")

    def test_solo_admin_in_pull_request_mode_is_the_only_allowed_bypass(self):
        res = self.bypass([gf.ADMIN_PR_MODE], {"solo_maintainer": True})
        self.assertClean(res, "gh.bypass")
        self.assertIn("pull_request mode", self.by_id(res, "gh.bypass")[0].message)

    def test_solo_admin_in_always_mode_is_an_error(self):
        hit = self.assertFails(self.bypass([gf.ADMIN_ALWAYS], {"solo_maintainer": True}), "gh.bypass")
        self.assertIn("pull_request mode", hit[0].message)

    def test_a_team_install_with_the_solo_bypass_is_an_error(self):
        hit = self.assertFails(self.bypass([gf.ADMIN_PR_MODE]), "gh.bypass")
        self.assertIn("teams use no bypass", hit[0].message)

    def test_any_app_as_bypass_actor(self):
        hit = self.assertFails(self.bypass([{"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}], {"solo_maintainer": True}), "gh.bypass")
        self.assertIn("automation must never bypass", hit[0].message)

    def test_the_bot_app_by_id_is_named(self):
        cfg = {"bot": {"kind": "github-app", "login": "acme-bot[bot]", "id": "123"}, "solo_maintainer": True}
        hit = self.assertFails(self.bypass([{"actor_id": 123, "actor_type": "Integration", "bypass_mode": "pull_request"}], cfg), "gh.bypass")
        self.assertIn("the bot", hit[0].message)

    def test_the_bot_machine_user_by_id(self):
        cfg = {"bot": {"kind": "machine-user-pat", "login": "acme-bot", "id": "555"}, "solo_maintainer": True}
        hit = self.assertFails(self.bypass([{"actor_id": 555, "actor_type": "User", "bypass_mode": "pull_request"}], cfg), "gh.bypass")
        self.assertIn("the bot", hit[0].message)

    def test_write_and_maintain_roles_and_teams_and_deploy_keys(self):
        for actor in ({"actor_id": 4, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}, {"actor_id": 2, "actor_type": "RepositoryRole", "bypass_mode": "always"},
                      {"actor_id": 9, "actor_type": "Team", "bypass_mode": "pull_request"}, {"actor_id": 1, "actor_type": "DeployKey", "bypass_mode": "always"}):
            with self.subTest(actor=actor):
                self.assertFails(self.bypass([actor], {"solo_maintainer": True}), "gh.bypass")

    def test_exempt_mode_counts_as_always(self):
        a = dict(gf.ADMIN_PR_MODE, bypass_mode="exempt")
        self.assertFails(self.bypass([a], {"solo_maintainer": True}), "gh.bypass")

    def test_boundary_fixture_the_bot_is_not_in_any_bypass_list(self):
        """Report §15 item 9 'Bypass list': on a solo install the only bypass is the human admin in pull_request mode."""
        good = self.bypass([gf.ADMIN_PR_MODE], {"solo_maintainer": True, "bot": {"kind": "github-app", "login": "acme-bot[bot]", "id": "123"}})
        self.assertClean(good, "gh.bypass")
        bad = self.bypass([gf.ADMIN_PR_MODE, {"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}],
                          {"solo_maintainer": True, "bot": {"kind": "github-app", "login": "acme-bot[bot]", "id": "123"}})
        self.assertFails(bad, "gh.bypass")

    def test_bypass_on_a_ruleset_that_does_not_cover_main_is_ignored(self):
        ctx, res = self.run_gh(gf.build(rulesets=[gf.ruleset(), gf.ruleset(rid=2, name="other", include=("refs/heads/docs/**",), bypass=[gf.ADMIN_ALWAYS])]))
        self.assertClean(res, "gh.bypass")


class EnvironmentsAndSettings(GhCase):
    def test_environment_with_reviewers_and_self_review_prevented(self):
        ctx, res = self.run_gh(gf.build(environments={"environments": [gf.env_with_reviewers()]}))
        self.assertClean(res, "gh.environments")
        self.assertTrue(levelcalc.env_reviewers_available(ctx))

    def test_environment_allowing_self_review(self):
        ctx, res = self.run_gh(gf.build(environments={"environments": [gf.env_with_reviewers(self_review_blocked=False)]}))
        self.assertIn("self-review", self.assertFails(res, "gh.environments")[0].message)

    def test_no_environment_reviewers_means_human_dispatch(self):
        ctx, res = self.run_gh(gf.build())
        hit = self.assertFails(res, "gh.environments")
        self.assertIn("human dispatch", hit[0].message)
        self.assertFalse(levelcalc.env_reviewers_available(ctx))

    def test_iac_without_environment_reviewers_caps_at_l3(self):
        repo = self.kit_repo(files={"infra/main.tf": ""})
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(gf.build()))
        res = self.results(ctx)
        cap = levelcalc.cap_platform(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, 3)
        self.assertIn("apply gate = human dispatch", cap.reason)

    def test_iac_with_environment_reviewers_is_not_capped_here(self):
        repo = self.kit_repo(files={"infra/main.tf": ""})
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(gf.build(environments={"environments": [gf.env_with_reviewers()]})))
        self.assertEqual(levelcalc.cap_platform(ctx, levelcalc.R(self.results(ctx))).level, 5)

    def test_actions_permissions(self):
        ctx, res = self.run_gh(gf.build(actions_perms={"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}))
        msg = self.assertFails(res, "gh.actions-permissions")[0].message
        self.assertIn("not read-only", msg)
        self.assertIn("approve pull requests", msg)

    def test_squash_setting(self):
        ctx, res = self.run_gh(gf.build(info=gf.repo_info(squash_ok=False)))
        self.assertIn("audit block", self.assertFails(res, "gh.squash-setting")[0].message)
        info = gf.repo_info(squash_ok=False)
        info["allow_squash_merge"] = False
        ctx, res = self.run_gh(gf.build(info=info))
        self.assertClean(res, "gh.squash-setting")

    def test_visibility_and_plan(self):
        fx = gf.build(info=gf.repo_info(private=True, owner_type="Organization"))
        fx["orgs/acme"] = {"plan": {"name": "team"}}
        ctx, res = self.run_gh(fx)
        r = self.by_id(res, "gh.visibility")[0]
        self.assertEqual(r.detail, {"visibility": "private", "owner_type": "Organization", "plan": "team"})

    def test_bot_permissions_from_the_installation(self):
        good = {"permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"}}
        ctx, res = self.run_gh(gf.build(installation=good), {"bot": {"kind": "github-app", "login": "acme-bot[bot]"}})
        self.assertClean(res, "gh.bot-permissions")
        bad = {"permissions": {"contents": "write", "pull_requests": "write", "workflows": "write", "actions": "read", "administration": "write",
                               "environments": "write", "secrets": "read", "checks": "write", "statuses": "write", "deployments": "write"}}
        ctx, res = self.run_gh(gf.build(installation=bad), {"bot": {"kind": "github-app", "login": "acme-bot[bot]"}})
        msgs = " ".join(r.message for r in self.assertFails(res, "gh.bot-permissions"))
        for name in ("workflows", "actions", "administration", "environments", "secrets", "checks", "statuses", "deployments"):
            self.assertIn("has %s:" % name, msgs)

    def test_bot_missing_write_scopes(self):
        ctx, res = self.run_gh(gf.build(installation={"permissions": {"contents": "read"}}), {"bot": {"kind": "github-app", "login": "x[bot]"}})
        self.assertEqual(len(self.assertFails(res, "gh.bot-permissions")), 2)

    def test_app_bot_without_readable_installation_is_unknown(self):
        ctx, res = self.run_gh(gf.build(), {"bot": {"kind": "github-app", "login": "acme-bot[bot]"}})
        self.assertEqual(self.by_id(res, "gh.bot-permissions")[0].status, "unknown")


class BotIdentity(GhCase):
    def ident(self, bot, now=1792000000.0):
        ctx = self.ctx(self.kit_repo(config={"bot": bot}), now=now)
        return self.by_id(self.results(ctx), "bot.identity")

    def test_none_is_unknown(self):
        r = self.ident({"kind": "none"})
        self.assertEqual(r[0].status, "unknown")

    def test_github_app_ok_and_human_looking_login_warns(self):
        self.assertEqual([x.status for x in self.ident({"kind": "github-app", "login": "acme-bot[bot]"})], ["pass"])
        r = self.ident({"kind": "github-app", "login": "some-human"})
        self.assertEqual([x.status for x in r], ["fail", "pass"])

    def test_machine_user_pat_expiry_rules(self):
        import calendar
        import time
        now = calendar.timegm(time.strptime("2026-10-26", "%Y-%m-%d")) + 3600.0
        self.assertEqual(self.ident({"kind": "machine-user-pat", "login": "acme-bot", "token_expires": "2026-11-03"}, now)[0].status, "pass")
        too_long = self.ident({"kind": "machine-user-pat", "login": "acme-bot", "token_expires": "2027-01-01"}, now)[0]
        self.assertEqual(too_long.status, "fail")
        self.assertIn("limit is 14 days", too_long.message)
        expired = self.ident({"kind": "machine-user-pat", "login": "acme-bot", "token_expires": "2026-10-20"}, now)[0]
        self.assertIn("expired", expired.message)
        missing = self.ident({"kind": "machine-user-pat", "login": "acme-bot"}, now)[0]
        self.assertEqual(missing.status, "fail")
        soon = self.ident({"kind": "machine-user-pat", "login": "acme-bot", "token_expires": "2026-10-27"}, now)[0]
        self.assertEqual((soon.status, soon.severity), ("fail", "warn"))

    def test_a_pat_is_never_enough_for_l3(self):
        cfg = {"bot": {"kind": "machine-user-pat", "login": "acme-bot", "token_expires": "2026-11-03"}, "autonomy_level": "L3"}
        ctx = self.ctx(self.kit_repo(config=cfg), now=1792000000.0)
        prereq = levelcalc.prerequisites(ctx, levelcalc.R(self.results(ctx)))
        l3 = {name: ok for name, ok, why in prereq[3]}
        self.assertFalse(l3["B1 bot is a GitHub App"])


class Cli(GhCase):
    def test_gh_fixture_flag_round_trip(self):
        import os
        repo = self.kit_repo()
        path = os.path.join(self.tmpdir(), "fx.json")
        with open(path, "w") as fh:
            json.dump(gf.build(), fh)
        rc, out, err = run_doctor(["--json", "--gh-fixture", path, "--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"], self.fake_home.env())
        doc = json.loads(out)
        main = [c for c in doc["checks"] if c["id"] == "gh.main-protection"][0]
        self.assertEqual(main["status"], "pass")


if __name__ == "__main__":
    unittest.main()
