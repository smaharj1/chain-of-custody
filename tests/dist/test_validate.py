"""tools/validate-plugin.py: every structural check fires on a plugin broken in exactly that way (Phase 9, IMP-3).

The unbroken plugin comes from the in-memory build; each case mutates a copy and expects a problem that names the defect.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests import REPO_ROOT
from tests.dist import build_once

from distlib import validate as V  # noqa: E402


def setj(files, rel, fn):
    doc = json.loads(files[rel].decode())
    fn(doc)
    files[rel] = json.dumps(doc).encode()


class Validate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = build_once()
        cls.plugin = d.plugin.files
        cls.market = d.extra.files[".claude-plugin/marketplace.json"]

    def check(self, mutate, expect, market=None, parse=False):
        files = dict(self.plugin)
        mutate(files)
        problems = V.validate(files, market if market is not None else self.market, parse_python=parse)    # parsing 150 scripts costs 0.2 s: only where it is the subject
        self.assertTrue([p for p in problems if expect in p], "expected a problem containing %r, got %r" % (expect, problems[:6]))

    def test_the_build_is_clean(self):
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

    def test_layout_checks(self):
        self.check(lambda f: f.__setitem__("tests/test_x.py", b"x = 1\n"), "does not belong in a plugin")
        self.check(lambda f: f.__setitem__("scripts/loop.jsonl", b"{}"), "runtime or junk")
        self.check(lambda f: f.__setitem__("scripts/__pycache__/x.pyc", b"x"), "does not belong in a plugin")
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
        self.check(lambda f: None, "does not point at the generated", market=m(lambda d: d["plugins"][0].update(source="./.")))
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
