"""Agent and command frontmatter obeys the Phase 0b rules (IMP-17, report §12.1, §19.6).

* every agent file has valid frontmatter with name, description, tools, disallowedTools, model, effort
* `maxTurns` is unset everywhere, with the explanatory comment in the file (no baseline supports a p95)
* engineers and critics deny Agent, Skill, Workflow and mcp__*, and carry an explicit `tools:`
  allowlist with no MCP entry (context7 is optional, not a requirement)
* the reviewers are read-only (no Edit, Write or NotebookEdit); the engineers can write
* effort is `high` on critics, database-engineer and backend-engineer (the `quality` profile)
* descriptions no longer promise things the kit does not do ("cheap", the interactive review skill)
* every command file has frontmatter with a description
"""
from __future__ import annotations

import glob
import os
import re
import unittest

from . import REPO_ROOT

AGENTS = sorted(glob.glob(os.path.join(REPO_ROOT, ".claude", "agents", "*.md")))
COMMANDS = sorted(glob.glob(os.path.join(REPO_ROOT, ".claude", "commands", "*.md")))

ENGINEERS = {"database-engineer", "backend-engineer", "app-engineer", "admin-app-engineer", "infra-engineer"}
CRITICS = {"design-critic", "code-reviewer", "security-reviewer"}
HIGH_EFFORT = CRITICS | {"database-engineer", "backend-engineer"}
DENY = {"Agent", "Skill", "Workflow", "mcp__*"}
FORBIDDEN_TOOLS = {"Agent", "Skill", "Workflow"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def split_frontmatter(path):
    """Return (dict of top-level `key: value` pairs, body). Fails the test on malformed frontmatter."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.startswith("---\n"):
        raise AssertionError("%s: does not start with a --- fence" % path)
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError("%s: frontmatter is not closed" % path)
    head, body = text[4:end], text[end + 5:]
    data = {}
    for n, line in enumerate(head.split("\n"), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not m:
            raise AssertionError("%s:%d: not a `key: value` line: %r" % (path, n, line))
        if m.group(1) in data:
            raise AssertionError("%s:%d: duplicate key %s" % (path, n, m.group(1)))
        data[m.group(1)] = m.group(2).strip()
    return data, body


def csv(value):
    return [v.strip() for v in value.split(",") if v.strip()]


class AgentFrontmatter(unittest.TestCase):
    def test_all_eight_agents_present(self):
        names = {os.path.basename(p)[:-3] for p in AGENTS}
        self.assertEqual(names, ENGINEERS | CRITICS)

    def test_required_keys_and_name_matches_file(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                fm, _ = split_frontmatter(path)
                for key in ("name", "description", "tools", "disallowedTools", "model", "effort"):
                    self.assertIn(key, fm, key)
                    self.assertTrue(fm[key], key)
                self.assertEqual(fm["name"], os.path.basename(path)[:-3])

    def test_max_turns_unset_with_comment(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                fm, body = split_frontmatter(path)
                self.assertNotIn("maxTurns", fm)
                self.assertIn("maxTurns is deliberately UNSET", body)

    def test_deny_list_exact(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                fm, _ = split_frontmatter(path)
                self.assertEqual(set(csv(fm["disallowedTools"])), DENY)

    def test_tools_allowlist_has_no_mcp_and_no_spawners(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                fm, _ = split_frontmatter(path)
                tools = csv(fm["tools"])
                self.assertEqual(len(tools), len(set(tools)))
                for t in tools:
                    self.assertFalse(t.startswith("mcp__"), "%s: MCP tool in allowlist" % t)
                    self.assertNotIn(t, FORBIDDEN_TOOLS)
                self.assertNotIn("context7", fm["tools"])
                self.assertNotIn("context7", fm["description"].lower())

    def test_reviewers_read_only_engineers_can_write(self):
        for path in AGENTS:
            name = os.path.basename(path)[:-3]
            with self.subTest(name):
                tools = set(csv(split_frontmatter(path)[0]["tools"]))
                if name in CRITICS:
                    self.assertFalse(tools & WRITE_TOOLS, "read-only role has a write tool")
                else:
                    self.assertTrue({"Edit", "Write"} <= tools)

    def test_effort_and_model(self):
        for path in AGENTS:
            name = os.path.basename(path)[:-3]
            with self.subTest(name):
                fm, _ = split_frontmatter(path)
                self.assertIn(fm["effort"], EFFORTS)
                if name in HIGH_EFFORT:
                    self.assertEqual(fm["effort"], "high")
                self.assertEqual(fm["model"], "opus")  # the `quality` profile; others are passed per invocation

    def test_security_reviewer_can_fetch_advisories(self):
        tools = csv(split_frontmatter(os.path.join(REPO_ROOT, ".claude", "agents", "security-reviewer.md"))[0]["tools"])
        self.assertIn("WebFetch", tools)
        self.assertIn("Bash", tools)  # scanner commands

    def test_descriptions_do_not_overpromise(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                desc = split_frontmatter(path)[0]["description"]
                self.assertNotIn("code-review:code-review", desc)
                self.assertNotRegex(desc, r"(?i)\bcheap\b")
                self.assertNotIn("Skill instead", desc)
        # engineers say how they are dispatched, so they are not picked for ad-hoc delegation
        for name in ENGINEERS:
            desc = split_frontmatter(os.path.join(REPO_ROOT, ".claude", "agents", name + ".md"))[0]["description"]
            self.assertIn("brief path", desc, name)

    def test_agents_never_ask_the_user(self):
        for path in AGENTS:
            with self.subTest(os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotRegex(text, r"(?i)stop and ask the user")
                self.assertNotRegex(text, r"(?i)escalate to the user")

    def test_parser_rejects_malformed(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        bad = os.path.join(d, "x.md")
        for text in ("no fence\n", "---\nname: x\n", "---\nname x\n---\nbody\n", "---\na: 1\na: 2\n---\n"):
            with open(bad, "w") as fh:
                fh.write(text)
            with self.assertRaises(AssertionError, msg=text):
                split_frontmatter(bad)


class CommandFrontmatter(unittest.TestCase):
    def test_every_command_has_a_description(self):
        names = {os.path.basename(p)[:-3] for p in COMMANDS}
        core = {"pm", "architect", "tech-lead", "planner", "orchestrate"}
        later = {"bootstrap", "retro"}   # modes added by Phases 11a and 10 (IMP-3): each is a command until Phase 9 converts them
        self.assertTrue(core <= names <= core | later, sorted(names ^ core))
        for path in COMMANDS:
            with self.subTest(os.path.basename(path)):
                fm, body = split_frontmatter(path)
                self.assertTrue(fm.get("description"))
                self.assertTrue(body.lstrip().startswith("# "))

    def test_tech_lead_description_names_the_agent_reviewer(self):
        fm, _ = split_frontmatter(os.path.join(REPO_ROOT, ".claude", "commands", "tech-lead.md"))
        self.assertIn("code-reviewer", fm["description"])
        self.assertNotIn("skill", fm["description"].lower())


if __name__ == "__main__":
    unittest.main()
