"""Second-review fixes, package D (R-INFRA-1 to R-INFRA-6): the ways round one's lint could still be walked around, and the hostile-input cost of its regexes.

Each positive row is an input the independent reviewer used or a variant of it; each negative row is a legitimate neighbour that must stay quiet.
The shipped templates are the baseline: they are mutated, never replaced.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import ci_lint
from doctorlib import workflows
from infra import ci_rules

from tests import REPO_ROOT

from .helpers import TEMPLATES

WF = os.path.join(TEMPLATES, "workflows")
GH_WF = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")
# a generous bound: the cases below take well under a second when linear, and minutes when not
BOUND = 6.0


def read(name):
    with open(os.path.join(WF, name), "r", encoding="utf-8") as fh:
        return fh.read()


def lint(text, name="w.yml", callers=None):
    return ci_lint.lint_text(text, name, callers=callers)


def errors(findings):
    return sorted({f.rule for f in findings if f.severity == "error"})


def mutate(text, old, new):
    assert old in text, "mutation anchor not found: %r" % old
    return text.replace(old, new, 1)


def wf_text(on, steps, perms="  id-token: write\n", extra=""):
    return "name: x\non: %s\npermissions:\n%s%sjobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (on, perms, extra, steps)


CHECKOUT_SHA = "      - uses: actions/checkout@v4\n        with:\n          ref: ${{ %s }}\n      - run: make test\n"


class ReusableWorkflowsAndMergeRefs(unittest.TestCase):
    """R-INFRA-1: a callee's default checkout is its caller's commit, and `github.sha` on a pull request is the merge commit."""

    CALLER = ("name: caller\non:%s\npermissions:\n  contents: read\njobs:\n  call:\n    permissions:\n      id-token: write\n      contents: read\n"
              "    uses: ./.github/workflows/callee.yml\n")
    CALLEE = ("name: callee\non: workflow_call\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
              "      - uses: aws-actions/configure-aws-credentials@v4\n        with:\n          role-to-assume: arn:aws:iam::1:role/x\n      - run: make test\n")

    def lint_dir(self, caller_on, callee=None):
        with tempfile.TemporaryDirectory() as d:
            wfdir = os.path.join(d, ".github", "workflows")
            os.makedirs(wfdir)
            with open(os.path.join(wfdir, "caller.yml"), "w") as fh:
                fh.write(self.CALLER % caller_on)
            with open(os.path.join(wfdir, "callee.yml"), "w") as fh:
                fh.write(callee or self.CALLEE)
            return ci_lint.lint_repo(d)

    def lint_files(self, files):
        with tempfile.TemporaryDirectory() as d:
            wfdir = os.path.join(d, ".github", "workflows")
            os.makedirs(wfdir)
            for n, t in files.items():
                with open(os.path.join(wfdir, n), "w") as fh:
                    fh.write(t)
            return ci_lint.lint_repo(d)

    def callee_errors(self, files):
        return errors([f for f in self.lint_files(files) if f.file.endswith("callee.yml")])

    TOP = "name: top\non: pull_request\njobs:\n  a:\n%s    uses: ./.github/workflows/mid.yml\n"
    MID = "name: mid\non: workflow_call\n%sjobs:\n  b:\n%s    uses: ./.github/workflows/callee.yml\n"
    GRANT = "    permissions:\n      id-token: write\n"

    def test_the_token_comes_down_a_chain_of_callers_that_set_no_permissions(self):
        # top grants id-token to mid; mid sets nothing, so GitHub passes the token on to callee, whose default checkout is the pull request
        files = {"top.yml": self.TOP % self.GRANT, "mid.yml": self.MID % ("", ""), "callee.yml": self.CALLEE}
        self.assertIn("ci.oidc-with-pr-code", self.callee_errors(files))

    def test_the_token_comes_down_three_levels(self):
        files = {"top.yml": self.TOP % self.GRANT, "mid.yml": self.MID % ("", ""), "callee.yml": self.CALLEE}
        files["mid.yml"] = files["mid.yml"].replace("./.github/workflows/callee.yml", "./.github/workflows/low.yml")
        files["low.yml"] = "name: low\non: workflow_call\njobs:\n  c:\n    uses: ./.github/workflows/callee.yml\n"
        self.assertIn("ci.oidc-with-pr-code", self.callee_errors(files))

    def test_a_chain_where_nobody_grants_the_token_is_quiet(self):
        files = {"top.yml": self.TOP % "", "mid.yml": self.MID % ("", ""), "callee.yml": self.CALLEE}
        self.assertNotIn("ci.oidc-with-pr-code", self.callee_errors(files))

    def test_a_middle_workflow_that_caps_its_permissions_stops_the_token(self):
        for where in ("workflow", "job"):
            with self.subTest(where):
                if where == "workflow":
                    mid = self.MID % ("permissions:\n  contents: read\n", "")
                else:
                    mid = self.MID % ("", "    permissions:\n      contents: read\n")
                files = {"top.yml": self.TOP % self.GRANT, "mid.yml": mid, "callee.yml": self.CALLEE}
                self.assertNotIn("ci.oidc-with-pr-code", self.callee_errors(files))

    def test_a_reusable_workflow_that_nothing_here_calls_may_pass_a_token_down(self):
        # mid has no caller in the lint set: whether a token comes down is not known, so the callee's credential action decides, as for a callee alone
        files = {"mid.yml": self.MID % ("", ""), "callee.yml": self.CALLEE}
        self.assertIn("ci.oidc-with-pr-code", self.callee_errors(files))

    def test_a_long_chain_of_files_whose_jobs_each_call_the_next_is_not_exponential(self):
        import signal
        k = 30
        files = {}
        for i in range(k):
            on = "pull_request" if i == 0 else "workflow_call"
            grant = self.GRANT if i == 0 else ""
            nxt = "callee" if i == k - 1 else "f%d" % (i + 1)
            files["f%d.yml" % i] = ("name: f%d\non: %s\njobs:\n" % (i, on)
                                    + "".join("  j%d:\n%s    uses: ./.github/workflows/%s.yml\n" % (j, grant, nxt) for j in range(2)))
        files["callee.yml"] = self.CALLEE

        def too_slow(*_):
            raise AssertionError("a chain of %d files took more than %ds (callers are followed one by one)" % (k, int(BOUND)))
        old = signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(int(BOUND * 2))
        t = time.time()
        try:
            got = self.callee_errors(files)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertLess(time.time() - t, BOUND)
        self.assertIn("ci.oidc-with-pr-code", got)

    def test_a_callee_with_the_pull_request_default_checkout_and_oidc_is_an_error(self):
        out = self.lint_dir("\n  pull_request:")
        self.assertIn("ci.oidc-with-pr-code", errors([f for f in out if f.file.endswith("callee.yml")]))

    def test_the_same_callee_called_only_from_main_is_quiet(self):
        out = self.lint_dir("\n  push:\n    branches: [main]")
        self.assertNotIn("ci.oidc-with-pr-code", errors(out))

    def test_a_callee_called_from_a_workflow_dispatch_only_is_quiet(self):
        out = self.lint_dir("\n  workflow_dispatch:")
        self.assertNotIn("ci.oidc-with-pr-code", errors(out))

    def test_a_caller_on_pull_request_target_gives_the_callee_the_default_branch(self):
        out = self.lint_dir("\n  pull_request_target:")
        self.assertNotIn("ci.oidc-with-pr-code", errors(out))

    def test_a_callee_that_caps_its_own_permissions_does_not_get_the_callers_id_token(self):
        callee = mutate(self.CALLEE, "jobs:\n", "permissions:\n  contents: read\njobs:\n")
        callee = mutate(callee, "      - uses: aws-actions/configure-aws-credentials@v4\n        with:\n          role-to-assume: arn:aws:iam::1:role/x\n", "")
        out = self.lint_dir("\n  pull_request:", callee)
        self.assertNotIn("ci.oidc-with-pr-code", errors(out))

    def test_a_callee_linted_alone_has_unknown_callers_and_is_read_as_a_pull_request(self):
        self.assertIn("ci.oidc-with-pr-code", errors(lint(self.CALLEE, "callee.yml")))

    def test_a_chain_of_callers_is_followed(self):
        with tempfile.TemporaryDirectory() as d:
            wfdir = os.path.join(d, ".github", "workflows")
            os.makedirs(wfdir)
            files = {"top.yml": "name: top\non: pull_request\njobs:\n  a:\n    permissions:\n      id-token: write\n    uses: ./.github/workflows/mid.yml\n",
                     "mid.yml": "name: mid\non: workflow_call\njobs:\n  b:\n    permissions:\n      id-token: write\n    uses: ./.github/workflows/callee.yml\n",
                     "callee.yml": self.CALLEE}
            for n, t in files.items():
                with open(os.path.join(wfdir, n), "w") as fh:
                    fh.write(t)
            out = ci_lint.lint_repo(d)
        self.assertIn("ci.oidc-with-pr-code", errors([f for f in out if f.file.endswith("callee.yml")]))

    def test_a_callee_does_not_loop_on_a_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            wfdir = os.path.join(d, ".github", "workflows")
            os.makedirs(wfdir)
            files = {"a.yml": "name: a\non: workflow_call\njobs:\n  x:\n    uses: ./.github/workflows/b.yml\n",
                     "b.yml": "name: b\non: workflow_call\njobs:\n  y:\n    uses: ./.github/workflows/a.yml\n"}
            for n, t in files.items():
                with open(os.path.join(wfdir, n), "w") as fh:
                    fh.write(t)
            ci_lint.lint_repo(d)

    def test_pull_request_with_the_merge_commit_sha_and_oidc_is_an_error(self):
        for expr in ("github.sha", "github.ref", "github['sha']", "github.SHA"):
            with self.subTest(expr):
                self.assertIn("ci.oidc-with-pr-code", errors(lint(wf_text("pull_request", CHECKOUT_SHA % expr))))

    def test_the_same_ref_on_pull_request_target_is_the_base_commit(self):
        for expr in ("github.sha", "github.ref", "github.event.repository.default_branch"):
            with self.subTest(expr):
                self.assertEqual(errors(lint(wf_text("pull_request_target", CHECKOUT_SHA % expr))), [])

    def test_the_base_sha_on_a_pull_request_is_still_quiet(self):
        self.assertNotIn("ci.oidc-with-pr-code", errors(lint(wf_text("pull_request", CHECKOUT_SHA % "github.event.pull_request.base.sha"))))

    def test_a_step_output_or_needs_output_ref_on_a_privileged_trigger_is_an_error(self):
        steps = "      - id: pr\n        run: echo \"sha=$(gh pr view 1 --json headRefOid -q .headRefOid)\" >> $GITHUB_OUTPUT\n" + CHECKOUT_SHA
        for expr in ("steps.pr.outputs.sha", "needs.a.outputs.sha"):
            with self.subTest(expr):
                out = errors(lint(wf_text("pull_request_target", steps % expr)))
                self.assertIn("ci.pr-target-checkout-expression", out)

    def test_a_step_output_ref_in_a_job_with_no_privilege_is_quiet(self):
        steps = "      - id: pr\n        run: echo sha=1 >> $GITHUB_OUTPUT\n" + CHECKOUT_SHA
        self.assertEqual(errors(lint(wf_text("pull_request_target", steps % "steps.pr.outputs.sha", perms="  contents: read\n"))), [])

    def test_an_issue_comment_or_issues_job_with_oidc_that_builds_an_archive_is_an_error(self):
        steps = ("      - run: |\n          curl -L https://github.com/x/y/archive/refs/heads/pr.tar.gz | tar xz\n          cd y-pr && make test\n")
        for on in ("issue_comment", "issues"):
            with self.subTest(on):
                self.assertIn("ci.pr-code-fetch", errors(lint(wf_text(on, steps))))

    def test_an_issue_comment_job_with_a_computed_checkout_ref_is_an_error(self):
        steps = "      - id: pr\n        run: echo sha=$X >> $GITHUB_OUTPUT\n" + CHECKOUT_SHA
        self.assertIn("ci.pr-target-checkout-expression", errors(lint(wf_text("issue_comment", steps % "steps.pr.outputs.sha"))))

    def test_the_shipped_comment_trigger_template_stays_clean(self):
        with open(os.path.join(GH_WF, "claude-review.yml"), "r", encoding="utf-8") as fh:
            self.assertEqual(errors(lint(fh.read(), "claude-review.yml")), [])


class EveryGateCommandIsRealOrNothing(unittest.TestCase):
    """R-INFRA-2: `echo gate.py X`, a step `if:`, `continue-on-error` and a custom `shell:` neutralise a gate; every gate command is read like `authorize`."""

    ENV = read("infra-apply.environment.yml")
    DIS = read("infra-apply.dispatch.yml")

    APPROVAL = 'python3 .claude/scripts/infra/gate.py require-run-approval --approvals-json approvals.json --environment production --approvers "$APPROVERS" --bot "$BOT_LOGIN"'
    HASH = 'run: python3 .claude/scripts/infra/gate.py verify-hash --file "$TF_ROOT/tfplan" --sha256 "$EXPECTED"'
    BASE = 'python3 .claude/scripts/infra/gate.py base-is-main --planned-sha "$PLANNED_SHA" --main-sha "$main_sha"'

    def test_the_shipped_templates_are_clean(self):
        self.assertEqual(errors(lint(self.ENV)), [])
        self.assertEqual(errors(lint(self.DIS)), [])

    def test_a_gate_that_is_only_an_echo_does_not_count(self):
        cases = [(self.ENV, self.APPROVAL, "echo gate.py require-run-approval", "infra.environment-approval-unverified"),
                 (self.ENV, self.HASH, "run: echo gate.py verify-hash", "infra.apply-plan-hash-unverified"),
                 (self.ENV, self.BASE, "echo gate.py base-is-main", "infra.apply-main-sha-unchecked")]
        for text, old, new, rule in cases:
            with self.subTest(rule):
                self.assertIn(rule, errors(lint(mutate(text, old, new))))

    def test_a_neutralised_step_does_not_count(self):
        anchors = {"infra.environment-approval-unverified": "      - name: A listed approver approved this run in the Environment\n",
                   "infra.apply-plan-hash-unverified": "      - name: The saved plan is the file that was made and reviewed\n",
                   "infra.apply-main-sha-unchecked": "      - name: The planned commit is still the tip of main\n"}
        extras = ["        if: ${{ false }}\n", "        if: ${{ failure() }}\n", "        if: false\n", "        if: ${{ github.actor != 'x' }}\n",
                  "        continue-on-error: true\n", "        continue-on-error: ${{ true }}\n", '        shell: "true {0}"\n', "        shell: python {0}\n"]
        for rule, anchor in anchors.items():
            for extra in extras:
                with self.subTest(rule=rule, extra=extra.strip()):
                    self.assertIn(rule, errors(lint(mutate(self.ENV, anchor, anchor + extra))))

    def test_a_neutralised_authorize_step_does_not_count(self):
        anchor = "      - name: Require an approver who is not a bot\n"
        for extra in ('        shell: "true {0}"\n', "        if: false\n", "        continue-on-error: true\n"):
            with self.subTest(extra.strip()):
                self.assertIn("infra.apply-ungated", errors(lint(mutate(self.DIS, anchor, anchor + extra))))

    def test_a_custom_shell_as_the_default_is_an_error(self):
        for where in ("workflow", "job"):
            with self.subTest(where):
                if where == "workflow":
                    text = mutate(self.ENV, "jobs:\n", 'defaults:\n  run:\n    shell: "true {0}"\njobs:\n')
                else:
                    text = mutate(self.ENV, "  apply:\n    needs: plan\n", '  apply:\n    defaults:\n      run:\n        shell: "true {0}"\n    needs: plan\n')
                self.assertIn("infra.gate-bypassable", errors(lint(text)))

    def test_bash_as_the_shell_is_fine(self):
        anchor = "      - name: A listed approver approved this run in the Environment\n"
        self.assertEqual(errors(lint(mutate(self.ENV, anchor, anchor + "        shell: bash\n"))), [])
        self.assertEqual(errors(lint(mutate(self.ENV, "jobs:\n", "defaults:\n  run:\n    shell: bash\njobs:\n"))), [])

    def test_a_gate_after_an_exit_or_inside_a_condition_does_not_count(self):
        for pre in ("exit 0\n          ", 'if [ "$X" = y ]; then\n            ', "[ -n \"$X\" ] &&\n          "):
            with self.subTest(pre):
                text = mutate(self.ENV, "          python3 .claude/scripts/infra/gate.py require-run-approval", "          " + pre + "python3 .claude/scripts/infra/gate.py require-run-approval")
                self.assertIn("infra.environment-approval-unverified", errors(lint(text)))

    def test_the_plan_digest_must_be_run_too(self):
        text = mutate(self.DIS, 'actual="$(python3 .claude/scripts/infra/gate.py plan-digest --plan-json "$TF_ROOT/applied.json")"', "actual=\"$(echo gate.py plan-digest)\"")
        # the dispatch variant has no Environment, so the digest check is what ties the apply to the plan a human read
        self.assertIn("infra.apply-digest-unchecked", errors(lint(text)))

    def test_a_main_ref_check_in_a_step_if_or_an_echo_does_not_count(self):
        no_job_if = mutate(self.DIS, "    if: ${{ github.ref == 'refs/heads/main' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n", "    runs-on: ubuntu-latest\n    timeout-minutes: 5\n")
        step = 'run: python3 .claude/scripts/infra/gate.py require-main-ref --ref "$GITHUB_REF"'
        self.assertEqual(errors(lint(no_job_if)), [])
        self.assertIn("infra.dispatch-ref-unchecked", errors(lint(mutate(no_job_if, step, "run: echo gate.py require-main-ref"))))
        # a job-level if that is widened with || is not a main-ref check
        widened = mutate(self.DIS, "    if: ${{ github.ref == 'refs/heads/main' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n",
                         "    if: ${{ github.ref == 'refs/heads/main' || true }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n")
        self.assertEqual(errors(lint(widened)), [])    # the step is still there
        self.assertIn("infra.dispatch-ref-unchecked", errors(lint(mutate(widened, step, "run: echo ok"))))

    def test_a_plan_ok_label_that_is_only_in_a_step_text_is_not_the_gate(self):
        plan = read("infra-plan.yml")
        self.assertEqual(errors(lint(plan)), [])
        text = mutate(plan, "github.event.label.name == 'plan-ok'", "github.event.label.name != ''")
        text = mutate(text, "      - name: Remove plan-ok after new commits\n", "      - name: Mention plan-ok\n        run: echo plan-ok\n      - name: Remove plan-ok after new commits\n")
        self.assertIn("infra.plan-ungated", errors(lint(text)))


class ExpressionInjection(unittest.TestCase):
    """R-INFRA-4: only a top-level comparison yields a boolean; an `env:` entry set from the event is the same injection."""

    def test_a_comparison_inside_a_call_is_not_a_boolean(self):
        for expr in ("format('{0}', github.event.issue.title, 1 == 1)", "format('{0}', github.event.issue.title, 1 < 2)",
                     "toJSON(github.event.issue.title) || 1 == 1", "join(github.event.issue.labels.*.name, '==')",
                     "(github.event.issue.title)", "((github.event.issue.title))", "fromJSON(github.event.issue.body)[0] ", "github.event.issue.title && 1 == 1"):
            with self.subTest(expr):
                self.assertTrue(ci_lint.injected("echo ${{ %s }}" % expr)[0], expr)

    def test_real_booleans_stay_allowed(self):
        for expr in ("github.event.issue.title == 'x'", "github.event.issue.title != ''", "contains(github.event.issue.title, 'x')",
                     "startsWith(github.event.comment.body, '/go')", "!github.event.issue.pull_request", "(github.event.issue.title == 'a')",
                     "(github.event.issue.title) == (github.event.issue.body)", "github.event.issue.number > 3", "contains(github.event.issue.title, '(') == true",
                     "github.event.issue.title == 'a''b'"):
            with self.subTest(expr):
                self.assertEqual(ci_lint.injected("echo ${{ %s }}" % expr)[0], [], expr)

    def test_a_boolean_in_one_operand_does_not_launder_another(self):
        self.assertTrue(ci_lint.injected("${{ github.event.issue.title == 'x' && github.event.issue.body }}")[0])
        self.assertTrue(ci_lint.injected("${{ contains(github.event.issue.title, 'x') && github.event.issue.body }}")[0])
        self.assertTrue(ci_lint.injected("${{ format('{0} && {1}', github.event.issue.title, 1) }}")[0])

    def test_an_env_entry_set_from_the_event_is_the_same_injection(self):
        text = ("name: x\non: issues\npermissions:\n  contents: read\nenv:\n  TITLE: ${{ github.event.issue.title }}\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo \"${{ env.TITLE }}\"\n")
        self.assertIn("ci.script-injection", errors(lint(text)))
        for where in ("job", "step"):
            with self.subTest(where):
                if where == "job":
                    t = text.replace("env:\n  TITLE: ${{ github.event.issue.title }}\njobs:\n  j:\n", "jobs:\n  j:\n    env:\n      TITLE: ${{ github.event.issue.title }}\n")
                else:
                    t = text.replace("env:\n  TITLE: ${{ github.event.issue.title }}\njobs:", "jobs:").replace(
                        "      - run: echo", "      - env:\n          TITLE: ${{ github.event.issue.title }}\n        run: echo")
                self.assertIn("ci.script-injection", errors(lint(t)))

    def test_an_env_chain_and_the_bracket_form_are_followed(self):
        text = ("name: x\non: issues\npermissions:\n  contents: read\nenv:\n  A: ${{ github.event.issue.title }}\n  B: x-${{ env.A }}\n  C: ${{ env.B }}\n"
                "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo \"${{ env['C'] }}\"\n")
        self.assertIn("ci.script-injection", errors(lint(text)))

    def test_an_env_entry_with_a_safe_value_is_quiet(self):
        text = ("name: x\non: issues\npermissions:\n  contents: read\nenv:\n  N: ${{ github.event.issue.number }}\n  R: ${{ github.repository }}\n  P: plain\n"
                "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo \"${{ env.N }} ${{ env.R }} ${{ env.P }} ${{ env.UNKNOWN }}\"\n")
        self.assertNotIn("ci.script-injection", errors(lint(text)))

    def test_passing_the_env_entry_through_the_shell_is_fine(self):
        text = ("name: x\non: issues\npermissions:\n  contents: read\nenv:\n  TITLE: ${{ github.event.issue.title }}\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo \"$TITLE\"\n")
        self.assertEqual(errors(lint(text)), [])

    def test_tainted_env_is_linear_in_a_long_chain(self):
        env = {"E0": "${{ github.event.issue.title }}"}
        for i in range(1, 4000):
            env["E%d" % i] = "${{ env.E%d }}" % (i - 1)
        t = time.time()
        got = ci_lint.tainted_env([env])
        self.assertEqual(len(got), 4000)
        self.assertLess(time.time() - t, BOUND)


class FlagValuesAndVerbs(unittest.TestCase):
    """The flags between a tool and its verb: what is a flag, what is a value, and which words may stand in front of `apply`. Each row is read by the
    doctor's deploy-class list (`workflows.deploy_labels`) and by the lint's apply-class list (`ci_rules.apply_calls`); both must give the same answer."""

    def labels(self, cmd):
        return workflows.deploy_labels(cmd)

    def calls(self, cmd):
        return ci_rules.apply_calls(workflows.Step({"run": cmd}, 0))

    def assertHit(self, cmd, label):
        self.assertTrue(any(label in l for l in self.labels(cmd)), "doctor missed %r (%s): %r" % (cmd, label, self.labels(cmd)))
        self.assertTrue(any(label in c for c in self.calls(cmd)), "lint missed %r (%s): %r" % (cmd, label, self.calls(cmd)))

    def assertQuiet(self, cmd, label):
        self.assertFalse(any(label in l for l in self.labels(cmd)), "doctor false hit on %r" % cmd)
        self.assertFalse(any(label in c for c in self.calls(cmd)), "lint false hit on %r" % cmd)

    def test_a_flag_value_that_begins_with_the_tool_name_does_not_hide_the_verb(self):
        # the tool's name as the start of a path or a file name is an ordinary value
        for cmd, label in (("cdk --app cdk.out deploy", "cdk deploy"), ("cdk -a cdk.out deploy --all", "cdk deploy"), ("cdk --app cdk/out/app destroy", "cdk"),
                           ("cdk --app=cdk.out deploy", "cdk deploy"), ("kubectl --kubeconfig kubectl/cfg apply -f k8s/", "kubectl apply"),
                           ("kubectl --kubeconfig=kubectl/cfg apply -f k8s/", "kubectl apply"), ("helm --kubeconfig helm/cfg upgrade app ./chart", "helm upgrade"),
                           ("terragrunt run --all --working-dir terragrunt/live apply", "terragrunt apply"),
                           ("terragrunt --working-dir terragrunt/live run --all destroy", "terragrunt apply"),
                           ("sls --config sls/serverless.yml deploy", "serverless deploy"), ("sam --config-file sam/samconfig.toml deploy", "sam deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_flag_value_that_is_a_verb_or_begins_with_one_does_not_hide_the_verb(self):
        for cmd, label in (("helm --kubeconfig deploy.yaml upgrade app ./chart", "helm upgrade"), ("kubectl --kubeconfig deploy.yaml delete ns x", "kubectl apply"),
                           ("cdk -a deploy.js deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_the_tool_name_itself_as_a_value_still_finds_the_next_call(self):
        for cmd, label in (("cdk -a cdk deploy", "cdk deploy"), ("cdk --a=cdk deploy", "cdk deploy"), ("cdk -a x/cdk deploy", "cdk deploy"),
                           ("kubectl -n kubectl apply -f x", "kubectl apply"), ("terragrunt --a=terragrunt apply", "terragrunt apply")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_whitespace_and_quotes_between_a_flag_and_its_value(self):
        for cmd, label in (("kubectl -n  prod apply -f x", "kubectl apply"), ("kubectl --context\tprod apply -f x", "kubectl apply"),
                           ("kubectl --kubeconfig \\\n    prod.cfg \\\n    apply -f x", "kubectl apply"), ('kubectl --token "a b" apply -f x', "kubectl apply"),
                           ("cdk -c 'k=v w' -c a=b deploy", "cdk deploy"), ('terragrunt run --all --queue-include-dir "a b" apply', "terragrunt apply")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_verb_that_is_not_there_stays_quiet(self):
        for cmd, label in (("cdk --app cdk.out synth", "cdk deploy"), ("cdk -a cdk.out", "cdk deploy"), ("kubectl --kubeconfig kubectl/cfg get pods", "kubectl apply"),
                           ("kubectl --description 'x y' get pods", "kubectl apply"), ("helm --kubeconfig helm/cfg list", "helm upgrade"),
                           ("terragrunt run --all --working-dir terragrunt/live plan", "terragrunt apply"),
                           ("terragrunt run --all --working-dir run plan", "terragrunt apply"), ("sls --config sls/serverless.yml package", "serverless deploy")):
            with self.subTest(cmd):
                self.assertQuiet(cmd, label)

    def test_terragrunt_apply_in_every_spelling(self):
        for cmd in ("terragrunt apply", "terragrunt destroy", "terragrunt apply-all", "terragrunt destroy-all", "terragrunt run-all apply", "terragrunt run-all destroy",
                    "terragrunt run --all apply", "terragrunt run -a apply", "terragrunt run --all destroy", "terragrunt --terragrunt-non-interactive run-all destroy",
                    "terragrunt -a run apply", "terragrunt run apply", "terragrunt run -- apply", "terragrunt run --all -- apply", "terragrunt run  --  apply",
                    "terragrunt -- apply", "terragrunt stack run apply", "terragrunt stack run --all apply", "terragrunt stack run -- apply",
                    "terragrunt run-all --terragrunt-non-interactive apply", "terragrunt run --all -no-color --terragrunt-non-interactive apply -auto-approve"):
            with self.subTest(cmd):
                self.assertHit(cmd, "terragrunt apply")

    def test_terragrunt_run_as_a_directory_or_a_value_does_not_hide_apply(self):
        # `run` is a word terragrunt knows, and also a plausible directory name: either way the verb is found
        for cmd in ("terragrunt run --all --working-dir run apply", "terragrunt run --all --working-dir run/prod apply", "terragrunt --working-dir run run --all apply",
                    "terragrunt run --all --working-dir run-all apply", "terragrunt run --working-dir stack apply", "terragrunt run --all --working-dir=run apply"):
            with self.subTest(cmd):
                self.assertHit(cmd, "terragrunt apply")

    def test_terragrunt_without_apply_or_destroy_stays_quiet(self):
        for cmd in ("terragrunt plan", "terragrunt run-all plan", "terragrunt run --all plan", "terragrunt run -- plan", "terragrunt run plan", "terragrunt stack run plan",
                    "terragrunt stack generate", "terragrunt hclfmt", "terragrunt run -- terraform plan"):
            with self.subTest(cmd):
                self.assertQuiet(cmd, "terragrunt apply")

    def test_the_doctor_still_reads_a_package_name_that_ends_with_the_tool(self):
        self.assertIn("cdk deploy", self.labels("npx aws-cdk deploy"))


class HostileInputCost(unittest.TestCase):
    """R-INFRA-3: every regex the lint and the doctor run over a stranger's workflow is linear. Each shape is a function of its size in characters; it is
    run at SMALL and at four times that, and a linear pipeline takes about four times as long where a quadratic one takes sixteen (an exponential one never
    comes back, and the alarm ends it). The ceiling is generous (ten times, and never less than 0.3 s) so a slow machine does not fail it."""

    SMALL = 4000

    def run_text(self, run):
        body = "\n".join("          " + l for l in run.split("\n"))
        return ("name: x\non: pull_request_target\npermissions:\n  id-token: write\njobs:\n  j:\n    environment: prod\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - uses: aws-actions/configure-aws-credentials@v4\n      - run: |\n%s\n" % body)

    def cost(self, run):
        import signal

        def too_slow(*_):
            raise AssertionError("more than %ss on %r (a regex is not linear)" % (int(BOUND), run[:40]))
        old = signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(int(BOUND))
        t = time.time()
        try:
            lint(self.run_text(run))
            workflows.deploy_labels(run)
            workflows.scan_text(run)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        return time.time() - t

    def linear(self, make):
        """`make(n)` is a run: script of about n characters."""
        small, big = self.cost(make(self.SMALL)), self.cost(make(4 * self.SMALL))
        self.assertLess(big, max(10 * small, 0.3), "%.3fs at %d characters, %.3fs at %d: not linear (%r)" % (small, self.SMALL, big, 4 * self.SMALL, make(60)))

    def test_a_run_of_repeated_flags_after_each_deploy_tool(self):
        for tool in ("terragrunt", "terraform", "tofu", "pulumi", "cdk", "cdktf", "sam", "copilot", "eb", "serverless", "kubectl", "helm", "npm", "sst", "docker"):
            with self.subTest(tool):
                self.linear(lambda n: tool + " " + "-a " * (n // 3) + "zzz")

    def test_other_shapes_of_flag_runs(self):
        for make in (lambda n: "terragrunt -a " * (n // 14), lambda n: "terragrunt -a terragrunt " * (n // 24), lambda n: "cdk -a cdk " * (n // 11),
                     lambda n: "kubectl -n " * (n // 11), lambda n: "terraform " + "-a b " * (n // 5) + "zzz", lambda n: "terraform " + "--a=b " * (n // 6) + "zzz",
                     lambda n: "helm " + "--a " * (n // 4) + "zzz", lambda n: "terragrunt run " + "-a " * (n // 3), lambda n: "terragrunt " + "--a " * (n // 4) + "zzz"):
            with self.subTest(make(30)):
                self.linear(make)

    def test_a_run_of_flags_whose_values_are_the_start_of_the_next_call(self):
        # a match may start at the tool's name wherever a word boundary allows (`=cdk`, `x/cdk`, `a.cdk`, `--cdk`); a flag run that swallowed such a token
        # would be scanned again from there, once per token (5,000 of these took seconds, 10,000 took ten)
        for make in (lambda n: "cdk" + " --a=cdk" * (n // 8), lambda n: "cdk" + " -a x/cdk" * (n // 9), lambda n: "cdk" + " -a x/cdk-app" * (n // 13),
                     lambda n: "cdk" + " -a a.cdk" * (n // 9), lambda n: "cdk" + " -a a-cdk" * (n // 9), lambda n: "cdk" + " -a ${D}/cdk" * (n // 12),
                     lambda n: "cdk" + " --cdk" * (n // 6), lambda n: "cdk" + " -a cdk.out" * (n // 11), lambda n: "kubectl" + " --a=kubectl" * (n // 12),
                     lambda n: "kubectl" + " --kubeconfig kubectl/cfg" * (n // 26), lambda n: "terragrunt" + " --a=terragrunt" * (n // 15),
                     lambda n: "terragrunt" + " -a x/terragrunt" * (n // 15), lambda n: "terragrunt run --all" + " --working-dir run" * (n // 18),
                     lambda n: "terragrunt run " + "-- " * (n // 3), lambda n: "terragrunt " + "run " * (n // 4), lambda n: "terragrunt stack run " * (n // 21),
                     lambda n: "terragrunt run-all " * (n // 19), lambda n: "helm" + " --a=helm" * (n // 9), lambda n: "cdk -a \"x " * (n // 8),
                     lambda n: "cdk -a \"x cdk " * (n // 12), lambda n: "cdk -a" + " " * n + "x" + " " * n + "zzz", lambda n: "cdk" + ' --a="x"' * (n // 9),
                     lambda n: "cdk" + ' -a "x y"' * (n // 9), lambda n: "cdk" + " --a='x y'" * (n // 11), lambda n: ('cdk -a "' + "cdk -b " * 20 + '" ') * (n // 150)):
            with self.subTest(make(40)[:30]):
                self.linear(make)

    def test_a_long_flag_run_is_still_read_as_an_apply(self):
        # no cap on the number of flags: nine (or ninety) flags in front of the verb do not hide it
        for n in (1, 9, 40, 400):
            with self.subTest(n):
                text = "terraform " + "-no-color " * n + "apply -auto-approve"
                self.assertIn("terraform apply", workflows.deploy_labels(text))
                self.assertTrue(ci_rules.apply_calls(workflows.Step({"run": "cdk " + "-v " * n + "deploy"}, 0)))
                self.assertTrue(ci_rules.apply_calls(workflows.Step({"run": "terragrunt " + "-a " * n + "apply"}, 0)) or n == 0)

    def test_a_flag_value_that_is_the_tool_name_does_not_hide_the_next_call(self):
        self.assertTrue(ci_rules.apply_calls(workflows.Step({"run": "cdk -a cdk deploy"}, 0)))
        self.assertIn("cdk deploy", workflows.deploy_labels("cdk --app cdk deploy"))
        self.assertIn("terraform apply", workflows.deploy_labels("terraform -chdir=infra apply tfplan"))
        self.assertIn("kubectl apply", workflows.deploy_labels("kubectl -n prod --context x apply -f y"))

    def test_gate_and_git_lines_that_repeat_a_word(self):
        for make in (lambda n: "gate.py authorize " * (n // 18), lambda n: "git fetch " * (n // 10), lambda n: "git checkout " * (n // 13),
                     lambda n: "curl " * (n // 5), lambda n: "rsync " * (n // 6), lambda n: "python3 gate.py authorize " + "/a" * (n // 2),
                     lambda n: "a\\" * (n // 2) + "b", lambda n: "'a'" * (n // 3), lambda n: "source_policy.py" * (n // 16),
                     lambda n: "x" * n + "source_policy.py", lambda n: "sha256sum -c " * (n // 13), lambda n: "$(which " * (n // 12) + "terraform" + ")" * (n // 12)):
            with self.subTest(make(40)[:30]):
                self.linear(make)

    def test_expressions_that_nest_or_repeat(self):
        for make in (lambda n: "${{ " + "(" * (n // 4) + "a" + ")" * (n // 4) + " }}", lambda n: "${{ " + "!" * (n // 2) + "a }}",
                     lambda n: "${{ " + "(a == b) && " * (n // 12) + " x }}", lambda n: "${{ github.event[" * (n // 17),
                     lambda n: "${{ github.event" + "[a]" * (n // 3) + " }}", lambda n: "${{ secrets[ " * (n // 12), lambda n: "${{ env.A }} " * (n // 13),
                     lambda n: "${{ 'a' " * (n // 8), lambda n: "${{ '" + "''" * (n // 4) + " }}"):
            with self.subTest(make(40)[:30]):
                self.linear(make)


    def test_a_long_workflow_of_many_steps_and_env_entries(self):
        steps = "".join("      - run: echo ${{ github.event.issue.number }} && terraform fmt\n        env:\n          K%d: ${{ env.K%d }}\n" % (i, i - 1) for i in range(1, 800))
        env = "".join("  K%d: ${{ github.event.issue.title }}\n" % i for i in range(800))
        text = "name: x\non: issues\npermissions:\n  contents: read\nenv:\n%sjobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n%s" % (env, steps)
        t = time.time()
        lint(text)
        self.assertLess(time.time() - t, BOUND * 3)


class SecretsAndCredentialedForms(unittest.TestCase):
    """R-INFRA-6: `secrets[format(...)]` and `secrets['AWS_...']` are secrets too."""

    def pr(self, expr):
        return wf_text("pull_request", "      - run: echo hi\n        env:\n          K: ${{ %s }}\n" % expr, perms="  contents: read\n")

    def test_a_computed_secret_name_in_a_pull_request_job_is_an_error(self):
        for expr in ("secrets[format('A{0}', 'B')]", "secrets[ format('A{0}', 'B') ]", "secrets[env.NAME]", "toJSON(secrets)", "secrets['API']", "secrets.API"):
            with self.subTest(expr):
                self.assertIn("ci.secrets-in-pr-job", errors(lint(self.pr(expr))))

    def test_github_token_is_still_not_a_secret(self):
        for expr in ("secrets.GITHUB_TOKEN", "secrets['GITHUB_TOKEN']"):
            with self.subTest(expr):
                self.assertNotIn("ci.secrets-in-pr-job", errors(lint(self.pr(expr))))

    def test_stored_cloud_keys_are_found_in_every_spelling(self):
        for expr in ("secrets.AWS_ACCESS_KEY_ID", "secrets['AWS_ACCESS_KEY_ID']", 'secrets["AWS_SECRET_ACCESS_KEY"]', "secrets[format('AWS_{0}', 'ACCESS_KEY_ID')]",
                     "toJSON(secrets)"):
            with self.subTest(expr):
                text = ("name: x\non: workflow_dispatch\npermissions:\n  contents: read\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: terraform plan\n"
                        "        env:\n          K: ${{ %s }}\n" % expr)
                self.assertIn("infra.static-credentials", errors(lint(text)))

    def test_a_secret_that_is_not_a_cloud_key_is_not_a_static_credential(self):
        text = ("name: x\non: workflow_dispatch\npermissions:\n  contents: read\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: terraform fmt\n"
                "        env:\n          K: ${{ secrets['SLACK_WEBHOOK'] }}\n")
        self.assertNotIn("infra.static-credentials", errors(lint(text)))


class ReviewPostIsRedacted(unittest.TestCase):
    """R-INFRA-6: `claude-review.yml` posts model output through kitlib.redact, and posts nothing if the redactor cannot run."""

    def script(self):
        with open(os.path.join(GH_WF, "claude-review.yml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        return text[text.index("      - name: Post the review"):]

    def test_the_post_step_runs_the_redactor_before_head_and_the_post(self):
        s = self.script()
        self.assertIn("kitlib import redact", s)
        self.assertLess(s.index("redact.redact"), s.index("head -c 60000"))
        self.assertLess(s.index("head -c 60000"), s.index("gh pr comment"))
        self.assertNotIn("jq -r '.result // \"The review returned no result.\"' .review/out.json | head", s)

    def test_the_redactor_one_liner_redacts(self):
        line = [l for l in self.script().split("\n") if "kitlib import redact" in l][0].strip()
        code = line.split("python3 -I -c '", 1)[1].rsplit("'", 1)[0]
        p = subprocess.run([sys.executable, "-I", "-c", code], input="key AKIAABCDEFGHIJKLMNOP and password=hunter2hunter2\n", cwd=REPO_ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", p.stdout)
        self.assertNotIn("hunter2hunter2", p.stdout)
        self.assertIn("[REDACTED", p.stdout)


class PlanReadonlyPolicyNarrowed(unittest.TestCase):
    def setUp(self):
        path = os.path.join(REPO_ROOT, ".claude", "templates", "security", "iam", "plan-readonly.policy.json")
        with open(path, "r", encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def allow_actions(self):
        return [a for st in self.doc["Statement"] if st["Effect"] == "Allow" for a in st["Action"]]

    def test_no_wildcard_get_on_lambda_and_only_the_named_reads(self):
        acts = self.allow_actions()
        self.assertNotIn("lambda:Get*", acts)
        self.assertIn("lambda:GetFunction", acts)           # the provider cannot read a function without it
        self.assertFalse([a for a in acts if a.startswith("lambda:") and a.endswith("*") and a != "lambda:List*"])
        self.assertFalse([a for a in acts if a.startswith("lambda:") and any(w in a for w in ("Invoke", "Update", "Create", "Delete", "Put", "Add", "Remove")) and a != "lambda:GetFunctionEventInvokeConfig"])

    def test_api_key_values_are_denied(self):
        st = [s for s in self.doc["Statement"] if s.get("Sid") == "NeverReadApiKeyValues"][0]
        self.assertEqual((st["Effect"], st["Action"]), ("Deny", ["apigateway:GET"]))
        self.assertIn("arn:aws:apigateway:*::/apikeys", st["Resource"])
        self.assertIn("arn:aws:apigateway:*::/apikeys/*", st["Resource"])
        self.assertNotIn("Condition", st)

    def test_usage_plan_keys_are_denied_too(self):
        # `GET /usageplans/{id}/keys` (GetUsagePlanKeys) and `.../keys/{keyId}` return each key's `value`, and `apigateway:GET` is allowed on `*`
        st = [s for s in self.doc["Statement"] if s.get("Sid") == "NeverReadApiKeyValues"][0]
        self.assertIn("arn:aws:apigateway:*::/usageplans/*/keys", st["Resource"])
        self.assertIn("arn:aws:apigateway:*::/usageplans/*/keys/*", st["Resource"])
        self.assertIn("apigateway:GET", self.allow_actions())

    def test_the_policy_names_every_path_through_which_a_key_value_is_returned(self):
        st = [s for s in self.doc["Statement"] if s.get("Sid") == "NeverReadApiKeyValues"][0]
        paths = set(r.split("::", 1)[1] for r in st["Resource"])
        self.assertTrue({"/apikeys", "/apikeys/*", "/usageplans/*/keys", "/usageplans/*/keys/*"} <= paths, sorted(paths))

    def test_the_statement_that_reads_configuration_does_not_claim_metadata_only(self):
        self.assertNotIn("ReadMetadataOnly", json.dumps(self.doc))


DISPATCH_WF = "name: x\non:\n  workflow_dispatch:\npermissions:\n  contents: read\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n%s\n"


def infra_rules(cmd):
    """The `infra.*` rules the lint reports for one command in a dispatch workflow."""
    body = "\n".join("          " + l for l in cmd.split("\n"))
    return sorted({f.rule for f in lint(DISPATCH_WF % body) if f.rule.startswith("infra.")})


class _BothReaders(object):
    """A row is read by the doctor's deploy-class list and by the lint's apply-class list and, end to end, by `lint_text`; all three must agree."""

    labels = FlagValuesAndVerbs.labels
    calls = FlagValuesAndVerbs.calls

    def assertHit(self, cmd, label):
        self.assertTrue(any(label in l for l in self.labels(cmd)), "doctor missed %r (%s): %r" % (cmd, label, self.labels(cmd)))
        self.assertTrue(any(label in c for c in self.calls(cmd)), "lint missed %r (%s): %r" % (cmd, label, self.calls(cmd)))
        self.assertIn("infra.apply-ungated", infra_rules(cmd), "lint_text found no ungated apply in %r" % cmd)

    def assertQuiet(self, cmd, label):
        self.assertFalse(any(label in l for l in self.labels(cmd)), "doctor false hit on %r" % cmd)
        self.assertFalse(any(label in c for c in self.calls(cmd)), "lint false hit on %r" % cmd)
        self.assertNotIn("infra.apply-ungated", infra_rules(cmd), "lint_text false hit on %r" % cmd)


class ExpressionsAndSubstitutionsAsFlagValues(_BothReaders, unittest.TestCase):
    """Round 3, D1: a flag value with blanks in it (`${{ matrix.dir }}`, `$(echo x y)`) is one word to the shell and must be one word to the flag run.
    `workflows.normalize_shell` adds a copy of such lines with the blanks inside the span made into one word; the original text stays beside it."""

    def test_the_reviewers_inputs_are_ungated_applies(self):
        for cmd, label in (("terraform -chdir=${{ matrix.dir }} apply -auto-approve", "terraform apply"), ("kubectl --context ${{ matrix.ctx }} apply", "kubectl apply"),
                           ("cdk --profile ${{ matrix.p }} deploy", "cdk deploy"), ("helm --kube-context ${{ matrix.c }} upgrade x y", "helm upgrade"),
                           ("pulumi --stack ${{ matrix.s }} up", "pulumi up"), ("terragrunt --working-dir ${{ inputs.dir }} apply", "terragrunt apply"),
                           ("kubectl --namespace $(echo x y) apply", "kubectl apply")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_the_same_command_without_the_expression_gives_the_same_findings(self):
        for with_expr, plain in (("terraform -chdir=${{ matrix.dir }} apply -auto-approve", "terraform -chdir=infra apply -auto-approve"),
                                 ("kubectl --context ${{ matrix.ctx }} apply", "kubectl --context prod apply"),
                                 ("cdk --profile ${{ matrix.p }} deploy", "cdk --profile prod deploy"),
                                 ("kubectl --namespace $(echo x y) apply", "kubectl --namespace x apply")):
            with self.subTest(with_expr):
                self.assertEqual(infra_rules(with_expr), infra_rules(plain))

    def test_other_ways_to_write_a_value_with_blanks(self):
        for cmd, label in (("kubectl -n `echo x y` apply", "kubectl apply"), ("kubectl -n ${X:-a b} apply", "kubectl apply"),
                           ("kubectl --context=${{ matrix.ctx }} apply", "kubectl apply"), ("kubectl --context ${{matrix.ctx}} apply", "kubectl apply"),
                           ("kubectl --context ${{   matrix.a   ||   matrix.b   }} apply", "kubectl apply"),
                           ("kubectl --kubeconfig $(mktemp -d)/cfg apply", "kubectl apply"), ("kubectl --token \"$(cat /tmp/t | head -c 50)\" apply", "kubectl apply"),
                           ("kubectl --context $(echo $(echo a b) c) apply", "kubectl apply"), ("kubectl --context $((1 + 2)) apply", "kubectl apply"),
                           ("kubectl --context \"$(echo \"a b\")\" apply", "kubectl apply"), ("kubectl --context ${{ a }} --namespace ${{ b c }} apply", "kubectl apply"),
                           ("terragrunt run --all --working-dir ${{ inputs.dir }} apply", "terragrunt apply"),
                           ("terraform -chdir=${{ inputs.dir }} apply tfplan", "terraform apply"), ("sam --region ${{ vars.R }} deploy", "sam deploy"),
                           ("serverless --stage ${{ inputs.stage }} deploy", "serverless deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_an_expression_that_holds_closing_braces_in_a_string(self):
        # the format string of `format()` escapes a brace by doubling it, so `}}` can sit inside a string inside the expression
        for cmd in ("kubectl --context ${{ format('{{0}}', 'a') }} apply", "kubectl --context ${{ format('}}', 'a') }} apply",
                    "kubectl --context ${{ format('}} }}', 'a b') }} apply"):
            with self.subTest(cmd):
                self.assertHit(cmd, "kubectl apply")

    def test_the_command_inside_the_span_is_still_read_as_written(self):
        # collapsing must not hide a command that sits inside a quote or a substitution
        for cmd, label in (("sh -c \"kubectl apply -f x\"", "kubectl apply"), ("bash -c 'terraform apply -auto-approve'", "terraform apply"),
                           ("echo \"$(kubectl apply -f x)\"", "kubectl apply"), ("X=$(cdk deploy --all)", "cdk deploy"), ("echo `helm upgrade x y`", "helm upgrade"),
                           ("ssh host \"cd app && kubectl --context ${{ a b }} apply\"", "kubectl apply"), ("env X='a b' cdk deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_command_string_with_its_own_flag_values_is_read_one_level_down(self):
        # `sh -c "..."`, `ssh host '...'`: the string is a command, and its values hold blanks too (up to three levels of quoting)
        for cmd, label in (('ssh host "cd app && kubectl --context ${{ a b }} apply"', "kubectl apply"), ("bash -c 'cdk -c \"a b\" deploy'", "cdk deploy"),
                           ('docker run --rm img sh -c "terraform -chdir=${{ matrix.dir }} apply -auto-approve"', "terraform apply"),
                           ("sh -c 'kubectl --context $(echo a b) apply'", "kubectl apply"), ('sh -c "ssh h \'kubectl --context ${{ a b }} apply\'"', "kubectl apply"),
                           ("echo \"$(kubectl --context ${{ a b }} apply)\"", "kubectl apply"), ("x=`cdk -c 'a b' deploy`", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_the_copies_are_apart_so_that_a_tool_and_a_verb_on_different_lines_do_not_meet(self):
        text = "cdk -c 'a b'\nfoo\ndeploy -c 'c d'"
        self.assertEqual(workflows.normalize_shell(text), text + "\n\x00\n" + "cdk -c 'a\x01b'" + "\n\x00\n" + "deploy -c 'c\x01d'")
        self.assertQuiet(text, "cdk deploy")

    def test_an_expression_in_a_value_does_not_make_a_plan_an_apply(self):
        for cmd, label in (("terraform -chdir=${{ matrix.dir }} plan", "terraform apply"), ("kubectl --context ${{ matrix.ctx }} get pods", "kubectl apply"),
                           ("cdk --profile ${{ matrix.p }} synth", "cdk deploy"), ("helm --kube-context ${{ matrix.c }} list", "helm upgrade"),
                           ("echo ${{ matrix.a }} apply", "kubectl apply"), ("kubectl get pods -l 'app in (a, b)'", "kubectl apply")):
            with self.subTest(cmd):
                self.assertQuiet(cmd, label)

    def test_an_opener_with_no_closer_is_an_ordinary_character_and_the_next_form_is_read(self):
        # these are syntax errors for GitHub and the shell, so nothing runs them; the point is that nothing is swallowed, nothing raises, and a closed
        # form after the stray opener is collapsed
        for cmd in ("echo $(\nkubectl --context ${{ a b }} apply", "echo ${{\nkubectl --context $(echo a b) apply", "echo \"x\nkubectl --context ${{ a b }} apply",
                    "echo `x\nkubectl --context ${{ a b }} apply", "echo ${\nkubectl --context ${{ a b }} apply"):
            with self.subTest(cmd):
                self.assertHit(cmd, "kubectl apply")

    def test_a_deeply_nested_or_long_form_is_followed_to_a_limit_and_never_raises(self):
        deep = "kubectl --context " + "$(echo " * 30 + "a b" + ")" * 30 + " apply"
        self.assertHit(deep, "kubectl apply")
        for n in (41, 200, 5000):
            with self.subTest(n):
                text = "kubectl --context " + "$(echo " * n + "a b" + ")" * n + " apply"
                workflows.deploy_labels(text)
                ci_rules.apply_calls(workflows.Step({"run": text}, 0))

    def test_the_copy_holds_only_the_lines_that_changed_and_keeps_the_original(self):
        text = "set -e\nkubectl --context ${{ a b }} apply\necho done\nkubectl get pods"
        out = workflows.normalize_shell(text)
        self.assertEqual(out, text + "\n\x00\n" + "kubectl --context ${{\x01a\x01b\x01}} apply")
        self.assertEqual(workflows.normalize_shell("kubectl --context ${{a}} apply"), "kubectl --context ${{a}} apply")      # no blank in the span: no copy
        self.assertEqual(workflows.normalize_shell('cdk -c "a=1" deploy'), 'cdk -c "a=1" deploy')

    def test_a_span_over_several_lines_is_one_line_in_the_copy(self):
        text = "cdk -c 'a\nb' deploy"
        self.assertEqual(workflows.normalize_shell(text), text + "\n\x00\n" + "cdk -c 'a\x01b' deploy")
        self.assertHit("cdk -c 'a\nb' deploy", "cdk deploy")
        self.assertHit('cdk -c "a\n  b\n  c" -c d=e deploy', "cdk deploy")

    def test_a_tool_call_is_listed_once_though_the_text_holds_it_twice(self):
        calls = ci_rules._tf_calls("terraform -chdir=${{ a b }} apply tfplan\nterraform apply other")
        self.assertEqual([(t, s) for t, s, _ in calls].count(("terraform", "apply")), 2)      # the two different lines: one each
        self.assertEqual(len(ci_rules._tf_calls('terraform apply -var "a=b c" tfplan')), 1)
        self.assertEqual(len(ci_rules.apply_calls(workflows.Step({"run": 'terraform apply -var "a=b c" tfplan'}, 0))), 1)


class PackageRunnersAndTheStartRule(_BothReaders, unittest.TestCase):
    """Round 3, D2: `npx aws-cdk deploy` and `npx cdk@2 deploy` are `cdk deploy`, for the lint and for the doctor alike; and the doctor and the lint share one
    rule (`workflows.PROG_BEHIND`) for what may stand in front of a program name, so `x-cdk` and `foo.cdk` are a call in neither."""

    def test_the_reviewers_inputs(self):
        for cmd in ("npx aws-cdk deploy", "npx aws-cdk@2 deploy --all", "npx cdk@2 deploy", "pnpm dlx aws-cdk deploy"):
            with self.subTest(cmd):
                self.assertHit(cmd, "cdk deploy")

    def test_a_full_deploy_workflow_reports_the_same_rules_for_each_spelling(self):
        base = infra_rules("npx cdk deploy")
        self.assertIn("infra.apply-ungated", base)
        for cmd in ("npx aws-cdk deploy", "npx aws-cdk@2 deploy --all", "npx cdk@2 deploy", "pnpm dlx aws-cdk deploy", "yarn dlx aws-cdk@latest deploy",
                    "bunx aws-cdk deploy", "npx -y aws-cdk@^2.150.0 deploy", "npx --yes cdk@latest deploy", "pnpm exec aws-cdk deploy", "npm exec -- aws-cdk deploy"):
            with self.subTest(cmd):
                self.assertEqual(infra_rules(cmd), base)

    def test_other_packages_that_run_a_deploy_tool(self):
        for cmd, label in (("npx cdktf-cli deploy", "cdktf deploy"), ("pipx run aws-sam-cli deploy", "sam deploy"), ("npx serverless@3 deploy", "serverless deploy"),
                           ("npx sls@3 deploy", "serverless deploy"), ("npx pulumi@3 up", "pulumi up"), ("pnpm dlx kubectl@1 apply -f x", "kubectl apply")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_the_doctor_alone_tools_are_read_through_their_packages(self):
        for cmd, label in (("npx firebase-tools deploy", "firebase deploy"), ("npx netlify-cli deploy --prod", "netlify deploy"), ("npx sst@latest deploy", "sst deploy"),
                           ("npx wrangler@3 deploy", "wrangler deploy")):
            with self.subTest(cmd):
                self.assertTrue(any(label in l for l in self.labels(cmd)), self.labels(cmd))

    def test_a_name_that_only_ends_with_a_tool_is_not_a_call_in_either(self):
        for cmd, label in (("x-cdk deploy", "cdk deploy"), ("foo.cdk deploy", "cdk deploy"), ("my-cdk destroy", "cdk deploy"), ("npx x-aws-cdk deploy", "cdk deploy"),
                           ("npm install aws-cdk-lib", "cdk deploy"), ("npx aws-cdk-lib deploy", "cdk deploy"), ("ssh sam@host deploy", "sam deploy"),
                           ("x-kubectl apply -f y", "kubectl apply"), ("foo.helm upgrade x y", "helm upgrade"), ("my-terraform apply", "terraform apply"),
                           ("a.pulumi up", "pulumi up"), ("x-sst deploy", "sst deploy"), ("x-wrangler deploy", "wrangler deploy"), ("a.flyctl deploy", "fly deploy"),
                           ("x-vercel deploy", "vercel deploy"), ("x-firebase deploy", "firebase deploy")):
            with self.subTest(cmd):
                self.assertFalse(any(label in l for l in self.labels(cmd)), "doctor read %r as a call: %r" % (cmd, self.labels(cmd)))
                self.assertFalse(any(label in c for c in self.calls(cmd)), "lint read %r as a call: %r" % (cmd, self.calls(cmd)))
        self.assertNotIn("infra.apply-ungated", infra_rules("x-cdk deploy"))
        self.assertNotIn("infra.apply-ungated", infra_rules("foo.cdk deploy"))

    def test_a_path_or_an_expanded_directory_before_the_tool_is_the_tool(self):
        for cmd in ("./node_modules/.bin/cdk deploy", "/usr/local/bin/cdk deploy", "${DIR}/cdk deploy", "\"$DIR\"/cdk deploy", "$(pwd)/node_modules/.bin/cdk deploy",
                    "${{ github.workspace }}/node_modules/.bin/cdk deploy"):
            with self.subTest(cmd):
                self.assertTrue(any("cdk deploy" in l for l in self.labels(cmd)), "doctor: %r" % (self.labels(cmd),))
        for cmd in ("./node_modules/.bin/cdk deploy", "/usr/local/bin/cdk deploy", "${DIR}/cdk deploy", "\"$DIR\"/cdk deploy", "$(pwd)/node_modules/.bin/cdk deploy"):
            with self.subTest(cmd):
                self.assertHit(cmd, "cdk deploy")

    def test_the_doctor_and_the_lint_share_the_start_rule(self):
        self.assertEqual(ci_rules._BEHIND, workflows.PROG_BEHIND)
        self.assertEqual(workflows.PROG_BEHIND, r"(?<![\w./-])")
        for label, rx in workflows.DEPLOY_COMMANDS:
            pat = getattr(rx, "pattern", "")
            if label in ("cdk deploy", "kubectl apply", "helm upgrade", "terraform apply", "terragrunt apply", "pulumi up", "sam deploy", "eb deploy", "sst deploy"):
                self.assertTrue(pat.startswith(workflows.PROG_BEHIND), label)

    def test_a_version_is_dropped_only_when_it_looks_like_one(self):
        for cmd in ("npx cdk@2 deploy", "npx cdk@2.150.0 deploy", "npx cdk@latest deploy", "npx cdk@next deploy", "npx cdk@^2 deploy", "npx cdk@~2.1 deploy",
                    "npx cdk@>=2 deploy"):
            with self.subTest(cmd):
                self.assertIn("cdk deploy", self.labels(cmd))
        for cmd in ("sam@host deploy", "ssh cdk@example.com deploy", "kubectl@prod apply"):
            with self.subTest(cmd):
                self.assertEqual(workflows.normalize_shell(cmd), cmd)


class QuotedAndEscapedFlagValues(_BothReaders, unittest.TestCase):
    """Round 3, D3: a quoted value of any length, a quote pair inside a word, and an escaped blank do not hide the verb after the flag."""

    def test_the_reviewers_inputs(self):
        for cmd, label in (('cdk -c a="x y" deploy', "cdk deploy"), ('helm --set a="b c" upgrade x y', "helm upgrade"), ("cdk -a'x y' deploy", "cdk deploy"),
                           ("kubectl -n my\\ ns apply", "kubectl apply"), ('cdk --app "' + "x " * 101 + '" deploy', "cdk deploy")):
            with self.subTest(cmd[:40]):
                self.assertHit(cmd, label)

    def test_a_quoted_value_of_any_length(self):
        for n in (50, 100, 101, 150, 1000, 20000):
            for q in ('"', "'"):
                cmd = "cdk --app %s%s%s deploy" % (q, "x " * n, q)
                with self.subTest("%s %d" % (q, n)):
                    self.assertIn("cdk deploy", self.labels(cmd))
                    self.assertTrue(any("cdk deploy" in c for c in self.calls(cmd)))
        self.assertHit("kubectl --token '" + "x " * 300 + "' apply", "kubectl apply")
        self.assertHit('cdk -c "' + "k=v " * 500 + 'z" -c "' + "a b " * 500 + '" deploy', "cdk deploy")

    def test_quotes_inside_a_word(self):
        for cmd, label in (('cdk -c a="x y" -c b="z w" deploy', "cdk deploy"), ("cdk -c a='x y' deploy", "cdk deploy"), ("cdk -a\"x y\" deploy", "cdk deploy"),
                           ('cdk -a x"y z" deploy', "cdk deploy"), ("cdk -ax\"y z\" deploy", "cdk deploy"), ("cdk --app=node'  'app.js deploy", "cdk deploy"),
                           ('cdk -c "a=$(echo "b c")" deploy', "cdk deploy"), ("cdk -c 'a=\"b c\"' deploy", "cdk deploy"), ('cdk -c "a=\\"b c\\"" deploy', "cdk deploy"),
                           ('kubectl --selector "a in (b, c)" apply', "kubectl apply"), ("kubectl -l 'a in (b, c)' apply -f x", "kubectl apply"),
                           ('cdk -a"x" deploy', "cdk deploy"), ("cdk -a'x' deploy", "cdk deploy"), ('terragrunt run --all --queue-include-dir "a b" apply', "terragrunt apply"),
                           ("cdk -c 'a\tb' deploy", "cdk deploy"), ("cdk -c \"a  b\" deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_escaped_blanks(self):
        for cmd, label in (("kubectl -n my\\ ns apply", "kubectl apply"), ("cdk --app node\\ app.js deploy", "cdk deploy"), ("cdk -c a=b\\ c\\ d deploy", "cdk deploy"),
                           ("kubectl --kubeconfig ~/my\\ dir/cfg apply", "kubectl apply"), ("cdk -c a=b\\\tc deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_quoted_value_over_several_lines(self):
        self.assertHit('cdk -c "a\nb" deploy', "cdk deploy")
        self.assertHit("kubectl --token 'a\n   b\n   c' apply -f x", "kubectl apply")

    def test_the_apostrophes_of_a_comment_do_not_pair_with_the_real_quotes(self):
        # the doctor reads the raw run: text, comments included
        text = "# don't deploy from here\ncdk -c a=\"x y\" deploy\n# it's a comment"
        self.assertIn("cdk deploy", workflows.deploy_labels(text))
        self.assertIn("cdk deploy", workflows.deploy_labels("echo it\\'s\n# a 'quote\ncdk -c a='x y' deploy # b' c"))

    def test_a_quote_that_is_not_closed_is_an_ordinary_character(self):
        for cmd in ('cdk --app "x y deploy', "cdk --app 'x y deploy", 'echo "cdk -c" deploy', "echo 'cdk -a' deploy"):
            with self.subTest(cmd):
                workflows.deploy_labels(cmd)
                self.assertQuiet(cmd, "cdk deploy")

    def test_a_verb_that_is_not_there_stays_quiet(self):
        for cmd, label in (('cdk -c a="x y" synth', "cdk deploy"), ("helm --set a='b c' list", "helm upgrade"), ("kubectl -n my\\ ns get pods", "kubectl apply"),
                           ('cdk --app "' + "x " * 400 + '" synth', "cdk deploy"), ('kubectl --token "a b" get pods', "kubectl apply"),
                           ('cdk -c "a b" --verbose', "cdk deploy"), ('cdk "deploy' + ' x"', "cdk deploy")):
            with self.subTest(cmd[:40]):
                self.assertQuiet(cmd, label)

    def test_the_text_the_flag_run_reads_has_no_blank_in_a_quoted_value(self):
        out = workflows.normalize_shell('cdk -c a="x y" deploy')
        self.assertEqual(out.split("\n\x00\n")[1], 'cdk -c a="x\x01y" deploy')

    def test_a_flag_with_a_quoted_value_attached_needs_a_closed_quote(self):
        pat = workflows.flags_re("cdk")
        rx = __import__("re").compile(r"cdk" + pat + r"\s+deploy")
        self.assertTrue(rx.search("cdk -a'x y' deploy"))
        self.assertTrue(rx.search('cdk -a"x y" deploy'))
        self.assertFalse(rx.search('echo "cdk -c" deploy'))
        self.assertTrue(rx.search("cdk -a'" + "x\x01" * 200 + "' deploy"))      # the long form after the blanks became one word


class SpanPass(unittest.TestCase):
    """The pass behind D1 and D3 (`workflows._blank_spans`): what it closes, what it leaves, and that it is bounded."""

    def spans(self, text):
        return [text[a:b] for a, b in sorted(workflows._blank_spans(text))]

    def test_each_kind_of_span(self):
        self.assertEqual(self.spans("a 'b c' d"), ["'b c'"])
        self.assertEqual(self.spans('a "b c" d'), ['"b c"'])
        self.assertEqual(self.spans("a `b c` d"), ["`b c`"])
        self.assertEqual(self.spans("a $(b c) d"), ["$(b c)"])
        self.assertEqual(self.spans("a ${b:-c d} e"), ["${b:-c d}"])
        self.assertEqual(self.spans("a ${{ b c }} d"), ["${{ b c }}"])
        self.assertEqual(self.spans("a b\\ c d"), ["\\ "])

    def test_nested_spans_are_all_closed(self):
        got = self.spans('x "a $(b "c d") e" y')
        self.assertIn('"a $(b "c d") e"', got)
        self.assertIn('$(b "c d")', got)
        self.assertIn('"c d"', got)

    def test_parentheses_and_braces_inside_a_substitution_are_counted(self):
        self.assertIn("$(a (b c) d)", self.spans("x $(a (b c) d) y"))
        self.assertIn("$((1 + 2))", self.spans("x $((1 + 2)) y"))
        self.assertIn("${a:-${b:-c d}}", self.spans("x ${a:-${b:-c d}} y"))
        self.assertIn('$(echo ")" a)', self.spans('x $(echo ")" a) y'))

    def test_a_quote_inside_the_other_kind_is_an_ordinary_character(self):
        self.assertEqual(self.spans("""a "it's" b 'say "hi"' c"""), ['"it\'s"', "'say \"hi\"'"])

    def test_an_escaped_quote_does_not_close(self):
        self.assertEqual(self.spans('a "b \\" c" d'), ['"b \\" c"'])
        self.assertEqual(self.spans("a \\' b"), [])

    def test_a_comment_is_skipped(self):
        self.assertEqual(self.spans("# it's\na 'b c'\n# don't"), ["'b c'"])
        self.assertEqual(self.spans("a 'b c' # d' e'"), ["'b c'"])
        self.assertEqual(self.spans("a${#b} 'c d'"), ["${#b}", "'c d'"])      # `#` that does not start a word is not a comment
        self.assertEqual(self.spans("echo a#b 'c d'"), ["'c d'"])

    def test_an_opener_with_no_closer_is_ordinary_and_later_spans_are_found(self):
        self.assertEqual(self.spans("a ' b\nc 'd e'"), ["' b\nc '"])      # the first quote pairs with the second (as a shell would)
        self.assertEqual(self.spans('a "b $(c d) e'), ["$(c d)"])
        self.assertEqual(self.spans("a $( b ${{ c d }} e"), ["${{ c d }}"])
        self.assertEqual(self.spans("a ${{ b c"), [])
        self.assertEqual(self.spans("a ${{ 'b }}' c }} d"), ["${{ 'b }}' c }}"])
        self.assertEqual(self.spans("a ${{ 'b }} c"), ["${{ 'b }}"])      # every `}}` is inside a string: the first one closes it

    def test_a_nest_deeper_than_the_limit_is_ordinary_text_and_does_not_raise(self):
        deep = "$(" * 100 + "a b" + ")" * 100
        self.assertTrue(len(self.spans(deep)) >= 1)
        self.assertLessEqual(len(self.spans(deep)), 100)

    def test_the_levels_stop_at_three(self):
        text = "\"a 'b \\\"$(c d)\\\" e' f\""
        levels = workflows._blank_levels("sh -c " + text)
        self.assertLessEqual(len(levels), 2 * workflows._SPAN_LEVELS)      # per level: the spans found line by line and those found over several lines

    def test_hostile_text_costs_time_in_proportion_to_its_size(self):
        # the pass is run at 4,000 and at 16,000 characters (see HostileInputCost.linear); the delimiter limit itself is tested below
        shapes = [lambda n: "$(" * (n // 2), lambda n: "$(" * (n // 4) + ")" * (n // 4), lambda n: '"' * n, lambda n: "'" * n, lambda n: "\"'" * (n // 2),
                  lambda n: "`" * n, lambda n: "` `" * (n // 3), lambda n: "${{ " * (n // 4), lambda n: "${{ ' " * (n // 6), lambda n: "${{ ' }} " * (n // 8),
                  lambda n: "${{ '' ' }} " * (n // 11), lambda n: "${{ x }} " * (n // 8), lambda n: "${ " * (n // 3), lambda n: "\\ " * (n // 2),
                  lambda n: '"$(' * (n // 3), lambda n: "'a b' " * (n // 6), lambda n: '"a b" ' * (n // 6), lambda n: "$(a b) " * (n // 7),
                  lambda n: 'cdk -c a="x y" ' * (n // 15), lambda n: 'echo "a\n' * (n // 8), lambda n: "\"$(\"$(" * (n // 6), lambda n: "$(' " * (n // 4),
                  lambda n: "$( ' ) " * (n // 6), lambda n: "`$(" * (n // 3), lambda n: "}}" * (n // 2), lambda n: "# '" * (n // 3), lambda n: ";#" * (n // 2),
                  lambda n: ("$(" * 40 + "a b" + ")" * 40 + " ") * (n // 90), lambda n: ("\"$(\"" * 10 + ")\"" * 10 + " ") * (n // 50),
                  lambda n: "'\"a b\" ' " * (n // 9)]

        def cost(text):
            t = time.time()
            try:
                out = workflows.normalize_shell.__wrapped__(text)   # not the cached one
                self.assertLess(len(out), 8 * len(text) + 100)      # the copies are lines of the text, a few levels at most
            except workflows.ScanLimit:                             # past the delimiter limit: unreadable, and quickly
                pass
            return time.time() - t
        for make in shapes:
            with self.subTest(make(30)[:30]):
                small, big = cost(make(4000)), cost(make(16000))
                self.assertLess(big, max(10 * small, 0.3), "%.3fs at 4000 characters, %.3fs at 16000" % (small, big))

    def test_the_delimiter_limit_makes_a_long_text_unreadable_not_clean(self):
        # D6: more delimiters than the span pass reads is `ScanLimit`, quickly; the callers turn it into a finding (see UnreadableIsNeverClean)
        for many in ("$(" * 60000, "'a b' " * 120000, "${{ x }} " * 70000):
            with self.subTest(many[:8]):
                t = time.time()
                self.assertRaises(workflows.ScanLimit, workflows.normalize_shell.__wrapped__, many)
                self.assertLess(time.time() - t, BOUND)

    def test_many_spans_below_the_limit_are_read(self):
        many = "'a b' " * 20000
        self.assertEqual(len(workflows._blank_spans(many)), 20000)
        t = time.time()
        out = workflows.normalize_shell.__wrapped__(many)
        self.assertLess(time.time() - t, BOUND)
        self.assertIn("\x01", out)


if __name__ == "__main__":
    unittest.main()
