"""The plugin-install checks (doctorlib/plugin.py) and the doctor's kit version: both come from the plugin folder, never the project."""
from __future__ import annotations

import json
import os
import unittest

from doctorlib import plugin  # noqa: F401  (registers the checks)

from tests import REPO_ROOT

from .helpers import BoundaryCase

PID = "chain-of-custody@chain-of-custody"


class PluginChecks(BoundaryCase):
    GROUPS = {"plugin", "version"}

    def row(self, repo, rid, **kw):
        return self.by_id(self.results(self.ctx(repo, **kw)), rid)[0]

    def test_kit_version_is_read_from_the_plugin_folder(self):
        repo = self.kit_repo(kit_version="2.3.4 (2026-10-30)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.310\n")
        repo.write(".claude/KIT_VERSION", "9.9.9 (2020-01-01)\n")         # an old copy install's file is not the kit's version
        r = self.row(repo, "version.kit")
        self.assertEqual(r.status, "pass")
        self.assertIn("2.3.4", r.message)

    def test_missing_kit_version_names_the_plugin_file(self):
        repo = self.kit_repo()
        os.unlink(os.path.join(self.kit_dir, "KIT_VERSION"))
        r = self.row(repo, "version.kit")
        self.assertEqual(r.status, "fail")
        self.assertIn(os.path.join(self.kit_dir, "KIT_VERSION"), r.message)

    def test_plugin_json_and_kit_version_agree(self):
        repo = self.kit_repo()
        self.assertEqual(self.row(repo, "plugin.version").status, "pass")
        with open(os.path.join(self.kit_dir, ".claude-plugin", "plugin.json"), "w") as fh:
            json.dump({"name": "chain-of-custody", "version": "1.9.0"}, fh)
        r = self.row(repo, "plugin.version")
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn("1.9.0", r.message)

    def test_a_settings_file_that_disables_the_plugin_is_an_error(self):
        repo = self.kit_repo(files={".claude/settings.json": json.dumps({"enabledPlugins": {PID: False}})})
        r = self.row(repo, "plugin.enabled")
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn(".claude/settings.json", r.message)

    def test_the_most_specific_scope_wins(self):
        repo = self.kit_repo(files={".claude/settings.json": json.dumps({"enabledPlugins": {PID: False}}),
                                    ".claude/settings.local.json": json.dumps({"enabledPlugins": {PID: True}})})
        self.assertEqual(self.row(repo, "plugin.enabled").status, "pass")
        repo2 = self.kit_repo()
        os.makedirs(os.path.join(self.fake_home.path, ".claude"), exist_ok=True)
        with open(os.path.join(self.fake_home.path, ".claude", "settings.json"), "w") as fh:
            json.dump({"enabledPlugins": {PID: False}}, fh)
        r = self.row(repo2, "plugin.enabled")
        self.assertEqual(r.status, "fail")
        self.assertIn("~/.claude/settings.json", r.message)

    def test_no_entry_means_enabled(self):
        self.assertEqual(self.row(self.kit_repo(), "plugin.enabled").status, "pass")

    def test_an_old_copy_install_points_to_migrate(self):
        repo = self.kit_repo(files={".claude/scripts/doctor.py": "", ".claude/KIT_MANIFEST": "{}"})
        r = self.row(repo, "plugin.copy-install")
        self.assertEqual((r.status, r.severity), ("fail", "error"))
        self.assertIn(".claude/scripts/", r.message)
        self.assertIn("/chain-of-custody:migrate", r.remediation)
        self.assertIn(os.path.join(REPO_ROOT, "scripts", "migrate.py"), r.remediation)

    def test_kit_hooks_in_settings_are_a_copy_install(self):
        hooks = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/scripts/guards/run.py git"}]}]}}
        r = self.row(self.kit_repo(files={".claude/settings.json": json.dumps(hooks)}), "plugin.copy-install")
        self.assertEqual(r.status, "fail")
        self.assertIn("settings.json", r.message)

    def test_a_plugin_project_has_no_copy_install(self):
        self.assertEqual(self.row(self.kit_repo(), "plugin.copy-install").status, "pass")

    def test_missing_seed_files_point_to_init(self):
        repo = self.kit_repo()
        os.unlink(os.path.join(repo.path, ".claude", "CLAUDE.md"))
        r = self.row(repo, "plugin.seed")
        self.assertEqual((r.status, r.severity), ("fail", "warn"))
        self.assertIn(".claude/CLAUDE.md", r.message)
        self.assertIn("/chain-of-custody:init", r.remediation)

    def test_agent_overrides_are_listed(self):
        repo = self.kit_repo(files={".claude/agents/backend-engineer.md": "mine\n", ".claude/agents/my-own-helper.md": "mine\n"})
        r = self.row(repo, "plugin.overrides")
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.detail["overrides"], ["backend-engineer"])

    def test_the_kit_checkout_is_not_a_project(self):
        for rid in ("plugin.copy-install", "plugin.seed", "plugin.overrides", "plugin.enabled"):
            ctx = self.ctx(type("R", (), {"path": REPO_ROOT})(), kit_root=REPO_ROOT)
            r = self.by_id(self.results(ctx), rid)[0]
            self.assertEqual(r.status, "skipped", rid)


if __name__ == "__main__":
    unittest.main()
