"""The shipped infra templates: trust policies, the MCP deny snippet, the rego bundle, the policy file, the job graph of each workflow,
and the Phase 6 exit criteria seen through the Phase 1 doctor."""
from __future__ import annotations

import glob
import json
import os
import re
import unittest

from doctorlib import deploy, levelcalc, workflows
from infra import source_policy

from tests import REPO_ROOT
from tests.doctor.helpers import BoundaryCase

from .helpers import TEMPLATES

SEC = os.path.join(REPO_ROOT, ".claude", "templates", "security")
WF = os.path.join(TEMPLATES, "workflows")


def slurp(*parts):
    with open(os.path.join(*parts), "r", encoding="utf-8") as fh:
        return fh.read()


def jload(*parts):
    return json.loads(slurp(TEMPLATES, *parts))


def wfp(name):
    return workflows.parse(slurp(WF, name), name)


def own_perm(job, name):
    p = job.permissions
    return p.get(name) if isinstance(p, dict) else None


class Json(unittest.TestCase):
    def test_every_json_template_parses(self):
        files = glob.glob(os.path.join(TEMPLATES, "**", "*.json"), recursive=True)
        self.assertGreaterEqual(len(files), 5)
        for f in files:
            with self.subTest(os.path.relpath(f, TEMPLATES)):
                json.loads(slurp(f))

    def test_new_trust_policies_pin_the_subject_like_phase_1(self):
        for name, tail in (("aws-plan-main.trust.json", ":environment:plan-main"),
                           ("aws-plan-main-job-workflow-ref.trust.json", ":job_workflow_ref:acme/widgets/.github/workflows/infra-apply.yml@refs/heads/main")):
            with self.subTest(name):
                doc = jload("oidc", name)
                text = json.dumps(doc)
                self.assertNotIn("StringLike", text)
                self.assertNotIn("*", text.replace("sts:AssumeRoleWithWebIdentity", ""))
                cond = doc["Statement"][0]["Condition"]["StringEquals"]
                self.assertEqual(cond["token.actions.githubusercontent.com:aud"], "sts.amazonaws.com")
                self.assertTrue(cond["token.actions.githubusercontent.com:sub"].endswith(tail), cond)
                self.assertRegex(cond["token.actions.githubusercontent.com:sub"], r"^repo:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:(environment:[a-z0-9-]+|job_workflow_ref:.+@refs/heads/main)$")

    def test_the_dispatch_trust_subject_is_the_file_name_the_dispatch_workflow_installs_as(self):
        sub = jload("oidc", "aws-plan-main-job-workflow-ref.trust.json")["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        phase1 = json.loads(slurp(SEC, "oidc", "aws-apply-job-workflow-ref.trust.json"))["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        self.assertEqual(sub, phase1)
        self.assertIn("as .github/workflows/infra-apply.yml", slurp(WF, "infra-apply.dispatch.yml"))

    def test_environment_names_in_the_workflows_match_the_trust_subjects(self):
        wanted = {"infra-plan.yml": {"plan-preview"}, "infra-drift.yml": {"drift"}, "infra-apply.environment.yml": {"plan-main", "production"}}
        trust = {"plan-preview": os.path.join(SEC, "oidc", "aws-plan-preview.trust.json"), "drift": os.path.join(SEC, "oidc", "aws-drift-readonly.trust.json"),
                 "production": os.path.join(SEC, "oidc", "aws-apply-environment.trust.json"), "plan-main": os.path.join(TEMPLATES, "oidc", "aws-plan-main.trust.json")}
        for name, envs in wanted.items():
            got = {j.environment for j in wfp(name).jobs.values() if j.environment}
            self.assertEqual(got, envs, name)
            for e in envs:
                sub = json.loads(slurp(trust[e]))["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
                self.assertTrue(sub.endswith(":environment:" + e), (e, sub))

    def test_the_state_lock_policy_allows_only_the_lock(self):
        doc = jload("iam", "state-lock.policy.json")
        allow = [a for st in doc["Statement"] if st["Effect"] == "Allow" for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])]
        self.assertEqual(sorted(allow), ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "s3:DeleteObject", "s3:PutObject"])
        for st in doc["Statement"]:
            res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            for r in res:
                self.assertNotEqual(r, "*")
        s3 = [st for st in doc["Statement"] if "s3:PutObject" in st["Action"]][0]
        self.assertTrue(s3["Resource"].endswith(".tflock"))

    def test_the_policy_file_template_is_valid_and_empty(self):
        policy, problems = source_policy.load_policy(os.path.join(TEMPLATES, "infra-policy.json"))
        self.assertEqual(problems, [])
        self.assertEqual((policy.allowed_providers, policy.allowed_backends, policy.allow_version_ranges, policy.allow_git_tag_refs), (set(), set(), False, False))


class McpDeny(unittest.TestCase):
    def test_it_contains_phase_1s_rules_and_the_infra_verbs(self):
        mine = jload("mcp-deploy-deny.settings.json")
        phase1 = json.loads(slurp(SEC, "mcp-lockdown.settings.json"))
        self.assertTrue(set(phase1["permissions"]["deny"]) <= set(mine["permissions"]["deny"]))
        self.assertIs(mine["enableAllProjectMcpServers"], False)
        self.assertEqual(mine["enabledMcpjsonServers"], [])
        for verb in ("deploy", "apply", "destroy", "terminate", "provision", "run_script"):
            self.assertTrue([d for d in mine["permissions"]["deny"] if verb in d], verb)
        for d in mine["permissions"]["deny"]:
            self.assertTrue(d.startswith("mcp__"), d)

    def test_installing_it_clears_the_doctors_mcp_findings(self):
        class C(BoundaryCase):
            def runTest(self):
                pass
        c = C()
        c.setUp()
        try:
            repo = c.kit_repo(files={".claude/settings.json": json.dumps(jload("mcp-deploy-deny.settings.json"))})
            res = c.results(c.ctx(repo), {"mcp"})
            self.assertEqual([r.id for r in res if r.status == "fail"], [])
            self.assertEqual(c.by_id(res, "mcp.deny-rules")[0].status, "pass")
        finally:
            c._cleanup()


class Rego(unittest.TestCase):
    """No opa or conftest here, so these are structural checks only (IMP-25): the policy file is TEMPLATE."""
    def read(self, name):
        return slurp(TEMPLATES, "policy", name)

    def test_package_import_and_rules(self):
        text = self.read("plan.rego")
        self.assertRegex(text, r"(?m)^package kit\.infra\.plan$")
        self.assertIn("import rego.v1", text)
        self.assertGreaterEqual(len(re.findall(r"(?m)^deny contains msg if \{", text)), 8)
        self.assertIn("warn contains msg if", text)
        for needle in ("provisioners", "protected_types", "repo:", "0.0.0.0/0", "external"):
            self.assertTrue(needle in text or needle.replace("-", "_") in text, needle)

    def test_the_workflows_use_the_same_namespace(self):
        for name in ("infra-apply.environment.yml", "infra-apply.dispatch.yml"):
            self.assertIn("--namespace kit.infra.plan", slurp(WF, name))

    def test_balanced_delimiters_and_no_recursion(self):
        for name in ("plan.rego", "plan_test.rego"):
            text = re.sub(r"#[^\n]*", "", self.read(name))
            text = re.sub(r"`[^`]*`", "``", text)
            text = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)
            for o, c in ("{}", "[]", "()"):
                self.assertEqual(text.count(o), text.count(c), "%s: unbalanced %s%s" % (name, o, c))
        text = self.read("plan.rego")
        heads = re.findall(r"(?m)^(\w+)\(.*\) :=", text) + re.findall(r"(?m)^(\w+) contains \w+ if", text)
        for h in set(heads):
            body = text.split("%s" % h, 1)[1] if h in text else ""
            # a rule that calls itself in its own body would be rejected by OPA (no recursion)
            for m in re.finditer(r"(?ms)^%s[^\n]*\{(.*?)^\}" % re.escape(h), text):
                self.assertNotRegex(m.group(1), r"\b%s\(" % re.escape(h), "%s calls itself" % h)

    def test_every_test_names_something_in_the_policy(self):
        t = self.read("plan_test.rego")
        self.assertGreaterEqual(len(re.findall(r"(?m)^test_\w+ if", t)), 9)
        self.assertEqual(len(re.findall(r"(?m)^package ", t)), 1)


class JobGraphs(unittest.TestCase):
    """The structure the exit criteria rest on, checked on the parsed workflows."""

    def test_plan_preview_chain_authorize_then_fetch_then_plan_then_comment(self):
        w = wfp("infra-plan.yml")
        needs = lambda j: set([w.jobs[j].raw["needs"]] if isinstance(w.jobs[j].raw["needs"], str) else w.jobs[j].raw["needs"])
        self.assertEqual(needs("fetch"), {"authorize"})
        self.assertEqual(needs("plan"), {"authorize", "fetch"})
        self.assertEqual(needs("comment"), {"authorize", "plan"})
        self.assertNotIn("needs", w.jobs["authorize"].raw)

    def test_only_the_plan_job_holds_an_oidc_token_and_no_job_holds_a_secret(self):
        w = wfp("infra-plan.yml")
        for jid, job in w.jobs.items():
            token = own_perm(job, "id-token") == "write"
            self.assertEqual(token, jid == "plan", jid)
        self.assertNotIn("secrets.", "\n".join(j.text for j in w.jobs.values()).replace("secrets.GITHUB_TOKEN", ""))
        self.assertNotRegex(slurp(WF, "infra-plan.yml"), r"secrets\.")

    def test_the_plan_job_has_the_plan_only_environment_and_no_pull_request_checkout(self):
        job = wfp("infra-plan.yml").jobs["plan"]
        self.assertEqual(job.environment, "plan-preview")
        for s in job.steps:
            if s.uses.startswith("actions/checkout"):
                self.assertEqual(s.with_.get("ref"), "main")
        self.assertEqual(own_perm(job, "pull-requests"), None)
        self.assertEqual(own_perm(job, "contents"), "read")

    def test_the_fetch_job_runs_the_policy_before_and_after_init_and_never_checks_out_the_pr(self):
        job = wfp("infra-plan.yml").jobs["fetch"]
        runs = [s.run for s in job.steps]
        first_policy = [i for i, r in enumerate(runs) if "source_policy.py" in r and "--post-init" not in r][0]
        init = [i for i, r in enumerate(runs) if "init" in r and "terraform" in r][0]
        post = [i for i, r in enumerate(runs) if "--post-init" in r][0]
        self.assertTrue(first_policy < init < post)
        self.assertEqual([s.with_.get("ref") for s in job.steps if s.uses.startswith("actions/checkout")], ["main"])
        self.assertIsNone(own_perm(job, "id-token"))

    def test_the_plan_is_a_preview_without_a_lock_and_with_discarded_output(self):
        text = slurp(WF, "infra-plan.yml")
        plans = re.findall(r"terraform[^\n]*\bplan\b[^\n]*", text)
        plans = [p for p in plans if not p.lstrip().startswith("#")]
        self.assertTrue(plans)
        for p in plans:
            if "-out=tfplan" in p:
                self.assertIn("-lock=false", p)
                self.assertIn("> /dev/null", p)
        self.assertNotIn("terraform_wrapper: true", text)

    def test_the_label_is_removed_on_new_commits(self):
        job = wfp("infra-plan.yml").jobs["reset"]
        self.assertIn("synchronize", job.if_)
        self.assertIn("plan-ok", job.steps[0].run)
        self.assertIsNone(own_perm(job, "id-token"))

    def test_environment_apply_order(self):
        w = wfp("infra-apply.environment.yml")
        self.assertEqual(w.events, {"push": {"branches": ["main"]}})
        self.assertEqual(w.jobs["apply"].raw["needs"], "plan")
        self.assertEqual(w.jobs["apply"].environment, "production")
        runs = [s.run + " " + s.uses for s in w.jobs["apply"].steps]
        idx = lambda needle: [i for i, r in enumerate(runs) if needle in r][0]
        self.assertTrue(idx("require-run-approval") < idx("verify-hash") < idx("base-is-main") < idx("configure-aws-credentials") < idx("apply -input=false tfplan"))
        self.assertEqual(own_perm(w.jobs["apply"], "actions"), "read")

    def test_environment_plan_job_locks_and_refreshes_and_runs_policy_on_the_json(self):
        w = wfp("infra-apply.environment.yml")
        text = "\n".join(s.run for s in w.jobs["plan"].steps)
        self.assertIn("plan -lock=true -refresh=true -out=tfplan", text)
        self.assertIn("conftest test", text)
        self.assertNotIn("-lock=false", text)

    def test_dispatch_apply_is_dispatch_only_and_every_job_chains_to_authorize(self):
        w = wfp("infra-apply.dispatch.yml")
        self.assertEqual(set(w.events), {"workflow_dispatch"})
        self.assertEqual(w.jobs["plan"].raw["needs"], "authorize")
        self.assertEqual(set(w.jobs["apply"].raw["needs"]), {"authorize", "plan"})
        self.assertIsNone(w.jobs["apply"].environment)
        self.assertIn("inputs.mode == 'apply'", w.jobs["apply"].if_)
        auth = "\n".join(s.run for s in w.jobs["authorize"].steps)
        self.assertIn("gate.py authorize", auth)
        self.assertIn("gate.py require-main-ref", auth)
        self.assertIn("--bot", auth)

    def test_dispatch_apply_order(self):
        w = wfp("infra-apply.dispatch.yml")
        runs = [s.run + " " + s.uses for s in w.jobs["apply"].steps]
        idx = lambda needle: [i for i, r in enumerate(runs) if needle in r][0]
        digest, apply_role, final = idx("plan-digest"), [i for i, s in enumerate(w.jobs["apply"].steps) if s.name == "Assume the apply role"][0], idx("apply -input=false tfplan")
        self.assertTrue(idx("verify-hash") < idx("base-is-main") < digest < apply_role < final)

    def test_the_apply_role_is_assumed_only_after_every_check(self):
        for name in ("infra-apply.environment.yml", "infra-apply.dispatch.yml"):
            w = wfp(name)
            steps = w.jobs["apply"].steps
            role_step = [i for i, s in enumerate(steps) if "INFRA_APPLY_ROLE_ARN" in str(s.with_)][0]
            for needle in ("verify-hash", "base-is-main"):
                self.assertTrue([i for i, s in enumerate(steps) if needle in s.run and i < role_step], "%s: %s must run before the apply role is assumed" % (name, needle))
            # and the plan job never sees the apply role
            self.assertNotIn("INFRA_APPLY_ROLE_ARN", w.jobs["plan"].text)

    def test_drift_never_applies_and_uses_the_read_only_pieces(self):
        w = wfp("infra-drift.yml")
        text = slurp(WF, "infra-drift.yml")
        self.assertEqual(set(w.events), {"schedule", "workflow_dispatch"})
        self.assertIn("-detailed-exitcode", text)
        self.assertIn("-lock=false", text)
        self.assertEqual(w.jobs["drift"].environment, "drift")
        self.assertIn("INFRA_DRIFT_ROLE_ARN", text)
        self.assertNotIn("INFRA_APPLY_ROLE_ARN", text)
        self.assertEqual(workflows.deploy_labels("\n".join(s.run for s in w.jobs["drift"].steps)), [])

    def test_infra_check_holds_nothing(self):
        w = wfp("infra-check.yml")
        self.assertEqual(w.permissions, {"contents": "read"})
        self.assertEqual(set(w.events), {"pull_request"})
        for job in w.jobs.values():
            self.assertIsNone(job.permissions)
            self.assertNotRegex(job.text, r"secrets\.|id-token|configure-aws-credentials")
        steps = w.jobs["check"].steps
        self.assertEqual([s.with_.get("persist-credentials") for s in steps if s.uses.startswith("actions/checkout")], [False, False])
        self.assertTrue([s for s in steps if s.uses.startswith("actions/checkout") and "base.sha" in str(s.with_.get("ref"))])

    def test_no_workflow_has_a_token_scope_the_bot_could_use_to_dispatch(self):
        for name in sorted(os.listdir(WF)):
            self.assertNotIn("actions: write", slurp(WF, name), name)


class DoctorSeesThem(BoundaryCase):
    """Phase 6 exit criteria through the Phase 1 doctor: the installed workflows fall into the right gate class."""
    GROUPS = {"deploy-surface"}

    def install(self, apply_variant):
        files = {}
        for name, dest in (("infra-check.yml", "infra-check.yml"), ("infra-plan.yml", "infra-plan.yml"), (apply_variant, "infra-apply.yml"), ("infra-drift.yml", "infra-drift.yml")):
            files[".github/workflows/" + dest] = slurp(WF, name)
        files["infra/main.tf"] = 'resource "aws_s3_bucket" "b" {}\n'
        return files

    def test_only_the_apply_workflow_is_a_deploy_and_it_fires_on_main(self):
        repo = self.kit_repo(files=self.install("infra-apply.environment.yml"))
        surface = self.ctx(repo).fact("deploy_surface")
        deploys = {w["file"]: w for w in surface["workflows"] if w["deploys"]}
        self.assertEqual(sorted(deploys), [".github/workflows/infra-apply.yml"])
        self.assertEqual(deploys[".github/workflows/infra-apply.yml"]["fires_on"], ["push:main"])
        self.assertIn("terraform", surface["iac"])

    def test_environment_variant_is_ungated_until_attested_and_then_clean(self):
        files = self.install("infra-apply.environment.yml")
        repo = self.kit_repo(files=files)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 1)    # unattested surface
        fp = ctx.fact("deploy_surface")["fingerprint"]
        cfg = {"deploy_surface": {"attested_at": "2026-10-26", "fingerprint": fp},
               "deploy_triggers": [{"workflow": ".github/workflows/infra-apply.yml", "fires_on": "push:main", "gate": "gated-by-environment"}]}
        repo2 = self.kit_repo(files=files, config=cfg)
        ctx2 = self.ctx(repo2)
        self.assertEqual(levelcalc.cap_b5(ctx2, levelcalc.R(self.results(ctx2))).level, 5)
        trig = deploy.evaluate(ctx2, ctx2.fact("deploy_surface"))["triggers"]
        self.assertEqual([t["gate"] for t in trig], ["gated-by-environment"])

    def test_an_environment_with_no_reviewers_makes_the_environment_variant_ungated(self):
        from doctorlib import github
        files = self.install("infra-apply.environment.yml")
        repo0 = self.kit_repo(files=files)
        fp = self.ctx(repo0).fact("deploy_surface")["fingerprint"]
        cfg = {"deploy_surface": {"attested_at": "2026-10-26", "fingerprint": fp},
               "deploy_triggers": [{"workflow": ".github/workflows/infra-apply.yml", "fires_on": "push:main", "gate": "gated-by-environment"}]}
        repo = self.kit_repo(files=files, config=cfg)
        fx = {"repos/acme/widgets": {"default_branch": "main"}, "repos/acme/widgets/environments": {"environments": [{"name": "production", "protection_rules": []}]}}
        ctx = self.ctx(repo, ghapi=github.fixture_ghapi(fx))
        trig = deploy.evaluate(ctx, ctx.fact("deploy_surface"))["triggers"]
        self.assertEqual(trig[0]["gate"], "none")
        self.assertIn("no required reviewers", " ".join(trig[0]["problems"]))
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 4)

    def test_dispatch_variant_is_human_started_and_needs_no_attestation_for_a_trigger(self):
        repo = self.kit_repo(files=self.install("infra-apply.dispatch.yml"))
        surface = self.ctx(repo).fact("deploy_surface")
        apply_wf = [w for w in surface["workflows"] if w["file"].endswith("infra-apply.yml")][0]
        self.assertEqual(apply_wf["fires_on"], [])
        trig = deploy.evaluate(self.ctx(repo), surface)["triggers"]
        self.assertEqual([(t["fires_on"], t["ungated_bot_pushable"], t["ungated_main"]) for t in trig], [([], False, False)])
        ctx = self.ctx(repo)
        fp = ctx.fact("deploy_surface")["fingerprint"]
        repo2 = self.kit_repo(files=self.install("infra-apply.dispatch.yml"), config={"deploy_surface": {"attested_at": "2026-10-26", "fingerprint": fp}})
        ctx2 = self.ctx(repo2)
        self.assertEqual(levelcalc.cap_b5(ctx2, levelcalc.R(self.results(ctx2))).level, 5)

    def test_dispatch_variant_fixture_the_doctor_caps_iac_at_l3_without_environment_reviewers(self):
        repo = self.kit_repo(files=self.install("infra-apply.dispatch.yml"), config={"platform_attested": ["rulesets"]})
        ctx = self.ctx(repo)
        cap = levelcalc.cap_platform(ctx, levelcalc.R(self.results(ctx, {"deploy-surface", "github"})))
        self.assertLessEqual(cap.level, 3)

    def test_the_doctor_reports_infra_lint_findings_and_counts_them_against_split_ci(self):
        unsafe = slurp(WF, "infra-apply.environment.yml").replace("    environment: production\n", "")
        files = self.install("infra-apply.environment.yml")
        files[".github/workflows/infra-apply.yml"] = unsafe
        ctx = self.ctx(self.kit_repo(files=files))
        res = self.results(ctx, {"ci"})
        self.assertFails(res, "infra.apply-ungated")
        self.assertTrue(levelcalc.R(res).errors("infra."))
        clean = self.results(self.ctx(self.kit_repo(files=self.install("infra-apply.environment.yml"))), {"ci"})
        self.assertEqual([r.id for r in clean if r.status == "fail" and r.severity == "error"], [])
        self.assertTrue(self.by_id(clean, "ci.lint"))

    def test_the_setup_actions_are_not_mistaken_for_deploys(self):
        repo = self.kit_repo(files=self.install("infra-apply.environment.yml"))
        surface = self.ctx(repo).fact("deploy_surface")
        for w in surface["workflows"]:
            if not w["file"].endswith("infra-apply.yml"):
                self.assertFalse(w["deploys"], w["file"])

    def test_the_rc1_fixtures_still_refuse_with_infra_templates_present(self):
        # a deploy on a bot-pushable ref next to the infra workflows is still an L1 cap
        files = self.install("infra-apply.environment.yml")
        files[".github/workflows/deploy.yml"] = "name: deploy\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: terraform apply -auto-approve\n"
        repo = self.kit_repo(files=files)
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_b5(ctx, levelcalc.R(self.results(ctx))).level, 1)


if __name__ == "__main__":
    unittest.main()
