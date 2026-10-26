"""Stale references stay removed from the kit's prose (Phase 0b exit criterion).

The v1 prose named plugins and mechanisms the v2 design drops or replaces. This test greps every
prose file the kit ships for them, so a later edit cannot quietly bring one back:

* `superpowers:` skills, the `code-review:code-review` skill, any hardcoded context7 tool name
* the hand-typed telemetry fields `model_used` and `tool_calls_approx`, and the old cap names
  `max_review_rounds` and `tool_call_budget`
* the old home of the stack guide, `docs/choosing-your-stack.md`
* `ESCALATION_NEEDED` used to mean "ask the user" inside a subagent (infra-engineer)

Also checks the readiness-grep contract in orchestrate.md: every `.claude/context/*.md` file that
carries a template sentinel is either a project primer or is on the skip list.

History and design records are excluded by name: CHANGELOG.md, docs/design/, changelog.d/, tests/.
GLOSSARY.md and docs/getting-started.md are hot files owned by the release stage; they are listed in
PENDING_RELEASE with what is still stale in them, and the test fails if one is fixed without removing
the entry, so the list cannot rot.
"""
from __future__ import annotations

import os
import re
import unittest

from . import REPO_ROOT

PROSE_SUFFIXES = (".md", ".mdc", ".json")
EXCLUDE_DIRS = {".git", "docs/design", "changelog.d", "tests", ".claude/worktrees", ".claude/scripts/kitlib/schema/templates"}
EXCLUDE_FILES = {"CHANGELOG.md"}

# pattern name -> regex
BANNED = {
    "superpowers skill": re.compile(r"superpowers:"),
    "code-review skill": re.compile(r"code-review:code-review"),
    "context7 (hardcoded tool or requirement)": re.compile(r"context7", re.I),
    "hand-typed model_used": re.compile(r"model_used"),
    "hand-typed tool_calls_approx": re.compile(r"tool_calls_approx"),
    "old cap name max_review_rounds": re.compile(r"max_review_rounds"),
    "tool-call budget field": re.compile(r"tool_call_budget"),
    "old stack guide path": re.compile(r"docs/choosing-your-stack\.md"),
}

# File -> set of banned pattern names still present there on purpose until the release stage edits it.
PENDING_RELEASE = {
    "GLOSSARY.md": {"context7 (hardcoded tool or requirement)"},
    "docs/getting-started.md": {"old stack guide path"},
    # tools/check-refs.allow keeps the old path allowlisted for the entries above
    "tools/check-refs.allow": {"old stack guide path"},
}

SENTINELS = ("<!--", "TODO(primer)")
PROJECT_PRIMERS = {"admin-app.md", "app.md", "architect.md", "backend.md", "database.md", "infra.md",
                   "local-dev.md", "shared-frontend.md"}


def prose_files():
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        rel_root = os.path.relpath(root, REPO_ROOT).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        dirs[:] = [d for d in dirs
                   if ((rel_root + "/" + d) if rel_root else d) not in EXCLUDE_DIRS and d != "__pycache__"]
        for f in files:
            rel = (rel_root + "/" + f) if rel_root else f
            if rel in EXCLUDE_FILES or not rel.endswith(PROSE_SUFFIXES + (".allow",)):
                continue
            out.append(rel)
    return sorted(out)


def read(rel):
    with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


class NoStaleReferences(unittest.TestCase):
    def test_banned_references_are_gone(self):
        files = prose_files()
        self.assertGreater(len(files), 40)  # the walk found the kit
        hits = []
        for rel in files:
            text = read(rel)
            for name, rx in BANNED.items():
                if rx.search(text) and name not in PENDING_RELEASE.get(rel, ()):
                    hits.append("%s: %s" % (rel, name))
        self.assertEqual(hits, [], "stale references came back (or fix the PENDING_RELEASE list)")

    def test_pending_release_entries_are_still_true(self):
        for rel, names in PENDING_RELEASE.items():
            text = read(rel)
            for name in names:
                with self.subTest("%s / %s" % (rel, name)):
                    self.assertTrue(BANNED[name].search(text),
                                    "%s no longer has this; remove it from PENDING_RELEASE" % rel)

    def test_infra_engineer_uses_needs_clarification_not_ask_the_user(self):
        text = read(".claude/agents/infra-engineer.md")
        self.assertIn("NEEDS_CLARIFICATION", text)
        self.assertNotRegex(text, r"(?i)stop and ask the user|escalate to the user")

    def test_engineer_protocol_has_one_vocabulary_and_one_cap_name(self):
        text = read(".claude/context/engineer-protocol.md")
        for word in ("NEEDS_CLARIFICATION", "ESCALATION_NEEDED", "max_retries_per_item", "Doc Delta", "agent_id"):
            self.assertIn(word, text)
        for word in ("tool-call budget table", "Nominal budget"):
            self.assertNotIn(word, text)

    def test_loop_prose_uses_the_registry_keys(self):
        # keys the prose relies on must exist in the registry (so the generated config documents them)
        from kitlib import registry
        reg = registry.load_registry()
        for key in ("model_profile", "max_design_rounds", "max_item_loc", "quick_lane_max_loc", "max_retries_per_item"):
            self.assertIn(key, reg, key)
        self.assertIn("max_design_rounds", read(".claude/commands/orchestrate.md"))
        self.assertIn("max_design_rounds", read(".claude/commands/architect.md"))
        self.assertIn("quick_lane_max_loc", read(".claude/commands/tech-lead.md"))
        self.assertIn("model_profile", read(".claude/context/model-profiles.md"))

    def test_telemetry_section_records_facts_only(self):
        text = read(".claude/commands/tech-lead.md")
        self.assertIn("Do not record the model used or a tool-call count", text)


class ReadinessGrepContract(unittest.TestCase):
    """orchestrate.md preamble 6(a) greps primers for sentinels and skips the kit-rule files."""

    def test_skip_list_covers_every_kit_rule_file_with_a_sentinel(self):
        orch = read(".claude/commands/orchestrate.md")
        m = re.search(r"\(skip the generic kit-rule files — (.+?) — they quote", orch)
        self.assertIsNotNone(m, "skip list sentence moved; update this test with it")
        skipped = set(re.findall(r"`([a-z0-9-]+\.md)`", m.group(1)))
        ctx = os.path.join(REPO_ROOT, ".claude", "context")
        carrying = {f for f in os.listdir(ctx) if f.endswith(".md")
                    and any(s in read(".claude/context/" + f) for s in SENTINELS)}
        unexplained = carrying - skipped - PROJECT_PRIMERS
        self.assertEqual(unexplained, set(), "a kit-rule file quotes sentinels but is not on the skip list: "
                                             "it would pin every run to per-feature")
        for f in skipped:
            self.assertTrue(os.path.exists(os.path.join(ctx, f)), f)
        self.assertFalse(skipped & PROJECT_PRIMERS)

    def test_project_primers_still_start_as_templates(self):
        for f in PROJECT_PRIMERS:
            with self.subTest(f):
                self.assertIn("<!--", read(".claude/context/" + f))


if __name__ == "__main__":
    unittest.main()
