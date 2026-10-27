"""The sandbox template denies writes to the whole H6 write set (review fixes D1, GD-3).

`guards.paths.SANDBOX_DENY_WRITE` is the one list. Every `.claude`-anchored and root-anchored H6 pattern is covered by one of its entries, the sandbox
template carries every entry as `denyWrite` and as an `Edit(...)` deny, the generated settings templates carry the same sandbox block, and the shared deny
block repeats the Edit rules. A new H6 path added to `paths.py` without a sandbox entry fails here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import unittest

from guards import paths, wiring

from tests import REPO_ROOT

TEMPLATE = os.path.join(REPO_ROOT, ".claude", "templates", "security", "sandbox.settings.json")


def _rep(pattern: str) -> str:
    """A representative path for a glob: the directory itself for `dir/**`, `x` for each wildcard."""
    p = pattern[:-3] if pattern.endswith("/**") else pattern
    return p.replace("**/", "").replace("*", "x").replace("?", "x")


def _covered(rep: str, entries) -> bool:
    r = paths.fold(rep)
    for e in entries:
        f = paths.fold(e)
        if r == f or r.startswith(f + "/") or fnmatch.fnmatchcase(r, f):
            return True
    return False


def _load() -> dict:
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        return json.load(fh)


class SandboxTemplate(unittest.TestCase):
    def test_every_h6_pattern_is_covered_by_a_sandbox_entry(self):
        missing = []
        for cat, pats in paths.CLAUDE_ANCHORED:
            if cat == "claude-json":                 # ~/.claude.json: outside the project, the home table covers it
                continue
            for pat in pats:
                if not _covered(".claude/" + _rep(pat), paths.SANDBOX_DENY_WRITE):
                    missing.append(".claude/" + pat)
        for cat, pats in paths.ROOT_ANCHORED:
            for pat in pats:
                if not _covered(_rep(pat), paths.SANDBOX_DENY_WRITE):
                    missing.append(pat)
        self.assertEqual(missing, [], "H6 patterns with no SANDBOX_DENY_WRITE entry (add them to guards/paths.py and the template)")

    def test_the_git_internals_and_the_registry_are_listed(self):
        for e in (".git/hooks", ".git/config", ".git/info", ".git/loopctl/sessions"):
            self.assertIn(e, paths.SANDBOX_DENY_WRITE)

    def test_context_primers_and_pr_files_stay_writable(self):
        """Primers are editable on main (g_file.MAIN_ALLOW); the pr/ directory holds drafts the conductor writes."""
        for ok in (".claude/context/backend.md", ".claude/pr/draft.md", ".claude/KIT_VERSION", "docs/features/x/requirements.md", "src/app.py"):
            self.assertFalse(_covered(ok, paths.SANDBOX_DENY_WRITE), ok)

    def test_the_run_state_the_conductor_writes_is_not_denied(self):
        for ok in (".git/loopctl/run.json", ".git/loopctl/lock", ".git/loopctl/HALT", ".git/loopctl/nonces/abc"):
            self.assertFalse(_covered(ok, paths.SANDBOX_DENY_WRITE), ok)
        self.assertTrue(_covered(".git/loopctl/sessions/s1.json", paths.SANDBOX_DENY_WRITE))

    def test_the_template_has_every_entry_as_denywrite_and_as_an_edit_deny(self):
        t = _load()
        wr = t["sandbox"]["filesystem"]["denyWrite"]
        deny = t["permissions"]["deny"]
        for e in paths.SANDBOX_DENY_WRITE:
            self.assertIn(e, wr, "denyWrite lacks %s" % e)
            self.assertIn(paths.sandbox_edit_rule(e), deny, "permissions.deny lacks %s" % paths.sandbox_edit_rule(e))
        self.assertEqual([e for e in wr if e not in paths.SANDBOX_DENY_WRITE], [], "the template lists entries paths.SANDBOX_DENY_WRITE does not")
        self.assertIn("Read(.git/loopctl/heldout/**)", deny)

    def test_directories_are_denied_with_a_glob_and_files_without_one(self):
        self.assertEqual(paths.sandbox_edit_rule(".claude/scripts"), "Edit(.claude/scripts/**)")
        self.assertEqual(paths.sandbox_edit_rule(".claude/settings.json"), "Edit(.claude/settings.json)")
        self.assertEqual(paths.sandbox_edit_rule(".git/loopctl/sessions"), "Edit(.git/loopctl/sessions/**)")
        for e in paths.SANDBOX_DENY_WRITE_FILES:
            self.assertIn(e, paths.SANDBOX_DENY_WRITE)

    def test_the_generated_settings_templates_carry_the_same_sandbox_block(self):
        t = _load()["sandbox"]
        for name, obj in wiring.build_templates(wiring.load_manifest()).items():
            if "sandbox" in obj:
                self.assertEqual(obj["sandbox"], t, name)

    def test_the_shared_deny_block_repeats_the_edit_rules(self):
        for e in paths.SANDBOX_DENY_WRITE:
            self.assertIn(paths.sandbox_edit_rule(e), wiring.DENY_BASELINE)

    def test_the_snippet_names_absolute_git_dir_paths(self):
        snip = wiring.sandbox_snippet(REPO_ROOT)
        wr = snip["sandbox"]["filesystem"]["denyWrite"]
        self.assertTrue(all(os.path.isabs(x) for x in wr))
        self.assertTrue(any(x.endswith(os.path.join("loopctl", "sessions")) for x in wr))
        self.assertTrue(all(r.startswith("Edit(//") for r in snip["permissions"]["deny"]), "an absolute permission path starts with //")


class McpDenyLists(unittest.TestCase):
    """IN-10: the lockdown snippet, the infra deploy-deny snippet and the shared deny block are one list."""

    def deny(self, *rel):
        with open(os.path.join(REPO_ROOT, ".claude", "templates", *rel), "r", encoding="utf-8") as fh:
            return json.load(fh)["permissions"]["deny"]

    def test_lockdown_and_infra_templates_and_the_deny_block_agree(self):
        self.assertEqual(self.deny("security", "mcp-lockdown.settings.json"), wiring.MCP_DEPLOY_DENY)
        self.assertEqual(self.deny("infra", "mcp-deploy-deny.settings.json"), wiring.MCP_DEPLOY_DENY)
        for r in wiring.MCP_DEPLOY_DENY:
            self.assertIn(r, wiring.DENY_BASELINE)
        for name, obj in wiring.build_templates(wiring.load_manifest()).items():
            for r in wiring.MCP_DEPLOY_DENY:
                self.assertIn(r, obj["permissions"]["deny"], name)


if __name__ == "__main__":
    unittest.main()
