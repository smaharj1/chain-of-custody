"""The bootstrap mode, the launch-app skill template and the docs agree with the scripts they name (Phase 11a)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

import bootstrap_config as bc
import bootstrap_detect as bd
import bootstrap_receipt as br
from kitlib import registry

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.helpers import BaseTestCase
from tests.test_frontmatter import split_frontmatter

CMD = os.path.join(REPO_ROOT, ".claude", "commands", "bootstrap.md")
SKILL = os.path.join(REPO_ROOT, ".claude", "templates", "launch-app-skill.md")


def read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class Command(unittest.TestCase):
    def test_frontmatter_and_title(self):
        fm, body = split_frontmatter(CMD)
        self.assertIn("Bootstrap", fm["description"])
        self.assertTrue(body.lstrip().startswith("# Bootstrap Mode Activated"))

    def test_the_never_list_names_the_dangerous_things(self):
        text = read(".claude", "commands", "bootstrap.md")
        never = text.split("## What you never do")[1].split("## The receipt")[0]
        for must in ("gh", "loop.config.md", ".github/**", "CODEOWNERS", ".claude/skills/**", "ANTHROPIC_API_KEY", "--go", "verify.py", "Merge, tag, release or deploy", "data"):
            self.assertIn(must, never)

    def test_child_sessions_are_refused(self):
        text = read(".claude", "commands", "bootstrap.md")
        self.assertIn("role prompt", text)
        self.assertIn("refuse", text)

    def test_receipt_ids_and_finish_agree(self):
        text = read(".claude", "commands", "bootstrap.md")
        for i in ("detect", "config-plan", "setup-script", "doctor", "ci-lint"):
            self.assertIn("`%s`" % i, text)
        self.assertIn("`finish`", text)

    def test_example_commands_parse_with_the_real_parsers(self):
        ap = br.parser()
        ap.parse_args(["init", "--mode", "greenfield", "--stack", "frontend=Next.js"])
        ap.parse_args(["run", "--id", "detect", "--title", "t", "--", "python3", ".claude/scripts/bootstrap_detect.py", "--json"])
        ap.parse_args(["emit", "--id", "config-plan", "--kind", "config-plan", "--path", ".claude/bootstrap/config-plan.json"])
        ap.parse_args(["record", "--id", "labels", "--title", "t", "--command", "c", "--exit", "0", "--no-output"])
        for argv in (["python3", ".claude/scripts/bootstrap_detect.py", "--json"], ["python3", ".claude/scripts/doctor.py", "--json"], ["python3", ".claude/scripts/doctor.py", "--level"],
                     ["python3", ".claude/scripts/ci_lint.py"], ["python3", ".claude/scripts/loopctl", "doctor"], ["python3", ".claude/scripts/probe_run.py"]):
            root = REPO_ROOT
            real, _ = br.check_run_argv(root, argv) if os.path.exists(os.path.join(root, argv[1])) else (None, None)
            if argv[1].endswith("loopctl"):
                continue   # loopctl lands with Phase 4a; the allowlist entry is covered in test_receipt
            self.assertIsNotNone(real, argv)
        reg = registry.load_registry()
        plan = bc.build_plan(REPO_ROOT, ["github_repo=acme/widgets", "solo_maintainer=true", "factory_approvers=[alice]", "verify_tests=pnpm test",
                                         "verify_build=pnpm run build", "verify_ports=[4310]"], reg)
        self.assertEqual(plan["set"]["verify_ports"], [4310])
        sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
        import repo_script
        text = repo_script.generate(repo_script.Params("acme/widgets", create_repo=True, visibility="private", variant="solo", boundary=True, labels_=True, approvers=["alice"],
                                                       workflows=["verify", "label-guard", "intake", "dependency-check", "smoke"],
                                                       config_plan=".claude/bootstrap/config-plan.json", launch_skill=".claude/bootstrap/launch-app.SKILL.md"))
        self.assertIn("setup.sh", text)

    def test_the_command_does_not_promise_what_is_design_only(self):
        text = read(".claude", "commands", "bootstrap.md")
        self.assertIn("Not built yet, on purpose", text)
        self.assertNotIn("creates the UAT", text)

    def test_every_script_path_in_the_prose_exists(self):
        for doc in (os.path.join(".claude", "commands", "bootstrap.md"), os.path.join("docs", "bootstrap.md"), os.path.join("docs", "ops.md"), os.path.join("docs", "release-automation.md")):
            for m in re.finditer(r"\.claude/(?:scripts|templates|context)/[A-Za-z0-9_./-]+", read(doc)):
                path = m.group(0).rstrip(".,:;)")
                if path.endswith("/") or path.endswith("scripts/loopctl"):
                    continue   # loopctl is Phase 4a's; the prose names it as a subcommand to run only if it exists
                self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, path)), "%s names %s" % (doc, path))


class LaunchSkill(unittest.TestCase):
    def test_template_is_a_valid_skill_with_no_hooks(self):
        fm, body = split_frontmatter(SKILL)
        self.assertEqual(fm["name"], "launch-app")
        self.assertTrue(fm["description"])
        self.assertEqual(set(fm), {"name", "description"})   # no hooks, no allowed-tools, nothing that runs
        for banned_name in ("run", "loop", "code-review"):
            self.assertNotEqual(fm["name"], banned_name)

    def test_template_tells_the_filler_what_to_do_and_what_never_to_do(self):
        text = read(".claude", "templates", "launch-app-skill.md")
        self.assertIn("<<", text)
        self.assertIn("Never run this against production", text)
        self.assertIn("process group", text)


class Docs(unittest.TestCase):
    def test_release_doc_names_every_tool_detect_knows(self):
        text = read("docs", "release-automation.md")
        for tool in ("release-please", "changesets", "semantic-release", "goreleaser"):
            self.assertIn(tool, text)
        self.assertEqual(sorted(t for t, _ in bd.RELEASE_FILES), ["changesets", "goreleaser", "release-please", "semantic-release"])
        self.assertIn("H5", text)
        self.assertIn("not auto-merged", text)
        self.assertIn("deploy trigger", text)

    def test_ops_doc_lists_the_labels_in_the_template(self):
        text = read("docs", "ops.md")
        with open(os.path.join(REPO_ROOT, ".claude", "templates", "github", "labels.json")) as fh:
            names = [l["name"] for l in json.load(fh)["labels"]]
        for n in names:
            self.assertIn(n.split(":")[-1], text, n)

    def test_ops_doc_marks_the_design_only_parts(self):
        text = read("docs", "ops.md").split("## What is not built")[1]
        for t in ("UAT feedback widget", "Monitoring to intake", "reproduce-first bug lane"):
            self.assertIn(t, text)

    def test_no_new_config_key_was_declared_by_this_phase(self):
        reg = registry.load_registry()
        self.assertEqual([k for k in reg if k.owner_phase.startswith("11a")], [])


class DoctorSeesTheTemplates(BaseTestCase):
    def test_installing_the_ops_templates_adds_no_deploy_trigger(self):
        root = os.path.realpath(self.tmpdir())
        wf = os.path.join(root, ".github", "workflows")
        os.makedirs(wf)
        src = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")
        for n in ("intake.yml", "dependency-check.yml", "smoke.yml", "label-guard.yml"):
            with open(os.path.join(src, n)) as fh, open(os.path.join(wf, n), "w") as out:
                out.write(fh.read())
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "doctor.py"), "--deploy-surface", "--json", "--repo", root, "--home", root],
                           env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        doc = json.loads(p.stdout)
        by = {c["id"]: c for c in doc["checks"]}
        self.assertEqual(by["deploy.surface"]["status"], "pass", by["deploy.surface"])
        self.assertEqual(by["deploy.release-automation"]["status"], "skipped")

    def test_a_release_please_workflow_is_seen_as_release_automation(self):
        root = os.path.realpath(self.tmpdir())
        wf = os.path.join(root, ".github", "workflows")
        os.makedirs(wf)
        with open(os.path.join(wf, "release.yml"), "w") as fh:
            fh.write("name: release\non:\n  push:\n    branches: [main]\npermissions:\n  contents: write\njobs:\n  r:\n    runs-on: ubuntu-latest\n    steps:\n"
                     "      - uses: googleapis/release-please-action@v4\n")
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "doctor.py"), "--deploy-surface", "--json", "--repo", root, "--home", root],
                           env=plain_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        by = {c["id"]: c for c in json.loads(p.stdout)["checks"]}
        self.assertEqual(by["deploy.release-automation"]["status"], "pass")
        self.assertIn("H5", by["deploy.release-automation"]["message"])


if __name__ == "__main__":
    unittest.main()
