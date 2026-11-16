"""migrate.py: move an old copy install of the kit to the plugin, keeping every change the person made (Phase 9, IMP-3, CO-33).

* a real v1.2.0 install (the shipped `migrate/v1.2.0.tar`): pristine, and with the edits an Install A / Install B style project has
* a v2 copy install, recognised by its `.claude/KIT_MANIFEST`
* the dry run changes nothing; the apply removes, backs up, strips the kit's hooks, does what /init does and writes a receipt
* the human-only rules: refuses inside a Claude Code session, refuses a dirty git tree, refuses while a path needs the person
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import REPO_ROOT
from tests.dist import call

import migrate  # noqa: E402  (tests/__init__ puts scripts/ on sys.path)
from kitlib import install, kitmanifest as km  # noqa: E402

V1_TAR = os.path.join(REPO_ROOT, "migrate", "v1.2.0.tar")
V1 = km.read_tar_tree(V1_TAR)
V1_SEED = {p for p in V1 if km.classify(p) != km.CLASS_KIT}


def seed_text(rel):
    with open(os.path.join(REPO_ROOT, "seed", *rel.split("/")), "rb") as fh:
        return fh.read()


# the starter files a pristine v1.2.0 install still has unchanged, which migrate replaces with the plugin's current ones
V1_UPDATED = {p for p in V1_SEED if migrate.UPDATABLE_SEED.match(p) and os.path.isfile(os.path.join(REPO_ROOT, "seed", *p.split("/")))
              and seed_text(p) != V1[p]}
KIT_HOOKS = ["$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh", "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-main-edit.sh"]
USER_HOOK = {"type": "command", "command": "say done"}


def write_tree(root, files):
    for rel, data in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode())


def tree_state(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kit-migrate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")       # never the real ~/.claude/plugins
        os.makedirs(self.home)
        self.p = os.path.join(self.tmp, "project")
        os.makedirs(self.p)

    def env(self, **kw):
        e = {"HOME": self.home}
        e.update(kw)
        return e

    def run_migrate(self, *args, env=None):
        return call(lambda a, e: migrate.main(a, e, REPO_ROOT), ["--repo", self.p] + list(args), env if env is not None else self.env())

    def migrate_json(self, *args, env=None):
        rc, out, err = self.run_migrate("--json", *args, env=env)
        return rc, (json.loads(out) if out.startswith("{") else None), err

    def path(self, rel):
        return os.path.join(self.p, rel)

    def read(self, rel):
        with open(self.path(rel), "rb") as fh:
            return fh.read()

    @staticmethod
    def by_action(doc):
        out = {}
        for a in doc["actions"]:
            out.setdefault(a["action"], []).append(a["path"])
        return out


class V1Install(Case):
    def setUp(self):
        super().setUp()
        write_tree(self.p, V1)

    def test_the_archive_is_the_v1_2_0_install(self):
        self.assertEqual(km.parse_version(V1[".claude/KIT_VERSION"].decode()), "1.2.0")
        for rel in (".claude/commands/pm.md", ".claude/hooks/guard-git.sh", ".claude/agents/code-reviewer.md", "AGENTS.md", ".claude/settings.json"):
            self.assertIn(rel, V1)
        self.assertFalse([p for p in V1 if p.startswith(".claude/scripts/")], "v1.2.0 had no scripts")

    def test_the_archive_is_what_git_archive_makes_of_the_tag(self):
        if subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "-q", "--verify", "v1.2.0^{commit}"], stdout=subprocess.DEVNULL).returncode != 0:
            self.skipTest("the v1.2.0 tag is not in this clone (a shallow CI checkout)")
        out = subprocess.run(["git", "-C", REPO_ROOT, "archive", "--format=tar", "v1.2.0", ".claude", "AGENTS.md", ".cursor", "skills"],
                             stdout=subprocess.PIPE, check=True).stdout
        with open(V1_TAR, "rb") as fh:
            self.assertEqual(fh.read(), out)

    def test_the_dry_run_plans_everything_and_changes_nothing(self):
        before = tree_state(self.p)
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 0, err)
        self.assertEqual(tree_state(self.p), before)
        acts = self.by_action(doc)
        self.assertEqual(set(acts), {"remove", "update"}, "a pristine install has only the kit's files and unchanged starter files")
        self.assertEqual(sorted(acts["remove"]), sorted(p for p in V1 if p not in V1_SEED))
        self.assertEqual(sorted(acts["update"]), sorted(V1_UPDATED))
        self.assertIn(".claude/CLAUDE.md", V1_UPDATED)
        self.assertNotIn(".claude/loop.config.md", acts["update"], "the config is never replaced")
        self.assertEqual((doc["from_version"], doc["kit"]), ("1.2.0", REPO_ROOT))
        self.assertEqual(sorted(doc["removed_hooks"]), sorted(KIT_HOOKS))
        self.assertIn(".claude/settings.json", doc["init"]["writes"])
        self.assertTrue(doc["has_changes"])
        self.assertFalse(doc["applied"])

    def test_apply_leaves_only_the_projects_files_and_a_receipt(self):
        original_settings = self.read(".claude/settings.json")
        rc, doc, err = self.migrate_json("--apply")
        self.assertEqual(rc, 0, err)
        for rel in V1:
            self.assertEqual(os.path.exists(self.path(rel)), rel in V1_SEED, rel)
        for rel in V1_SEED - {".claude/settings.json"} - V1_UPDATED:
            self.assertEqual(self.read(rel), V1[rel], "the project's own file is untouched: " + rel)
        for rel in V1_UPDATED:
            self.assertEqual(self.read(rel), seed_text(rel), "an unchanged starter file is the plugin's current one: " + rel)
        for d in (".claude/commands", ".claude/hooks", ".claude/templates", ".claude/spec", ".cursor", "skills"):
            self.assertFalse(os.path.exists(self.path(d)), "the emptied directory is gone: " + d)
        s = json.loads(self.read(".claude/settings.json"))
        self.assertNotIn("hooks", s, "the plugin's hooks.json supplies the guards")
        self.assertEqual(s["permissions"]["allow"], [], "the allow list is the project's")
        self.assertIn("Bash(terraform apply:*)", s["permissions"]["deny"])
        self.assertIs(s["sandbox"]["enabled"], True)
        self.assertEqual(self.read(".claude/settings.json.pre-init"), original_settings, "the original settings are backed up")
        self.assertEqual(install.copy_install_markers(self.p), [])
        with open(self.path(".claude/.migrate/report.json")) as fh:
            receipt = json.load(fh)
        self.assertEqual((receipt["schema"], receipt["applied"]), ("kit.migrate.v2", True))
        self.assertIn(".claude/KIT_VERSION", receipt["done"])
        self.assertFalse(os.path.exists(self.path(".claude/kit-backup")), "nothing was edited, so nothing is backed up")
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 0, err)
        self.assertEqual((doc["actions"], doc["removed_hooks"], doc["has_changes"]), ([], [], False), "a second run is empty")
        self.assertIn("no copy install of the kit was found here", " ".join(doc["notes"]))

    def test_every_edit_is_kept_overridden_backed_up_or_left_in_place(self):
        settings = json.loads(V1[".claude/settings.json"])
        settings["hooks"]["Stop"] = [{"hooks": [USER_HOOK]}]
        settings["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": "npm run lint"})
        write_tree(self.p, {
            ".claude/agents/backend-engineer.md": V1[".claude/agents/backend-engineer.md"] + b"\n## Local rule\nUse our ORM wrapper.\n",
            ".claude/commands/pm.md": V1[".claude/commands/pm.md"] + b"\nOur PM tweak.\n",
            ".claude/context/engineer-protocol.md": V1[".claude/context/engineer-protocol.md"] + b"\nours\n",
            "AGENTS.md": V1["AGENTS.md"] + b"\nours\n",
            ".claude/CLAUDE.md": V1[".claude/CLAUDE.md"] + b"\nOur stack: Rails.\n",
            ".claude/agents/ours-extra.md": "---\nname: ours-extra\n---\nours\n",
            ".claude/scripts/our_tool.py": "print('ours')\n",
            ".claude/settings.json": json.dumps(settings),
        })
        rc, doc, err = self.migrate_json("--apply")
        self.assertEqual(rc, 0, err)
        by = {a["path"]: a["action"] for a in doc["actions"]}
        self.assertEqual(by[".claude/agents/backend-engineer.md"], "override")
        self.assertEqual(by[".claude/commands/pm.md"], "backup")
        self.assertEqual(by[".claude/context/engineer-protocol.md"], "backup")
        self.assertEqual(by[".claude/scripts/our_tool.py"], "backup")
        self.assertEqual(by["AGENTS.md"], "left-in-place")
        self.assertEqual(by[".claude/agents/code-reviewer.md"], "remove")
        for rel in (".claude/CLAUDE.md", ".claude/agents/ours-extra.md"):
            self.assertNotIn(rel, by, "the project's own file is not the kit's to judge")
        self.assertEqual(by[".claude/context/infra.md"], "update", "an unchanged primer is replaced")
        self.assertIn(b"Use our ORM wrapper.", self.read(".claude/agents/backend-engineer.md"))
        self.assertIn(b"Our stack: Rails.", self.read(".claude/CLAUDE.md"))
        self.assertIn(b"ours", self.read(".claude/agents/ours-extra.md"))
        self.assertIn(b"ours", self.read("AGENTS.md"))
        for rel in (".claude/commands/pm.md", ".claude/context/engineer-protocol.md", ".claude/scripts/our_tool.py"):
            self.assertFalse(os.path.exists(self.path(rel)), rel)
        self.assertIn(b"Our PM tweak.", self.read(".claude/kit-backup/1.2.0/commands/pm.md"))
        self.assertEqual(self.read(".claude/kit-backup/1.2.0/scripts/our_tool.py"), b"print('ours')\n")
        self.assertFalse(os.path.exists(self.path(".claude/scripts")), "the folder has to go: /init and doctor look for it")
        s = json.loads(self.read(".claude/settings.json"))
        self.assertEqual(s["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "npm run lint"}]}],
                                      "Stop": [{"hooks": [USER_HOOK]}]}, "the person's hooks stay, the kit's go")
        notes = " ".join(doc["notes"])
        self.assertIn("project override", notes)
        self.assertIn("/chain-of-custody:<mode>", notes)
        self.assertEqual(install.copy_install_markers(self.p), [])

    def test_text_output_names_the_kit_and_the_full_apply_command(self):
        rc, out, err = self.run_migrate()
        self.assertEqual(rc, 0, err)
        self.assertIn("  kit:     %s" % REPO_ROOT, out)
        self.assertIn("migrate: copy install 1.2.0 -> plugin ", out)
        self.assertIn("python3 %s --repo %s --apply" % (os.path.join(REPO_ROOT, "scripts", "migrate.py"), os.path.realpath(self.p)), out)
        self.assertIn("the plugin does not look installed", out)
        for cmd in install.INSTALL_COMMANDS:
            self.assertIn(cmd, out)

    def test_an_installed_plugin_is_not_asked_for_again(self):
        os.makedirs(os.path.join(self.home, ".claude", "plugins"))
        with open(os.path.join(self.home, ".claude", "plugins", "installed_plugins.json"), "w") as fh:
            json.dump({"plugins": {install.PLUGIN_ID: [{}]}}, fh)
        rc, out, err = self.run_migrate()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("the plugin does not look installed", out)

    def test_human_only(self):
        before = tree_state(self.p)
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            rc, out, err = self.run_migrate("--apply", env=self.env(**{marker: "1"}))
            self.assertEqual(rc, 3)
            self.assertIn("refusing to run inside a Claude Code session", err)
        self.assertEqual(tree_state(self.p), before)
        rc, out, err = self.run_migrate(env=self.env(CLAUDECODE="1"))
        self.assertEqual(rc, 0, "the dry run is fine inside a session")

    def test_a_dirty_git_tree_is_refused(self):
        git = ["git", "-C", self.p, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "v1"]):
            subprocess.run(git + cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(self.path(".claude/CLAUDE.md"), "ab") as fh:
            fh.write(b"\nuncommitted\n")
        rc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes", err)
        self.assertTrue(os.path.exists(self.path(".claude/KIT_VERSION")), "nothing was changed")
        rc, out, err = self.run_migrate("--apply", "--allow-dirty")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.path(".claude/KIT_VERSION")))

    def test_a_symlink_needs_the_person_and_blocks_the_apply(self):
        os.makedirs(self.path(".claude/scripts"))
        os.symlink(self.path(".claude/CLAUDE.md"), self.path(".claude/scripts/link.py"))
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 1)
        self.assertEqual(self.by_action(doc)["problem"], [".claude/scripts/link.py"])
        before = tree_state(self.p)
        rc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("need you first", err)
        self.assertEqual(tree_state(self.p), before)
        self.assertTrue(os.path.islink(self.path(".claude/scripts/link.py")))

    def test_a_local_settings_file_loses_the_kits_hooks_and_is_backed_up(self):
        local = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": KIT_HOOKS[0]}]}],
                                      "Stop": [{"hooks": [USER_HOOK]}]}, "env": {"A": "1"}})
        write_tree(self.p, {".claude/settings.local.json": local})
        rc, doc, err = self.migrate_json()
        self.assertEqual(self.read(".claude/settings.local.json"), local.encode(), "the dry run changes nothing")
        rc, doc, err = self.migrate_json("--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn(".claude/settings.local.json had hook entries", " ".join(doc["notes"]))
        self.assertEqual(json.loads(self.read(".claude/settings.local.json")), {"hooks": {"Stop": [{"hooks": [USER_HOOK]}]}, "env": {"A": "1"}})
        self.assertEqual(self.read(".claude/settings.local.json.pre-migrate"), local.encode())
        self.assertEqual(os.stat(self.path(".claude/settings.local.json")).st_mode & 0o777, 0o600)
        self.assertEqual(install.copy_install_markers(self.p), [])

    def test_a_team_script_the_settings_still_run_needs_the_person(self):
        settings = json.loads(V1[".claude/settings.json"])
        settings["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "python3 .claude/scripts/lint.py"}]}]
        write_tree(self.p, {".claude/scripts/lint.py": "print('lint')\n", ".claude/scripts/spare.py": "print('spare')\n",
                            ".claude/settings.json": json.dumps(settings)})
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 1)
        by = {a["path"]: a for a in doc["actions"]}
        self.assertEqual(by[".claude/scripts/lint.py"]["action"], "problem")
        self.assertIn("move it to a project path", by[".claude/scripts/lint.py"]["detail"])
        self.assertEqual(by[".claude/scripts/spare.py"]["action"], "backup", "a script nothing names is moved to the backup")
        self.assertNotIn("python3 .claude/scripts/lint.py", doc["removed_hooks"], "the team's hook is not the kit's")

    def test_a_backup_keeps_the_file_mode(self):
        write_tree(self.p, {".claude/scripts/tool.sh": "#!/bin/sh\n"})
        os.chmod(self.path(".claude/scripts/tool.sh"), 0o755)
        rc, doc, err = self.migrate_json("--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.stat(self.path(".claude/kit-backup/1.2.0/scripts/tool.sh")).st_mode & 0o777, 0o755)

    def test_a_symlinked_folder_needs_the_person(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        shutil.move(self.path(".claude/commands"), os.path.join(outside, "commands"))
        os.symlink(os.path.join(outside, "commands"), self.path(".claude/commands"))
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 1)
        problems = self.by_action(doc)["problem"]
        self.assertIn(".claude/commands/pm.md", problems)
        rc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.isfile(os.path.join(outside, "commands", "pm.md")), "nothing outside the project is removed")

    def test_a_symlink_outside_claude_is_left(self):
        os.remove(self.path("AGENTS.md"))
        os.symlink(self.path(".claude/CLAUDE.md"), self.path("AGENTS.md"))
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 0, err)
        self.assertEqual({a["path"]: a["action"] for a in doc["actions"]}["AGENTS.md"], "left-in-place")

    def test_the_printed_apply_command_carries_the_dry_runs_choices(self):
        rc, out, err = self.run_migrate("--from-version", "1.2.0", "--option", "integration_branch=dev")
        self.assertEqual(rc, 0, err)
        self.assertIn("--from-version 1.2.0", out)
        self.assertIn("--option integration_branch=dev", out)
        self.assertTrue(out.rstrip().endswith("--apply") or "--apply\n" in out)

    def test_an_edited_claude_md_that_names_the_old_paths_gets_a_note(self):
        write_tree(self.p, {".claude/CLAUDE.md": V1[".claude/CLAUDE.md"] + b"\nRun .claude/commands/pm.md first.\n"})
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 0, err)
        self.assertIn("still names .claude/commands/", " ".join(doc["notes"]))

    def test_an_older_v1_is_backed_up_where_it_differs(self):
        write_tree(self.p, {".claude/KIT_VERSION": "1.1.0 (2026-05-01)\n", ".claude/agents/code-reviewer.md": "the 1.1.0 text\n"})
        rc, doc, err = self.migrate_json()
        self.assertEqual(rc, 0, err)
        self.assertIn("the kit ships the hashes of v1.2.0 only", " ".join(doc["notes"]))
        by = {a["path"]: a["action"] for a in doc["actions"]}
        self.assertEqual(by[".claude/agents/code-reviewer.md"], "override", "an agent that is not the v1.2.0 text is kept, never deleted")


class V2CopyInstall(Case):
    KIT = {".claude/agents/backend-engineer.md": b"kit backend\n", ".claude/skills/pm/SKILL.md": b"kit pm\n",
           ".claude/scripts/doctor.py": b"print('doctor')\n", ".claude/scripts/kitlib/__init__.py": b"",
           ".claude/CLAUDE.md": b"# seed\n", ".claude/loop.config.md": b"```yaml\nautonomy_level: L1\n```\n"}

    def setUp(self):
        super().setUp()
        files = dict(self.KIT)
        files[km.MANIFEST_REL] = km.dumps(km.build_manifest("2.0.0-rc.1", self.KIT))
        files[km.VERSION_REL] = "2.0.0-rc.1 (2026-09-01)\n"
        files[".claude/scripts/__pycache__/doctor.cpython-39.pyc"] = b"\0cache"
        files[".claude/scripts/doctor.py"] = b"print('doctor, patched by us')\n"
        write_tree(self.p, files)

    def test_the_manifest_tells_the_kits_files_from_the_edits(self):
        rc, doc, err = self.migrate_json("--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc["from_version"], "2.0.0-rc.1")
        by = {a["path"]: a["action"] for a in doc["actions"]}
        self.assertEqual(by, {".claude/agents/backend-engineer.md": "remove", ".claude/skills/pm/SKILL.md": "remove",
                              ".claude/scripts/kitlib/__init__.py": "remove", ".claude/scripts/__pycache__/doctor.cpython-39.pyc": "remove",
                              ".claude/scripts/doctor.py": "backup", km.MANIFEST_REL: "remove", km.VERSION_REL: "remove",
                              ".claude/CLAUDE.md": "update"})
        self.assertEqual(self.read(".claude/kit-backup/2.0.0-rc.1/scripts/doctor.py"), b"print('doctor, patched by us')\n")
        for d in (".claude/scripts", ".claude/skills", ".claude/agents"):
            self.assertFalse(os.path.exists(self.path(d)), d)
        self.assertEqual(self.read(".claude/CLAUDE.md"), seed_text(".claude/CLAUDE.md"), "an unchanged starter file is replaced")
        self.assertEqual(self.read(".claude/loop.config.md"), self.KIT[".claude/loop.config.md"], "the config is the project's")
        self.assertTrue(os.path.isfile(self.path(".claude/context/backend.md")), "/init copies the starter files that are missing")
        self.assertEqual(install.copy_install_markers(self.p), [])

    def test_a_broken_manifest_stops_everything(self):
        write_tree(self.p, {km.MANIFEST_REL: "not json"})
        before = tree_state(self.p)
        rc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("will not guess which files are the kit's", err)
        self.assertEqual(tree_state(self.p), before)


class NotACopyInstall(Case):
    def test_a_project_without_claude_points_to_init(self):
        rc, out, err = self.run_migrate()
        self.assertEqual(rc, 2)
        self.assertIn("/chain-of-custody:init", err)

    def test_the_kit_checkout_is_refused(self):
        rc, out, err = call(lambda a, e: migrate.main(a, e, REPO_ROOT), ["--repo", REPO_ROOT], self.env())
        self.assertEqual(rc, 2)
        self.assertIn("the kit's own checkout", err)


if __name__ == "__main__":
    unittest.main()
