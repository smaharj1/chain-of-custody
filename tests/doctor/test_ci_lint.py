"""ci_lint: the split-CI rules of IMP-23 (RC-3, RC-18, DEC-39), including the 'skill hooks in CI' fixture."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import ci_lint

from tests import REPO_ROOT
from tests.helpers import BaseTestCase

HERE = os.path.dirname(os.path.abspath(__file__))
CI_LINT = os.path.join(REPO_ROOT, ".claude", "scripts", "ci_lint.py")


def read_fixture(name):
    with open(os.path.join(HERE, "fixtures", "workflows", name), "r", encoding="utf-8") as fh:
        return fh.read()


def lint(text, name="w.yml", **kw):
    return ci_lint.lint_text(text, name, ci_lint.Options(**kw) if kw else None)


def rules(findings, severity=None):
    return sorted({f.rule for f in findings if severity is None or f.severity == severity})


def wf(on, job, extra_top=""):
    return "name: t\n%s\non:\n%s\njobs:\n  j:\n    runs-on: ubuntu-latest\n%s" % (extra_top, on, job)


class OidcWithPrCode(unittest.TestCase):
    def test_pull_request_default_checkout_with_id_token_write(self):
        text = wf("  pull_request:", "    permissions:\n      id-token: write\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo hi\n")
        self.assertIn("ci.oidc-with-pr-code", rules(lint(text), "error"))

    def test_workflow_level_permission_is_inherited(self):
        text = wf("  pull_request:", "    steps:\n      - uses: actions/checkout@v4\n", extra_top="permissions:\n  id-token: write\n")
        self.assertIn("ci.oidc-with-pr-code", rules(lint(text), "error"))

    def test_job_override_drops_it(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n", extra_top="permissions:\n  id-token: write\n")
        self.assertNotIn("ci.oidc-with-pr-code", rules(lint(text)))

    def test_pull_request_target_checking_out_the_head(self):
        for ref in ("${{ github.event.pull_request.head.sha }}", "${{ github.head_ref }}", "refs/pull/${{ github.event.number }}/merge", "${{ github.event.pull_request.head.ref }}"):
            with self.subTest(ref=ref):
                text = wf("  pull_request_target:", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          ref: %s\n" % ref)
                r = rules(lint(text), "error")
                self.assertIn("ci.oidc-with-pr-code", r)
                self.assertIn("ci.pr-target-head-checkout", r)

    def test_pull_request_target_default_checkout_is_the_base(self):
        text = wf("  pull_request_target:", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/checkout@v4\n")
        self.assertEqual(rules(lint(text), "error"), [])

    def test_push_to_main_with_oidc_is_fine(self):
        text = wf("  push:\n    branches: [main]", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/checkout@v4\n")
        self.assertEqual(rules(lint(text), "error"), [])

    def test_manual_fetch_of_pr_head_counts(self):
        for run in ("gh pr checkout ${{ github.event.number }}", "git fetch origin pull/${{ github.event.number }}/head && git checkout FETCH_HEAD",
                    "git checkout ${{ github.event.pull_request.head.sha }}"):
            with self.subTest(run=run):
                text = wf("  pull_request_target:", "    permissions:\n      id-token: write\n    steps:\n      - run: %s\n" % json.dumps(run))
                self.assertIn("ci.oidc-with-pr-code", rules(lint(text), "error"))

    def test_workflow_run_with_head_sha(self):
        text = wf("  workflow_run:\n    workflows: [CI]", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.workflow_run.head_sha }}\n")
        r = rules(lint(text), "error")
        self.assertIn("ci.oidc-with-pr-code", r)
        self.assertIn("ci.pr-target-head-checkout", r)


class SecretsInPrJobs(unittest.TestCase):
    def test_pull_request_job_using_a_secret(self):
        text = wf("  pull_request:", "    steps:\n      - run: ./x\n        env:\n          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n")
        self.assertIn("ci.secrets-in-pr-job", rules(lint(text), "error"))

    def test_github_token_is_allowed(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - run: gh pr view\n        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n")
        self.assertEqual(rules(lint(text), "error"), [])

    def test_secrets_inherit_into_a_reusable_workflow(self):
        text = "name: t\non:\n  pull_request:\njobs:\n  j:\n    uses: ./.github/workflows/x.yml\n    secrets: inherit\n"
        self.assertIn("ci.secrets-in-pr-job", rules(lint(text), "error"))

    def test_workflow_level_env_secret(self):
        text = wf("  pull_request:", "    steps:\n      - run: ./x\n", extra_top="env:\n  K: ${{ secrets.API_KEY }}\n")
        self.assertIn("ci.secrets-in-pr-job", rules(lint(text), "error"))

    def test_pull_request_target_without_pr_code_may_use_secrets(self):
        text = wf("  pull_request_target:", "    steps:\n      - run: ./x\n        env:\n          K: ${{ secrets.LABEL_BOT }}\n")
        self.assertNotIn("ci.secrets-in-pr-job", rules(lint(text)))

    def test_pull_request_target_with_pr_code_and_secrets(self):
        text = wf("  pull_request_target:", "    steps:\n      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n      - run: ./x\n        env:\n          K: ${{ secrets.K }}\n")
        r = rules(lint(text), "error")
        self.assertIn("ci.secrets-in-pr-job", r)
        self.assertIn("ci.pr-target-head-checkout", r)

    def test_push_job_may_use_secrets(self):
        text = wf("  push:\n    branches: [main]", "    steps:\n      - run: ./x\n        env:\n          K: ${{ secrets.K }}\n")
        self.assertEqual(rules(lint(text), "error"), [])


class SkillHooksInCi(unittest.TestCase):
    """Report §15 item 9 'Skill hooks in CI (RC-18)': a PR adds a skill whose frontmatter hook writes a marker;
    the CI claude -p job must not create it. Offline half: ci_lint rejects a workflow that does not restore skill directories
    from the base ref before -p, and accepts the one that does."""

    def read(self, name):
        with open(os.path.join(HERE, "fixtures", "workflows", name), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_unsafe_workflow_is_rejected(self):
        f = lint(self.read("claude-review-unsafe.yml"), "claude-review-unsafe.yml")
        hit = [x for x in f if x.rule == "ci.skills-not-restored"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].severity, "error")
        for p in (".claude", ".mcp.json", "plugin", ".claude-plugin"):
            self.assertIn(p, hit[0].message)

    def test_safe_workflow_passes_every_error_rule(self):
        f = lint(self.read("claude-review-safe.yml"), "claude-review-safe.yml", restore_paths=ci_lint.DEFAULT_RESTORE + ("skills",))
        self.assertEqual(rules(f, "error"), [], [str(x) for x in f])

    def test_partial_restore_is_not_enough(self):
        text = self.read("claude-review-safe.yml").replace(" .mcp.json plugin .claude-plugin skills", "")
        f = lint(text)
        self.assertIn("ci.skills-not-restored", rules(f, "error"))

    def test_restore_in_a_later_step_is_too_late(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: claude -p x --setting-sources user\n"
                  "      - run: git checkout $BASE -- .claude .mcp.json plugin .claude-plugin\n")
        self.assertIn("ci.skills-not-restored", rules(lint(text), "error"))

    def test_restore_with_git_restore_source_form(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: git restore --source=origin/main -- .claude .mcp.json plugin .claude-plugin\n"
                  "      - run: claude -p x --setting-sources user\n")
        self.assertNotIn("ci.skills-not-restored", rules(lint(text)))

    def test_a_job_that_checks_out_only_the_base_needs_no_restore(self):
        text = wf("  pull_request_target:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.base.sha }}\n"
                  "      - run: claude -p x --setting-sources user\n")
        self.assertNotIn("ci.skills-not-restored", rules(lint(text)))

    def test_claude_code_action_in_a_pr_job_is_covered_too(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - uses: anthropics/claude-code-action@v1\n")
        self.assertIn("ci.skills-not-restored", rules(lint(text), "error"))

    def test_trusted_only_triggers_do_not_need_a_restore(self):
        for on in ("  push:\n    branches: [main]", "  schedule:\n    - cron: '0 3 * * *'", "  workflow_dispatch:"):
            with self.subTest(on=on):
                text = wf(on, "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: claude -p x --setting-sources user\n")
                self.assertNotIn("ci.skills-not-restored", rules(lint(text)))

    def test_push_to_any_branch_is_untrusted(self):
        text = wf("  push:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: claude -p x --setting-sources user\n")
        self.assertIn("ci.skills-not-restored", rules(lint(text), "error"))

    def test_settings_sources_flag(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: git checkout $B -- .claude .mcp.json plugin .claude-plugin\n      - run: claude -p x\n")
        f = [x for x in lint(text) if x.rule == "ci.claude-settings-sources"]
        self.assertEqual([x.severity for x in f], ["error"])

    def test_repo_with_a_root_skills_dir_requires_it_to_be_restored(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "skills"))
            os.makedirs(os.path.join(tmp, ".github", "workflows"))
            text = self.read("claude-review-safe.yml").replace(" skills", "")
            with open(os.path.join(tmp, ".github", "workflows", "r.yml"), "w") as fh:
                fh.write(text)
            f = ci_lint.lint_repo(tmp)
            self.assertIn("ci.skills-not-restored", rules(f, "error"))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class OtherRules(unittest.TestCase):
    def test_script_injection_in_run(self):
        for expr in ("github.event.pull_request.title", "github.event.issue.body", "github.head_ref", "github.event.comment.body", "github.event.pull_request.head.ref",
                     "github.event.head_commit.message", "github.event.review.body", "github.event.commits[0].author.email", "github.event.label.name"):
            with self.subTest(expr=expr):
                text = wf("  pull_request_target:", "    permissions:\n      contents: read\n    steps:\n      - run: echo \"${{ %s }}\"\n" % expr)
                self.assertIn("ci.script-injection", rules(lint(text), "error"))

    def test_untrusted_value_through_env_is_the_safe_pattern(self):
        text = wf("  pull_request_target:", "    permissions:\n      contents: read\n    steps:\n      - run: echo \"$TITLE\"\n        env:\n          TITLE: ${{ github.event.pull_request.title }}\n")
        self.assertNotIn("ci.script-injection", rules(lint(text)))

    def test_deploy_in_pr_job(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - run: npx cdk deploy --all\n")
        self.assertIn("ci.deploy-in-pr-job", rules(lint(text), "error"))
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: superfly/flyctl-actions@master\n")
        self.assertIn("ci.deploy-in-pr-job", rules(lint(text), "error"))

    def test_floating_action_tags_warn_and_shas_do_not(self):
        text = wf("  push:\n    branches: [main]", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n"
                  "      - uses: actions/setup-node@%s\n      - uses: ./local\n      - uses: docker://alpine@sha256:abc\n" % ("a" * 40))
        f = [x for x in lint(text) if x.rule == "ci.floating-action-tag"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "warn")

    def test_missing_permissions_warns(self):
        text = "name: t\non: push\njobs:\n  j:\n    runs-on: x\n    steps:\n      - run: echo\n"
        self.assertIn("ci.missing-permissions", rules(lint(text), "warn"))

    def test_write_permissions_alongside_pr_code_warn(self):
        text = wf("  pull_request:", "    permissions:\n      contents: write\n    steps:\n      - uses: actions/checkout@v4\n")
        self.assertIn("ci.write-permissions-pr-job", rules(lint(text), "warn"))

    def test_persist_credentials_warning(self):
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n")
        self.assertIn("ci.persist-credentials", rules(lint(text), "warn"))
        text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n")
        self.assertNotIn("ci.persist-credentials", rules(lint(text)))

    def test_comment_triggers_need_an_author_association_check(self):
        text = wf("  issue_comment:\n    types: [created]", "    permissions:\n      contents: read\n    steps:\n      - run: ./x\n")
        self.assertIn("ci.untrusted-comment-trigger", rules(lint(text), "warn"))
        text = wf("  issue_comment:\n    types: [created]", "    permissions:\n      contents: read\n    if: contains(fromJSON('[\"OWNER\",\"MEMBER\"]'), github.event.comment.author_association)\n    steps:\n      - run: ./x\n")
        self.assertNotIn("ci.untrusted-comment-trigger", rules(lint(text)))

    def test_issue_labeled_is_not_a_stranger_text_trigger(self):
        text = wf("  issues:\n    types: [labeled]", "    permissions:\n      issues: write\n    steps:\n      - run: ./x\n")
        self.assertNotIn("ci.untrusted-comment-trigger", rules(lint(text)))
        text = wf("  issues:", "    permissions:\n      issues: write\n    steps:\n      - run: ./x\n")
        self.assertIn("ci.untrusted-comment-trigger", rules(lint(text)))


class BypassAttempts(unittest.TestCase):
    """Ways to dodge the rules above that an earlier version of ci_lint missed."""

    def test_secrets_by_bracket_syntax_and_tojson(self):
        for expr in ("${{ secrets['DEPLOY_TOKEN'] }}", '${{ secrets["DEPLOY_TOKEN"] }}', "${{ toJSON(secrets) }}", "${{ tojson( secrets ) }}"):
            with self.subTest(expr=expr):
                text = wf("  pull_request:", "    steps:\n      - run: ./x\n        env:\n          K: %s\n" % json.dumps(expr))
                self.assertIn("ci.secrets-in-pr-job", rules(lint(text), "error"))

    def test_secrets_passed_to_a_reusable_workflow_by_name(self):
        text = "name: t\non:\n  pull_request:\njobs:\n  j:\n    uses: ./.github/workflows/x.yml\n    secrets:\n      TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"
        self.assertIn("ci.secrets-in-pr-job", rules(lint(text), "error"))

    def test_claude_invoked_by_path_or_through_npx(self):
        for run in ("/usr/local/bin/claude -p x --setting-sources user", "npx -y @anthropic-ai/claude-code -p x --setting-sources user",
                    "pnpm dlx @anthropic-ai/claude-code --print x --setting-sources user", "cd x && claude --print y --setting-sources user"):
            with self.subTest(run=run):
                text = wf("  pull_request:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: %s\n" % json.dumps(run))
                self.assertIn("ci.skills-not-restored", rules(lint(text), "error"))

    def test_pr_head_pulled_by_git_clone_or_merge_in_a_script(self):
        for run in ("git clone https://github.com/${{ github.event.pull_request.head.repo.full_name }} src",
                    "git fetch origin ${{ github.event.pull_request.head.sha }} && git merge FETCH_HEAD",
                    "git cherry-pick ${{ github.event.pull_request.head.sha }}"):
            with self.subTest(run=run):
                text = wf("  pull_request_target:", "    permissions:\n      id-token: write\n    steps:\n      - run: %s\n" % json.dumps(run))
                self.assertIn("ci.oidc-with-pr-code", rules(lint(text), "error"))

    def test_artifact_poisoning_through_workflow_run(self):
        text = wf("  workflow_run:\n    workflows: [CI]", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/download-artifact@v4\n      - run: ./build/run.sh\n")
        self.assertIn("ci.artifact-from-pr-run", rules(lint(text), "error"))
        text = wf("  workflow_run:\n    workflows: [CI]", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/download-artifact@v4\n")
        self.assertNotIn("ci.artifact-from-pr-run", rules(lint(text)))

    def test_push_with_a_default_checkout_is_not_a_base_only_job(self):
        text = wf("  push:", "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v4\n      - run: claude -p x --setting-sources user\n")
        self.assertIn("ci.skills-not-restored", rules(lint(text), "error"))

    def test_pinned_by_sha_checkout_is_still_a_checkout(self):
        text = wf("  pull_request:", "    permissions:\n      id-token: write\n    steps:\n      - uses: actions/checkout@%s\n" % ("a" * 40))
        self.assertIn("ci.oidc-with-pr-code", rules(lint(text), "error"))


class Unparsed(unittest.TestCase):
    def test_unreadable_yaml_is_never_a_pass(self):
        text = 'name: t\non: push\njobs:\n  j:\n    runs-on: x\n    steps:\n      - run: echo "a: b"\n'
        f = lint(text)
        self.assertEqual(rules(f, "error"), ["ci.unparsed"])
        self.assertIn("manual review", f[0].message)

    def test_unparsed_with_deploy_text_says_so(self):
        text = 'name: t\non: pull_request\njobs:\n  j:\n    runs-on: x\n    permissions: {id-token: write}\n    steps:\n      - run: terraform apply\n'
        msgs = " ".join(x.message for x in lint(text))
        self.assertIn("terraform apply", msgs)
        self.assertIn("id-token: write", msgs)

    def test_no_jobs_or_no_on_is_unparsed(self):
        self.assertEqual(rules(lint("name: t\non: push\n")), ["ci.unparsed"])
        self.assertEqual(rules(lint("name: t\njobs:\n  j:\n    runs-on: x\n")), ["ci.unparsed"])

    def test_flow_mapping_and_anchors(self):
        self.assertEqual(rules(lint("name: t\non: push\njobs:\n  j: {runs-on: x}\n")), ["ci.unparsed"])
        self.assertEqual(rules(lint("name: t\non: push\ndefaults: &d\n  a: 1\njobs:\n  j:\n    runs-on: x\n")), ["ci.unparsed"])


class Shipped(unittest.TestCase):
    def test_label_guard_template_has_no_findings(self):
        p = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows", "label-guard.yml")
        f = ci_lint.lint_paths([p])
        self.assertEqual([str(x) for x in f], [])

    def test_this_repos_own_workflows_have_no_errors(self):
        f = ci_lint.lint_repo(REPO_ROOT)
        self.assertEqual([str(x) for x in f if x.severity == "error"], [])


class Cli(BaseTestCase):
    def run_cli(self, *args, cwd=None):
        p = subprocess.run([sys.executable, CI_LINT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, timeout=30)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def test_exit_codes_and_json(self):
        d = self.tmpdir()
        os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "ok.yml"), "w") as fh:
            fh.write("name: ok\non: push\njobs:\n  j:\n    runs-on: x\n    permissions:\n      contents: read\n    steps:\n      - run: echo\n")
        rc, out, _ = self.run_cli("--repo", d)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(d, ".github", "workflows", "bad.yml"), "w") as fh:
            fh.write(read_fixture("claude-review-unsafe.yml"))
        rc, out, _ = self.run_cli("--repo", d, "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["schema"], "ci_lint.v1")
        self.assertFalse(doc["ok"])
        self.assertIn("ci.skills-not-restored", [f["rule"] for f in doc["findings"]])

    def test_unparsed_file_exits_1(self):
        d = self.tmpdir()
        p = os.path.join(d, "w.yml")
        with open(p, "w") as fh:
            fh.write("name: t\non: push\njobs: {a: b}\n")
        rc, out, _ = self.run_cli(p)
        self.assertEqual(rc, 1)
        self.assertIn("unparsed", out)

    def test_runs_from_any_directory_without_arguments(self):
        d = self.tmpdir()
        rc, out, _ = self.run_cli(cwd=d)
        self.assertEqual(rc, 0)
        self.assertIn("0 file(s)", out)

    def test_restore_paths_flag(self):
        d = self.tmpdir()
        p = os.path.join(d, "w.yml")
        with open(p, "w") as fh:
            fh.write(read_fixture("claude-review-safe.yml").replace(" skills", ""))
        self.assertEqual(self.run_cli(p, cwd=d)[0], 0)
        self.assertEqual(self.run_cli(p, "--restore-paths", ".claude,.mcp.json,plugin,.claude-plugin,docs", cwd=d)[0], 1)


if __name__ == "__main__":
    unittest.main()
