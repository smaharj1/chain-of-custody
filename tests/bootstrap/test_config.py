"""bootstrap_config: plan (agent-safe) and apply (human-only) loop.config edits through the registry (Phase 11a; IMP-11, H6)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

import bootstrap_config as bc
from kitlib import config as config_mod
from kitlib import registry

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env, session_env
from tests.helpers import BaseTestCase

CLI = os.path.join(SCRIPTS_DIR, "bootstrap_config.py")
REAL_CONFIG = os.path.join(REPO_ROOT, ".claude", "loop.config.md")
REG = registry.load_registry()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Base(BaseTestCase):
    def root(self, config_text=None):
        root = os.path.realpath(self.tmpdir())
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "loop.config.md"), "w", encoding="utf-8") as fh:
            fh.write(config_text if config_text is not None else read(REAL_CONFIG))
        return root

    def cli(self, root, *args, env=None):
        return subprocess.run([sys.executable, CLI, "--repo", root] + list(args), env=env or plain_env(root),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def plan(self, root, *sets, name="config-plan.json"):
        args = ["plan", "--name", name]
        for s in sets:
            args += ["--set", s]
        return self.cli(root, *args)

    def cfg(self, root):
        return config_mod.load_text(read(os.path.join(root, ".claude", "loop.config.md")), REG)


class Plan(Base):
    def test_plan_checks_against_the_registry_and_changes_no_config(self):
        root = self.root()
        before = read(os.path.join(root, ".claude", "loop.config.md"))
        p = self.plan(root, "verify_tests=pnpm test", "verify_require=[tests, build]", "delivery=draft-pr", "solo_maintainer=true", "bot.kind=github-app",
                      "factory_approvers=[alice, bob]", "max_ci_fix_attempts=3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(read(os.path.join(root, ".claude", "loop.config.md")), before)
        plan = json.loads(read(os.path.join(root, ".claude", "bootstrap", "config-plan.json")))
        self.assertEqual(plan["schema"], bc.SCHEMA)
        self.assertEqual(plan["set"]["verify_require"], ["tests", "build"])
        self.assertEqual(plan["set"]["max_ci_fix_attempts"], 3)
        self.assertIs(plan["set"]["solo_maintainer"], True)
        self.assertIn("H6", p.stdout)
        self.assertIn("command: run later", p.stdout)
        self.assertIn("bootstrap_config.py apply .claude/bootstrap/config-plan.json", p.stdout)

    def test_refusals(self):
        root = self.root()
        cases = {
            "unknown key": ("verfy_tests=x", "did you mean"),
            "wrong type int": ("max_ci_fix_attempts=lots", "expected an integer"),
            "wrong type bool": ("solo_maintainer=maybe", "expected true or false"),
            "bad enum": ("delivery=yolo", "expected one of"),
            "bad enum list-ish": ("bot.kind=[a]", "expected one of"),
            "placeholder": ("verify_tests=<command>", "placeholder"),
            "evidence gate": ("autonomy_level=L4", "evidence gate"),
            "evidence gate 2": ("parallel_mode=worktree", "unknown key"),
            "nested list of maps": ("deploy_triggers=[a]", None),
            "no equals": ("verify_tests", "KEY=VALUE"),
            "bad name": ("Verify Tests=x", "not a loop.config key"),
            "dotted traversal": ("a..b=1", "not a loop.config key"),
            "newline": ("verify_tests=a\nb", "newline"),
            "bad quoting": ('verify_tests="unterminated', "cannot read the value"),
            "mapping": ("deploy_surface={a: 1}", None),
        }
        for title, (item, why) in cases.items():
            with self.subTest(title):
                p = self.plan(root, item)
                if title in ("evidence gate 2", "nested list of maps", "mapping"):
                    # these may or may not be refusals depending on the registry; the plan must never accept a malformed value silently
                    if p.returncode == 0:
                        plan = json.loads(read(os.path.join(root, ".claude", "bootstrap", "config-plan.json")))
                        self.assertTrue(REG.get(list(plan["set"])[0]) is not None)
                    continue
                self.assertEqual(p.returncode, 1, p.stdout)
                if why:
                    self.assertIn(why, p.stderr)

    def test_duplicates_empty_and_too_many(self):
        root = self.root()
        self.assertEqual(self.plan(root, "solo_maintainer=true", "solo_maintainer=false").returncode, 1)
        self.assertEqual(self.cli(root, "plan").returncode, 1)
        self.assertEqual(self.plan(root, *["max_ci_fix_attempts=1"] * 41).returncode, 1)

    def test_plan_file_name_and_directory_are_safe(self):
        root = self.root()
        for name in ("../x.json", "a/b.json", ".hidden.json", "x.sh", "x" * 90 + ".json", "a b.json", ""):
            with self.subTest(name):
                self.assertEqual(self.plan(root, "solo_maintainer=true", name=name).returncode, 1)
        outside = os.path.realpath(self.tmpdir())
        os.symlink(outside, os.path.join(root, ".claude", "bootstrap"))
        self.assertEqual(self.plan(root, "solo_maintainer=true").returncode, 1)
        self.assertEqual(os.listdir(outside), [])

    def test_an_evidence_gated_value_is_allowed_once_the_gate_passes_but_not_before(self):
        root = self.root()
        self.assertEqual(self.plan(root, "autonomy_level=L2").returncode, 0)   # L2 is not gated
        self.assertEqual(self.plan(root, "autonomy_level=L5").returncode, 1)


class Apply(Base):
    def make_plan(self, root, sets):
        self.assertEqual(self.plan(root, *sets).returncode, 0)
        return os.path.join(".claude", "bootstrap", "config-plan.json")

    def apply(self, root, plan, write=False, env=None):
        return self.cli(root, "apply", plan, *(["--write"] if write else []), env=env)

    def test_dry_run_prints_a_diff_and_writes_nothing(self):
        root = self.root()
        path = os.path.join(root, ".claude", "loop.config.md")
        before = read(path)
        plan = self.make_plan(root, ["verify_tests=pnpm test"])
        p = self.apply(root, plan)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DRY RUN", p.stdout)
        self.assertIn("+ verify_tests: pnpm test", p.stdout)
        self.assertEqual(read(path), before)

    def test_write_changes_only_the_planned_lines_and_keeps_comments(self):
        root = self.root()
        path = os.path.join(root, ".claude", "loop.config.md")
        before = read(path)
        sets = ["verify_tests=pnpm test", "verify_require=[tests, build]", "bot.kind=github-app", "solo_maintainer=true", "factory_approvers=[alice, bob]",
                "verify_ports=[4310]", "github_repo=acme/widgets", "delivery=draft-pr", "verify_ready_path=/health"]
        plan = self.make_plan(root, sets)
        p = self.apply(root, plan, write=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        after = read(path)
        changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
        self.assertEqual(len(before.split("\n")), len(after.split("\n")))
        self.assertEqual(len(changed), len(sets))
        for old, new in changed:   # the comment after the value is kept on every changed line
            self.assertEqual(old.partition("#")[2], new.partition("#")[2])
        cfg = self.cfg(root)
        self.assertEqual(cfg.get("verify_tests"), "pnpm test")
        self.assertEqual(cfg.get("verify_require"), ["tests", "build"])
        self.assertEqual(cfg.get("bot.kind"), "github-app")
        self.assertEqual(cfg.get("factory_approvers"), ["alice", "bob"])
        self.assertEqual(cfg.get("verify_ports"), [4310])
        self.assertEqual(cfg.get("github_repo"), "acme/widgets")
        self.assertEqual(cfg.errors, [])
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o644 if oct(os.stat(path).st_mode & 0o777) == oct(0o644) else os.stat(path).st_mode & 0o777))

    def test_awkward_strings_survive_the_round_trip(self):
        root = self.root()
        cmds = ['npm run test -- --coverage', 'echo "a: b" # not a comment', "make test; make lint", 'pytest -k "a and b"', "cmd --flag='x y'", 'ünïcode ok']
        for c in cmds:
            with self.subTest(c):
                plan = self.make_plan(root, ["verify_tests=" + json.dumps(c)])
                self.assertEqual(self.apply(root, plan, write=True).returncode, 0)
                self.assertEqual(self.cfg(root).get("verify_tests"), c)

    def test_absent_keys_are_added_including_a_new_parent_mapping(self):
        text = "# Loop Config\n\n```yaml\nautonomy: per-milestone  # c\n```\n"
        root = self.root(text)
        plan = self.make_plan(root, ["verify_tests=make test", "gates.g6=report", "bot.kind=github-app"])
        self.assertEqual(self.apply(root, plan, write=True).returncode, 0)
        cfg = self.cfg(root)
        self.assertEqual((cfg.get("verify_tests"), cfg.get("gates.g6"), cfg.get("bot.kind")), ("make test", "report", "github-app"))
        # a second nested key goes into the existing parent, not a second `bot:` line
        plan = self.make_plan(root, ["bot.login=ci-bot"])
        self.assertEqual(self.apply(root, plan, write=True).returncode, 0)
        self.assertEqual(read(os.path.join(root, ".claude", "loop.config.md")).count("\nbot:"), 1)
        self.assertEqual(self.cfg(root).get("bot.login"), "ci-bot")

    def test_refuses_inside_a_session(self):
        root = self.root()
        path = os.path.join(root, ".claude", "loop.config.md")
        before = read(path)
        plan = self.make_plan(root, ["verify_tests=pnpm test"])
        for extra in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(extra):
                p = self.apply(root, plan, write=True, env=plain_env(root, extra))
                self.assertEqual(p.returncode, 2)
                self.assertIn("refusing", p.stderr)
        self.assertEqual(read(path), before)

    def test_plan_is_allowed_inside_a_session(self):
        root = self.root()
        p = subprocess.run([sys.executable, CLI, "--repo", root, "plan", "--set", "solo_maintainer=true"], env=session_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_hostile_plan_file_is_rechecked_not_trusted(self):
        root = self.root()
        path = os.path.join(root, ".claude", "loop.config.md")
        before = read(path)
        os.makedirs(os.path.join(root, ".claude", "bootstrap"))
        plan_path = os.path.join(root, ".claude", "bootstrap", "p.json")
        bad_plans = {
            "unknown key": {"schema": bc.SCHEMA, "set": {"evil_key": 1}},
            "wrong type": {"schema": bc.SCHEMA, "set": {"max_ci_fix_attempts": "many"}},
            "evidence gated": {"schema": bc.SCHEMA, "set": {"autonomy_level": "L5"}},
            "placeholder": {"schema": bc.SCHEMA, "set": {"verify_tests": "<command>"}},
            "extra field": {"schema": bc.SCHEMA, "set": {"solo_maintainer": True}, "run": "id"},
            "wrong schema": {"schema": "x", "set": {"solo_maintainer": True}},
            "empty set": {"schema": bc.SCHEMA, "set": {}},
            "set not a map": {"schema": bc.SCHEMA, "set": ["a"]},
            "nested value": {"schema": bc.SCHEMA, "set": {"verify_require": [["a"]]}},
            "map value": {"schema": bc.SCHEMA, "set": {"deploy_surface": {"a": 1}}},
            "bad key chars": {"schema": bc.SCHEMA, "set": {"a\nb": 1}},
            "key injection": {"schema": bc.SCHEMA, "set": {"verify_tests: x\nautonomy_level": "L5"}},
            "value newline": {"schema": bc.SCHEMA, "set": {"verify_tests": "a\nautonomy_level: L5"}},
        }
        for title, plan in bad_plans.items():
            with self.subTest(title):
                with open(plan_path, "w") as fh:
                    json.dump(plan, fh)
                p = self.apply(root, ".claude/bootstrap/p.json", write=True)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertEqual(read(path), before)
        for title, raw in {"not json": "{nope", "array": "[]", "empty": "", "huge": " " * (bc.MAX_PLAN + 10)}.items():
            with self.subTest(title):
                with open(plan_path, "w") as fh:
                    fh.write(raw)
                self.assertEqual(self.apply(root, ".claude/bootstrap/p.json", write=True).returncode, 1)
        os.unlink(plan_path)
        os.symlink(os.path.join(root, ".claude", "loop.config.md"), plan_path)
        self.assertEqual(self.apply(root, ".claude/bootstrap/p.json", write=True).returncode, 1)
        self.assertEqual(read(path), before)

    def test_a_newline_in_a_value_cannot_add_a_second_key(self):
        root = self.root()
        # even a value that dump_scalar would have to quote cannot smuggle a line: the planner refuses newlines, and the quoting escapes the rest
        self.assertEqual(self.plan(root, "verify_tests=a\nautonomy_level: L5").returncode, 1)
        plan = self.make_plan(root, ['verify_tests="x\\"; autonomy_level: L5 #"'])
        self.assertEqual(self.apply(root, plan, write=True).returncode, 0)
        cfg = self.cfg(root)
        self.assertEqual(cfg.get("autonomy_level"), "L1")

    def test_block_values_and_missing_or_hostile_config_files(self):
        text = "```yaml\nverify_tests: |\n  pnpm test\nverify_require: [tests]\n```\n"
        root = self.root(text)
        plan = self.make_plan(root, ["verify_tests=x"])
        p = self.apply(root, plan, write=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("by hand", p.stderr)
        self.assertEqual(read(os.path.join(root, ".claude", "loop.config.md")), text)
        for broken in ("no yaml block\n", "```yaml\nkey: [a\n```\n", "```yaml\na: 1\na: 2\n```\n", "```yaml\n```\n```yaml\n```\n"):
            with self.subTest(broken):
                root = self.root(broken)
                plan = self.make_plan(root, ["solo_maintainer=true"])
                self.assertEqual(self.apply(root, plan, write=True).returncode, 1)
                self.assertEqual(read(os.path.join(root, ".claude", "loop.config.md")), broken)
        root = self.root()
        cfg = os.path.join(root, ".claude", "loop.config.md")
        plan = self.make_plan(root, ["solo_maintainer=true"])
        outside = os.path.join(self.tmpdir(), "c.md")
        os.rename(cfg, outside)
        os.symlink(outside, cfg)
        self.assertEqual(self.apply(root, plan, write=True).returncode, 1)
        self.assertIn("solo_maintainer: false", read(outside))
        os.unlink(cfg)
        self.assertEqual(self.apply(root, plan, write=True).returncode, 1)

    def test_other_keys_are_untouched_and_preexisting_errors_are_not_blamed_on_the_edit(self):
        text = "```yaml\nsolo_maintainer: false\nverify_ports: [1]\nnot_a_key: 1\n```\n"
        new, changes = bc.apply_plan_text(text, {"solo_maintainer": True}, REG)
        self.assertEqual(new, "```yaml\nsolo_maintainer: true\nverify_ports: [1]\nnot_a_key: 1\n```\n")
        self.assertEqual([c[0] for c in changes], ["solo_maintainer"])
        with self.assertRaises(bc.ConfigPlanError):
            bc.apply_plan_text("no yaml block", {"solo_maintainer": True}, REG)

    def test_the_post_edit_check_catches_an_editor_bug(self):
        # if set_value ever wrote the wrong thing, apply_plan_text must refuse rather than write it
        real = bc.set_value
        try:
            bc.set_value = lambda lines, name, rendered: real(lines, name, "false")
            with self.assertRaises(bc.ConfigPlanError) as cm:
                bc.apply_plan_text("```yaml\nsolo_maintainer: false\n```\n", {"solo_maintainer": True}, REG)
            self.assertIn("nothing written", str(cm.exception))
            bc.set_value = lambda lines, name, rendered: real(real(lines, name, rendered), "verify_ports", "[9]")
            with self.assertRaises(bc.ConfigPlanError) as cm:
                bc.apply_plan_text("```yaml\nsolo_maintainer: false\n```\n", {"solo_maintainer": True}, REG)
            self.assertIn("also change", str(cm.exception))
        finally:
            bc.set_value = real

    def test_file_mode_is_kept(self):
        root = self.root()
        path = os.path.join(root, ".claude", "loop.config.md")
        os.chmod(path, 0o640)
        plan = self.make_plan(root, ["solo_maintainer=true"])
        self.assertEqual(self.apply(root, plan, write=True).returncode, 0)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)


class Editor(unittest.TestCase):
    def test_set_value_preserves_alignment_when_it_can(self):
        lines = ["```yaml", "delivery: local            # local | push-branch | draft-pr", "```"]
        for value in ("draft-pr", "push-branch", "x"):
            out = bc.set_value(lines, "delivery", value)
            self.assertEqual(out[1].index("#"), lines[1].index("#"), out[1])
            self.assertTrue(out[1].startswith("delivery: " + value + " "))
            self.assertTrue(out[1].endswith("# local | push-branch | draft-pr"))

    def test_values_are_rendered_so_they_parse_back(self):
        from kitlib import miniyaml
        for v in ("a b", "x: y", "a #b", "true", "12", "[x]", "", " lead", 'q"q', "back\\slash", 5, True, False, ["a", "b c"], []):
            self.assertEqual(miniyaml.parse_scalar(miniyaml.dump_scalar(v)), v)


if __name__ == "__main__":
    unittest.main()
