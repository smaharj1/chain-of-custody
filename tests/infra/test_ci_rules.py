"""ci_lint's infrastructure rules (infra/ci_rules.py): the shipped templates pass, and each way of weakening a gate is caught."""
from __future__ import annotations

import os
import re
import unittest

import ci_lint
from doctorlib import workflows
from infra import ci_rules

from tests import REPO_ROOT

from .helpers import TEMPLATES

WF = os.path.join(TEMPLATES, "workflows")


def read(name):
    with open(os.path.join(WF, name), "r", encoding="utf-8") as fh:
        return fh.read()


def lint(text, name="w.yml"):
    return ci_lint.lint_text(text, name)


def rules(findings, severity=None):
    return sorted({f.rule for f in findings if severity is None or f.severity == severity})


def mutate(text, old, new, count=-1):
    assert old in text, "mutation anchor not found: %r" % old
    return text.replace(old, new, count)


class ShippedTemplates(unittest.TestCase):
    NAMES = ["infra-check.yml", "infra-plan.yml", "infra-apply.environment.yml", "infra-apply.dispatch.yml", "infra-drift.yml"]

    def test_the_directory_holds_exactly_these_workflows(self):
        self.assertEqual(sorted(os.listdir(WF)), sorted(self.NAMES))

    def test_no_errors_and_nothing_unparsed(self):
        for n in self.NAMES:
            with self.subTest(n):
                f = lint(read(n), n)
                self.assertEqual([str(x) for x in f if x.severity == "error"], [])
                self.assertNotIn("ci.unparsed", rules(f))

    def test_the_only_warnings_are_the_floating_action_tags_the_install_step_pins(self):
        for n in self.NAMES:
            with self.subTest(n):
                self.assertEqual(rules(lint(read(n), n), "warn"), ["ci.floating-action-tag"])

    def test_every_action_tag_says_to_pin_it(self):
        for n in self.NAMES:
            for line in read(n).splitlines():
                if re.search(r"^\s*(?:- )?uses: [\w./-]+@v\d", line):
                    with self.subTest(n=n, line=line.strip()):
                        self.assertIn("# pin to SHA", line)

    def test_the_cli_exits_zero_on_the_directory(self):
        findings = ci_lint.lint_paths([WF], None, REPO_ROOT)
        self.assertEqual([f for f in findings if f.severity == "error"], [])

    def test_no_template_uses_a_stored_cloud_credential(self):
        for n in self.NAMES:
            text = read(n)
            self.assertNotRegex(text, r"secrets\.(AWS|ARM|GOOGLE|TF_|PULUMI)", n)
            self.assertNotIn("aws-access-key-id", text)
            self.assertNotIn("secrets: inherit", text)

    def test_no_template_runs_terraform_apply_outside_the_two_apply_workflows(self):
        for n in ("infra-check.yml", "infra-plan.yml", "infra-drift.yml"):
            self.assertNotRegex(read(n), r"\bterraform[^\n]*\b(apply|destroy)\b(?!-)", n) if n != "infra-drift.yml" else None
            for line in read(n).splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotRegex(line, r"terraform\b[^\n]*\s(apply|destroy)\b", "%s: %s" % (n, line))


class OtherWorkflowsAreUntouched(unittest.TestCase):
    def test_label_guard_and_the_phase_1_fixtures_get_no_infra_findings(self):
        paths = [os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows", "label-guard.yml")]
        fx = os.path.join(REPO_ROOT, "tests", "doctor", "fixtures", "workflows")
        paths += [os.path.join(fx, f) for f in sorted(os.listdir(fx))]
        for f in ci_lint.lint_paths(paths, None, REPO_ROOT):
            self.assertFalse(f.rule.startswith("infra."), str(f))

    def test_an_application_deploy_workflow_gets_no_infra_findings(self):
        text = "name: app\non:\n  push:\n    branches: [main]\npermissions:\n  contents: read\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm test\n      - run: npm publish\n"
        self.assertFalse([f for f in lint(text) if f.rule.startswith("infra.")])


ENV = read("infra-apply.environment.yml")
DISPATCH = read("infra-apply.dispatch.yml")
PLAN = read("infra-plan.yml")
CHECK = read("infra-check.yml")
DRIFT = read("infra-drift.yml")

# (name, template text, mutated text) -> the rule that must now fire as an error (or warning for WARN_ROWS).
ERROR_ROWS = [
    # --- the Environment variant ---
    ("env: apply without an Environment", mutate(ENV, "    environment: production\n", ""), "infra.apply-ungated"),
    ("env: a schedule can start the apply", mutate(ENV, "on:\n  push:\n    branches: [main]\n", "on:\n  push:\n    branches: [main]\n  schedule:\n    - cron: \"0 3 * * *\"\n"), "infra.apply-trigger"),
    ("env: any branch can start the apply", mutate(ENV, "branches: [main]", "branches: [\"**\"]"), "infra.apply-trigger"),
    ("env: a tag can start the apply", mutate(ENV, "    branches: [main]\n", "    branches: [main]\n    tags: [\"v*\"]\n"), "infra.apply-trigger"),
    ("env: push with no branch filter", mutate(ENV, "  push:\n    branches: [main]\n", "  push:\n"), "infra.apply-trigger"),
    ("env: a pull request can start the apply", mutate(ENV, "on:\n  push:\n    branches: [main]\n", "on:\n  push:\n    branches: [main]\n  pull_request:\n"), "infra.apply-trigger"),
    ("env: a comment can start the apply", mutate(ENV, "on:\n  push:\n    branches: [main]\n", "on:\n  push:\n    branches: [main]\n  issue_comment:\n    types: [created]\n"), "infra.apply-trigger"),
    ("env: repository_dispatch can start the apply", mutate(ENV, "on:\n  push:\n    branches: [main]\n", "on:\n  push:\n    branches: [main]\n  repository_dispatch:\n"), "infra.apply-trigger"),
    ("env: auto-approve with no saved plan", mutate(ENV, "apply -input=false tfplan", "apply -auto-approve"), "infra.apply-without-saved-plan"),
    ("env: apply that plans again", mutate(ENV, "apply -input=false tfplan", "apply -input=false"), "infra.apply-without-saved-plan"),
    ("env: apply with only variables", mutate(ENV, "apply -input=false tfplan", "apply -var env=prod"), "infra.apply-without-saved-plan"),
    ("env: destroy", mutate(ENV, "apply -input=false tfplan", "destroy -auto-approve"), "infra.apply-without-saved-plan"),
    ("env: no hash check", mutate(ENV, "gate.py verify-hash", "gate.py hash-file"), "infra.apply-plan-hash-unverified"),
    ("env: no main check", mutate(ENV, "gate.py base-is-main", "gate.py hash-file"), "infra.apply-main-sha-unchecked"),
    ("env: lock off on the plan that is applied", mutate(ENV, "plan -lock=true", "plan -lock=false"), "infra.apply-flags"),
    ("env: refresh off on the plan that is applied", mutate(ENV, "-refresh=true", "-refresh=false"), "infra.apply-flags"),
    ("env: lock off on the apply", mutate(ENV, "apply -input=false tfplan", "apply -lock=false tfplan"), "infra.apply-flags"),
    ("env: plan inside the apply job", mutate(ENV, "      - name: terraform apply of the saved plan\n", "      - run: terraform plan -out=tfplan\n      - name: terraform apply of the saved plan\n"), "infra.apply-same-job-as-plan"),
    ("env: the apply checks out a ref chosen elsewhere", mutate(ENV, "      - name: Checkout the planned commit\n        uses: actions/checkout@v4 # pin to SHA\n        with:\n", "      - name: Checkout the planned commit\n        uses: actions/checkout@v4 # pin to SHA\n        with:\n          ref: ${{ github.head_ref }}\n"), "infra.apply-ref-input"),
    ("env: a stored cloud key", mutate(ENV, "      - name: terraform init\n        run: terraform -chdir=\"$TF_ROOT\" init -lockfile=readonly\n      - name: terraform apply", "      - name: terraform init\n        env:\n          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}\n        run: terraform -chdir=\"$TF_ROOT\" init -lockfile=readonly\n      - name: terraform apply"), "infra.static-credentials"),
    ("env: a workflow input in a script", mutate(ENV, "      - name: terraform apply of the saved plan\n        run: terraform -chdir=\"$TF_ROOT\" apply -input=false tfplan", "      - name: terraform apply of the saved plan\n        run: echo ${{ inputs.who }} && terraform -chdir=\"$TF_ROOT\" apply -input=false tfplan"), "infra.input-in-run"),
    # --- the dispatch variant ---
    ("dispatch: no actor check", mutate(DISPATCH, "gate.py authorize", "gate.py hash-file"), "infra.apply-ungated"),
    ("dispatch: no main ref check", mutate(mutate(DISPATCH, "gate.py require-main-ref", "gate.py hash-file"), "    if: ${{ github.ref == 'refs/heads/main' }}\n", ""), "infra.dispatch-ref-unchecked"),
    ("dispatch: no digest comparison", mutate(DISPATCH, "gate.py plan-digest", "gate.py hash-file"), "infra.apply-digest-unchecked"),
    ("dispatch: a push can start it", mutate(DISPATCH, "on:\n  workflow_dispatch:\n", "on:\n  push:\n    branches: [feature/x]\n  workflow_dispatch:\n"), "infra.apply-trigger"),
    ("dispatch: a schedule can start it", mutate(DISPATCH, "on:\n  workflow_dispatch:\n", "on:\n  schedule:\n    - cron: \"0 3 * * *\"\n  workflow_dispatch:\n"), "infra.apply-trigger"),
    ("dispatch: a pull request can start it", mutate(DISPATCH, "on:\n  workflow_dispatch:\n", "on:\n  pull_request:\n  workflow_dispatch:\n"), "infra.apply-trigger"),
    ("dispatch: a dispatch input in a script", mutate(DISPATCH, "          MODE: ${{ inputs.mode }}\n          APPROVED_DIGEST: ${{ inputs.approved_digest }}\n        run: |\n          set -eu\n", "          MODE: ${{ inputs.mode }}\n        run: |\n          set -eu\n          echo ${{ inputs.approved_digest }}\n"), "infra.input-in-run"),
    ("dispatch: no hash check", mutate(DISPATCH, "gate.py verify-hash", "gate.py hash-file"), "infra.apply-plan-hash-unverified"),
    ("dispatch: no main-sha check", mutate(DISPATCH, "gate.py base-is-main", "gate.py hash-file"), "infra.apply-main-sha-unchecked"),
    ("dispatch: apply with an Environment-free auto-approve", mutate(DISPATCH, "apply -input=false tfplan", "apply -auto-approve"), "infra.apply-without-saved-plan"),
    ("env: gate step that cannot fail the job", mutate(ENV, "      - name: A listed approver approved this run in the Environment\n", "      - name: A listed approver approved this run in the Environment\n        continue-on-error: true\n"), "infra.gate-bypassable"),
    ("env: hash check followed by || true", mutate(ENV, '--sha256 "$EXPECTED"', '--sha256 "$EXPECTED" || true'), "infra.gate-bypassable"),
    ("env: hash check piped to tee", mutate(ENV, '--sha256 "$EXPECTED"', '--sha256 "$EXPECTED" | tee check.log'), "infra.gate-bypassable"),
    ("env: set +e before the checks", mutate(ENV, "          set -eu\n          main_sha=", "          set +e\n          main_sha="), "infra.gate-bypassable"),
    ("env: the plan job may fail without stopping the apply", mutate(ENV, "  plan:\n    runs-on: ubuntu-latest\n", "  plan:\n    continue-on-error: true\n    runs-on: ubuntu-latest\n"), "infra.gate-bypassable"),
    ("env: the apply runs after a failed plan", mutate(ENV, "    needs: plan\n    runs-on: ubuntu-latest\n    timeout-minutes: 60\n", "    needs: plan\n    if: ${{ always() }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 60\n"), "infra.gate-bypassable"),
    ("env: the apply runs when the plan failed", mutate(ENV, "    needs: plan\n    runs-on: ubuntu-latest\n    timeout-minutes: 60\n", "    needs: plan\n    if: ${{ failure() || success() }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 60\n"), "infra.gate-bypassable"),
    ("env: the Environment is chosen at run time", mutate(ENV, "    environment: production\n", "    environment: ${{ vars.APPLY_ENV }}\n"), "infra.apply-environment-dynamic"),
    ("dispatch: the actor check can fail quietly", mutate(DISPATCH, '--approvers "$APPROVERS" --bot "$BOT_LOGIN"', '--approvers "$APPROVERS" --bot "$BOT_LOGIN" || echo skipped'), "infra.gate-bypassable"),
    ("dispatch: the authorize job may fail without stopping the apply", mutate(DISPATCH, "  authorize:\n", "  authorize:\n    continue-on-error: true\n"), "infra.gate-bypassable"),
    # --- the preview plan ---
    ("plan: the human gate is gone", mutate(mutate(PLAN, "github.event.label.name == 'plan-ok'", "github.event.label.name == 'x'"), "gate.py authorize", "gate.py hash-file"), "infra.plan-ungated"),
    ("plan: no approver check", mutate(PLAN, "gate.py authorize", "gate.py hash-file"), "infra.plan-ungated"),
    ("plan: the label is not required", mutate(PLAN, "github.event.action == 'labeled' && github.event.label.name == 'plan-ok'", "github.event.action == 'labeled'"), "infra.plan-ungated"),
    ("plan: plan job detached from the checks", mutate(PLAN, "    needs: [authorize, fetch]\n", ""), "infra.plan-ungated"),
    ("plan: no policy before init in the plan job", mutate(mutate(PLAN, "source_policy.py --root pr --base base --policy base/.claude/infra-policy.json --post-init", "true"), "source_policy.py --root pr --base base --policy base/.claude/infra-policy.json", "true"), "infra.policy-after-init"),
    ("plan: policy can fail quietly", mutate(PLAN, "      - name: Source policy, before terraform runs\n        run:", "      - name: Source policy, before terraform runs\n        continue-on-error: true\n        run:"), "infra.policy-bypassable"),
    ("plan: policy with || true", mutate(PLAN, "--policy base/.claude/infra-policy.json\n      - name: Install terraform", "--policy base/.claude/infra-policy.json || true\n      - name: Install terraform"), "infra.policy-bypassable"),
    ("plan: a plan on a plain pull_request event with a credential", mutate(PLAN, "on:\n  pull_request_target:\n    types: [labeled, synchronize]\n", "on:\n  pull_request:\n  pull_request_target:\n    types: [labeled, synchronize]\n"), "infra.plan-ungated"),
    ("plan: a comment can start it", mutate(PLAN, "on:\n  pull_request_target:\n", "on:\n  issue_comment:\n    types: [created]\n  pull_request_target:\n"), "infra.plan-ungated"),
    ("plan: apply smuggled into the preview", mutate(PLAN, "          terraform -chdir=\"pr/$TF_ROOT\" show -json tfplan > plan.json\n", "          terraform -chdir=\"pr/$TF_ROOT\" show -json tfplan > plan.json\n          terraform -chdir=\"pr/$TF_ROOT\" apply tfplan\n"), "infra.apply-trigger"),
    # --- the source check ---
    ("check: init before the policy", mutate(CHECK, "      - name: Source policy, before terraform runs\n        run: python3 base/.claude/scripts/infra/source_policy.py --root pr --base base --policy base/.claude/infra-policy.json $INITIAL_FLAG\n", ""), "infra.policy-after-init"),
    ("check: the policy comes from the PR's own tree", mutate(CHECK, "python3 base/.claude/scripts/infra/source_policy.py --root pr --base base --policy base/.claude/infra-policy.json $INITIAL_FLAG\n", "python3 pr/.claude/scripts/infra/source_policy.py --root pr --base base --policy base/.claude/infra-policy.json $INITIAL_FLAG\n"), "infra.policy-from-pr"),
    ("check: the policy from the workspace root where the PR is checked out", mutate(mutate(CHECK, "          path: pr\n", "          path: .\n"), "python3 base/.claude/scripts/infra/source_policy.py --root pr --base base --policy base/.claude/infra-policy.json $INITIAL_FLAG\n", "python3 .claude/scripts/infra/source_policy.py --root . --base base --policy base/.claude/infra-policy.json $INITIAL_FLAG\n"), "infra.policy-from-pr"),
    ("check: an OIDC token next to PR code", mutate(CHECK, "permissions:\n  contents: read\n", "permissions:\n  contents: read\n  id-token: write\n"), "ci.oidc-with-pr-code"),
    ("check: a secret in the pull request job", mutate(CHECK, "      - name: terraform validate\n", "      - name: terraform validate\n        env:\n          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"), "ci.secrets-in-pr-job"),
    ("check: a stored cloud credential", mutate(CHECK, "      - name: terraform validate\n", "      - name: terraform validate\n        env:\n          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}\n"), "infra.static-credentials"),
    # --- drift ---
    ("drift: a scheduled apply", mutate(DRIFT, "      - name: Open or update the drift issue\n", "      - run: terraform apply tfplan\n      - name: Open or update the drift issue\n"), "infra.apply-trigger"),
    ("drift: scheduled destroy", mutate(DRIFT, "      - name: Open or update the drift issue\n", "      - run: terraform destroy -auto-approve\n      - name: Open or update the drift issue\n"), "infra.apply-trigger"),
    ("drift: a scheduled cdk deploy", mutate(DRIFT, "      - name: Open or update the drift issue\n", "      - run: npx cdk deploy --require-approval never\n      - name: Open or update the drift issue\n"), "infra.apply-trigger"),
    ("drift: a scheduled pulumi up", mutate(DRIFT, "      - name: Open or update the drift issue\n", "      - run: pulumi up --yes\n      - name: Open or update the drift issue\n"), "infra.apply-trigger"),
    ("drift: a scheduled kubectl apply", mutate(DRIFT, "      - name: Open or update the drift issue\n", "      - run: kubectl apply -f k8s/\n      - name: Open or update the drift issue\n"), "infra.apply-trigger"),
]

WARN_ROWS = [
    ("env: no approval check", mutate(ENV, "gate.py require-run-approval", "gate.py hash-file"), "infra.environment-approval-unverified"),
    ("env: no concurrency", mutate(ENV, "concurrency:\n  group: infra-apply\n  cancel-in-progress: false\n\n", ""), "infra.apply-concurrency"),
    ("dispatch: no concurrency", mutate(DISPATCH, "concurrency:\n  group: infra-apply\n  cancel-in-progress: false\n\n", ""), "infra.apply-concurrency"),
    ("plan: a computed path warning is not here but a pinned-by-tag action is", PLAN, "ci.floating-action-tag"),
    ("env: the command hidden behind a variable", mutate(ENV, "      - name: terraform apply of the saved plan\n        run: terraform -chdir=\"$TF_ROOT\" apply -input=false tfplan", "      - name: terraform apply of the saved plan\n        run: |\n          T=terraform\n          $T apply -auto-approve\n          terraform -chdir=\"$TF_ROOT\" apply -input=false tfplan"), "infra.apply-indirect"),
]


class Mutations(unittest.TestCase):
    def test_the_error_rows(self):
        for name, text, rule in ERROR_ROWS:
            with self.subTest(name):
                f = lint(text)
                self.assertIn(rule, rules(f, "error"), "%s: got %s" % (name, [str(x) for x in f if x.severity == "error"]))

    def test_the_warning_rows(self):
        for name, text, rule in WARN_ROWS:
            with self.subTest(name):
                self.assertIn(rule, rules(lint(text), "warn"))

    def test_every_template_text_unmutated_has_no_error(self):
        for text in (ENV, DISPATCH, PLAN, CHECK, DRIFT):
            self.assertEqual(rules(lint(text), "error"), [])

    def test_at_least_fifty_error_rows(self):
        self.assertGreaterEqual(len(ERROR_ROWS), 50)


class Helpers(unittest.TestCase):
    def args(self, line):
        calls = ci_rules._tf_calls(line)
        return calls[0][2] if calls else None

    def test_saved_plan_argument_detection(self):
        yes = ["terraform apply tfplan", "terraform apply -auto-approve tfplan", 'terraform apply -input=false "$PLAN"', "terraform apply -parallelism 5 tfplan",
               "tofu apply plan.out", "terraform -chdir=infra apply tfplan", "terraform apply \\\n  -input=false \\\n  tfplan", "terraform apply -lock-timeout=60s tfplan"]
        no = ["terraform apply", "terraform apply -auto-approve", "terraform apply -var env=prod", "terraform apply -var-file x.tfvars", "terraform apply -target aws_x.y",
              "terraform apply -replace aws_x.y", "terraform apply -var-file=prod.tfvars -auto-approve", "tofu destroy -auto-approve", "terraform destroy"]
        for line in yes:
            with self.subTest(line=line):
                calls = [c for c in ci_rules._tf_calls(line) if c[1] in ("apply", "destroy")]
                self.assertTrue(calls and ci_rules._has_plan_arg(calls[0][2]), line)
        for line in no:
            with self.subTest(line=line):
                calls = [c for c in ci_rules._tf_calls(line) if c[1] in ("apply", "destroy")]
                self.assertTrue(calls and not ci_rules._has_plan_arg(calls[0][2]), line)

    def test_comments_and_chained_commands(self):
        self.assertEqual(ci_rules._tf_calls("# terraform apply -auto-approve\n"), [])
        calls = ci_rules._tf_calls("terraform plan -out=tfplan && terraform apply tfplan")
        self.assertEqual([c[1] for c in calls], ["plan", "apply"])

    def test_workflow_detection(self):
        def wfp(run):
            return workflows.parse("name: x\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: %s\n" % run, "x.yml")
        self.assertTrue(ci_rules.is_infra_workflow(wfp("terraform fmt")))
        self.assertTrue(ci_rules.is_infra_workflow(wfp("cdk synth")))
        self.assertTrue(ci_rules.is_infra_workflow(wfp("aws cloudformation validate-template --template-body x")))
        self.assertFalse(ci_rules.is_infra_workflow(wfp("npm test")))
        self.assertFalse(ci_rules.is_infra_workflow(wfp("echo terraformer")))


if __name__ == "__main__":
    unittest.main()
