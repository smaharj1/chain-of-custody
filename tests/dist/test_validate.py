"""tools/validate-plugin.py: every structural check fires on a plugin broken in exactly that way (Phase 9, IMP-3).

The unbroken plugin is this repository (its root is the plugin root); each case mutates a copy and expects a problem that names the defect.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest

from tests import REPO_ROOT
from tests.dist import TOOLS_DIR

from pluginlib import validate as V  # noqa: E402


def load_cli():
    spec = importlib.util.spec_from_file_location("validate_plugin", os.path.join(TOOLS_DIR, "validate-plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def setj(files, rel, fn):
    doc = json.loads(files[rel].decode())
    fn(doc)
    files[rel] = json.dumps(doc).encode()


class Validate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_cli().load_tree(REPO_ROOT)
        cls.market = cls.plugin[".claude-plugin/marketplace.json"]

    def check(self, mutate, expect, market=None, parse=False):
        files = dict(self.plugin)
        mutate(files)
        problems = V.validate(files, market if market is not None else self.market, parse_python=parse)    # parsing 150 scripts costs 0.2 s: only where it is the subject
        self.assertTrue([p for p in problems if expect in p], "expected a problem containing %r, got %r" % (expect, problems[:6]))

    def test_the_repository_is_clean(self):
        self.assertEqual(V.validate(self.plugin, self.market), [])

    def test_manifest_checks(self):
        pj = ".claude-plugin/plugin.json"
        self.check(lambda f: f.pop(pj), "plugin.json is missing")
        self.check(lambda f: f.__setitem__(pj, b"{nope"), "not valid JSON")
        self.check(lambda f: setj(f, pj, lambda d: d.update(name="Bad Name")), "kebab-case")
        self.check(lambda f: setj(f, pj, lambda d: d.update(version="one")), "semantic version")
        self.check(lambda f: setj(f, pj, lambda d: d.pop("author")), "author.name")
        self.check(lambda f: setj(f, pj, lambda d: d["userConfig"]["integration_branch"].update(type="text")), "userConfig.integration_branch.type")
        self.check(lambda f: setj(f, pj, lambda d: d["userConfig"]["integration_branch"].pop("title")), "integration_branch.title")
        self.check(lambda f: setj(f, pj, lambda d: d["userConfig"].update({"Bad-Key": d["userConfig"]["main_edit_allow"]})), "snake_case")
        self.check(lambda f: setj(f, pj, lambda d: d.update(skills="./nowhere/")), "does not exist")
        self.check(lambda f: setj(f, pj, lambda d: d.update(agents="../outside")), "stay inside the plugin")
        self.check(lambda f: setj(f, pj, lambda d: d["userConfig"]["main_edit_allow"].update(sensitive=True)), "sensitive and has a default")

    def test_hook_checks(self):
        hj = "hooks/hooks.json"
        self.check(lambda f: f.pop(hj), "hooks/hooks.json is missing")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"].update(WorktreeCreate=d["hooks"]["PreToolUse"])), "must not be in the plugin")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"].update(NotAnEvent=d["hooks"]["PreToolUse"])), "unknown event")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].pop("timeout")), "no timeout")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(command='python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/guards/run.py"')),
                   "project's .claude/scripts")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(command='exec python3 "${CLAUDE_PLUGIN_ROOT}/scripts/guards/gone.py"')),
                   "gone.py does not exist")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(command="exec python3 ${CLAUDE_PLUGIN_ROOT}/scripts/guards/run.py")),
                   "does not quote")
        self.check(lambda f: setj(f, hj, lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(type="prompt")), "is not a command")

    def test_agent_checks(self):
        a = "agents/backend-engineer.md"
        def edit(old, new):
            return lambda f: f.__setitem__(a, f[a].decode().replace(old, new, 1).encode())
        self.check(edit("name: backend-engineer", "name: other"), "does not match the file name")
        self.check(edit("disallowedTools: ", "disallowedTools: Bash(rm:*), "), "permission rule")
        self.check(edit("\nmodel:", "\npermissionMode: acceptEdits\nmodel:"), "permissionMode")
        self.check(edit("\nmodel:", "\nhooks: x\nmodel:"), "`hooks` is ignored")
        self.check(edit("\neffort: ", "\neffort: huge\n#"), "effort 'huge'")
        self.check(lambda f: [f.pop(k) for k in [k for k in f if k.startswith("agents/")]], "no agent files")
        # an unquoted description with ": " is not YAML (a nested mapping); one with " #" is cut at the comment
        self.check(edit("Senior staff backend engineer.", "Senior staff backend engineer: the one"), "unquoted value of description contains ': '")
        self.check(edit("Senior staff backend engineer.", "Senior staff backend engineer #1."), "unquoted value of description contains ' #'")

    def test_every_shipped_frontmatter_is_plain_yaml(self):
        for p in sorted(k for k in self.plugin if k.startswith("agents/") and k.endswith(".md") or k.startswith("skills/") and k.endswith("/SKILL.md")):
            self.assertIsNone(V.frontmatter(self.plugin[p].decode())[1], p)

    def test_skill_checks(self):
        s = "skills/pm/SKILL.md"
        def edit(old, new):
            return lambda f: f.__setitem__(s, f[s].decode().replace(old, new, 1).encode())
        self.check(edit("name: pm", "name: pmx"), "does not match the directory")
        self.check(lambda f: f.__setitem__("skills/loop/SKILL.md", f[s].replace(b"name: pm", b"name: loop")), "reserved")
        self.check(lambda f: f.__setitem__("skills/code-review/SKILL.md", f[s].replace(b"name: pm", b"name: code-review")), "collides with a bundled command")
        self.check(edit("effort: medium", "effort: medium\ncontext: fork"), "never uses `context: fork`")
        self.check(edit("effort: medium", "effort: medium\nbogus-key: 1"), "unknown frontmatter key")
        self.check(edit("effort: medium", "effort: medium\ndisable-model-invocation: maybe"), "must be true or false")
        self.check(edit("description:", "descr:"), "`description` is required")
        self.check(lambda f: f.__setitem__(s, b"# no frontmatter\n"), "no --- fence")
        self.check(lambda f: f.pop("skills/tech-lead/lane-a-full-feature.md"), "link to lane-a-full-feature.md")
        self.check(lambda f: f.__setitem__("skills/empty/notes.md", b"x"), "no SKILL.md")

    def test_path_checks(self):
        s = "skills/pm/SKILL.md"
        self.check(lambda f: f.__setitem__(s, f[s] + b"\nread ${CLAUDE_PLUGIN_ROOT}/spec/nope.md\n"), "spec/nope.md is not in the plugin")
        self.check(lambda f: f.__setitem__(s, f[s] + b"\nread .claude/spec/loopctl.md\n"), "kit path not rewritten")
        self.check(lambda f: f.__setitem__("context/x.md", b"python3 .claude/scripts/doctor.py\n"), "kit path not rewritten")
        self.check(lambda f: f.__setitem__(s, f[s] + b"\nread .claude/context/engineer-protocol.md\n"), "kit path not rewritten")
        # project paths stay: a primer, an override, the project's launch-app skill, and history under docs/design
        ok = b"\nread .claude/context/backend.md, .claude/agents/<role>.md, .claude/skills/launch-app/SKILL.md\n"
        files = dict(self.plugin)
        files[s] = files[s] + ok
        files["docs/design/old.md"] = b"python3 .claude/scripts/doctor.py\n"
        # human docs may name an old copy install's .claude/scripts/ and the bare `${CLAUDE_PLUGIN_ROOT}/...` form
        files["docs/old-layout.md"] = b"an old install kept `.claude/scripts/`; kit prose says `${CLAUDE_PLUGIN_ROOT}/...`.\n"
        files[s] = files[s] + b"\nkit prose names kit files as `${CLAUDE_PLUGIN_ROOT}/...`.\n"
        self.assertEqual(V.validate(files, self.market, parse_python=False), [])
        # but a ${CLAUDE_PLUGIN_ROOT}/ path in human docs must still exist
        self.check(lambda f: f.__setitem__("docs/x.md", b"read ${CLAUDE_PLUGIN_ROOT}/spec/nope.md\n"), "docs/x.md: ${CLAUDE_PLUGIN_ROOT}/spec/nope.md")

    def test_layout_checks(self):
        self.check(lambda f: f.__setitem__("plugin/x.md", b"x\n"), "does not belong in the plugin")
        self.check(lambda f: f.__setitem__("scripts/loop.jsonl", b"{}"), "runtime or junk")
        self.check(lambda f: f.__setitem__("scripts/__pycache__/x.pyc", b"x"), "does not belong in the plugin")
        self.check(lambda f: f.__setitem__("scripts/broken.py", b"def (:\n"), "does not parse", parse=True)
        self.check(lambda f: f.__setitem__("settings.json", b'{"permissions": {"deny": []}}'), "applies only `agent`")
        self.check(lambda f: f.pop("KIT_VERSION"), "KIT_VERSION is missing")
        self.check(lambda f: f.__setitem__("KIT_VERSION", b"9.9.9\n"), "does not match plugin.json")

    def test_marketplace_checks(self):
        def m(fn):
            doc = json.loads(self.market.decode())
            fn(doc)
            return json.dumps(doc).encode()
        self.assertTrue([p for p in V.validate(self.plugin, None) if "marketplace.json is missing" in p])
        self.check(lambda f: None, "not valid JSON", market=b"{")
        self.check(lambda f: None, "kebab-case", market=m(lambda d: d.update(name="My Market")))
        self.check(lambda f: None, "owner.name", market=m(lambda d: d.pop("owner")))
        self.check(lambda f: None, "non-empty list", market=m(lambda d: d.update(plugins=[])))
        self.check(lambda f: None, "relative ./ path", market=m(lambda d: d["plugins"][0].update(source="/abs/plugin")))
        self.check(lambda f: None, "relative ./ path", market=m(lambda d: d["plugins"][0].update(source="./../plugin")))
        self.check(lambda f: None, "does not point at the repository root", market=m(lambda d: d["plugins"][0].update(source="./plugin")))
        self.check(lambda f: None, "differs from plugin.json", market=m(lambda d: d["plugins"][0].update(name="other")))
        self.check(lambda f: None, "version", market=m(lambda d: d["plugins"][0].update(version="9.9.9")))
        self.check(lambda f: None, "duplicate plugin name", market=m(lambda d: d["plugins"].append(dict(d["plugins"][0]))))
        self.check(lambda f: None, "`name` and `source`", market=m(lambda d: d["plugins"][0].pop("source")))

    def test_the_frontmatter_reader(self):
        fm, err = V.frontmatter("---\na: 1\nb: \"two: words\"\n---\nbody")
        self.assertEqual((fm, err), ({"a": "1", "b": "two: words"}, None))
        self.assertIn("duplicate", V.frontmatter("---\na: 1\na: 2\n---\n")[1])
        self.assertIn("not closed", V.frontmatter("---\na: 1\n")[1])
        self.assertIn("not `key: value`", V.frontmatter("---\nnot yaml\n---\n")[1])
        self.assertIn("contains ': '", V.frontmatter("---\nd: one: two\n---\n")[1])
        self.assertIn("contains ' #'", V.frontmatter("---\nd: one #two\n---\n")[1])
        for ok in ("d: 'one: two'", "d: \"a #b\"", "d: a#b, c:d", "d: >", "d: [a, b]", "d:"):
            self.assertIsNone(V.frontmatter("---\n%s\n---\n" % ok)[1], ok)

    def test_cli_ok_on_the_repository_and_fails_on_an_empty_one(self):
        script = os.path.join(REPO_ROOT, "tools", "validate-plugin.py")
        p = subprocess.run([sys.executable, script, "--strict"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            p = subprocess.run([sys.executable, script, "--repo", empty], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 1)
            self.assertIn("plugin.json is missing", p.stdout.decode())


if __name__ == "__main__":
    unittest.main()
