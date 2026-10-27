"""init_project.py: the project settings a plugin cannot ship (Phase 9, IMP-3, report P19).

Run against the generated plugin/ (the plugin layout) and .claude/ (the copy layout). In-process; no git.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from tests import REPO_ROOT
from tests.dist import call

import init_project  # noqa: E402  (tests/__init__ puts .claude/scripts on sys.path)

PLUGIN = os.path.join(REPO_ROOT, "plugin")
COPY = os.path.join(REPO_ROOT, ".claude")


def run(project, *args, env=None, kit=PLUGIN):
    return call(lambda argv, e: init_project.main(argv, e, kit), ["--repo", project] + list(args), env if env is not None else {})


def read(project, rel):
    with open(os.path.join(project, rel), encoding="utf-8") as fh:
        return fh.read()


class InitProject(unittest.TestCase):
    def setUp(self):
        self.p = tempfile.mkdtemp(prefix="kit-init-")
        self.addCleanup(shutil.rmtree, self.p, True)

    def test_dry_run_prints_diffs_and_writes_nothing(self):
        rc, out, err = run(self.p)
        self.assertEqual(rc, 0, err)
        for needle in ("--- .claude/settings.json", "+++ b/.claude/settings.json", "Bash(terraform apply:*)", "--- .claude/CLAUDE.md", "--- .claude/loop.config.md",
                       "--- .claude/context/backend.md", "--- .gitignore", "to apply, from a plain terminal"):
            self.assertIn(needle, out)
        self.assertEqual(os.listdir(self.p), [])

    def test_apply_writes_the_deny_block_the_sandbox_the_seeds_and_the_gitignore(self):
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 0, err)
        s = json.loads(read(self.p, ".claude/settings.json"))
        self.assertIn("Bash(terraform apply:*)", s["permissions"]["deny"])
        self.assertIn("Edit(.claude/settings.json)", s["permissions"]["deny"])
        self.assertNotIn("allow", s["permissions"], "the allow list is the project's")
        self.assertNotIn("hooks", s, "the plugin's hooks.json supplies the guards")
        self.assertIs(s["sandbox"]["enabled"], True)
        self.assertIs(s["sandbox"]["allowUnsandboxedCommands"], False)
        self.assertIs(s["enableAllProjectMcpServers"], False)
        for rel in (".claude/CLAUDE.md", ".claude/loop.config.md", ".claude/context/backend.md", ".claude/context/local-dev.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.p, rel)), rel)
        gi = read(self.p, ".gitignore")
        for line in (".env*", "*.pem", ".claude/loop.jsonl", ".claude/.migrate/", ".claude/settings.local.json"):
            self.assertIn(line, gi.split("\n"))

    def test_a_second_apply_has_nothing_to_do(self):
        run(self.p, "--apply")
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)

    def test_existing_settings_are_added_to_never_removed_or_changed(self):
        os.makedirs(os.path.join(self.p, ".claude"))
        mine = {"permissions": {"allow": ["Bash(make test:*)"], "deny": ["Bash(rm -rf:*)"], "defaultMode": "plan"}, "sandbox": {"enabled": False, "extra": 1},
                "model": "opus", "hooks": {"Stop": []}}
        with open(os.path.join(self.p, ".claude/settings.json"), "w") as fh:
            json.dump(mine, fh)
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 0, err)
        s = json.loads(read(self.p, ".claude/settings.json"))
        self.assertEqual(s["permissions"]["allow"], ["Bash(make test:*)"])
        self.assertEqual(s["permissions"]["deny"][0], "Bash(rm -rf:*)")
        self.assertGreater(len(s["permissions"]["deny"]), 50)
        self.assertEqual(s["permissions"]["defaultMode"], "plan")
        self.assertIs(s["sandbox"]["enabled"], False, "a value of yours that differs is kept")
        self.assertEqual(s["sandbox"]["extra"], 1)
        self.assertIs(s["sandbox"]["failIfUnavailable"], True, "what you had no value for is set")
        self.assertEqual((s["model"], s["hooks"]), ("opus", {"Stop": []}))
        self.assertIn("sandbox.enabled is false in your settings", out)
        self.assertEqual(json.loads(read(self.p, ".claude/settings.json.pre-init")), mine, "the original is backed up")

    def test_settings_that_are_not_plain_json_are_refused_untouched(self):
        os.makedirs(os.path.join(self.p, ".claude"))
        with open(os.path.join(self.p, ".claude/settings.json"), "w") as fh:
            fh.write('{ // comment\n"a": 1}')
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("not plain JSON", err)
        self.assertEqual(read(self.p, ".claude/settings.json"), '{ // comment\n"a": 1}')

    def test_human_only(self):
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            rc, out, err = run(self.p, "--apply", env={marker: "1"})
            self.assertEqual(rc, 3)
            self.assertIn("refusing to run inside a Claude Code session", err)
        self.assertEqual(os.listdir(self.p), [])
        rc, out, err = run(self.p, env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 0, "the dry run is fine inside a session")

    def test_user_config_choices_become_registry_checked_loop_config_keys(self):
        rc, out, err = run(self.p, "--option", "protected_branches=main, release", "--option", "integration_branch=v2/integration",
                           "--option", "main_edit_allow=docs/**,notes/**", "--apply")
        self.assertEqual(rc, 0, err)
        cfg = read(self.p, ".claude/loop.config.md")
        self.assertRegex(cfg, r"(?m)^protected_branches: \[main, release\]")
        self.assertRegex(cfg, r"(?m)^integration_branch: v2/integration")
        self.assertRegex(cfg, r"(?m)^main_edit_allow: \[docs/\*\*, notes/\*\*\]")
        self.assertIn("H6 keys", out)

    def test_the_environment_variables_claude_code_sets_are_read(self):
        rc, out, err = run(self.p, env={"CLAUDE_PLUGIN_OPTION_INTEGRATION_BRANCH": "dev"}, kit=PLUGIN)
        self.assertEqual(rc, 0, err)
        self.assertIn("integration_branch", out)

    def test_defaults_and_unknown_options(self):
        rc, out, err = run(self.p, "--option", "protected_branches=main,master")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("keys set from your choices", out, "the registry default needs no write")
        rc, out, err = run(self.p, "--option", "autonomy_level=5")
        self.assertEqual(rc, 2)
        self.assertIn("unknown option", err)
        rc, out, err = run(self.p, "--option", "nonsense")
        self.assertEqual(rc, 2)

    def test_a_copy_install_gets_settings_and_gitignore_only(self):
        rc, out, err = run(self.p, kit=COPY)
        self.assertEqual(rc, 0, err)
        self.assertIn("copy install", out)
        self.assertNotIn("--- .claude/CLAUDE.md", out, "a copy install already has its seed files")
        self.assertIn("--- .claude/settings.json", out)
        self.assertIn("activating the v2 guards", out)

    def test_existing_seed_files_are_not_overwritten(self):
        os.makedirs(os.path.join(self.p, ".claude"))
        with open(os.path.join(self.p, ".claude/CLAUDE.md"), "w") as fh:
            fh.write("mine\n")
        run(self.p, "--apply")
        self.assertEqual(read(self.p, ".claude/CLAUDE.md"), "mine\n")

    def test_a_kit_without_templates_fails_clearly(self):
        empty = tempfile.mkdtemp(prefix="kit-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        rc, out, err = run(self.p, kit=empty)
        self.assertEqual(rc, 2)
        self.assertIn("interactive.settings.json is missing", err)


if __name__ == "__main__":
    unittest.main()
