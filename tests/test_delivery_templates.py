"""The Phase 3 workflow templates, PR body template and config keys (report §8.4, RC-3, RC-18; IMP-11, IMP-25).

The workflows must pass the Phase 1 linter (ci_lint) with no errors, and the linter must still catch the mistakes a human could
make while adapting them: that is what the mutation tests prove.
"""
from __future__ import annotations

import os
import re
import unittest

import ci_lint
from kitlib import config, registry

from . import REPO_ROOT

WF_DIR = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")


def read(name):
    with open(os.path.join(WF_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def code(name):
    """The workflow without its comment lines."""
    return "\n".join(ln for ln in read(name).split("\n") if not ln.lstrip().startswith("#"))


def lint(text, name="t.yml"):
    return ci_lint.lint_text(text, name)


def errors(findings):
    return sorted({f.rule for f in findings if f.severity == "error"})


class Workflows(unittest.TestCase):
    def test_templates_pass_ci_lint_with_no_errors(self):
        findings = ci_lint.lint_paths([WF_DIR], repo=REPO_ROOT)
        self.assertEqual([str(f) for f in findings if f.severity == "error"], [])
        files = {os.path.basename(f.file) for f in findings}
        self.assertNotIn("ci.unparsed", {f.rule for f in findings})
        # the only warning allowed is "pin to a SHA", which the human does
        self.assertEqual({f.rule for f in findings if f.severity == "warn"}, {"ci.floating-action-tag"})
        for name in ("verify.yml", "claude-review.yml", "label-guard.yml"):
            self.assertTrue(os.path.isfile(os.path.join(WF_DIR, name)), name)
        del files

    def test_verify_is_the_untrusted_job(self):
        text = code("verify.yml")
        self.assertRegex(text, r"(?m)^on:\n  pull_request:")
        for banned in ("secrets.", "id-token", "pull_request_target", "workflow_run", "contents: write", "pull-requests: write", "deploy"):
            self.assertNotIn(banned, text, banned)
        self.assertIn("contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertRegex(text, r"(?m)^  verify:\n    name: verify$")
        # the kit comes from the base sha, not from the pull request
        self.assertIn("git archive \"$BASE_SHA\" .claude/scripts", text)
        self.assertIn("--base \"$BASE_SHA\"", text)
        self.assertIn("--apply-gate", text)
        self.assertNotIn("python3 .claude/scripts/verify.py", text)

    def test_claude_review_is_the_trusted_job_on_the_base_ref(self):
        text = code("claude-review.yml")
        self.assertRegex(text, r"(?m)^on:\n  issue_comment:")
        self.assertNotIn("pull_request:", text.split("jobs:")[0].replace("pull-requests", ""))
        self.assertNotIn("id-token", text)
        self.assertNotIn("contents: write", text)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", text)
        self.assertIn("author_association", text)
        self.assertIn("--setting-sources user", text)
        self.assertIn("--permission-mode dontAsk", text)
        # the key is scoped to the one step that runs claude, and that step has no shell, no edit and no web
        steps = re.split(r"(?m)^      - ", text)
        with_key = [s for s in steps if "secrets.ANTHROPIC_API_KEY" in s]
        self.assertEqual(len(with_key), 1)
        self.assertIn("claude -p", with_key[0])
        for tool in ("Bash", "Edit", "Write", "WebFetch", "Agent", "Workflow"):
            self.assertIn(tool, with_key[0].split("--disallowedTools")[1].split("\n")[0])
        self.assertIn('--allowedTools "Read,Grep,Glob"', with_key[0])
        # the PR is fetched as data, never checked out
        self.assertIn("gh pr diff", text)
        self.assertNotRegex(text, r"gh pr checkout|refs/pull|head\.sha|head_ref")

    def test_every_untrusted_value_goes_through_env_not_run(self):
        for name in ("verify.yml", "claude-review.yml"):
            text = read(name)
            for m in re.finditer(r"(?ms)^\s+run: \|\n(.*?)(?=^\s+- |\Z)", text):
                self.assertNotIn("${{", m.group(1), "%s interpolates an expression in a run script" % name)


class Mutations(unittest.TestCase):
    """If a human adapts a template badly, ci_lint still says so."""

    def test_oidc_in_verify(self):
        bad = read("verify.yml").replace("permissions:\n  contents: read", "permissions:\n  contents: read\n  id-token: write", 1)
        self.assertIn("ci.oidc-with-pr-code", errors(lint(bad)))

    def test_a_secret_in_verify(self):
        bad = read("verify.yml").replace("        env:\n          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n        run: |\n          set +e",
                                         "        env:\n          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n        run: |\n          set +e", 1)
        self.assertNotEqual(bad, read("verify.yml"))
        self.assertIn("ci.secrets-in-pr-job", errors(lint(bad)))

    def test_a_deploy_command_in_verify(self):
        bad = read("verify.yml") + "      - run: terraform apply -auto-approve\n"
        self.assertIn("ci.deploy-in-pr-job", errors(lint(bad)))

    def test_review_switched_to_pull_request_with_the_key(self):
        bad = read("claude-review.yml").replace("on:\n  issue_comment:\n    types: [created]", "on:\n  pull_request:\n    branches: [main]")
        bad = bad.replace("          ref: ${{ github.event.repository.default_branch }}\n", "")
        found = errors(lint(bad))
        self.assertIn("ci.secrets-in-pr-job", found)
        self.assertIn("ci.skills-not-restored", found)

    def test_review_checking_out_the_pull_request(self):
        bad = read("claude-review.yml").replace("ref: ${{ github.event.repository.default_branch }}", "ref: refs/pull/${{ github.event.issue.number }}/head")
        self.assertIn("ci.pr-target-head-checkout", errors(lint(bad)))

    def test_review_without_the_settings_flag_or_the_author_check(self):
        bad = read("claude-review.yml").replace("            --setting-sources user \\\n", "")
        self.assertIn("ci.claude-settings-sources", {f.rule for f in lint(bad)})
        worse = read("claude-review.yml").replace(" && contains(fromJSON('[\"OWNER\",\"MEMBER\"]'), github.event.comment.author_association)", "")
        self.assertIn("ci.untrusted-comment-trigger", {f.rule for f in lint(worse)})

    def test_review_interpolating_the_comment_into_a_script(self):
        bad = read("claude-review.yml").replace('gh pr diff "$PR_NUMBER"', 'echo "${{ github.event.comment.body }}"; gh pr diff "$PR_NUMBER"', 1)
        self.assertIn("ci.script-injection", errors(lint(bad)))


class ConfigKeys(unittest.TestCase):
    def test_keys_are_registered_with_the_documented_defaults(self):
        reg = registry.load_registry()
        expect = {"delivery": "local", "git_autonomy": "none", "max_ci_fix_attempts": 2, "always_human_paths": [], "verify_build": "",
                  "verify_lint": "", "verify_typecheck": "", "verify_require": ["tests"], "verify_ports": [], "verify_dev": "",
                  "verify_ready_path": "/", "verify_timeout_s": 600, "verify_run_timeout_s": 120, "verify_env_allow": []}
        for name, default in expect.items():
            with self.subTest(name):
                key = reg.get(name)
                self.assertIsNotNone(key)
                self.assertEqual(key.default, default)
                self.assertEqual(key.owner_phase, "3-pr-delivery")
                self.assertTrue(key.h6_protected)
                self.assertFalse(key.evidence_gated)
        self.assertEqual(reg.get("delivery").choices, ("local", "push-branch", "draft-pr"))
        self.assertEqual(reg.get("git_autonomy").choices, ("none", "branch", "branch+commit"))

    def test_the_generated_kit_config_loads_clean_with_v1_behaviour_by_default(self):
        cfg = config.load_file(os.path.join(REPO_ROOT, ".claude", "loop.config.md"))
        self.assertEqual(cfg.errors, [])
        self.assertEqual((cfg.get_str("delivery"), cfg.get_str("git_autonomy")), ("local", "none"))
        self.assertTrue(cfg.is_placeholder("verify_build"))
        self.assertEqual(cfg.evidence_gated_violations(), [])

    def test_a_branch_cannot_change_delivery_unnoticed(self):
        base = config.load_text("```yaml\ndelivery: local\n```\n")
        head = config.load_text("```yaml\ndelivery: draft-pr\nverify_tests: true\n```\n")
        changed = {n for n, _b, _h in config.h6_changes(base, head)}
        self.assertTrue({"delivery", "verify_tests"} <= changed)


class PrBodyTemplate(unittest.TestCase):
    def test_pr_prepare_and_verify_are_importable_with_a_documented_api(self):
        import pr_prepare
        import verify
        for name in ("FIELDS", "parse_body", "validate_body", "parse_report", "classify_paths", "find_reverts", "reverts_from_prs", "check",
                     "init_body", "build_commands"):
            self.assertTrue(hasattr(pr_prepare, name), name)
        for name in ("load_definition", "run_verify", "verify", "parse_test_counts", "exit_code", "DefinitionError"):
            self.assertTrue(hasattr(verify, name), name)
        self.assertIn("verify.v1", verify.__doc__)
        self.assertIn("pr_prepare.v1", pr_prepare.__doc__)


if __name__ == "__main__":
    unittest.main()
