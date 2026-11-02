"""build-dist: the generated plugin, marketplace, copy install and KIT_MANIFEST (Phase 9, IMP-3).

* drift: the committed outputs equal what the build produces (the `--check` the CI runs)
* the build reads TRACKED files only, so a worktree, settings.local.json, runtime files, tests/ and tools/ can not leak
* every pointer earlier phases left for Phase 9 ships (changelog.d "Phase 9" lines)
* the plugin is structurally sound, its hooks come from the guards manifest, the worktree hooks are not in it
* the path rewrite, the manifest and the write/diff round trip
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.dist import build_once

from distlib import build as B  # noqa: E402  (tests.dist puts tools/ on sys.path)
from distlib import rewrite, validate as V  # noqa: E402
from guards import wiring  # noqa: E402
from kitlib import kitmanifest as km  # noqa: E402


class Drift(unittest.TestCase):
    def test_committed_outputs_match_the_build(self):
        d = build_once()
        problems = B.diff(REPO_ROOT, d)
        self.assertEqual(problems, [], "run `python3 tools/build-dist.py --write` and commit the result:\n" + "\n".join(problems[:10]))

    def test_cli_check_exits_zero(self):
        p = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "tools", "build-dist.py"), "--check"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())


class Contents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = build_once()
        cls.out = cls.d.outputs().files

    def test_plugin_and_marketplace_are_valid(self):
        self.assertEqual(V.validate(self.d.plugin.files, self.d.extra.files[B.MARKETPLACE]), [])

    def test_every_mode_is_a_skill_in_both_installs_and_commands_are_gone(self):
        modes = {"pm", "architect", "tech-lead", "planner", "orchestrate", "bootstrap", "retro", "init", "migrate"}
        for tree in (self.d.plugin.files, self.d.copy.files):
            prefix = "skills/" if tree is self.d.plugin.files else ".claude/skills/"
            self.assertEqual({p[len(prefix):].split("/")[0] for p in tree if p.startswith(prefix)}, modes)
            self.assertFalse([p for p in tree if "/commands/" in p or p.startswith("commands/")])

    def test_hooks_json_is_the_guards_manifest_for_the_plugin_root(self):
        want = wiring.build_plugin_hooks(wiring.load_manifest())
        got = json.loads(self.d.plugin.files["hooks/hooks.json"].decode())
        self.assertEqual(got, want)
        self.assertFalse({"WorktreeCreate", "WorktreeRemove"} & set(got["hooks"]), "the worktree hooks are an opt-in snippet (Phase 8)")
        self.assertNotIn(".claude/scripts", json.dumps(got))

    def test_what_never_ships(self):
        bad = [p for p in self.out
               if p.startswith(("tests/", "tools/", "plugin/tests/", "plugin/tools/", "dist/copy-install/tests/", "dist/copy-install/tools/"))
               or "/worktrees/" in p or "settings.local.json" in p or p.endswith((".lock", ".pyc", ".DS_Store", "loop.jsonl", "metrics.jsonl"))
               or "/.claude/migrate/" in p or "/__pycache__/" in p]
        self.assertEqual(bad, [])
        self.assertFalse([p for p in self.out if p.endswith("ablate.py")], "tools/evals/ablate.py is dev-only")

    def test_untracked_and_runtime_files_cannot_reach_an_output(self):
        rows = B.tracked_files(REPO_ROOT)
        src, exe = B.read_sources(REPO_ROOT, rows)
        evil = {".claude/settings.local.json": b"{}", ".claude/worktrees/x/.claude/agents/y.md": b"x", ".claude/loop.jsonl": b"{}",
                ".claude/metrics.jsonl": b"{}", ".claude/orchestrate.lock": b"{}", ".claude/bootstrap-receipt.json": b"{}",
                ".claude/scripts/__pycache__/x.cpython-39.pyc": b"x", ".claude/.DS_Store": b"x", ".claude/commands/pm.md": b"x",
                ".claude/bootstrap/setup.sh": b"x", ".claude/scripts/x.lock": b"x"}
        src2 = dict(src)
        src2.update(evil)
        rows2 = rows + [(p, "100644") for p in evil]
        out = B.build(REPO_ROOT, rows2, (src2, exe)).outputs().files
        self.assertEqual(sorted(out), sorted(self.out), "a junk or runtime file reached an output")
        self.assertEqual(out, self.out)

    def test_a_tracked_symlink_in_a_shipped_area_is_refused(self):
        with self.assertRaises(B.BuildError):
            B.read_sources(REPO_ROOT, [(".claude/agents/x.md", "120000")])

    def test_an_unmapped_claude_area_fails_loudly(self):
        with self.assertRaises(B.BuildError):
            B.plugin_target(".claude/newthing/x.md")
        with self.assertRaises(B.BuildError):
            B.plugin_target(".claude/NEWFILE")

    def test_earlier_phases_pointers_ship(self):
        """changelog.d lines marked 'Phase 9' name files that must be in the manifest and the build."""
        files = set(self.d.copy.files)
        for rel in (".claude/templates/intake_schema.json", ".claude/scripts/intake_validate.py", ".claude/scripts/notify.py",
                    ".claude/templates/notify/notify-hooks.settings.json", ".claude/agents/intake-triage.md", ".claude/scripts/loopctl/unattended.py",
                    ".claude/scripts/loopctl/intake.py", ".claude/scripts/loopctl/promotion.py", "docs/unattended.md",
                    ".claude/scripts/worktree_hook.py", ".claude/scripts/loopctl/parallel/__init__.py", ".claude/scripts/doctorlib/parallel.py",
                    ".claude/templates/parallel/worktree-hooks.settings.json", ".claude/templates/worktreeinclude", ".claude/spec/loopctl-parallel.md",
                    ".claude/spec/pipeline-host-workflow.md", ".claude/scripts/loopctl/learning/gaps.py", ".claude/scripts/loopctl/gates/docs_delta.py",
                    "docs/learning.md", "docs/harness-review.md", ".claude/spec/loopctl-learning.md",
                    ".claude/scripts/loopctl/remote_lock.py", ".claude/scripts/loopctl/claims.py", ".claude/scripts/loopctl/merge_class.py",
                    ".claude/scripts/loopctl/manifest.py", ".claude/scripts/loopctl/extras.py", ".claude/scripts/loopctl/gates/g10_product.py",
                    ".claude/scripts/doctorlib/extras.py", ".claude/templates/github/workflows/g10-product.yml", ".claude/spec/loopctl-extras.md",
                    "docs/loopctl-extras.md", ".claude/scripts/github/repo_script.py", ".claude/scripts/bootstrap_receipt.py", ".claude/KIT_MANIFEST"):
            self.assertIn(rel, files, rel)
        plugin = set(self.d.plugin.files)
        for rel in ("agents/intake-triage.md", "templates/intake_schema.json", "scripts/intake_validate.py", "evals/ablation/manifest.json",
                    "evals/cases/Q-default-posture-pm.json", "spec/loopctl-extras.md", "docs/learning.md", "skills/retro/SKILL.md"):
            self.assertIn(rel, plugin, rel)

    def test_seed_files_are_in_the_plugin_for_init(self):
        for rel in ("seed/.claude/CLAUDE.md", "seed/.claude/loop.config.md", "seed/.claude/context/backend.md", "seed/.claude/context/local-dev.md"):
            self.assertIn(rel, self.d.plugin.files)
        self.assertNotIn("context/backend.md", self.d.plugin.files)
        self.assertIn("context/engineer-protocol.md", self.d.plugin.files)

    def test_plugin_json_matches_kit_version_and_declares_user_config(self):
        pj = json.loads(self.d.plugin.files[".claude-plugin/plugin.json"].decode())
        with open(os.path.join(REPO_ROOT, ".claude", "KIT_VERSION")) as fh:
            self.assertEqual(pj["version"], km.parse_version(fh.read()))
        self.assertEqual(set(pj["userConfig"]), {"protected_branches", "integration_branch", "main_edit_allow"})
        from kitlib import registry
        reg = registry.load_registry()
        for key in pj["userConfig"]:
            self.assertIn(key, reg, "userConfig.%s must be a real loop.config key (init writes it there)" % key)

    def test_executable_bits_follow_the_source(self):
        src_exe = {p for p, mode in B.tracked_files(REPO_ROOT) if mode == "100755" and p.startswith(".claude/")}
        self.assertTrue(src_exe, "the kit has executable scripts")
        for p in src_exe:
            self.assertIn("dist/copy-install/" + p, self.d.outputs().exe)


class Manifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = build_once()
        cls.m = km.loads(cls.d.copy.files[km.MANIFEST_REL].decode())

    def test_lists_every_copy_install_file_with_its_hash_and_class(self):
        listed = set(self.m["files"])
        have = set(self.d.copy.files) - {km.MANIFEST_REL, km.VERSION_REL}
        self.assertEqual(listed, have)
        for rel, ent in self.m["files"].items():
            self.assertEqual(ent["sha256"], km.sha256_bytes(self.d.copy.files[rel]), rel)
            self.assertEqual(ent["class"], km.classify(rel))

    def test_source_and_dist_manifest_are_the_same_bytes(self):
        self.assertEqual(self.d.extra.files[km.MANIFEST_REL], self.d.copy.files[km.MANIFEST_REL])
        with open(os.path.join(REPO_ROOT, km.MANIFEST_REL), "rb") as fh:
            self.assertEqual(fh.read(), self.d.copy.files[km.MANIFEST_REL])

    def test_classes(self):
        c = km.classify
        self.assertEqual(c(".claude/settings.json"), "frozen")
        for rel in (".claude/CLAUDE.md", ".claude/loop.config.md", ".claude/context/backend.md", ".claude/context/local-dev.md", ".claude/rules/x.md"):
            self.assertEqual(c(rel), "seed", rel)
        for rel in (".claude/context/engineer-protocol.md", ".claude/agents/pm.md", ".claude/scripts/doctor.py", "AGENTS.md", "docs/learning.md"):
            self.assertEqual(c(rel), "kit", rel)

    def test_loads_rejects_malformed_manifests(self):
        good = json.loads(km.dumps(self.m))
        cases = {"schema": lambda d: d.update(schema="x"), "no version": lambda d: d.pop("version"),
                 "traversal": lambda d: d["files"].update({"../evil": {"sha256": "0" * 64, "class": "kit"}}),
                 "absolute": lambda d: d["files"].update({"/etc/passwd": {"sha256": "0" * 64, "class": "kit"}}),
                 "bad hash": lambda d: d["files"].update({"a": {"sha256": "zz", "class": "kit"}}),
                 "bad class": lambda d: d["files"].update({"a": {"sha256": "0" * 64, "class": "root"}})}
        for name, mutate in cases.items():
            with self.subTest(name):
                doc = json.loads(json.dumps(good))
                mutate(doc)
                with self.assertRaises(ValueError):
                    km.loads(json.dumps(doc))
        with self.assertRaises(ValueError):
            km.loads("not json")


class Rewrite(unittest.TestCase):
    PR = ("backend", "local-dev")

    def rw(self, text, docs=("docs/learning.md",)):
        return rewrite.rewrite_text(text, self.PR, set(docs))

    def test_kit_paths_become_plugin_paths(self):
        out = self.rw("run `python3 .claude/scripts/doctor.py` and read .claude/spec/loopctl.md, .claude/templates/brief.md, .claude/agents/code-reviewer.md")
        self.assertEqual(out.count("${CLAUDE_PLUGIN_ROOT}/"), 4)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py", out)
        self.assertEqual(rewrite.leftovers(out), [])

    def test_project_files_globs_and_primers_stay(self):
        text = ("`.claude/CLAUDE.md` `.claude/loop.config.md` `.claude/settings.json` `.claude/loop.jsonl` `.claude/pr/` `.claude/bootstrap/setup.sh` "
                "`.claude/context/backend.md` `.claude/context/*.md` `.claude/agents/*.md` `.claude/skills/**` `.claude/scripts` `.claude/skills/launch-app/SKILL.md` "
                "`.claude/spec/<name>` `docs/features/x/requirements.md`")
        self.assertEqual(self.rw(text), text)

    def test_kit_context_files_and_shipped_docs_are_rewritten_only_when_they_exist_in_the_plugin(self):
        out = self.rw("`.claude/context/engineer-protocol.md` `.claude/context/backend.md` docs/learning.md docs/features/a.md docs/nothere.md")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/context/engineer-protocol.md", out)
        self.assertIn(".claude/context/backend.md", out)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/docs/learning.md", out)
        self.assertIn(" docs/features/a.md docs/nothere.md", out)

    def test_it_is_idempotent_and_does_not_touch_longer_paths(self):
        once = self.rw("python3 .claude/scripts/loopctl run; $HOME/.claude/scripts/x; ../.claude/scripts/y")
        self.assertEqual(self.rw(once), once)
        self.assertIn("$HOME/.claude/scripts/x", once)
        self.assertIn("../.claude/scripts/y", once)

    def test_relative_doc_links_into_claude_lose_the_dot_dir(self):
        self.assertEqual(self.rw("[a](../.claude/spec/x.md) [b](../../.claude/spec/y.md) [c](../README.md)"), "[a](../spec/x.md) [b](../../spec/y.md) [c](../README.md)")

    def test_leftovers_flags_a_kit_path(self):
        self.assertEqual(rewrite.leftovers("ok\nsee .claude/spec/x.md\n")[0][0], 2)
        self.assertEqual(rewrite.leftovers("`.claude/skills/launch-app/SKILL.md`"), [])


class WriteAndDiff(unittest.TestCase):
    """The write/check round trip on a tiny synthetic Dist in a temp directory."""

    def tiny(self):
        d = B.Dist()
        d.plugin.put("a.txt", b"one")
        d.plugin.put("bin/run.sh", b"#!/bin/sh\n", exe=True)
        d.copy.put(".claude/x.md", b"x")
        d.extra.put(km.MANIFEST_REL, b"{}")
        d.extra.put(B.MARKETPLACE, b"{}")
        return d

    def test_write_then_check_then_drift(self):
        with tempfile.TemporaryDirectory() as repo:
            d = self.tiny()
            self.assertEqual(B.write(repo, d), (5, 0))
            self.assertEqual(B.diff(repo, d), [])
            self.assertTrue(os.stat(os.path.join(repo, "plugin/bin/run.sh")).st_mode & 0o111)
            self.assertEqual(B.write(repo, d), (0, 0), "a second write changes nothing")
            with open(os.path.join(repo, "plugin/a.txt"), "w") as fh:
                fh.write("edited")
            os.remove(os.path.join(repo, "dist/copy-install/.claude/x.md"))
            os.makedirs(os.path.join(repo, "plugin/__pycache__"))
            with open(os.path.join(repo, "plugin/__pycache__/junk.pyc"), "w") as fh:
                fh.write("ignored")
            with open(os.path.join(repo, "plugin/stray.txt"), "w") as fh:
                fh.write("stray")
            with open(os.path.join(repo, "outside.txt"), "w") as fh:
                fh.write("mine")
            os.chmod(os.path.join(repo, "plugin/bin/run.sh"), 0o644)
            problems = B.diff(repo, d)
            self.assertEqual(sorted(problems), sorted(["differs: plugin/a.txt", "missing: dist/copy-install/.claude/x.md",
                                                       "stale (not generated): plugin/stray.txt", "mode differs: plugin/bin/run.sh"]))
            written, removed = B.write(repo, d)
            self.assertEqual(removed, 1)
            self.assertEqual(B.diff(repo, d), [])
            self.assertTrue(os.path.exists(os.path.join(repo, "outside.txt")), "files outside the output roots are never deleted")


if __name__ == "__main__":
    unittest.main()
