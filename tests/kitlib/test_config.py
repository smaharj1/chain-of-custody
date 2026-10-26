"""Config loading, validation, typed access, base-sha loading (IMP-11, report §9)."""
from __future__ import annotations

import os
import unittest

from kitlib import config, registry
from tests.helpers import BaseTestCase

HEADER = "# Loop Config\n\nintro\n\n```yaml\n"
FOOTER = "```\n\n## Notes\n- x\n"


def cfg_text(body):
    return HEADER + body + FOOTER


class ExtractBlock(unittest.TestCase):
    def test_extracts_the_single_block_and_its_offset(self):
        text = "a\nb\n```yaml\nx: 1\n```\nz\n"
        self.assertEqual(config.extract_yaml_block(text), ("x: 1", 3))

    def test_no_block_two_blocks_and_unclosed_fence_are_errors(self):
        for text, frag in (("nothing", "no fenced"), ("```yaml\na: 1\n```\n```yaml\nb: 2\n```\n", "2 fenced"),
                           ("```yaml\na: 1\n", "never closed")):
            with self.subTest(frag):
                with self.assertRaises(config.ConfigError) as cm:
                    config.extract_yaml_block(text)
                self.assertIn(frag, str(cm.exception))

    def test_other_languages_and_longer_fences(self):
        text = "```bash\nls\n```\n````yaml\nk: v\n```\nstill inside\n````\n"
        body, off = config.extract_yaml_block(text)
        self.assertIn("k: v", body)
        self.assertIn("still inside", body)

    def test_tilde_fence_and_case(self):
        self.assertEqual(config.extract_yaml_block("~~~YAML\na: 1\n~~~\n")[0], "a: 1")


class Validation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry.load_registry()

    def load(self, body):
        return config.load_text(cfg_text(body), self.reg)

    def test_valid_file_has_no_issues(self):
        c = self.load("autonomy: per-feature\nmax_items_per_run: 5\n")
        self.assertEqual(c.issues, [])
        self.assertEqual(c.get("autonomy"), "per-feature")
        self.assertEqual(c.get_int("max_items_per_run"), 5)

    def test_unknown_key_has_suggestion_and_file_line(self):
        c = self.load("autonomy: per-feature\nmax_item_per_run: 5\n")
        self.assertEqual(len(c.errors), 1)
        e = c.errors[0]
        self.assertEqual(e.key, "max_item_per_run")
        self.assertIn("did you mean 'max_items_per_run'", e.message)
        self.assertEqual(e.line, 7)  # header is 5 lines, yaml starts on line 6
        with self.assertRaises(config.ConfigError):
            c.require_valid()

    def test_type_errors(self):
        c = self.load("max_items_per_run: many\nenforce_engineer_git_hook: 1\nautonomy: sometimes\nverify_tests: 5\n")
        self.assertEqual(sorted(i.key for i in c.errors),
                         ["autonomy", "enforce_engineer_git_hook", "max_items_per_run", "verify_tests"])

    def test_gate_toggles_nested_and_validated(self):
        c = self.load("gates:\n  g6: report\n  g9: off\n")
        self.assertEqual(c.errors, [])
        self.assertEqual(c.gate("g6"), "report")
        self.assertEqual(c.gate("g9"), "off")
        self.assertEqual(c.gate("g1"), "on")  # unset means on
        self.assertEqual(c.gates(), {"g6": "report", "g9": "off"})
        bad = self.load("gates:\n  g6: reprot\n")
        self.assertEqual([i.key for i in bad.errors], ["gates.g6"])
        self.assertEqual(bad.gate("g6"), "on")  # an invalid toggle fails closed to on

    def test_yaml_error_becomes_config_error_with_file_line(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load("a: 1\na: 2\n")
        self.assertIn("line 7", str(cm.exception))

    def test_non_mapping_block(self):
        with self.assertRaises(config.ConfigError):
            self.load("- a\n- b\n")

    def test_empty_block_is_all_defaults(self):
        c = self.load("")
        self.assertEqual(c.get("autonomy"), "per-milestone")
        self.assertEqual(c.effective()["max_tool_calls_per_run"], 600)

    def test_placeholder_detection(self):
        c = self.load("verify_tests: pnpm test\n")
        self.assertFalse(c.is_placeholder("verify_tests"))
        self.assertTrue(c.is_placeholder("verify_run"))  # default is <command>

    def test_typed_accessors_reject_wrong_types(self):
        c = self.load("")
        with self.assertRaises(config.ConfigError):
            c.get_int("autonomy")
        with self.assertRaises(config.ConfigError):
            c.get_str("max_items_per_run")
        with self.assertRaises(config.ConfigError):
            c.get_int("enforce_engineer_git_hook")  # a bool is not an int
        self.assertIs(c.get_bool("enforce_engineer_git_hook"), True)
        self.assertEqual(c.get_float("max_items_per_run"), 25.0)

    def test_evidence_gated_violations(self):
        self.assertEqual(self.load("autonomy: unattended\n").evidence_gated_violations(), ["autonomy"])
        self.assertEqual(self.load("autonomy: per-feature\n").evidence_gated_violations(), [])

    def test_get_unknown_key_uses_fallback(self):
        self.assertEqual(self.load("").get("not_a_key", "fb"), "fb")


class Deprecation(BaseTestCase):
    def test_deprecated_key_moves_to_replacement_with_warning(self):
        import json
        d = self.tmpdir()
        base = {"owner_phase": "t", "h6_protected": False, "evidence_gated": False, "section": "s", "doc": ["d"], "type": "int"}
        with open(os.path.join(d, "v1.json"), "w") as fh:
            json.dump({"schema_version": 1, "phase": "t", "sections": [{"id": "s"}], "keys": [
                dict(base, name="old_name", default=1, deprecated_by="new_name"),
                dict(base, name="new_name", default=2, deprecated_by=None)]}, fh)
        reg = registry.load_registry(d)
        c = config.load_text(cfg_text("old_name: 9\n"), reg)
        self.assertEqual(c.get("new_name"), 9)
        self.assertEqual([w.key for w in c.warnings], ["old_name"])
        c2 = config.load_text(cfg_text("old_name: 9\nnew_name: 4\n"), reg)
        self.assertEqual(c2.get("new_name"), 4)  # an explicit new key wins


class BaseShaLoading(BaseTestCase):
    def test_load_at_sha_reads_the_committed_file_not_the_working_tree(self):
        repo = self.repo()
        path = ".claude/loop.config.md"
        base = repo.commit_file(path, cfg_text("max_items_per_run: 5\ngates:\n  g6: on\n"), "base")
        repo.commit_file(path, cfg_text("max_items_per_run: 99\ngates:\n  g6: off\n"), "head weakens the gate")
        base_cfg = config.load_at_sha(repo.path, base)
        head_cfg = config.load_at_sha(repo.path, repo.head())
        self.assertEqual(base_cfg.get_int("max_items_per_run"), 5)
        self.assertEqual(base_cfg.gate("g6"), "on")
        self.assertEqual(head_cfg.gate("g6"), "off")
        # an uncommitted edit changes nothing about a sha
        repo.write(path, cfg_text("max_items_per_run: 1\n"))
        self.assertEqual(config.load_at_sha(repo.path, base).get_int("max_items_per_run"), 5)

    def test_missing_at_sha_is_a_config_error(self):
        repo = self.repo()
        with self.assertRaises(config.ConfigError):
            config.load_at_sha(repo.path, repo.head())

    def test_h6_changes(self):
        reg = registry.load_registry()
        a = config.load_text(cfg_text("max_items_per_run: 5\ndb_local: sqlite\ngates:\n  g6: on\n"), reg)
        b = config.load_text(cfg_text("max_items_per_run: 50\ndb_local: postgres\ngates:\n  g6: off\n"), reg)
        changes = config.h6_changes(a, b)
        self.assertEqual([c[0] for c in changes], ["gates.g6", "max_items_per_run"])
        self.assertEqual(changes[1], ("max_items_per_run", 5, 50))
        self.assertEqual(config.h6_changes(a, a), [])
        # db_local is not H6, so changing it alone is not reported
        c = config.load_text(cfg_text("max_items_per_run: 5\ndb_local: postgres\ngates:\n  g6: on\n"), reg)
        self.assertEqual(config.h6_changes(a, c), [])

    def test_the_real_config_loads_from_a_sha_in_this_repo(self):
        from tests import REPO_ROOT
        head = config.gitutil.rev_parse("HEAD", REPO_ROOT)
        self.assertEqual(config.load_at_sha(REPO_ROOT, head).errors, [])


if __name__ == "__main__":
    unittest.main()
