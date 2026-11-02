"""Review fixes, package D (IN-1 to IN-9, IN-14): each way of putting a credentialed deploy-class job behind a gate the lint cannot verify.

One positive row (the lint must fire) and one negative row (a legitimate neighbour must stay quiet) per item, plus the variants an attacker
would try next. The shipped templates are the baseline: they are mutated, never replaced.
"""
from __future__ import annotations

import os
import unittest

import ci_lint
from doctorlib import workflows
from infra import ci_rules

from tests import REPO_ROOT

from .helpers import TEMPLATES

WF = os.path.join(TEMPLATES, "workflows")
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32


def read(name):
    with open(os.path.join(WF, name), "r", encoding="utf-8") as fh:
        return fh.read()


def lint(text, name="w.yml"):
    return ci_lint.lint_text(text, name)


def rules(findings, severity=None):
    return sorted({f.rule for f in findings if severity is None or f.severity == severity})


def mutate(text, old, new):
    assert old in text, "mutation anchor not found: %r" % old
    return text.replace(old, new)


ENV = read("infra-apply.environment.yml")
DISPATCH = read("infra-apply.dispatch.yml")


def job_wf(on, body, perms="permissions:\n  contents: read\n  id-token: write\n"):
    return "name: t\non:\n%s\n%sjobs:\n  j:\n    runs-on: ubuntu-latest\n%s" % (on, perms, body)


PLAN_STEPS = ("    steps:\n      - uses: actions/checkout@%s\n      - uses: aws-actions/configure-aws-credentials@%s\n        with:\n          role-to-assume: arn:aws:iam::1:role/x\n"
              "      - run: terraform plan -out=tfplan\n") % (SHA, SHA)
BUILD_STEPS = "    steps:\n      - uses: actions/checkout@%s\n      - run: npm ci && npm run build\n" % SHA


class BotReachablePush(unittest.TestCase):
    """IN-1: a push to any branch but main (or a tag) is something the bot can do."""

    PUSHES = {
        "every branch": "  push:\n    branches: ['**']",
        "a glob beside main": "  push:\n    branches: [main, 'release/**']",
        "branches-ignore": "  push:\n    branches-ignore: [docs]",
        "tags": "  push:\n    tags: ['v*']",
        "no filter": "  push:",
        "a feature branch": "  push:\n    branches: [feature/x]",
    }

    def test_bot_reachable_function(self):
        for name, on in self.PUSHES.items():
            with self.subTest(name):
                wf = workflows.parse(job_wf(on, PLAN_STEPS), "w.yml")
                self.assertIn("push", ci_rules.bot_reachable(wf.events))
        for on in ("  push:\n    branches: [main]", "  push:\n    branches: [main, master]", "  workflow_dispatch:", "  schedule:\n    - cron: '0 0 * * *'"):
            with self.subTest(on):
                wf = workflows.parse(job_wf(on, PLAN_STEPS), "w.yml")
                self.assertEqual(ci_rules.bot_reachable(wf.events), [])

    def test_a_credentialed_plan_on_a_non_main_push_is_ungated(self):
        for name, on in self.PUSHES.items():
            with self.subTest(name):
                f = lint(job_wf(on, PLAN_STEPS))
                self.assertIn("infra.plan-ungated", rules(f, "error"))
                self.assertIn("ci.credentials-on-bot-push", rules(f, "error"))

    def test_the_same_plan_on_a_push_to_main_only_is_quiet(self):
        f = lint(job_wf("  push:\n    branches: [main]", PLAN_STEPS))
        self.assertNotIn("infra.plan-ungated", rules(f))
        self.assertNotIn("ci.credentials-on-bot-push", rules(f))

    def test_a_credentialed_build_that_is_not_infrastructure_is_caught(self):
        f = lint(job_wf("  push:\n    branches: ['**']", BUILD_STEPS))
        self.assertEqual(rules(f, "error"), ["ci.credentials-on-bot-push"])

    def test_an_environment_is_the_human_gate_for_such_a_job(self):
        body = "    environment: staging\n" + BUILD_STEPS
        self.assertEqual(rules(lint(job_wf("  push:\n    branches: ['**']", body)), "error"), [])

    def test_an_environment_chosen_at_run_time_is_not_a_gate(self):
        body = "    environment: ${{ github.ref_name }}\n" + BUILD_STEPS
        self.assertIn("ci.credentials-on-bot-push", rules(lint(job_wf("  push:\n    branches: ['**']", body)), "error"))

    def test_a_secret_counts_and_the_github_token_does_not(self):
        secret = BUILD_STEPS + "      - run: npm publish\n        env:\n          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}\n"
        self.assertIn("ci.credentials-on-bot-push", rules(lint(job_wf("  push:\n    branches: ['**']", secret, perms="permissions:\n  contents: read\n")), "error"))
        gh = BUILD_STEPS + "      - run: gh pr list\n        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
        self.assertEqual(rules(lint(job_wf("  push:\n    branches: ['**']", gh, perms="permissions:\n  contents: read\n")), "error"), [])

    def test_a_job_without_credentials_on_any_push_is_fine(self):
        self.assertEqual(rules(lint(job_wf("  push:\n    branches: ['**']", BUILD_STEPS, perms="permissions:\n  contents: read\n")), "error"), [])

    def test_the_apply_trigger_rule_still_rejects_such_an_apply(self):
        text = mutate(ENV, "branches: [main]", "branches-ignore: [docs]")
        self.assertIn("infra.apply-trigger", rules(lint(text), "error"))


# --- IN-2, IN-6: the deploy vocabulary and the spellings of a command word ------------------------------------------------------------

APPLY_COMMANDS = [
    "sam deploy --guided", "cdktf deploy", "pulumi update --yes", "pulumi up -y", "terragrunt run --all apply", "terragrunt run-all apply",
    "terragrunt run --all --non-interactive apply", "terragrunt apply", "copilot deploy", "copilot svc deploy --name x", "copilot env deploy",
    "eb deploy prod", "eb create prod-env", "ansible-playbook site.yml", "serverless deploy", "sls deploy --stage prod",
    "/usr/local/bin/terraform apply -auto-approve", "./bin/tofu apply -auto-approve", "terraform 'apply' -auto-approve", '"terraform" "apply" tfplan',
    "$(which terraform) apply tfplan", "`which tofu` apply tfplan", "${TF_BIN:-terraform} apply tfplan", "command -v terraform >/dev/null; $(command -v terraform) apply tfplan",
    "npx cdk deploy", "./node_modules/.bin/cdk deploy --all", "kubectl apply -f k8s/", "helm upgrade --install x ./chart", "cdk destroy",
    "TF=terraform\n$TF apply -auto-approve", "ter\\raform apply -auto-approve",
]
NOT_APPLY = ["terraform plan", "terraform fmt -check", "sam build", "sam validate", "eb status", "eb list", "cdk synth", "cdk8s synth", "cdktf synth",
             "pulumi preview", "terragrunt plan", "terragrunt run --all plan", "copilot svc show", "ansible --version", "echo terraform", "ls infra/terraform",
             "cd infra/terraform && terraform init -backend=false"]


class DeployVocabulary(unittest.TestCase):
    def test_ci_rules_sees_every_spelling_as_an_apply(self):
        for cmd in APPLY_COMMANDS:
            with self.subTest(cmd):
                self.assertTrue(ci_rules.apply_calls(workflows.Step({"run": cmd}, 0)), cmd)

    def test_the_doctor_scan_sees_every_spelling_as_a_deploy(self):
        for cmd in APPLY_COMMANDS:
            with self.subTest(cmd):
                self.assertTrue(workflows.deploy_labels(cmd), cmd)

    def test_neighbours_that_are_not_applies_stay_quiet(self):
        for cmd in NOT_APPLY:
            with self.subTest(cmd):
                self.assertEqual(ci_rules.apply_calls(workflows.Step({"run": cmd}, 0)), [], cmd)
                self.assertEqual(workflows.deploy_labels(cmd), [], cmd)

    def test_the_apply_actions(self):
        for uses, with_ in (("gruntwork-io/terragrunt-action@v2", {"tg_command": "run --all apply"}), ("gruntwork-io/terragrunt-action@v2", {"tg_command": "apply"}),
                            ("dflook/tofu-apply@v1", {}), ("dawidd6/action-ansible-playbook@v2", {})):
            with self.subTest(uses):
                self.assertTrue(ci_rules.apply_calls(workflows.Step({"uses": uses, "with": with_}, 0)))
                self.assertIsNotNone(workflows.deploy_uses_label(workflows.Step({"uses": uses, "with": with_}, 0)))
        quiet = workflows.Step({"uses": "gruntwork-io/terragrunt-action@v2", "with": {"tg_command": "plan"}}, 0)
        self.assertEqual(ci_rules.apply_calls(quiet), [])

    def test_an_apply_in_a_pull_request_workflow_is_a_trigger_error_whatever_the_spelling(self):
        for cmd in ("sam deploy", "cdktf deploy", "pulumi update", "terragrunt run --all apply", "copilot deploy", "eb deploy", "ansible-playbook x.yml",
                    "/usr/local/bin/terraform apply", "./bin/tofu apply", "terraform 'apply'", "$(which terraform) apply"):
            with self.subTest(cmd):
                text = job_wf("  pull_request:", "    steps:\n      - run: %s\n" % cmd)
                self.assertIn("infra.apply-trigger", rules(lint(text), "error"))

    def test_a_command_word_hidden_in_a_variable_is_an_error_when_credentialed_and_a_warning_otherwise(self):
        body = "    steps:\n      - run: $TOOL apply -auto-approve\n"
        cred = lint(job_wf("  workflow_dispatch:", body))
        self.assertIn("infra.apply-indirect", rules(cred, "error"))
        plain = lint(job_wf("  workflow_dispatch:", "    steps:\n      - run: terraform fmt\n      - run: $TOOL apply -auto-approve\n", perms="permissions:\n  contents: read\n"))
        self.assertIn("infra.apply-indirect", rules(plain, "warn"))
        self.assertNotIn("infra.apply-indirect", rules(plain, "error"))

    def test_the_indirect_forms(self):
        for run in ('"$TOOL" apply -auto-approve', "${TOOL} destroy", "$TOOL deploy", "$TOOL up", "sudo $TOOL apply", "FOO=1 $TOOL apply",
                    "$(echo terraform) apply tfplan", "if true; then $TOOL apply; fi"):
            with self.subTest(run):
                text = job_wf("  workflow_dispatch:", "    steps:\n      - run: |\n          %s\n" % run)
                self.assertIn("infra.apply-indirect", rules(lint(text), "error"), run)

    def test_text_that_only_mentions_a_variable_is_not_indirect(self):
        for run in ('echo "planning $ENV apply"', "echo $TOOL", "x=$TOOL", 'echo "deploy $ENV up"'):
            with self.subTest(run):
                text = job_wf("  workflow_dispatch:", "    steps:\n      - run: |\n          terraform fmt\n          %s\n" % run)
                self.assertNotIn("infra.apply-indirect", rules(lint(text)), run)

    def test_a_variable_set_to_the_tool_in_the_same_script_is_followed(self):
        text = job_wf("  workflow_dispatch:", "    steps:\n      - run: |\n          TF=terraform\n          $TF apply -auto-approve\n")
        r = rules(lint(text), "error")
        self.assertIn("infra.apply-without-saved-plan", r)
        self.assertNotIn("infra.apply-indirect", r)


# --- IN-3: docker:// ------------------------------------------------------------------------------------------------------------------

def docker_wf(uses, args="", on="  workflow_dispatch:", perms="permissions:\n  contents: read\n  id-token: write\n"):
    w = "    steps:\n      - uses: %s\n" % uses
    if args:
        w += "        with:\n          args: %s\n" % args
    return job_wf(on, w, perms)


class DockerUses(unittest.TestCase):
    PINNED = "docker://hashicorp/terraform:1.9.8@" + DIGEST

    def test_an_unpinned_image_is_an_error_with_credentials_and_a_warning_without(self):
        f = lint(docker_wf("docker://alpine:3.20"))
        self.assertIn("ci.docker-uses-unpinned", rules(f, "error"))
        g = lint(docker_wf("docker://alpine:3.20", perms="permissions:\n  contents: read\n"))
        self.assertIn("ci.docker-uses-unpinned", rules(g, "warn"))
        self.assertNotIn("ci.docker-uses-unpinned", rules(g, "error"))

    def test_a_digest_pinned_image_is_quiet(self):
        self.assertNotIn("ci.docker-uses-unpinned", rules(lint(docker_wf("docker://alpine@" + DIGEST))))
        self.assertNotIn("ci.docker-uses-unpinned", rules(lint(docker_wf("docker://ghcr.io/acme/tool:1.2@" + DIGEST))))

    def test_a_short_or_malformed_digest_is_not_a_pin(self):
        self.assertIn("ci.docker-uses-unpinned", rules(lint(docker_wf("docker://alpine@sha256:abc")), "error"))

    def test_an_iac_image_with_apply_args_is_an_apply(self):
        f = lint(docker_wf(self.PINNED, args="apply -auto-approve", on="  pull_request:"))
        self.assertIn("infra.apply-trigger", rules(f, "error"))
        self.assertTrue(ci_rules.apply_calls(workflows.Step({"uses": self.PINNED, "with": {"args": "apply tfplan"}}, 0)))

    def test_an_iac_image_with_no_args_counts_as_deploy_capable(self):
        for image in ("hashicorp/terraform", "hashicorp/tfc-agent", "opentofu/opentofu", "pulumi/pulumi", "amazon/aws-cli", "bitnami/kubectl", "alpine/helm",
                      "gruntwork/terragrunt", "ghcr.io/acme/my-terraform-runner", "dtzar/helm-kubectl"):
            with self.subTest(image):
                step = workflows.Step({"uses": "docker://%s:1@%s" % (image, DIGEST)}, 0)
                self.assertTrue(ci_rules.apply_calls(step), image)
                self.assertIsNotNone(workflows.deploy_uses_label(step), image)

    def test_an_iac_image_that_only_plans_is_not_an_apply(self):
        step = workflows.Step({"uses": self.PINNED, "with": {"args": "plan -out=tfplan"}}, 0)
        self.assertEqual(ci_rules.apply_calls(step), [])
        self.assertIsNone(workflows.deploy_uses_label(step))

    def test_the_entrypoint_and_a_plain_image_are_read_too(self):
        step = workflows.Step({"uses": "docker://alpine@" + DIGEST, "with": {"entrypoint": "/bin/sh", "args": "-c 'terraform apply -auto-approve'"}}, 0)
        self.assertTrue(ci_rules.apply_calls(step))
        quiet = workflows.Step({"uses": "docker://alpine@" + DIGEST, "with": {"args": "echo hello"}}, 0)
        self.assertEqual(ci_rules.apply_calls(quiet), [])
        self.assertIsNone(workflows.deploy_uses_label(quiet))

    def test_args_computed_at_run_time_leave_the_verb_invisible(self):
        for args in ("${{ inputs.cmd }}", "$ARGS", "${ARGS}"):
            step = workflows.Step({"uses": self.PINNED, "with": {"args": args}}, 0)
            self.assertTrue(ci_rules.apply_calls(step), args)

    def test_docker_run_in_a_script_reads_through_the_tag_and_the_digest(self):
        for cmd in ("docker run --rm -v $PWD:/w -w /w hashicorp/terraform:1.9.8 apply -auto-approve", "docker run ghcr.io/opentofu/opentofu:1.8 apply tfplan",
                    "docker run hashicorp/terraform@sha256:%s apply" % ("a" * 64), "docker run --rm alpine/helm:3.15 upgrade x ./c", "docker run bitnami/kubectl:1.30 apply -f x.yaml"):
            with self.subTest(cmd):
                self.assertTrue(ci_rules.apply_calls(workflows.Step({"run": cmd}, 0)), cmd)
                self.assertTrue(workflows.deploy_labels(cmd), cmd)
        for cmd in ("docker pull hashicorp/terraform:1.9", "docker run hashicorp/terraform:1.9 plan", "docker run --rm alpine echo apply"):
            with self.subTest(cmd):
                self.assertEqual(ci_rules.apply_calls(workflows.Step({"run": cmd}, 0)), [], cmd)

    def test_an_infra_workflow_is_recognised_by_its_image(self):
        wf = workflows.parse(docker_wf(self.PINNED, args="plan"), "w.yml")
        self.assertTrue(ci_rules.is_infra_workflow(wf))

    def test_image_parsing(self):
        self.assertEqual(workflows.docker_image("docker://ghcr.io/acme/tool:1.2@sha256:" + "a" * 64), ("ghcr.io/acme/tool", "1.2", "sha256:" + "a" * 64))
        self.assertEqual(workflows.docker_image("docker://localhost:5000/tool"), ("localhost:5000/tool", "", ""))
        self.assertEqual(workflows.docker_image("docker://alpine"), ("alpine", "", ""))


# --- IN-4: an Environment is not a gate unless the run checks who approved -----------------------------------------------------------

class EnvironmentApproval(unittest.TestCase):
    def test_an_environment_only_apply_is_an_error(self):
        text = mutate(ENV, "gate.py require-run-approval", "gate.py hash-file")
        f = lint(text)
        self.assertIn("infra.environment-approval-unverified", rules(f, "error"))

    def test_the_check_in_a_job_the_apply_needs_counts(self):
        text = job_wf("  push:\n    branches: [main]",
                      "    environment: production\n    steps:\n      - run: python3 gate.py require-run-approval --x\n      - run: terraform apply tfplan\n")
        self.assertNotIn("infra.environment-approval-unverified", rules(lint(text)))

    def test_the_shipped_environment_variant_is_quiet(self):
        self.assertNotIn("infra.environment-approval-unverified", rules(lint(ENV)))


# --- IN-5: the actor check has to be real --------------------------------------------------------------------------------------------

AUTHZ_LINE = 'run: python3 .claude/scripts/infra/gate.py authorize --actor "$ACTOR" --actor "$TRIGGERING_ACTOR" --approvers "$APPROVERS" --bot "$BOT_LOGIN"'
AUTHZ_STEP = "      - name: Require an approver who is not a bot\n"


class ActorCheck(unittest.TestCase):
    def test_the_template_is_the_baseline(self):
        self.assertNotIn("infra.apply-ungated", rules(lint(DISPATCH)))

    def ungated(self, text):
        self.assertIn("infra.apply-ungated", rules(lint(text), "error"))

    def test_a_step_if_that_can_be_false(self):
        self.ungated(mutate(DISPATCH, AUTHZ_STEP, AUTHZ_STEP + "        if: ${{ false }}\n"))
        self.ungated(mutate(DISPATCH, AUTHZ_STEP, AUTHZ_STEP + "        if: github.actor == 'nobody'\n"))

    def test_continue_on_error_other_than_the_literal_false(self):
        self.ungated(mutate(DISPATCH, AUTHZ_STEP, AUTHZ_STEP + "        continue-on-error: ${{ true }}\n"))
        self.ungated(mutate(DISPATCH, AUTHZ_STEP, AUTHZ_STEP + "        continue-on-error: ${{ github.event_name == 'x' }}\n"))

    def test_the_literal_false_is_fine(self):
        text = mutate(DISPATCH, AUTHZ_STEP, AUTHZ_STEP + "        continue-on-error: false\n")
        self.assertNotIn("infra.apply-ungated", rules(lint(text)))

    def test_the_command_only_named_in_an_echo_or_a_comment(self):
        self.ungated(mutate(DISPATCH, AUTHZ_LINE, 'run: echo "python3 .claude/scripts/infra/gate.py authorize"'))
        self.ungated(mutate(DISPATCH, AUTHZ_LINE, "run: |\n          # python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR\n          true"))
        self.ungated(mutate(DISPATCH, AUTHZ_LINE, "run: printf '%s' gate.py authorize"))

    def test_the_command_skipped_by_shell_logic(self):
        for lines in ("false && python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR",
                      "true || python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR",
                      "if false; then\n            python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR\n          fi",
                      "exit 0\n          python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR",
                      "[ -n \"$X\" ] &&\n            python3 .claude/scripts/infra/gate.py authorize --actor $ACTOR"):
            with self.subTest(lines):
                self.ungated(mutate(DISPATCH, AUTHZ_LINE, "run: |\n          " + lines))

    def test_a_constant_actor_makes_the_check_pass_for_ever(self):
        self.ungated(mutate(DISPATCH, '--actor "$ACTOR" --actor "$TRIGGERING_ACTOR"', "--actor alice"))
        self.ungated(mutate(DISPATCH, '--actor "$ACTOR" --actor "$TRIGGERING_ACTOR"', "--approvers x"))

    def test_an_expression_actor_is_fine(self):
        text = mutate(DISPATCH, '--actor "$ACTOR" --actor "$TRIGGERING_ACTOR"', '--actor "${{ github.actor }}"')
        self.assertNotIn("infra.apply-ungated", rules(lint(text)))

    def test_the_check_in_a_job_the_apply_does_not_need(self):
        self.ungated(mutate(DISPATCH, "    needs: [authorize, plan]\n", "    needs: [plan]\n").replace("  plan:\n    needs: authorize\n", "  plan:\n"))

    def test_the_check_after_the_apply_in_the_same_job_gates_nothing(self):
        text = job_wf("  workflow_dispatch:",
                      "    steps:\n      - run: terraform apply tfplan\n      - run: python3 gate.py authorize --actor $ACTOR --approvers y\n")
        self.ungated(text)

    def test_a_job_level_if_on_the_authorize_job_is_fine_because_the_apply_needs_it(self):
        self.assertIn("if:", DISPATCH.split("  authorize:")[1].split("steps:")[0])
        self.assertNotIn("infra.apply-ungated", rules(lint(DISPATCH)))

    def test_a_job_level_if_on_the_apply_job_itself_is_fine(self):
        text = job_wf("  workflow_dispatch:", "    if: ${{ github.ref == 'refs/heads/main' }}\n    steps:\n      - run: python3 gate.py authorize --actor $ACTOR --approvers y\n"
                                              "      - run: python3 gate.py require-main-ref --ref x\n      - run: terraform apply tfplan\n")
        self.assertNotIn("infra.apply-ungated", rules(lint(text)))


# --- IN-7: remote reusable workflows ----------------------------------------------------------------------------------------------

def remote_wf(on, uses, extra="    secrets: inherit\n", perms="permissions:\n  contents: read\n"):
    return "name: t\non:\n%s\n%sjobs:\n  j:\n    uses: %s\n%s" % (on, perms, uses, extra)


class RemoteWorkflows(unittest.TestCase):
    PINNED = "acme/shared/.github/workflows/build.yml@" + SHA

    def test_secrets_into_a_remote_workflow_on_a_trigger_the_bot_reaches_is_an_error(self):
        for on in ("  pull_request:", "  pull_request_target:", "  workflow_run:\n    workflows: [x]", "  push:\n    branches: ['**']", "  issue_comment:"):
            with self.subTest(on):
                self.assertIn("ci.remote-workflow-secrets", rules(lint(remote_wf(on, self.PINNED)), "error"))

    def test_on_main_or_dispatch_only_it_is_a_warning_when_pinned(self):
        for on in ("  push:\n    branches: [main]", "  workflow_dispatch:"):
            with self.subTest(on):
                f = lint(remote_wf(on, self.PINNED))
                self.assertIn("ci.remote-workflow-secrets", rules(f, "warn"))
                self.assertNotIn("ci.remote-workflow-secrets", rules(f, "error"))

    def test_an_unpinned_ref_is_an_error_either_way(self):
        for ref in ("v1", "main", "abc123"):
            with self.subTest(ref):
                f = lint(remote_wf("  workflow_dispatch:", "acme/shared/.github/workflows/build.yml@" + ref))
                self.assertIn("ci.remote-workflow-secrets", rules(f, "error"))

    def test_any_secrets_value_counts_not_only_inherit(self):
        f = lint(remote_wf("  pull_request:", self.PINNED, extra="    secrets:\n      token: ${{ secrets.X }}\n"))
        self.assertIn("ci.remote-workflow-secrets", rules(f, "error"))

    def test_an_oidc_grant_counts(self):
        f = lint(remote_wf("  workflow_dispatch:", self.PINNED, extra="", perms="permissions:\n  id-token: write\n"))
        self.assertIn("ci.remote-workflow-secrets", rules(f, "warn"))
        g = lint(remote_wf("  pull_request:", self.PINNED, extra="", perms="permissions:\n  id-token: write\n"))
        self.assertIn("ci.remote-workflow-secrets", rules(g, "error"))

    def test_no_secrets_and_no_token_is_not_a_deploy_surface(self):
        self.assertNotIn("ci.remote-workflow-secrets", rules(lint(remote_wf("  pull_request:", self.PINNED, extra=""))))

    def test_a_local_reusable_workflow_is_not_remote(self):
        self.assertNotIn("ci.remote-workflow-secrets", rules(lint(remote_wf("  pull_request:", "./.github/workflows/build.yml"))))


# --- IN-8: script injection --------------------------------------------------------------------------------------------------------

class ScriptInjection(unittest.TestCase):
    BAD = ["${{ github.event.pull_request.title }}", "${{ github.event.pull_request.head.ref }}", "${{ github.ref_name }}", "${{ github.event.ref }}",
           "${{ github.event.client_payload.cmd }}", "${{ github.event.release.name }}", "${{ github.event.release.body }}", "${{ toJSON(github.event) }}",
           "${{ toJson(github.event) }}", "${{ inputs.version }}", "${{ github.event.inputs.version }}", "${{ github.head_ref }}", "${{ github.ref }}",
           "${{ github.event.comment.body }}", "${{github.event.issue.title}}", "${{ github.event.pull_request.number || github.event.issue.title }}",
           "${{ github.event.workflow_run.head_branch }}", "${{ github.event.head_commit.message }}", "${{ github.event.sender.login }}",
           "${{ format('{0}', github.event.issue.body) }}"]
    GOOD = ["${{ github.sha }}", "${{ github.run_id }}", "${{ github.repository }}", "${{ github.event.number }}", "${{ github.event.pull_request.number }}",
            "${{ github.event.issue.number }}", "${{ github.event.pull_request.number || github.event.issue.number }}", "${{ github.event.repository.default_branch }}",
            "${{ github.event.action }}", "${{ github.event_name }}", "${{ github.ref == 'refs/heads/main' }}", "${{ github.event.action == 'labeled' }}",
            "${{ contains(github.event.pull_request.title, 'x') }}", "${{ secrets.GITHUB_TOKEN }}", "${{ vars.X }}", "${{ needs.a.outputs.b }}", "${{ env.X }}"]

    def wf(self, expr, where="run"):
        if where == "run":
            return job_wf("  pull_request:", "    steps:\n      - run: echo %s\n" % expr, perms="permissions:\n  contents: read\n")
        return job_wf("  pull_request:", "    steps:\n      - uses: actions/github-script@%s\n        with:\n          script: |\n            console.log('%s')\n" % (SHA, expr),
                      perms="permissions:\n  contents: read\n")

    def test_the_stranger_chosen_values(self):
        for expr in self.BAD:
            for where in ("run", "script"):
                with self.subTest(expr=expr, where=where):
                    self.assertIn("ci.script-injection", rules(lint(self.wf(expr, where)), "error"))

    def test_the_values_that_are_safe_to_interpolate(self):
        for expr in self.GOOD:
            with self.subTest(expr):
                self.assertNotIn("ci.script-injection", rules(lint(self.wf(expr))))

    def test_the_actor_is_a_warning(self):
        f = lint(self.wf("${{ github.actor }}"))
        self.assertIn("ci.script-injection", rules(f, "warn"))
        self.assertNotIn("ci.script-injection", rules(f, "error"))

    def test_passing_the_value_through_env_is_the_fix(self):
        text = job_wf("  pull_request:", "    steps:\n      - env:\n          TITLE: ${{ github.event.pull_request.title }}\n        run: echo \"$TITLE\"\n",
                      perms="permissions:\n  contents: read\n")
        self.assertNotIn("ci.script-injection", rules(lint(text)))

    def test_property_names_are_case_insensitive_and_brackets_are_dots(self):
        for expr in ("${{ GitHub.Event.Issue.Title }}", "${{ github['event']['issue']['title'] }}", "${{ github.event['issue'].title }}", "${{ INPUTS.version }}",
                     "${{ inputs['version'] }}", "${{ github['head_ref'] }}", "${{ GITHUB.REF_NAME }}"):
            with self.subTest(expr):
                self.assertIn("ci.script-injection", rules(lint(self.wf(expr)), "error"))
        self.assertNotIn("ci.script-injection", rules(lint(self.wf("${{ GitHub.Event.Number }}"))))

    def test_an_expression_with_no_expressions_is_untouched(self):
        self.assertEqual(ci_lint.injected("echo hello"), ([], []))


# --- IN-9: pull-request code into a privileged job -----------------------------------------------------------------------------------

def priv_wf(on, steps, perms="permissions:\n  contents: read\n  id-token: write\n"):
    return job_wf(on, "    steps:\n" + steps, perms)


class PrivilegedTriggerInputs(unittest.TestCase):
    TRIGGERS = ("  pull_request_target:\n    types: [labeled]", "  workflow_run:\n    workflows: [ci]")

    def test_a_computed_checkout_ref_or_repository(self):
        for on in self.TRIGGERS:
            for key, val in (("ref", "${{ steps.pr.outputs.head }}x"), ("ref", "${{ inputs.ref }}"), ("ref", "${{ github.event.pull_request.number }}"),
                             ("repository", "${{ github.event.pull_request.head.repo.full_name }}"), ("repository", "${{ steps.x.outputs.repo }}")):
                with self.subTest(on=on.split(":")[0], key=key, val=val):
                    text = priv_wf(on, "      - uses: actions/checkout@%s\n        with:\n          %s: %s\n" % (SHA, key, val))
                    self.assertIn("ci.pr-target-checkout-expression", rules(lint(text), "error"))

    def test_the_allowed_refs_and_a_job_with_no_secrets(self):
        for on in self.TRIGGERS:
            text = priv_wf(on, "      - uses: actions/checkout@%s\n        with:\n          ref: ${{ github.sha }}\n" % SHA)
            self.assertNotIn("ci.pr-target-checkout-expression", rules(lint(text)))
            text = priv_wf(on, "      - uses: actions/checkout@%s\n        with:\n          repository: ${{ github.repository }}\n          ref: main\n" % SHA)
            self.assertNotIn("ci.pr-target-checkout-expression", rules(lint(text)))
            bare = priv_wf(on, "      - uses: actions/checkout@%s\n        with:\n          ref: ${{ inputs.ref }}\n" % SHA, perms="permissions:\n  contents: read\n")
            self.assertNotIn("ci.pr-target-checkout-expression", rules(lint(bare)))

    def test_a_tarball_fetched_and_unpacked(self):
        for fetch in ('gh api "repos/$R/tarball/$SHA" > pr.tgz', 'curl -sSL -o pr.tgz "https://api.github.com/repos/$R/tarball/$SHA"',
                      "wget https://github.com/o/r/archive/$SHA.tar.gz", "curl -L https://x/y.zip -o y.zip"):
            for on in self.TRIGGERS:
                with self.subTest(fetch=fetch, on=on.split(":")[0]):
                    unpack = "unzip y.zip" if fetch.endswith("y.zip") else "tar -xzf pr.tgz"
                    text = priv_wf(on, "      - run: |\n          %s\n          %s\n" % (fetch, unpack))
                    self.assertIn("ci.pr-code-fetch", rules(lint(text), "error"))

    def test_a_fetch_without_unpacking_or_without_privilege_is_quiet(self):
        text = priv_wf(self.TRIGGERS[0], "      - run: gh api repos/o/r/tarball/x > pr.tgz\n")
        self.assertNotIn("ci.pr-code-fetch", rules(lint(text)))
        bare = priv_wf(self.TRIGGERS[0], "      - run: |\n          gh api repos/o/r/tarball/x > pr.tgz\n          tar -xzf pr.tgz\n", perms="permissions:\n  contents: read\n")
        self.assertNotIn("ci.pr-code-fetch", rules(lint(bare)))

    def test_any_download_artifact_action_in_a_privileged_job(self):
        for uses in ("actions/download-artifact@%s" % SHA,):
            text = priv_wf(self.TRIGGERS[1], "      - uses: %s\n" % uses)
            self.assertIn("ci.artifact-from-pr-run", rules(lint(text), "error"))
        for uses in ("dawidd6/action-download-artifact@%s" % SHA, "someone/fork-download-artifact@%s" % SHA, "actions/download-artifact-v2@%s" % SHA):
            for on in self.TRIGGERS:
                with self.subTest(uses=uses, on=on.split(":")[0]):
                    text = priv_wf(on, "      - uses: %s\n" % uses)
                    self.assertIn("ci.artifact-from-pr-run", rules(lint(text), "error"))

    def test_the_official_action_names_another_run_only_with_run_id_repository_or_token(self):
        for key in ("run-id: ${{ github.event.workflow_run.id }}", "repository: o/r", "github-token: ${{ github.token }}"):
            text = priv_wf(self.TRIGGERS[0], "      - uses: actions/download-artifact@%s\n        with:\n          name: x\n          %s\n" % (SHA, key))
            self.assertIn("ci.artifact-from-pr-run", rules(lint(text), "error"), key)
        text = priv_wf(self.TRIGGERS[0], "      - uses: actions/download-artifact@%s\n        with:\n          name: x\n" % SHA)
        self.assertNotIn("ci.artifact-from-pr-run", rules(lint(text)))

    def test_no_privilege_no_error(self):
        for on in self.TRIGGERS:
            text = priv_wf(on, "      - uses: dawidd6/action-download-artifact@%s\n" % SHA, perms="permissions:\n  contents: read\n")
            self.assertNotIn("ci.artifact-from-pr-run", rules(lint(text)))


class HostileInputIsLinear(unittest.TestCase):
    """A workflow comes from a stranger's pull request. The new matching must cost time in proportion to the text, whatever the text repeats."""

    N = 30000
    LIMIT = 3.0

    def test_no_repeated_token_makes_the_scan_quadratic(self):
        import time
        n = self.N
        texts = {"slashes": "a/" * n, "path words": ("abc/" * 20 + " ") * (n // 10), "quotes": '"a" ' * n, "variables": "$X " * n, "assignments": "".join("A%d=terraform\n" % i for i in range(n // 4)),
                 "which": "$(which " * n, "command substitutions": "$(" * n, "backticks": "`" * n, "unclosed expressions": "${{ " * n, "curls": "curl " * n,
                 "gh api": "gh api " * n, "sudo": "sudo env a=b " * n, "terraform": "terraform " * n, "braces": "${A:-" * n}
        for name, text in texts.items():
            with self.subTest(name):
                t0 = time.time()
                try:
                    workflows.deploy_labels(text)
                    norm = ci_rules._norm_run(text)
                    ci_rules._INDIRECT_RE.search(norm)
                    ci_rules._tf_calls(text)
                except workflows.ScanLimit:      # more delimiters than the span pass reads: the script is unreadable, which is an answer too (D6)
                    pass
                ci_lint.injected(text)
                ci_lint._fetches_archive(text)
                self.assertLess(time.time() - t0, self.LIMIT, name)


class JobPermAlias(unittest.TestCase):
    def test_the_alias_is_gone(self):
        self.assertFalse(hasattr(ci_lint, "_job_perm_for"))
        self.assertTrue(callable(ci_lint._job_perm))


if __name__ == "__main__":
    unittest.main()
