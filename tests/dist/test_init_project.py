"""init_project.py: the starter files and project settings a plugin cannot ship (Phase 9, IMP-3, report P19).

The kit is this repository (the plugin root). In-process; git only where a test says so.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from tests import REPO_ROOT
from tests.dist import call

import init_project  # noqa: E402  (tests/__init__ puts scripts/ on sys.path)

KIT = REPO_ROOT


def run(project, *args, env=None, kit=KIT):
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
        rc, out, err = run(self.p, env={"CLAUDE_PLUGIN_OPTION_INTEGRATION_BRANCH": "dev"})
        self.assertEqual(rc, 0, err)
        self.assertIn("integration_branch", out)

    def test_the_printed_apply_command_carries_the_choices_from_the_environment(self):
        rc, out, err = run(self.p, env={"CLAUDE_PLUGIN_OPTION_INTEGRATION_BRANCH": "dev"})
        self.assertEqual(rc, 0, err)
        self.assertIn("%s --repo %s --option integration_branch=dev --apply" % (os.path.join(KIT, "scripts", "init_project.py"),
                                                                                os.path.realpath(self.p)), out)

    def test_a_choice_the_config_already_has_is_not_written_again(self):
        run(self.p, "--option", "integration_branch=dev", "--apply")
        rc, out, err = run(self.p, "--option", "integration_branch=dev")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("keys set from your choices", out)
        self.assertNotIn("--- .claude/loop.config.md", out)

    def test_the_settings_turn_the_plugin_on_for_teammates(self):
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 0, err)
        s = json.loads(read(self.p, ".claude/settings.json"))
        self.assertIs(s["enabledPlugins"]["chain-of-custody@chain-of-custody"], True)
        self.assertEqual(s["extraKnownMarketplaces"]["chain-of-custody"]["source"], {"source": "github", "repo": "smaharj1/chain-of-custody"})
        os.makedirs(os.path.join(self.p, "sub"))
        with open(os.path.join(self.p, ".claude", "settings.json"), "w") as fh:
            json.dump(dict(s, enabledPlugins={"chain-of-custody@chain-of-custody": False}), fh)
        rc, out, err = run(self.p)
        self.assertEqual(rc, 0, err)
        self.assertFalse(json.loads(read(self.p, ".claude/settings.json"))["enabledPlugins"]["chain-of-custody@chain-of-custody"])
        self.assertIn("yours kept", out, "a project that turned the plugin off keeps it off")

    def test_a_subfolder_of_a_git_repository_means_the_repository_root(self):
        import subprocess
        subprocess.run(["git", "init", "-q", self.p], check=True)
        sub = os.path.join(self.p, "pkg")
        os.makedirs(sub)
        rc, out, err = run(sub, "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("works on the repository root", out)
        self.assertTrue(os.path.isfile(os.path.join(self.p, ".claude", "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(sub, ".claude")))

    def test_defaults_and_unknown_options(self):
        rc, out, err = run(self.p, "--option", "protected_branches=main,master")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("keys set from your choices", out, "the registry default needs no write")
        rc, out, err = run(self.p, "--option", "autonomy_level=5")
        self.assertEqual(rc, 2)
        self.assertIn("unknown option", err)
        rc, out, err = run(self.p, "--option", "nonsense")
        self.assertEqual(rc, 2)

    def test_an_old_copy_install_is_refused_untouched(self):
        for rel, text in ((".claude/scripts/doctor.py", ""), (".claude/KIT_MANIFEST", "{}"), (".claude/KIT_VERSION", "1.2.0\n"),
                          (".claude/settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                              {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh"}]}]}}))):
            p = tempfile.mkdtemp(prefix="kit-init-copy-")
            self.addCleanup(shutil.rmtree, p, True)
            os.makedirs(os.path.dirname(os.path.join(p, rel)), exist_ok=True)
            with open(os.path.join(p, rel), "w") as fh:
                fh.write(text)
            before = sorted(os.listdir(os.path.join(p, ".claude")))
            for args in ((), ("--apply",)):
                rc, out, err = run(p, *args)
                self.assertEqual(rc, 2, rel)
                self.assertIn("old copy install", err)
                self.assertIn("/chain-of-custody:migrate", err)
            self.assertEqual(sorted(os.listdir(os.path.join(p, ".claude"))), before, rel)
            self.assertFalse(os.path.exists(os.path.join(p, ".gitignore")))

    def test_the_kit_checkout_is_refused(self):
        rc, out, err = run(REPO_ROOT)
        self.assertEqual(rc, 2)
        self.assertIn("the kit's own checkout", err)

    def test_the_summary_names_the_kit_folder_and_the_full_path_hint(self):
        rc, out, err = run(self.p)
        self.assertEqual(rc, 0, err)
        lines = out.split("\n")
        self.assertEqual(lines[1], "kit: %s" % KIT)
        self.assertIn("full path", lines[2])
        self.assertIn("note: the guard hooks come from the plugin's hooks/hooks.json", out)

    def test_copied_and_kept_starter_files_are_listed(self):
        os.makedirs(os.path.join(self.p, ".claude", "context"))
        with open(os.path.join(self.p, ".claude/context/backend.md"), "w") as fh:
            fh.write("ours\n")
        rc, out, err = run(self.p, "--apply")
        self.assertEqual(rc, 0, err)
        copied = [ln for ln in out.split("\n") if ln.startswith("starter files copied")][0]
        kept = [ln for ln in out.split("\n") if ln.startswith("starter files you already have")][0]
        self.assertIn(".claude/CLAUDE.md", copied)
        self.assertNotIn("backend.md", copied)
        self.assertIn(".claude/context/backend.md", kept)
        self.assertEqual(read(self.p, ".claude/context/backend.md"), "ours\n")
        seed = os.path.join(KIT, "seed")
        for dirpath, _, files in os.walk(seed):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), seed)
                self.assertTrue(os.path.isfile(os.path.join(self.p, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(self.p, ".claude", "scripts")), "no kit script is copied")
        self.assertFalse(os.path.exists(os.path.join(self.p, ".claude", "agents")), "no kit agent is copied")

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
