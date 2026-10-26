"""The ops workflow templates: intake, dependency-check, smoke (Phase 11a; report 5.3, 8.4, 19.7; RC-3, RC-18, CS-9, DEC-32).

They must pass the Phase 1 linter with no errors (only the "pin to a SHA" warning is allowed: the human pins), and the linter must
still catch the mistakes a human could make while adapting them. The structural tests pin the separation of duties between jobs.
"""
from __future__ import annotations

import os
import re
import unittest

import ci_lint

from tests import REPO_ROOT

WF_DIR = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")
OPS = ("intake.yml", "dependency-check.yml", "smoke.yml")


def read(name):
    with open(os.path.join(WF_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def code(name):
    return "\n".join(ln for ln in read(name).split("\n") if not ln.lstrip().startswith("#"))


def job_blocks(name):
    """{job id: text} of the jobs: section (two-space-indented ids)."""
    text = code(name).split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9_-]*):\n", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def lint(text, name="t.yml"):
    return ci_lint.lint_text(text, name)


def errors(findings):
    return sorted({f.rule for f in findings if f.severity == "error"})


class Lint(unittest.TestCase):
    def test_templates_pass_ci_lint_with_no_errors_and_only_the_pin_warning(self):
        findings = ci_lint.lint_paths([WF_DIR], repo=REPO_ROOT)
        self.assertEqual([str(f) for f in findings if f.severity == "error"], [])
        self.assertNotIn("ci.unparsed", {f.rule for f in findings})
        self.assertEqual({f.rule for f in findings if f.severity == "warn"}, {"ci.floating-action-tag"})
        for n in OPS:
            self.assertTrue(os.path.isfile(os.path.join(WF_DIR, n)), n)

    def test_the_linter_still_catches_adaptation_mistakes(self):
        # a human pastes an issue title into a run: step
        bad = read("intake.yml").replace('gh issue comment "$ISSUE_NUMBER"', 'echo "${{ github.event.issue.title }}"; gh issue comment "$ISSUE_NUMBER"')
        self.assertIn("ci.script-injection", errors(lint(bad)))
        # a human makes smoke run on pull requests, checks out the PR and gives the job OIDC
        bad = read("smoke.yml").replace("on:\n  schedule:", "on:\n  pull_request:\n  schedule:", 1)
        bad = bad.replace("          ref: ${{ github.event.repository.default_branch }}\n", "", 1)
        bad = bad.replace("  smoke:\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:\n      contents: read",
                          "  smoke:\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:\n      contents: read\n      id-token: write")
        self.assertIn("ci.oidc-with-pr-code", errors(lint(bad)))
        # a human lets the comment trigger run for strangers
        bad = read("claude-review.yml").replace(" && contains(fromJSON('[\"OWNER\",\"MEMBER\"]'), github.event.comment.author_association)", "")
        self.assertIn("ci.untrusted-comment-trigger", {f.rule for f in lint(bad)})

    def test_every_template_is_commented_with_its_report_references(self):
        for n in OPS:
            head = read(n).split("\n", 3)[:3]
            self.assertTrue(all(h.startswith("#") for h in head[:2]), n)
            self.assertRegex(read(n), r"report")


class Intake(unittest.TestCase):
    def test_triggers_only_on_the_go_label_from_a_user(self):
        text = code("intake.yml")
        self.assertRegex(text, r"(?m)^on:\n  issues:\n    types: \[labeled\]")
        self.assertNotIn("pull_request", text.split("jobs:")[0].replace("pull-requests", ""))
        self.assertIn("github.event.label.name == 'factory:go'", text)
        self.assertIn("github.event.sender.type == 'User'", text)

    def test_approver_is_checked_by_the_workflow_itself(self):
        text = code("intake.yml")
        self.assertIn("vars.FACTORY_APPROVERS", text)
        self.assertIn("github.event.sender.login", text)
        # every later triage step is gated on the approval
        triage = job_blocks("intake.yml")["triage"]
        steps = re.split(r"(?m)^      - ", triage)[1:]
        self.assertGreater(len(steps), 4)
        for s in steps:
            if "id: gate" in s:
                continue
            self.assertIn("steps.gate.outputs.approved == 'yes'", s, s[:60])

    def test_the_model_job_cannot_write_and_the_write_job_has_no_model(self):
        jobs = job_blocks("intake.yml")
        triage, apply_ = jobs["triage"], jobs["apply"]
        self.assertNotIn("issues: write", triage)
        self.assertNotIn("pull-requests", triage)
        self.assertIn("ANTHROPIC_API_KEY", triage)
        self.assertNotIn("claude ", apply_.replace("claude-code", ""))
        self.assertNotIn("ANTHROPIC", apply_)
        self.assertNotIn("secrets.", apply_)
        self.assertIn("issues: write", apply_)

    def test_the_key_is_in_one_step_that_has_no_tools(self):
        text = code("intake.yml")
        steps = re.split(r"(?m)^      - ", text)
        with_key = [s for s in steps if "secrets.ANTHROPIC_API_KEY" in s]
        self.assertEqual(len(with_key), 1)
        s = with_key[0]
        self.assertIn("claude -p", s)
        self.assertIn("--setting-sources user", s)
        self.assertIn("--permission-mode dontAsk", s)
        denied = s.split("--disallowedTools")[1].split("\n")[0]
        for tool in ("Bash", "Read", "Grep", "Glob", "Edit", "Write", "WebFetch", "WebSearch", "Agent", "Skill", "Workflow"):
            self.assertIn(tool, denied)
        self.assertIn("--max-budget-usd", s)
        self.assertIn("< .intake/issue.json", s)  # the untrusted text arrives on stdin, not as a file the model reads
        self.assertNotRegex(s, r"\$\{\{\s*github\.event\.issue")

    def test_both_jobs_run_the_default_branch_kit(self):
        for job, body in job_blocks("intake.yml").items():
            if "actions/checkout" in body:
                self.assertIn("ref: ${{ github.event.repository.default_branch }}", body, job)
                self.assertIn("persist-credentials: false", body, job)

    def test_only_the_four_type_labels_can_be_added(self):
        text = code("intake.yml")
        self.assertIn("factory:feature|factory:bug|factory:deps|factory:chore)", text)
        self.assertNotIn("add-label factory:go", text)
        self.assertNotRegex(text, r"--add-label\s+\"?factory:(go|running|halt)")

    def test_untrusted_values_reach_the_shell_only_through_env(self):
        for n in OPS:
            for m in re.finditer(r"(?m)^\s+run: \|\n((?:\s{10,}.*\n?)+)", read(n)):
                self.assertNotRegex(m.group(1), r"\$\{\{\s*github\.event\.(issue|comment|label|sender|pull_request)", n)


class DependencyCheck(unittest.TestCase):
    def test_scan_reads_and_report_writes_issues(self):
        jobs = job_blocks("dependency-check.yml")
        self.assertNotIn("issues: write", jobs["scan"])
        self.assertIn("issues: write", jobs["report"])
        self.assertNotIn("secrets.", code("dependency-check.yml"))
        self.assertNotIn("npm install", code("dependency-check.yml"))
        self.assertIn("npm ci --ignore-scripts", jobs["scan"])
        for n in ("npm", "pip", "osv-scanner"):
            self.assertIn(n, jobs["scan"])
        self.assertNotIn("npm ", jobs["report"].replace("npm-outdated", ""))

    def test_schedule_and_manual_start_only(self):
        text = code("dependency-check.yml").split("jobs:")[0]
        self.assertIn("schedule:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request", text.replace("pull-requests", ""))
        self.assertNotIn("push:", text)

    def test_it_calls_the_script_with_a_cap_and_dedupes_against_open_issues(self):
        text = code("dependency-check.yml")
        self.assertIn("python3 .claude/scripts/dependency_report.py", text)
        self.assertIn("--max-issues 10", text)
        self.assertIn("gh issue list --label factory:deps --state open", text)
        self.assertIn("--label factory:deps", text)

    def test_the_script_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, ".claude", "scripts", "dependency_report.py")))
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, ".claude", "scripts", "intake_validate.py")))


class Smoke(unittest.TestCase):
    def test_project_code_runs_in_a_job_that_cannot_write(self):
        jobs = job_blocks("smoke.yml")
        self.assertNotIn("issues: write", jobs["smoke"])
        self.assertIn("verify.py --only smoke", jobs["smoke"])
        self.assertIn("issues: write", jobs["report"])
        self.assertNotIn("checkout", jobs["report"])
        self.assertNotIn("run: python", jobs["report"])
        self.assertNotIn("verify.py", jobs["report"])

    def test_the_issue_holds_fixed_text_and_a_run_link(self):
        rep = job_blocks("smoke.yml")["report"]
        self.assertIn("RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}", rep)
        self.assertIn("--label factory:bug", rep)
        self.assertNotIn("needs.smoke.outputs", rep)
        self.assertNotIn("steps.", rep)

    def test_url_check_is_https_only(self):
        text = code("smoke.yml")
        self.assertIn("https://*) ;;", text)
        self.assertIn("vars.SMOKE_URL", text)
        self.assertNotIn("secrets.", text)

    def test_only_scheduled_or_manual(self):
        head = code("smoke.yml").split("jobs:")[0]
        self.assertIn("schedule:", head)
        self.assertNotIn("pull_request", head.replace("pull-requests", ""))
        self.assertNotIn("issues:\n    types", head)


if __name__ == "__main__":
    unittest.main()
