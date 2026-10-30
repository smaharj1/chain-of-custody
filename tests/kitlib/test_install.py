"""kitlib.install: copy-install markers, the kit's hook entries, and the plugin's enabled/installed state."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import install

from tests.helpers import BaseTestCase

PID = install.PLUGIN_ID


def hook(cmd):
    return {"type": "command", "command": cmd}


class KitHooks(BaseTestCase):
    def test_kit_hook_commands(self):
        for cmd in ("python3 .claude/scripts/guards/run.py git", "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh",
                    "sh .claude/hooks/guard-main-edit.sh", 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_start.py"',
                    "python3 $CLAUDE_PLUGIN_ROOT/scripts/guards/run.py"):
            self.assertTrue(install.is_kit_hook(cmd), cmd)
        for cmd in ("say done", "npm run lint", ".claude/hooks/my-own.sh", None, 3):
            self.assertFalse(install.is_kit_hook(cmd), cmd)

    def test_strip_keeps_the_persons_hooks_and_drops_what_empties(self):
        s = {"permissions": {"allow": ["Read"]},
             "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook("python3 .claude/scripts/guards/run.py git"), hook("say pre")]},
                                      {"matcher": "Edit", "hooks": [hook("python3 .claude/scripts/guards/run.py edit")]}],
                       "SessionStart": [{"hooks": [hook("python3 .claude/scripts/session_start.py")]}],
                       "Stop": [{"hooks": [hook("say done")]}]}}
        out, removed = install.strip_kit_hooks(s)
        self.assertEqual(len(removed), 3)
        self.assertEqual(out["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": [hook("say pre")]}], "Stop": [{"hooks": [hook("say done")]}]})
        self.assertEqual(out["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 2, "the input is not changed")

    def test_strip_removes_an_empty_hooks_key(self):
        out, removed = install.strip_kit_hooks({"hooks": {"Stop": [{"hooks": [hook(".claude/hooks/guard-git.sh")]}]}})
        self.assertEqual(out, {})
        self.assertEqual(removed, [".claude/hooks/guard-git.sh"])

    def test_strip_leaves_odd_shapes_alone(self):
        s = {"hooks": {"Stop": "not a list", "X": [{"matcher": "y"}]}}
        self.assertEqual(install.strip_kit_hooks(s), (s, []))


class Markers(BaseTestCase):
    def test_plugin_project_has_none(self):
        r = self.repo()
        r.write(".claude/CLAUDE.md", "x")
        r.write(".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [hook("say done")]}]}}))
        self.assertEqual(install.copy_install_markers(r.path), [])

    def test_each_marker(self):
        r = self.repo()
        r.write(".claude/scripts/doctor.py", "")
        r.write(".claude/KIT_MANIFEST", "{}")
        r.write(".claude/KIT_VERSION", "1.2.0\n")
        r.write(".claude/hooks/guard-git.sh", "")
        r.write(".claude/settings.local.json", json.dumps({"hooks": {"PreToolUse": [{"hooks": [hook(".claude/hooks/guard-main-edit.sh")]}]}}))
        got = install.copy_install_markers(r.path)
        self.assertEqual(got, [".claude/scripts/", ".claude/KIT_MANIFEST", ".claude/KIT_VERSION", ".claude/hooks/guard-git.sh",
                               ".claude/settings.local.json (hooks that run the copied kit)"])

    def test_a_fifo_settings_file_does_not_hang(self):
        r = self.repo()
        os.makedirs(os.path.join(r.path, ".claude"), exist_ok=True)
        os.mkfifo(os.path.join(r.path, ".claude", "settings.json"))
        self.assertIsNone(install.load_settings(os.path.join(r.path, ".claude", "settings.json")))
        self.assertEqual(install.copy_install_markers(r.path), [])


class PluginState(BaseTestCase):
    def test_disabled(self):
        self.assertTrue(install.plugin_disabled({"enabledPlugins": {PID: False}}))
        for s in ({"enabledPlugins": {PID: True}}, {"enabledPlugins": {}}, {}, None, {"enabledPlugins": [PID]}):
            self.assertFalse(install.plugin_disabled(s), s)

    def test_installed(self):
        h = self.tmpdir()
        self.assertIsNone(install.plugin_installed(h))
        os.makedirs(os.path.join(h, ".claude", "plugins"))
        p = os.path.join(h, ".claude", "plugins", "installed_plugins.json")
        for doc, want in (({"plugins": {PID: [{}]}}, True), ({"plugins": {"other@x": []}}, False), ({"plugins": [{"id": PID}]}, True),
                          ({"version": 2}, None)):
            with open(p, "w") as fh:
                json.dump(doc, fh)
            self.assertEqual(install.plugin_installed(h), want, doc)
        with open(p, "w") as fh:
            fh.write("not json")
        self.assertIsNone(install.plugin_installed(h))


if __name__ == "__main__":
    unittest.main()
