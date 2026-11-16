"""The effective level: min(configured, prerequisites, credentials, sandbox, MCP, B5, platform, evidence, version) (IMP-21).

`ready()` builds a repo and machine where every cap passes, so the result is L5. Each test then breaks exactly one thing and
asserts which cap binds, at which level, and that the reason is printed.
"""
from __future__ import annotations

import json
import os
import unittest

from kitlib import levels, runstate

from doctorlib import github, guardchecks, levelcalc  # noqa: F401  guardchecks adds the guards cap, as doctor.py's import does
from doctorlib.model import run_checks
from guards import wiring

from . import ghfixtures as gf
from .helpers import BoundaryCase, KIT_VERSION_V2, config_text

FILLED_CODEOWNERS = None


def filled_codeowners():
    global FILLED_CODEOWNERS
    if FILLED_CODEOWNERS is None:
        from tests import REPO_ROOT
        with open(os.path.join(REPO_ROOT, "templates", "security", "CODEOWNERS.template"), "r", encoding="utf-8") as fh:
            t = fh.read()
        FILLED_CODEOWNERS = t.replace("@PLACEHOLDER-owner", "@alice").replace("@PLACEHOLDER-guardrail-owners", "@alice").replace("@PLACEHOLDER-infra-owners", "@alice") \
            .replace("@PLACEHOLDER-data-owners", "@alice").replace("@PLACEHOLDER-security-owners", "@alice")
    return FILLED_CODEOWNERS


EVIDENCE = {"schema": "evidence.v1", "fixture_runs": [{"id": "r1", "clean": True}, {"id": "r2", "clean": True}, {"id": "r3", "clean": True}],
            "real_feature": {"id": "feat-1", "completed": True}, "attested_by": "A. Maintainer", "attested_at": "2026-10-26"}


def template_filesystem():
    """The deny lists of the shipped sandbox template: what an L4/L5 install merges (D1: the whole H6 write set plus the session registry)."""
    from tests import REPO_ROOT
    with open(os.path.join(REPO_ROOT, "templates", "security", "sandbox.settings.json"), "r", encoding="utf-8") as fh:
        fs = json.load(fh)["sandbox"]["filesystem"]
    return {"denyRead": fs["denyRead"], "denyWrite": fs["denyWrite"]}


def guard_manifest():
    from tests import REPO_ROOT
    with open(os.path.join(REPO_ROOT, "scripts", "guards", "hooks.manifest.json"), "r", encoding="utf-8") as fh:
        return fh.read()


# the project's settings as /init writes them: permissions and the sandbox, no hooks (the plugin's hooks/hooks.json wires the guards)
GUARD_MANIFEST = guard_manifest()
WIRED = wiring.build_templates(json.loads(GUARD_MANIFEST))["interactive.settings.json"]
SANDBOX_BASE = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "excludedCommands": []}
SETTINGS = {"sandbox": dict(SANDBOX_BASE, filesystem=template_filesystem()),
            "permissions": {"allow": WIRED["permissions"]["allow"],
                            "deny": ["WebFetch", "WebSearch", "Workflow", "mcp__*__run_script", "mcp__*deploy*"]}}

BASE_CONFIG = {"autonomy_level": "L5", "bot": {"kind": "github-app", "login": "acme-bot[bot]", "id": "123"},
               "sandbox_verified": "2026-10-26", "managed_settings_verified": "2026-10-26", "promotion_attested": "2026-10-26",
               "platform_attested": ["g3_ci_only"], "run_budget_usd": 50, "gates": {"g4": "on"},
               # Phase 11b: L4 also needs a usable notify_command and a watchdog that has checked in; L5 needs the policy switch
               "merge_policy": "auto-low-risk", "notify_command": "/bin/echo"}


class Ready(BoundaryCase):
    def ready(self, config_edit=None, files_edit=None, gh_fx=None, evidence=True, loopctl=True, nonce=True, kit_version=KIT_VERSION_V2, claude="2.1.305",
              origin="https://github.com/acme/widgets.git", reach=False, watchdog=True):
        files = {".claude/settings.json": json.dumps(SETTINGS), ".github/CODEOWNERS": filled_codeowners(), ".gitignore": ".env*\n!.env.example\n"}
        if not loopctl:                                                     # a plugin folder without loopctl
            os.unlink(os.path.join(self.kit_dir, "scripts", "loopctl"))
        if evidence:
            files["docs/baseline/evidence.json"] = json.dumps(EVIDENCE)
        files.update(files_edit or {})
        repo = self.kit_repo(files=files, kit_version=kit_version, origin=origin)
        cfg = json.loads(json.dumps(BASE_CONFIG))
        # two passes: the attestation fingerprint depends on the repo's deploy-surface files
        ctx0 = self.ctx(repo)
        cfg["deploy_surface"] = {"attested_at": "2026-10-26", "fingerprint": ctx0.fact("deploy_surface")["fingerprint"]}
        if config_edit:
            config_edit(cfg)
        repo.write(".claude/loop.config.md", config_text(cfg))
        if nonce:
            runstate.RunState.for_path(repo.path).write_nonce("sess-1")
        if watchdog:
            from kitlib import jsonio
            from loopctl import unattended
            st = runstate.RunState.for_path(repo.path).ensure()
            jsonio.atomic_write_json(unattended.watch_path(st), {"ts": 1792000000.0 - 60, "lastStatus": "idle", "notified": {}})
        fx = gh_fx if gh_fx is not None else gf.build()
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(fx), claude_version=claude, reach=(lambda h, p, t=1.0: reach))
        return repo, ctx

    def level(self, ctx):
        res = run_checks(ctx)
        return levelcalc.compute(ctx, res), res

    def binding(self, eff):
        return {c.name: c.level for c in eff.binding}


class BaselineAndOutOfTheBox(Ready):
    def test_everything_in_place_is_l5(self):
        repo, ctx = self.ready()
        eff, res = self.level(ctx)
        self.assertEqual(levels.level_name(eff.effective), "L5", eff.explain())
        self.assertEqual(self.binding(eff), {})

    def test_out_of_the_box_with_nothing_attested_is_l1(self):
        """IMP-21: out of the box v2 is an L1 kit with better guards."""
        repo = self.kit_repo()
        ctx = self.ctx(repo, claude_version=None)
        eff, res = self.level(ctx)
        self.assertEqual(eff.effective, 1)
        self.assertEqual(eff.configured, 1)

    def test_a_v1_config_loads_and_means_l1(self):
        from tests import REPO_ROOT
        with open(os.path.join(REPO_ROOT, "tests", "data", "v1", "loop.config.md")) as fh:
            v1 = fh.read()
        repo = self.kit_repo(kit_version="1.2.0 (2026-07-30)\n")
        repo.write(".claude/loop.config.md", v1)
        ctx = self.ctx(repo)
        self.assertEqual(self.level(ctx)[0].configured, 1)

    def test_configured_level_override_for_the_level_flag(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(autonomy_level="L2"))
        ctx.configured_level_override = "L4"
        eff, _ = self.level(ctx)
        self.assertEqual((eff.configured, eff.effective), (4, 4))

    def test_every_cap_prints_a_reason_even_when_not_binding(self):
        repo, ctx = self.ready()
        eff, _ = self.level(ctx)
        self.assertEqual([c.name for c in eff.caps], ["prerequisites", "credentials", "sandbox", "mcp", "b5-deploy-surface", "platform", "evidence", "version",
                                                     "guards"])
        self.assertTrue(all(c.reason for c in eff.caps))


class OneThingBroken(Ready):
    def check_cap(self, ctx, cap_name, level, expect_effective=None, reason_has=None):
        eff, res = self.level(ctx)
        b = self.binding(eff)
        self.assertEqual(b.get(cap_name), level, eff.explain())
        if expect_effective is not None:
            self.assertEqual(eff.effective, expect_effective, eff.explain())
        if reason_has:
            cap = [c for c in eff.caps if c.name == cap_name][0]
            self.assertIn(reason_has, cap.reason)
        return eff

    # -- prerequisites -------------------------------------------------------------------
    def test_no_nonce_means_no_canary(self):
        repo, ctx = self.ready(nonce=False)
        self.check_cap(ctx, "prerequisites", 1, 1, "hook-liveness canary")

    def test_machine_user_pat_stops_at_l2(self):
        import time
        in_a_week = time.strftime("%Y-%m-%d", time.gmtime(1792000000.0 + 7 * 86400))
        repo, ctx = self.ready(config_edit=lambda c: c.update(bot={"kind": "machine-user-pat", "login": "acme-bot", "id": "123", "token_expires": in_a_week}))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 2, eff.explain())

    def test_no_bot_identity(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(bot={"kind": "none"}))
        self.check_cap(ctx, "prerequisites", 1, 1, "B1 bot identity")

    def test_held_out_isolation_unattested(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(platform_attested=[]))
        self.check_cap(ctx, "prerequisites", 1, 1, "held-out isolation")

    def test_loopctl_not_installed_stops_at_l3(self):
        repo, ctx = self.ready(loopctl=False)
        self.check_cap(ctx, "prerequisites", 3, 3, "loopctl")

    def test_no_spend_ceiling_stops_at_l3(self):
        def edit(c):
            del c["run_budget_usd"]
        repo, ctx = self.ready(config_edit=edit)
        self.check_cap(ctx, "prerequisites", 3, 3, "run_budget_usd")

    def test_promotion_bar_unattested_stops_at_l4(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(promotion_attested=""))
        self.check_cap(ctx, "prerequisites", 4, 4, "promotion bar")

    def test_mutation_testing_off_stops_at_l4(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(gates={"g1": "on"}))
        self.check_cap(ctx, "prerequisites", 4, 4, "mutation testing")

    def test_bypass_for_the_bot_stops_at_l2(self):
        bypass = [{"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}]
        repo, ctx = self.ready(gh_fx=gf.build(rulesets=[gf.ruleset(bypass=bypass)]))
        eff = self.check_cap(ctx, "prerequisites", 2, 2, "B4 rulesets")

    def test_missing_required_checks_stops_at_l2(self):
        repo, ctx = self.ready(gh_fx=gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(), gf.checks_rule(("lint",))])]))
        self.check_cap(ctx, "prerequisites", 2, 2, "required checks")

    def test_codeowners_not_covering_guardrails_stops_at_l2(self):
        repo, ctx = self.ready(files_edit={".github/CODEOWNERS": "* @alice\n"})
        self.check_cap(ctx, "prerequisites", 2, 2, "CODEOWNERS")

    def test_unsplit_ci_stops_at_l2(self):
        bad = "name: x\non:\n  pull_request:\npermissions:\n  id-token: write\njobs:\n  j:\n    runs-on: x\n    steps:\n      - uses: actions/checkout@v4\n"
        repo, ctx = self.ready(files_edit={".github/workflows/x.yml": bad})
        eff, _ = self.level(ctx)
        self.assertLessEqual(eff.effective, 2, eff.explain())
        self.assertIn("split CI", [c for c in eff.caps if c.name == "prerequisites"][0].reason)

    def test_web_tools_not_denied_stops_at_l2(self):
        s = json.loads(json.dumps(SETTINGS))
        s["permissions"]["deny"] = ["Workflow", "mcp__*__run_script", "mcp__*deploy*"]
        repo, ctx = self.ready(files_edit={".claude/settings.json": json.dumps(s)})
        self.check_cap(ctx, "prerequisites", 2, 2, "no web tools")

    def test_no_managed_settings_or_container_stops_at_l2(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(managed_settings_verified=""))
        self.check_cap(ctx, "prerequisites", 2, 2, "managed settings")

    def test_container_isolation_replaces_managed_settings(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(managed_settings_verified="", agent_isolation="container"))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 5, eff.explain())

    # -- the named caps ------------------------------------------------------------------
    def test_reachable_credentials_cap_at_l1(self):
        self.fake_home.plant_aws_credentials()
        repo, ctx = self.ready()
        self.check_cap(ctx, "credentials", 1, 1, "AWS profile")

    def test_unprobed_metadata_endpoints_cap_at_l1_unless_the_sandbox_denies_them(self):
        repo, ctx = self.ready()
        ctx.reach = None
        self.check_cap(ctx, "credentials", 1, 1, "metadata endpoints")
        s = json.loads(json.dumps(SETTINGS))
        s["sandbox"]["network"] = {"deniedCidrs": ["169.254.0.0/16"]}
        repo, ctx = self.ready(files_edit={".claude/settings.json": json.dumps(s)})
        ctx.reach = None
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 5, eff.explain())

    def test_sandbox_unattested(self):
        repo, ctx = self.ready(config_edit=lambda c: c.update(sandbox_verified=""))
        self.check_cap(ctx, "sandbox", 1, 1, "V19")

    def test_sandbox_not_fail_closed(self):
        s = json.loads(json.dumps(SETTINGS))
        s["sandbox"]["failIfUnavailable"] = False
        repo, ctx = self.ready(files_edit={".claude/settings.json": json.dumps(s)})
        self.check_cap(ctx, "sandbox", 1, 1, "failIfUnavailable")

    def test_deploy_capable_mcp_server(self):
        repo, ctx = self.ready(files_edit={".mcp.json": json.dumps({"mcpServers": {"aws-infra": {"command": "x"}}})})
        self.check_cap(ctx, "mcp", 1, 1, "aws-infra")

    def test_credential_in_the_environment(self):
        repo, ctx = self.ready()
        ctx.env["GH_TOKEN"] = "x"
        self.check_cap(ctx, "credentials", 1, 1, "cred.env.github")

    def test_unknown_claude_version_caps_at_l1(self):
        repo, ctx = self.ready(claude=None)
        ctx.env.pop("KIT_CLAUDE_VERSION", None)
        self.check_cap(ctx, "version", 1, 1, "could not be read")

    def test_untested_claude_version_caps_at_l2(self):
        repo, ctx = self.ready(kit_version="2.0.0-rc.1 (2026-10-26)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: none\n")
        self.check_cap(ctx, "version", 2, 2, "canary has not passed")

    def test_below_floor_and_above_tested_cap_at_l2(self):
        repo, ctx = self.ready(claude="2.1.250")
        self.check_cap(ctx, "version", 2, 2, "below")
        repo, ctx = self.ready(claude="2.2.0")
        self.check_cap(ctx, "version", 2, 2, "above")

    def test_missing_evidence_caps_at_l3(self):
        repo, ctx = self.ready(evidence=False)
        self.check_cap(ctx, "evidence", 3, 3, "evidence.json")

    def test_incomplete_evidence_caps_at_l3(self):
        ev = json.loads(json.dumps(EVIDENCE))
        ev["fixture_runs"] = ev["fixture_runs"][:2]
        repo, ctx = self.ready(files_edit={"docs/baseline/evidence.json": json.dumps(ev)})
        self.check_cap(ctx, "evidence", 3, 3, "need 3 clean fixture runs")

    def test_no_protection_on_main_caps_at_l1(self):
        repo, ctx = self.ready(gh_fx=gf.build(rulesets=[]))
        self.check_cap(ctx, "platform", 1, 1, "no protection")

    def test_no_code_owner_review_caps_at_l2(self):
        repo, ctx = self.ready(gh_fx=gf.build(rulesets=[gf.ruleset(rules=[gf.pr_rule(code_owner=False), gf.checks_rule()])]))
        self.check_cap(ctx, "platform", 2, 2, "code-owner review")

    def test_non_github_origin(self):
        repo, ctx = self.ready(origin="https://gitlab.com/acme/widgets.git")
        self.check_cap(ctx, "platform", 1, 1, "not a GitHub-hosted repo")

    def test_iac_without_environment_reviewers_stays_at_l3(self):
        repo, ctx = self.ready(files_edit={"infra/main.tf": ""}, config_edit=lambda c: c.update(platform_attested=["g3_ci_only", "apply_human_dispatch"]))
        self.check_cap(ctx, "platform", 3, 3, "apply gate = human dispatch")

    def test_iac_with_environment_reviewers_reaches_l5(self):
        repo, ctx = self.ready(files_edit={"infra/main.tf": ""}, gh_fx=gf.build(environments={"environments": [gf.env_with_reviewers()]}))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 5, eff.explain())

    def test_iac_without_any_apply_gate_fails_the_l3_prerequisite(self):
        repo, ctx = self.ready(files_edit={"infra/main.tf": ""})
        eff, _ = self.level(ctx)
        self.assertLessEqual(eff.effective, 2, eff.explain())
        self.assertIn("IaC apply gate", [c for c in eff.caps if c.name == "prerequisites"][0].reason)


class SandboxListsEndToEnd(Ready):
    """GT-12 through the real `sandbox.deny-paths` check: lists that miss the kit's paths cap the level at L3; no lists at all do not."""

    def with_lists(self, deny_read, deny_write):
        sandbox = dict(SANDBOX_BASE, filesystem={"denyRead": deny_read, "denyWrite": deny_write})
        return {".claude/settings.json": json.dumps(dict(SETTINGS, sandbox=sandbox))}

    def test_lists_that_miss_the_paths_cap_at_l3(self):
        repo, ctx = self.ready(files_edit=self.with_lists(["~/.ssh"], [".git/hooks"]))
        eff, res = self.level(ctx)
        self.assertEqual([r.status for r in res if r.id == "sandbox.deny-paths"], ["fail"])
        self.assertEqual(self.binding(eff).get("sandbox"), 3, eff.explain())
        self.assertEqual(eff.effective, 3)

    def test_complete_lists_do_not_cap(self):
        repo, ctx = self.ready(files_edit=self.with_lists(["~/.aws", "~/.ssh", "~/.config/gh", "~/.kube", ".env.production"], template_filesystem()["denyWrite"]))
        eff, res = self.level(ctx)
        self.assertEqual([r.status for r in res if r.id == "sandbox.deny-paths"], ["pass"])
        self.assertEqual(eff.effective, 5, eff.explain())

    def test_only_the_three_git_directories_still_cap_at_l3(self):
        # the pre-D1 list: the rest of the H6 write set (scripts, hooks, settings, CI) stays writable to a sandboxed command
        repo, ctx = self.ready(files_edit=self.with_lists(["~/.aws", "~/.ssh", "~/.config/gh", "~/.kube", ".env.production"],
                                                          [".git/hooks", ".git/config", ".git/info", ".git/loopctl/sessions"]))
        eff, res = self.level(ctx)
        self.assertEqual([r.status for r in res if r.id == "sandbox.deny-paths"], ["fail"])
        self.assertEqual([r.status for r in res if r.id == "sandbox.registry-path"], ["pass"])
        self.assertEqual(eff.effective, 3, eff.explain())

    def test_no_lists_at_all_leaves_deny_paths_unknown_but_the_registry_row_caps(self):
        # "unknown" does not cap (GT-12), but with no denyWrite the session registry is writable, and at L5 that row fails (D1 item 3)
        sandbox = {".claude/settings.json": json.dumps(dict(SETTINGS, sandbox=dict(SANDBOX_BASE)))}
        repo, ctx = self.ready(files_edit=sandbox)
        eff, res = self.level(ctx)
        self.assertEqual([r.status for r in res if r.id == "sandbox.deny-paths"], ["unknown"])
        self.assertEqual([r.status for r in res if r.id == "sandbox.registry-path"], ["fail"])
        self.assertEqual(self.binding(eff).get("sandbox"), 3, eff.explain())
        cap = next(c for c in eff.caps if c.name == "sandbox")
        self.assertIn("sandbox.registry-path", cap.reason)
        self.assertNotIn("sandbox.deny-paths", cap.reason)


class DeployOnMergeRefusesL5(Ready):
    """Report §15 item 9 'Deploy by side effect': a repo that deploys on merge to main refuses L5 auto-merge."""

    DEPLOY_MAIN = "name: deploy\non:\n  push:\n    branches: [main]\npermissions:\n  contents: read\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npx sst deploy --stage prod\n"
    DEPLOY_ANY = DEPLOY_MAIN.replace("    branches: [main]\n", "")

    def test_ungated_deploy_on_main_caps_at_l4_even_with_every_other_prerequisite(self):
        repo, ctx = self.ready(files_edit={".github/workflows/deploy.yml": self.DEPLOY_MAIN})
        eff, res = self.level(ctx)
        self.assertEqual(eff.configured, 5)
        self.assertEqual(eff.effective, 4, eff.explain())
        cap = [c for c in eff.caps if c.name == "b5-deploy-surface"][0]
        self.assertEqual(cap.level, 4)
        self.assertIn("deploy.yml", cap.reason)
        self.assertIn("no auto-merge", cap.reason)

    def test_the_same_deploy_gated_by_a_reviewed_environment_is_fine(self):
        gated = self.DEPLOY_MAIN.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    environment: production\n")

        def edit(c):
            c["deploy_triggers"] = [{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "gated-by-environment"}]
        repo, ctx = self.ready(files_edit={".github/workflows/deploy.yml": gated}, config_edit=edit,
                               gh_fx=gf.build(environments={"environments": [gf.env_with_reviewers("production")]}))
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 5, eff.explain())

    def test_deploy_on_any_push_refuses_even_l2(self):
        repo, ctx = self.ready(files_edit={".github/workflows/deploy.yml": self.DEPLOY_ANY})
        eff, _ = self.level(ctx)
        self.assertEqual(eff.effective, 1, eff.explain())

    def test_attestation_goes_stale_when_the_surface_changes_after_it(self):
        repo, ctx = self.ready()
        repo.write("Dockerfile", "FROM scratch\n")
        eff, _ = self.level(self.ctx(repo, ghapi=github.fixture_ghapi(gf.build()), claude_version="2.1.305", reach=lambda h, p, t=1.0: False))
        self.assertEqual(eff.effective, 1, eff.explain())
        self.assertIn("changed", [c for c in eff.caps if c.name == "b5-deploy-surface"][0].reason)


class EvidenceGatedKeys(Ready):
    def test_l4_and_l5_are_refused_without_evidence(self):
        repo, ctx = self.ready(evidence=False)
        res = run_checks(ctx)
        hit = self.assertFails(res, "config.evidence-gate")
        self.assertIn("autonomy_level", hit[0].message)

    def test_allowed_with_evidence(self):
        repo, ctx = self.ready()
        self.assertClean(run_checks(ctx), "config.evidence-gate")

    def test_l3_needs_no_evidence(self):
        repo, ctx = self.ready(evidence=False, config_edit=lambda c: (c.update(autonomy_level="L3"), c.pop("gates", None), c.pop("merge_policy", None)))   # gates.g4: on and merge_policy: auto-low-risk are evidence-gated
        self.assertClean(run_checks(ctx), "config.evidence-gate")

    def test_v1_unattended_autonomy_is_gated_too(self):
        repo, ctx = self.ready(evidence=False, config_edit=lambda c: c.update(autonomy="unattended"))
        self.assertIn("autonomy", self.assertFails(run_checks(ctx), "config.evidence-gate")[0].message)


class ConfigRef(Ready):
    def test_config_can_be_read_from_a_git_revision_not_the_working_tree(self):
        """Report §9: gates and attestations are read from the base sha, never the head."""
        repo, ctx = self.ready()
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "attestation")
        base = repo.head()
        repo.write(".claude/loop.config.md", config_text({"autonomy_level": "L5", "bot": {"kind": "none"}}))
        c_head = self.ctx(repo)
        c_base = self.ctx(repo, config_ref=base)
        self.assertEqual(c_head.cfg("bot.kind"), "none")
        self.assertEqual(c_base.cfg("bot.kind"), "github-app")

    def test_missing_config_is_reported_not_fatal(self):
        repo = self.repo()
        ctx = self.ctx(repo)
        res = run_checks(ctx)
        self.assertFails(res, "config.load")
        self.assertEqual(levelcalc.compute(ctx, res).effective, 1)

    def test_a_config_with_an_unknown_key_is_a_warning_with_a_suggestion(self):
        repo = self.kit_repo(config={"autonomy_levl": "L3"})
        res = run_checks(self.ctx(repo))
        hit = self.assertFails(res, "config.load")
        self.assertIn("did you mean", hit[0].message)


class SandboxDenyListsCap(BoundaryCase):
    """GT-12: a sandbox that is on and attested, but whose deny lists miss the kit's protected paths, is not an L4/L5 sandbox.
    Stub rows stand in for the `sandbox.*` checks, so these hold whatever the settings checks themselves report."""

    def rows(self, **status):
        from doctorlib.model import failed, passed, unknown
        out = [passed(i, "ok") for i in ("sandbox.enabled", "sandbox.failIfUnavailable", "sandbox.allowUnsandboxedCommands", "sandbox.excludedCommands",
                                         "sandbox.attestation")]
        for rid in ("sandbox.deny-paths", "sandbox.registry-path"):
            st = status.get(rid.split(".")[1].replace("-", "_"), "pass")
            out.append(passed(rid, "ok") if st == "pass" else failed(rid, "lists miss the protected paths", severity="warn") if st == "fail" else unknown(rid, "no lists found"))
        return out

    def cap(self, **status):
        ctx = self.ctx(self.kit_repo())
        return levelcalc.cap_sandbox(ctx, levelcalc.R(self.rows(**status)))

    def test_all_rows_passing_is_l5(self):
        self.assertEqual(self.cap().level, 5)

    def test_a_failing_deny_paths_row_caps_at_l3(self):
        cap = self.cap(deny_paths="fail")
        self.assertEqual(cap.level, 3)
        self.assertIn("sandbox.deny-paths", cap.reason)

    def test_a_failing_registry_path_row_caps_at_l3(self):
        cap = self.cap(registry_path="fail")
        self.assertEqual(cap.level, 3)
        self.assertIn("sandbox.registry-path", cap.reason)

    def test_unknown_does_not_cap(self):
        self.assertEqual(self.cap(deny_paths="unknown", registry_path="unknown").level, 5)

    def test_without_the_attestation_the_cap_is_still_l1_and_names_the_deny_row(self):
        ctx = self.ctx(self.kit_repo())
        rows = [r for r in self.rows(deny_paths="fail") if r.id != "sandbox.attestation"]
        cap = levelcalc.cap_sandbox(ctx, levelcalc.R(rows))
        self.assertEqual(cap.level, 1)

    def test_a_container_attested_by_the_human_is_not_judged_by_the_settings_lists(self):
        ctx = self.ctx(self.kit_repo(config={"agent_isolation": "container"}))
        self.assertEqual(levelcalc.cap_sandbox(ctx, levelcalc.R(self.rows(deny_paths="fail"))).level, 5)

if __name__ == "__main__":
    unittest.main()
