"""Secrets policy (report §5.4) and CODEOWNERS coverage (H3/H6, B4)."""
from __future__ import annotations

import os
import unittest

from doctorlib import codeowners

from .helpers import BoundaryCase

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".claude", "templates", "security")


def read(*parts):
    with open(os.path.join(TEMPLATE, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


class Secrets(BoundaryCase):
    GROUPS = {"secrets"}

    def test_tracked_env_files_keys_state_and_credentials(self):
        files = {".env": "A=1\n", ".env.production": "A=1\n", "app/.env.local": "A=1\n", "certs/server.key": "-----BEGIN RSA " + "PRIVATE KEY-----\nxx\n",
                 "infra/terraform.tfstate": "{}", ".netrc": "machine x\n", "infra/prod.auto.tfvars": "x=1\n", ".env.example": "A=\n", ".env.sample": "A=\n",
                 "docs/public.key": "just a public key file"}
        repo = self.kit_repo(files=files)
        res = self.results(self.ctx(repo))
        msgs = " ".join(r.message for r in res if r.id == "secrets.tracked" and r.status == "fail")
        for f in (".env ", ".env.production", "app/.env.local", "certs/server.key", "infra/terraform.tfstate", ".netrc"):
            self.assertIn(f, msgs, f)
        self.assertNotIn(".env.example", msgs)
        self.assertNotIn(".env.sample", msgs)
        self.assertNotIn("docs/public.key", msgs)
        tfvars = [r for r in res if "prod.auto.tfvars" in r.message][0]
        self.assertEqual(tfvars.severity, "warn")

    def test_tracked_env_test_is_a_warning(self):
        repo = self.kit_repo(files={".env.test": "A=1\n"})
        hit = self.assertFails(self.results(self.ctx(repo)), "secrets.tracked")
        self.assertEqual(hit[0].severity, "warn")

    def test_clean_repo(self):
        self.assertClean(self.results(self.ctx(self.kit_repo())), "secrets.tracked")

    def test_gitignore_coverage_with_the_shipped_snippet(self):
        repo = self.kit_repo(files={".gitignore": read("gitignore.snippet")})
        self.assertClean(self.results(self.ctx(repo)), "secrets.gitignore")

    def test_gitignore_missing_patterns_are_listed(self):
        repo = self.kit_repo(files={".gitignore": "node_modules/\n.env\n"})
        msg = self.assertFails(self.results(self.ctx(repo)), "secrets.gitignore")[0].message
        self.assertIn(".env.production", msg)
        self.assertIn("server.key", msg)
        self.assertNotIn("covers: .env,", msg)

    def test_gitignore_that_hides_the_env_example_is_flagged(self):
        repo = self.kit_repo(files={".gitignore": ".env*\n*.pem\n*.key\n"})
        self.assertIn(".env.example", self.assertFails(self.results(self.ctx(repo)), "secrets.gitignore")[0].message)

    def test_worktreeinclude(self):
        repo = self.kit_repo(files={".worktreeinclude": ".env.local\n.env.test\n"})
        self.assertClean(self.results(self.ctx(repo)), "secrets.worktreeinclude")
        repo = self.kit_repo(files={".worktreeinclude": ".env.local\n.env.production\n*.pem\n"})
        msg = self.assertFails(self.results(self.ctx(repo)), "secrets.worktreeinclude")[0].message
        self.assertIn(".env.production", msg)

    def test_secret_scanner_presence(self):
        self.assertEqual(self.assertFails(self.results(self.ctx(self.kit_repo())), "secrets.scanner")[0].severity, "warn")
        repo = self.kit_repo(files={".pre-commit-config.yaml": "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n"})
        self.assertClean(self.results(self.ctx(repo)), "secrets.scanner")
        repo = self.kit_repo(files={".github/workflows/s.yml": "name: s\non: push\njobs:\n  s:\n    runs-on: x\n    steps:\n      - uses: trufflesecurity/trufflehog@main\n"})
        self.assertClean(self.results(self.ctx(repo)), "secrets.scanner")

    def test_secret_shaped_strings_in_kit_files(self):
        token = "gh" + "p_" + "A" * 36
        repo = self.kit_repo(files={".claude/context/backend.md": "use token %s here\n" % token})
        hit = self.assertFails(self.results(self.ctx(repo)), "secrets.in-kit-files")
        self.assertIn("backend.md", hit[0].message)
        self.assertNotIn(token, hit[0].message)

    def test_not_a_git_repo_is_unknown(self):
        d = self.tmpdir()
        ctx = self.ctx(type("R", (), {"path": d})())
        res = self.results(ctx)
        self.assertEqual(self.by_id(res, "secrets.tracked")[0].status, "unknown")


class CodeownersParsing(unittest.TestCase):
    def test_matching_follows_gitignore_semantics(self):
        cases = [("*", "a/b.txt", True), ("/.claude/", ".claude/settings.json", True), ("/.claude/", ".claude", False), (".claude", ".claude/x/y.md", True),
                 ("/.mcp.json", ".mcp.json", True), ("/.mcp.json", "sub/.mcp.json", False), ("*.tf", "infra/deep/main.tf", True), ("/infra/", "infra/main.tf", True),
                 ("/scripts/", "docs/scripts/x.sh", False), ("scripts/", "docs/scripts/x.sh", True), ("/bin/*.ts", "bin/app.ts", True), ("/bin/*.ts", "bin/x/app.ts", False),
                 ("docs/**/*.md", "docs/a/b/c.md", True), ("/Pulumi*.yaml", "Pulumi.dev.yaml", True), ("kustomization.yaml", "k8s/base/kustomization.yaml", True),
                 ("/Dockerfile*", "Dockerfile.prod", True), ("?.txt", "a.txt", True), ("?.txt", "ab.txt", False)]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(codeowners.matches(pattern, path), want)

    def test_last_matching_rule_wins(self):
        rules = codeowners.parse("* @a\n/.claude/ @b @c\n/.claude/hooks/ @d\n")
        self.assertEqual(codeowners.owner_of(rules, ".claude/hooks/x.sh")["owners"], ["@d"])
        self.assertEqual(codeowners.owner_of(rules, ".claude/agents/x.md")["owners"], ["@b", "@c"])
        self.assertEqual(codeowners.owner_of(rules, "src/x.py")["owners"], ["@a"])

    def test_a_rule_with_no_owners_unowns_the_path(self):
        rules = codeowners.parse("* @a\n/.claude/\n")
        self.assertEqual(codeowners.owner_of(rules, ".claude/x")["owners"], [])

    def test_comments_and_blank_lines(self):
        rules = codeowners.parse("# c\n\n* @a  # trailing\n")
        self.assertEqual([(r["pattern"], r["owners"]) for r in rules], [("*", ["@a"])])


class CodeownersChecks(BoundaryCase):
    GROUPS = {"codeowners"}

    def co(self, text, config=None, files=None):
        f = {".github/CODEOWNERS": text}
        f.update(files or {})
        repo = self.kit_repo(config=config, files=f)
        return self.results(self.ctx(repo))

    def filled_template(self):
        return read("CODEOWNERS.template").replace("@PLACEHOLDER-owner", "@alice").replace("@PLACEHOLDER-guardrail-owners", "@alice").replace("@PLACEHOLDER-infra-owners", "@alice")\
            .replace("@PLACEHOLDER-data-owners", "@alice").replace("@PLACEHOLDER-security-owners", "@alice")

    def test_shipped_template_covers_every_guardrail_path(self):
        res = self.co(self.filled_template(), files={"infra/main.tf": "", "Dockerfile": "FROM x\n", "vercel.json": "{}"})
        self.assertClean(res, "codeowners.coverage")
        self.assertClean(res, "codeowners.syntax")

    def test_unfilled_template_placeholders_are_flagged(self):
        res = self.co(read("CODEOWNERS.template"))
        self.assertFails(res, "codeowners.syntax")

    def test_catch_all_only_is_a_warning(self):
        res = self.co("* @alice\n")
        hit = self.assertFails(res, "codeowners.coverage")
        self.assertEqual(hit[0].severity, "warn")
        self.assertIn("catch-all", hit[0].message)

    def test_a_guardrail_path_with_no_owner_is_an_error(self):
        res = self.co("/src/ @alice\n")
        hit = self.assertFails(res, "codeowners.coverage")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn(".claude/settings.json", hit[0].message)

    def test_missing_file(self):
        repo = self.kit_repo()
        res = self.results(self.ctx(repo))
        self.assertEqual(self.assertFails(res, "codeowners.file")[0].severity, "warn")

    def test_the_bot_may_not_be_a_code_owner(self):
        cfg = {"bot": {"kind": "github-app", "login": "acme-bot[bot]"}}
        res = self.co(self.filled_template() + "/docs/ @acme-bot\n", cfg)
        self.assertIn("acme-bot", self.assertFails(res, "codeowners.bot")[0].message)
        res = self.co(self.filled_template(), cfg)
        self.assertClean(res, "codeowners.bot")

    def test_codeowners_file_location_precedence(self):
        repo = self.kit_repo(files={"CODEOWNERS": "* @a\n", "docs/CODEOWNERS": "* @b\n"})
        self.assertEqual(self.ctx(repo).fact("codeowners")["path"], "CODEOWNERS")

    def test_iac_files_actually_present_are_sampled(self):
        text = "* @alice\n/.claude/ @alice\n/.mcp.json @alice\n/.claude-plugin/ @alice\n/plugin/ @alice\n/scripts/ @alice\n/.github/ @alice\n"
        res = self.co(text, files={"deploy/stack/main.tf": ""})
        hit = self.assertFails(res, "codeowners.coverage")
        self.assertIn("deploy/stack/main.tf", hit[0].message)


if __name__ == "__main__":
    unittest.main()
