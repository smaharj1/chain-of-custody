"""The shipped templates are consistent with the report and with the doctor that checks installs of them (TEMPLATE form, IMP-1, IMP-15)."""
from __future__ import annotations

import glob
import json
import os
import re
import unittest

from doctorlib import github

from tests import REPO_ROOT

from . import ghfixtures as gf
from .helpers import BoundaryCase

SEC = os.path.join(REPO_ROOT, ".claude", "templates", "security")


def load(*parts):
    with open(os.path.join(SEC, *parts), "r", encoding="utf-8") as fh:
        return json.load(fh)


class Rulesets(BoundaryCase):
    def test_every_json_template_parses(self):
        files = glob.glob(os.path.join(SEC, "**", "*.json"), recursive=True) + glob.glob(os.path.join(SEC, "*.example"))
        self.assertGreaterEqual(len(files), 12)
        for f in files:
            with self.subTest(file=os.path.relpath(f, SEC)):
                with open(f, "r", encoding="utf-8") as fh:
                    json.load(fh)

    def test_branch_rulesets_follow_rc9_and_b4(self):
        team, solo = load("rulesets", "main-protection.team.json"), load("rulesets", "main-protection.solo.json")
        self.assertEqual(team["bypass_actors"], [])
        self.assertEqual(solo["bypass_actors"], [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}])
        for rs in (team, solo):
            self.assertEqual((rs["target"], rs["enforcement"]), ("branch", "active"))
            self.assertIn("~DEFAULT_BRANCH", rs["conditions"]["ref_name"]["include"])
            types = {r["type"] for r in rs["rules"]}
            self.assertTrue({"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= types)
            pr = [r for r in rs["rules"] if r["type"] == "pull_request"][0]["parameters"]
            self.assertTrue(pr["require_code_owner_review"] and pr["require_last_push_approval"])
            self.assertGreaterEqual(pr["required_approving_review_count"], 1)
            checks = [r for r in rs["rules"] if r["type"] == "required_status_checks"][0]["parameters"]["required_status_checks"]
            self.assertEqual({c["context"] for c in checks}, {"verify", "evidence", "secrets-scan"})
            self.assertTrue(all(c.get("integration_id") for c in checks), "required checks must be pinned to an app")

    def test_no_template_ever_names_the_bot_or_an_app_as_a_bypass_actor(self):
        for f in glob.glob(os.path.join(SEC, "rulesets", "*.json")):
            with self.subTest(file=os.path.basename(f)):
                for a in load("rulesets", os.path.basename(f))["bypass_actors"]:
                    self.assertNotIn(a["actor_type"], ("Integration", "User", "DeployKey"))

    def test_tag_rulesets(self):
        for variant in ("solo", "team"):
            rs = load("rulesets", "tags-protection.%s.json" % variant)
            self.assertEqual(rs["target"], "tag")
            self.assertEqual({r["type"] for r in rs["rules"]}, {"creation", "update", "deletion", "non_fast_forward"})

    def test_the_doctor_accepts_what_the_templates_create(self):
        """Apply the templates in imagination, record the result, and the doctor's GitHub checks pass (a consistency check, not a live one)."""
        for variant, cfg in (("team", {}), ("solo", {"solo_maintainer": True})):
            with self.subTest(variant=variant):
                rs = dict(load("rulesets", "main-protection.%s.json" % variant), id=1, source_type="Repository", source=gf.SLUG)
                repo = self.kit_repo(config=cfg)
                ctx = self.ctx(repo, ghapi=github.fixture_ghapi(gf.build(rulesets=[rs])))
                res = self.results(ctx, {"github"})
                for rid in ("gh.main-protection", "gh.required-review", "gh.last-push-approval", "gh.codeowner-review", "gh.required-checks", "gh.required-checks-source",
                            "gh.bypass", "gh.force-push"):
                    self.assertClean(res, rid)

    def test_a_team_install_with_the_solo_template_is_flagged_by_the_doctor(self):
        rs = dict(load("rulesets", "main-protection.solo.json"), id=1)
        ctx = self.ctx(self.kit_repo(), ghapi=github.fixture_ghapi(gf.build(rulesets=[rs])))
        self.assertFails(self.results(ctx, {"github"}), "gh.bypass")


class OidcTrust(unittest.TestCase):
    FILES = sorted(glob.glob(os.path.join(SEC, "oidc", "*.json")))

    def test_files_exist(self):
        names = {os.path.basename(f) for f in self.FILES}
        self.assertEqual(names, {"aws-apply-environment.trust.json", "aws-apply-job-workflow-ref.trust.json", "aws-plan-preview.trust.json", "aws-drift-readonly.trust.json"})

    def test_subjects_are_pinned_exactly_never_repo_wildcards_or_refs(self):
        for f in self.FILES:
            with self.subTest(file=os.path.basename(f)):
                doc = load("oidc", os.path.basename(f))
                text = json.dumps(doc)
                self.assertNotIn("StringLike", text)
                self.assertNotIn("*", text.replace("sts:AssumeRoleWithWebIdentity", ""))
                self.assertNotIn(":ref:", text)
                self.assertNotRegex(text, r'"repo:[^:"]*:?"')
                cond = doc["Statement"][0]["Condition"]["StringEquals"]
                self.assertEqual(cond["token.actions.githubusercontent.com:aud"], "sts.amazonaws.com")
                sub = cond["token.actions.githubusercontent.com:sub"]
                self.assertRegex(sub, r"^repo:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:(environment:[a-z0-9-]+|job_workflow_ref:.+@refs/heads/main)$")
                self.assertEqual(doc["Statement"][0]["Action"], "sts:AssumeRoleWithWebIdentity")

    def test_apply_trust_is_environment_bound_or_workflow_pinned(self):
        env = load("oidc", "aws-apply-environment.trust.json")["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        self.assertTrue(env.endswith(":environment:production"))
        ref = load("oidc", "aws-apply-job-workflow-ref.trust.json")["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        self.assertIn("job_workflow_ref:acme/widgets/.github/workflows/infra-apply.yml@refs/heads/main", ref)

    def test_plan_and_drift_roles_use_their_own_environments(self):
        self.assertTrue(load("oidc", "aws-plan-preview.trust.json")["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"].endswith("environment:plan-preview"))
        self.assertTrue(load("oidc", "aws-drift-readonly.trust.json")["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"].endswith("environment:drift"))

    def test_the_readonly_policy_never_allows_secret_values(self):
        doc = load("iam", "plan-readonly.policy.json")
        allow = [a for st in doc["Statement"] if st["Effect"] == "Allow" for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])]
        for bad in ("secretsmanager:GetSecretValue", "kms:Decrypt", "ssm:GetParameter", "iam:PassRole", "sts:AssumeRole"):
            self.assertNotIn(bad, allow)
        self.assertFalse([a for a in allow if re.search(r"(Create|Put|Update|Delete|Attach|Run|Start|Terminate|Invoke)", a)])
        deny = [st for st in doc["Statement"] if st["Effect"] == "Deny"][0]
        self.assertIn("secretsmanager:GetSecretValue", deny["Action"])


class GuardrailSnippets(BoundaryCase):
    def test_sandbox_snippet_has_the_report_keys(self):
        s = load("sandbox.settings.json")["sandbox"]
        self.assertTrue(s["enabled"] and s["failIfUnavailable"])
        self.assertIs(s["allowUnsandboxedCommands"], False)
        self.assertEqual(s["excludedCommands"], [])
        self.assertIn("169.254.0.0/16", s["network"]["deniedCidrs"])

    def test_installing_the_snippets_clears_the_doctors_sandbox_and_mcp_findings(self):
        merged = {}
        for f in ("sandbox.settings.json", "mcp-lockdown.settings.json"):
            d = load(f)
            for k, v in d.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        merged["permissions"]["deny"] = list(load("sandbox.settings.json")["permissions"]["deny"]) + list(load("mcp-lockdown.settings.json")["permissions"]["deny"])
        repo = self.kit_repo(files={".claude/settings.json": json.dumps(merged)}, config={"sandbox_verified": "2026-10-26"})
        ctx = self.ctx(repo)
        res = self.results(ctx, {"sandbox", "mcp"})
        self.assertEqual([r.id for r in res if r.status == "fail"], [])
        self.assertEqual(self.by_id(res, "cred.net.metadata") or [], [])
        self.assertEqual(self.check_one(ctx, "cred.net.metadata")[0].status, "pass")

    def test_managed_settings_example(self):
        m = load("managed-settings.example.json")
        self.assertIs(m["allowManagedHooksOnly"], True)
        self.assertEqual(m["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertIn("Workflow", m["permissions"]["deny"])
        cmd = m["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.startswith("python3 /opt/"), "managed hooks must point at a root-owned copy, not the repo")

    def test_mcp_lockdown_and_example(self):
        m = load("mcp-lockdown.settings.json")
        self.assertIs(m["enableAllProjectMcpServers"], False)
        self.assertEqual(m["enabledMcpjsonServers"], [])
        self.assertTrue({"mcp__*__run_script", "mcp__*deploy*"} <= set(m["permissions"]["deny"]))
        ex = load("mcp.json.example")
        self.assertEqual(list(ex["mcpServers"]), ["docs"])

    def test_devcontainer_template_blocks_credential_forwarding(self):
        d = load("devcontainer", "devcontainer.json")
        self.assertEqual(d["mounts"], [])
        s = d["customizations"]["vscode"]["settings"]
        self.assertIs(s["remote.containers.copyGitConfig"], False)
        self.assertEqual(s["dev.containers.gitCredentialHelperConfigLocation"], "none")
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "AWS_ACCESS_KEY_ID"):
            self.assertEqual(d["remoteEnv"][name], "")
        self.assertIn("--cap-drop=ALL", d["runArgs"])

    def test_gitignore_snippet_hides_secrets_and_keeps_the_example(self):
        with open(os.path.join(SEC, "gitignore.snippet"), "r", encoding="utf-8") as fh:
            snippet = fh.read()
        repo = self.kit_repo(files={".gitignore": snippet})
        paths = [".env", ".env.local", ".env.production.local", "a/b/.env.staging", "key.pem", "id_rsa", "terraform.tfstate", ".terraform/x", ".env.example"]
        out = repo.git("check-ignore", "--no-index", "--stdin", input_text="\n".join(paths) + "\n", check=False)
        ignored = set(out.split("\n"))
        self.assertEqual(ignored, set(paths) - {".env.example"})


class LabelGuard(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows", "label-guard.yml"), "r", encoding="utf-8") as fh:
            self.text = fh.read()

    def test_least_privilege_and_no_checkout(self):
        self.assertNotIn("actions/checkout", self.text)
        self.assertNotRegex(self.text, r"\$\{\{\s*secrets\.")
        self.assertRegex(self.text, r"permissions:\n  issues: write\n  pull-requests: write\n")
        self.assertNotRegex(self.text, r"contents:\s*write|id-token")

    def test_untrusted_values_reach_the_shell_only_through_env(self):
        run_block = self.text.split("run: |", 1)[1]
        self.assertNotIn("${{", run_block)
        self.assertIn("LABEL: ${{ github.event.label.name }}", self.text)
        self.assertIn("SENDER: ${{ github.event.sender.login }}", self.text)

    def test_covers_the_labels_the_report_names(self):
        for needle in ("factory:*", "plan-ok", "preview", "factory:go", "factory:requested", "FACTORY_APPROVERS"):
            self.assertIn(needle, self.text)
        self.assertIn("pull_request_target", self.text)

    def test_script_logic_in_a_shell_harness(self):
        """Extract the run: block and execute it with a stubbed gh to prove the decision logic."""
        import subprocess
        import tempfile
        block = self.text.split("run: |\n", 1)[1]
        script = "\n".join(l[10:] if l.startswith("          ") else l for l in block.split("\n"))
        d = tempfile.mkdtemp()
        log = os.path.join(d, "calls.log")
        with open(os.path.join(d, "gh"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> %s\n' % log)
        os.chmod(os.path.join(d, "gh"), 0o755)

        def run(label, sender, approvers="alice, bob"):
            if os.path.exists(log):
                os.unlink(log)
            env = {"PATH": d + os.pathsep + "/usr/bin:/bin", "REPO": "acme/widgets", "LABEL": label, "SENDER": sender, "NUMBER": "7", "APPROVERS": approvers}
            p = subprocess.run(["sh", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            calls = []
            if os.path.exists(log):
                with open(log) as lf:
                    calls = lf.read().strip().split("\n")
            return p.returncode, calls
        try:
            self.assertEqual(run("factory:go", "alice"), (0, []))
            self.assertEqual(run("plan-ok", "bob"), (0, []))
            rc, calls = run("factory:go", "mallory")
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0], "api -X DELETE repos/acme/widgets/issues/7/labels/factory%3Ago")
            self.assertIn("labels[]=factory:requested", calls[1])
            rc, calls = run("preview", "mallory")
            self.assertEqual(calls, ["api -X DELETE repos/acme/widgets/issues/7/labels/preview"])
            self.assertEqual(run("bug", "mallory"), (0, []))
            self.assertEqual(run("factory:requested", "mallory"), (0, []))
            self.assertEqual(run("plan-ok", "mallory", approvers="")[1][0].split()[0], "api")  # an empty approver list approves nobody
            self.assertEqual(run("plan-ok", "alice", approvers="alicia")[1][0].split()[0], "api")  # no substring matching
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class CodeownersTemplate(unittest.TestCase):
    def test_names_the_paths_the_report_requires(self):
        with open(os.path.join(SEC, "CODEOWNERS.template"), "r", encoding="utf-8") as fh:
            text = fh.read()
        for path in ("/.claude/", "/plugin/", "/.claude-plugin/", "/.mcp.json", "/scripts/", "/.github/", "*.tf", "/Dockerfile*", "/vercel.json", "/k8s/", "/helm/"):
            self.assertRegex(text, r"(?m)^%s\s+@PLACEHOLDER" % re.escape(path))
        self.assertIn("never list the bot", text)


if __name__ == "__main__":
    unittest.main()
