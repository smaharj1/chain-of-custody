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
EXCLUDE_DIRS = {".git", "docs/design", "changelog.d", "tests", ".claude/worktrees", "scripts/kitlib/schema/templates"}
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
    # tools/check-refs.allow keeps the old path allowlisted: CHANGELOG.md history still names it
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


def skill_text(name):
    """SKILL.md plus the reference files next to it (the long modes are split into progressive-disclosure files)."""
    d = os.path.join(REPO_ROOT, "skills", name)
    return "\n".join(read("skills/%s/%s" % (name, f)) for f in sorted(os.listdir(d)) if f.endswith(".md"))


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
        text = read("agents/infra-engineer.md")
        self.assertIn("NEEDS_CLARIFICATION", text)
        self.assertNotRegex(text, r"(?i)stop and ask the user|escalate to the user")

    def test_engineer_protocol_has_one_vocabulary_and_one_cap_name(self):
        text = read("context/engineer-protocol.md")
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
        self.assertIn("max_design_rounds", skill_text("orchestrate"))
        self.assertIn("max_design_rounds", skill_text("architect"))
        self.assertIn("quick_lane_max_loc", skill_text("tech-lead"))
        self.assertIn("model_profile", read("context/model-profiles.md"))

    def test_telemetry_section_records_facts_only(self):
        text = skill_text("tech-lead")
        self.assertIn("Do not record the model used or a tool-call count", text)


ORCHESTRATE = [os.path.join(REPO_ROOT, "skills", "orchestrate", n) for n in ("SKILL.md", "loop.md", "preamble.md")]
# commands only a human runs (kitlib/loopctl_table.py): the orchestrate prose may only PRINT them, on a line that says HUMAN STEP
HUMAN_CMD = re.compile(r"loopctl (approve (plan|env|breaker|cost|demotion)\b|resume\b|reconcile --live|remote-lock (acquire|renew|release|steal)\b|claim (take|release)\b|"
                       r"migrate-plan --write --force|parallel \S+ --apply)")


class HumanStepsInProse(unittest.TestCase):
    """The conductor never runs a human-only loopctl command and never pushes (review fixes D3, D11)."""

    def lines(self):
        for path in ORCHESTRATE:
            with open(path, encoding="utf-8") as fh:
                for n, ln in enumerate(fh, 1):
                    yield os.path.basename(path), n, ln

    def test_human_only_commands_appear_only_on_human_step_lines(self):
        for f, n, ln in self.lines():
            if HUMAN_CMD.search(ln):
                self.assertIn("HUMAN STEP", ln, "%s:%d names a human-only command outside a HUMAN STEP line" % (f, n))

    def test_the_merge_approval_and_the_reset_are_human_steps_and_the_relay_forms_are_complete(self):
        relay_seen = 0
        for f, n, ln in self.lines():
            for m in re.finditer(r"loopctl approve (item|unblock|checkpoint) ([^`\n]*)", ln):
                scope, rest = m.group(1), m.group(2)
                if "HUMAN STEP" in ln.split("loopctl approve", 1)[0] or "HUMAN STEP" in ln and ln.index("HUMAN STEP") < m.start():
                    continue
                relay_seen += 1
                if scope == "item":
                    self.assertRegex(rest, r"--choice (reject|stop)", "%s:%d: approve item without --choice reject|stop is the merge approval" % (f, n))
                elif scope == "unblock":
                    self.assertRegex(rest, r"--choice (keep|replan)", "%s:%d: approve unblock may be relayed only with keep or replan" % (f, n))
                self.assertIn("--note", rest, "%s:%d: a relay form carries the human's reply in --note" % (f, n))
                self.assertRegex(rest, r"^(<id>|<item>)", "%s:%d: the scope is followed by the item id" % (f, n))
        self.assertGreaterEqual(relay_seen, 4)

    def test_remote_lock_and_claims_are_printed_for_the_human_never_run(self):
        text = "".join(ln for _f, _n, ln in self.lines())
        self.assertIn("HUMAN STEP: in a plain terminal run: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/loopctl remote-lock acquire", text)
        self.assertIn("you never push the lock", text)
        # the push tier: the conductor pushes only its own item branch, never a protected one
        self.assertIn("push to main or any other protected branch", text)


class ReadinessGrepContract(unittest.TestCase):
    """the orchestrate skill, preamble 6(a), greps primers for sentinels and skips the kit-rule files."""

    def test_skip_list_covers_every_kit_rule_file_with_a_sentinel(self):
        orch = skill_text("orchestrate")
        m = re.search(r"\(skip the generic kit-rule files — (.+?) — they quote", orch)
        self.assertIsNotNone(m, "skip list sentence moved; update this test with it")
        skipped = set(re.findall(r"`([a-z0-9-]+\.md)`", m.group(1)))
        # the kit's own context files (a project that migrated from a copy install may still hold copies of them in .claude/context)
        ctx = os.path.join(REPO_ROOT, "context")
        carrying = {f for f in os.listdir(ctx) if f.endswith(".md")
                    and any(s in read("context/" + f) for s in SENTINELS)}
        unexplained = carrying - skipped - PROJECT_PRIMERS
        self.assertEqual(unexplained, set(), "a kit-rule file quotes sentinels but is not on the skip list: "
                                             "it would pin every run to per-feature")
        for f in skipped:
            self.assertTrue(os.path.exists(os.path.join(ctx, f)), f)
        self.assertFalse(skipped & PROJECT_PRIMERS)

    def test_project_primers_still_start_as_templates(self):
        for f in PROJECT_PRIMERS:
            with self.subTest(f):
                self.assertIn("<!--", read("seed/.claude/context/" + f))


if __name__ == "__main__":
    unittest.main()
