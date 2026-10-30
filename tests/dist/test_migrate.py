"""migrate.py: upgrade a copy install without hand-merging and without overwriting your changes (Phase 9, IMP-3, CO-33).

* a table of per-file scenarios on a small synthetic kit (every branch of the decision table)
* dry run changes nothing; apply backs up, writes, defers KIT_VERSION while a conflict is open, and is idempotent
* the human-only rules: refuses inside a Claude Code session, refuses a dirty git tree, refuses a tampered kit source
* the exit criterion: a real v1.2.0 install (git archive of the tag) migrates onto this repository's dist/copy-install, with the forks
  an Install A / Install B style project has, without a hand merge for every non-overlapping change
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.dist import build_once, call

import migrate  # noqa: E402  (tests/__init__ puts .claude/scripts on sys.path)
from kitlib import kitmanifest as km  # noqa: E402

LINES = "".join("line %d\n" % i for i in range(1, 11))


def lines_with(**edits):
    out = LINES
    for old, new in edits.items():
        out = out.replace(old.replace("_", " "), new, 1)
    return out


def write_tree(root, files, exe=()):
    for rel, data in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode())
        if rel in exe:
            os.chmod(full, 0o755)


def make_tar(path, files):
    with tarfile.open(path, "w") as tf:
        for rel, data in files.items():
            data = data if isinstance(data, bytes) else data.encode()
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def tree_state(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p):
                out[os.path.relpath(p, root)] = b"->" + os.readlink(p).encode()
            else:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = fh.read()
    return out


A = ".claude/agents/"
BASE = {
    ".claude/agents/same.md": LINES, ".claude/agents/upd.md": LINES, ".claude/agents/keep.md": LINES, ".claude/agents/merge.md": LINES,
    ".claude/agents/conf.md": LINES, ".claude/agents/deleted.md": LINES, ".claude/agents/retired.md": LINES, ".claude/agents/retired-mod.md": LINES,
    ".claude/agents/binary.bin": b"\0\1\2", ".claude/agents/link.md": LINES, ".claude/hooks/run.sh": "#!/bin/sh\necho 1\n",
    ".claude/CLAUDE.md": "template v1\n", ".claude/loop.config.md": "config v1\n", ".claude/settings.json": '{"v": 1}\n', ".claude/context/backend.md": "primer v1\n",
    ".claude/KIT_VERSION": "1.0.0 (2026-01-01)\n",
}
NEW = {
    ".claude/agents/same.md": LINES, ".claude/agents/upd.md": lines_with(line_5="line five NEW"), ".claude/agents/keep.md": LINES,
    ".claude/agents/merge.md": lines_with(line_8="line eight KIT"), ".claude/agents/conf.md": lines_with(line_3="line three KIT"),
    ".claude/agents/deleted.md": lines_with(line_1="line one KIT"), ".claude/agents/binary.bin": b"\0\1\3", ".claude/agents/link.md": lines_with(line_2="two KIT"),
    ".claude/agents/brand-new.md": "new\n", ".claude/agents/collide.md": "kit collide\n", ".claude/hooks/run.sh": "#!/bin/sh\necho 2\n",
    ".claude/CLAUDE.md": "template v2\n", ".claude/loop.config.md": "config v2\n", ".claude/settings.json": '{"v": 2}\n', ".claude/context/backend.md": "primer v2\n",
}
LOCAL = dict(BASE)
LOCAL.update({
    ".claude/agents/keep.md": lines_with(line_4="line four MINE"), ".claude/agents/merge.md": lines_with(line_2="line two MINE"),
    ".claude/agents/conf.md": lines_with(line_3="line three MINE"), ".claude/agents/retired-mod.md": lines_with(line_6="mine"),
    ".claude/agents/binary.bin": b"\0\1\4", ".claude/agents/collide.md": "my own collide\n", ".claude/agents/mine-only.md": "my agent\n",
    ".claude/settings.json": '{"v": "mine"}\n',
})
del LOCAL[".claude/agents/deleted.md"]


class Scenarios(unittest.TestCase):
    """One synthetic kit (1.0.0 -> 2.0.0) and one project in every state the decision table distinguishes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kit-migrate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = os.path.join(self.tmp, "kit")
        self.project = os.path.join(self.tmp, "proj")
        files = dict(NEW)
        files[".claude/KIT_VERSION"] = "2.0.0 (2026-02-02)\n"
        write_tree(self.source, files, exe={".claude/hooks/run.sh"})
        manifest = km.build_manifest("2.0.0", {k: (v if isinstance(v, bytes) else v.encode()) for k, v in files.items()})
        write_tree(self.source, {km.MANIFEST_REL: km.dumps(manifest)})
        os.makedirs(os.path.join(self.source, ".claude", "migrate"))
        make_tar(os.path.join(self.source, ".claude", "migrate", "v1.0.0.tar"), BASE)
        write_tree(self.project, LOCAL, exe={".claude/hooks/run.sh"})
        os.remove(os.path.join(self.project, ".claude/agents/link.md"))
        os.symlink(os.path.join(self.tmp, "elsewhere.md"), os.path.join(self.project, ".claude/agents/link.md"))

    def run_migrate(self, *extra, env=None):
        rc, out, err = call(migrate.main, ["--repo", self.project, "--source", self.source, "--json"] + list(extra), env)
        doc = json.loads(out) if out.strip().startswith("{") else None
        return rc, doc, out, err

    def actions(self, doc):
        return {a["path"].split("/")[-1]: a["action"] for a in doc["actions"]}

    def test_the_decision_table(self):
        rc, doc, _, err = self.run_migrate()
        self.assertEqual(rc, 1, err)     # exit 1 = conflicts exist (informational in a dry run)
        got = self.actions(doc)
        want = {"upd.md": "update", "keep.md": "keep", "merge.md": "merge", "conf.md": "conflict", "deleted.md": "skip-deleted",
                "retired.md": "remove", "retired-mod.md": "keep-retired", "binary.bin": "conflict", "link.md": "conflict",
                "brand-new.md": "add", "collide.md": "conflict", "run.sh": "update",
                "CLAUDE.md": "update", "loop.config.md": "update", "backend.md": "update", "settings.json": "keep"}
        for name, action in want.items():
            self.assertEqual(got.get(name), action, name)
        self.assertNotIn("same.md", got, "up to date files are not listed")
        self.assertNotIn("mine-only.md", got, "files the kit never shipped are not in the plan")
        self.assertFalse(doc["applied"])

    def test_dry_run_changes_nothing(self):
        before = tree_state(self.project)
        self.run_migrate()
        self.assertEqual(tree_state(self.project), before)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".claude", ".migrate")))

    def test_apply_writes_merges_and_defers_the_version_while_a_conflict_is_open(self):
        before = tree_state(self.project)
        rc, doc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 1, err)
        after = tree_state(self.project)
        p = lambda rel: after[rel.replace("/", os.sep)]
        self.assertEqual(p(".claude/agents/upd.md"), lines_with(line_5="line five NEW").encode())
        merged = p(".claude/agents/merge.md").decode()
        self.assertIn("line two MINE", merged)
        self.assertIn("line eight KIT", merged)
        self.assertNotIn("<<<<<<<", merged)
        self.assertEqual(p(".claude/agents/keep.md"), before[".claude/agents/keep.md".replace("/", os.sep)])
        self.assertEqual(p(".claude/agents/conf.md"), before[".claude/agents/conf.md".replace("/", os.sep)], "a conflicted file is never written")
        marked = p(".claude/.migrate/conflicts/.claude/agents/conf.md").decode()
        self.assertTrue("<<<<<<< yours" in marked and ">>>>>>> kit 2.0.0" in marked and "line three MINE" in marked and "line three KIT" in marked)
        self.assertEqual(p(".claude/agents/collide.md"), b"my own collide\n")
        self.assertNotIn(".claude/agents/retired.md".replace("/", os.sep), after)
        self.assertIn(".claude/agents/retired-mod.md".replace("/", os.sep), after)
        self.assertEqual(p(".claude/settings.json"), b'{"v": "mine"}\n', "settings are a guardrail: never touched")
        self.assertEqual(p(".claude/CLAUDE.md"), b"template v2\n", "an unedited template is refreshed")
        self.assertEqual(p(".claude/agents/brand-new.md"), b"new\n")
        self.assertNotIn(".claude/agents/deleted.md".replace("/", os.sep), after, "a file you deleted stays deleted")
        self.assertEqual(p(".claude/KIT_VERSION"), b"1.0.0 (2026-01-01)\n", "the version moves only when nothing is left to resolve")
        self.assertNotIn(km.MANIFEST_REL.replace("/", os.sep), after)
        backups = [k for k in after if ".migrate" + os.sep + "backup" in k]
        self.assertTrue(any(k.endswith("upd.md") for k in backups) and any(k.endswith("retired.md") for k in backups) and any(k.endswith("merge.md") for k in backups))
        self.assertTrue(os.access(os.path.join(self.project, ".claude/hooks/run.sh"), os.X_OK), "executable bit follows the kit")
        report = json.loads(p(".claude/.migrate/report.json"))
        self.assertFalse(report["finished"])
        self.assertIn(".claude/agents/conf.md", report["pending"])

    def test_resolving_every_conflict_finishes_and_a_second_run_has_nothing_to_do(self):
        self.run_migrate("--apply")
        # conf.md: merge by hand; binary.bin: take the kit's; collide.md: take the kit's; link.md: remove the link and let the kit's file come
        with open(os.path.join(self.project, ".claude/agents/conf.md"), "w") as fh:
            fh.write(lines_with(line_3="line three BOTH"))
        os.remove(os.path.join(self.project, ".claude/agents/link.md"))
        rc, out, err = call(migrate.main, ["--repo", self.project, "--source", self.source, "--resolved", ".claude/agents/conf.md"])
        self.assertEqual(rc, 0, err)
        rc, doc, _, err = self.run_migrate("--take-kit", ".claude/agents/binary.bin", ".claude/agents/collide.md")
        self.assertEqual(rc, 0, err)
        self.assertTrue(doc["finished"])
        with open(os.path.join(self.project, ".claude/KIT_VERSION")) as fh:
            self.assertEqual(fh.read(), "2.0.0 (2026-02-02)\n")
        self.assertTrue(os.path.exists(os.path.join(self.project, km.MANIFEST_REL)))
        with open(os.path.join(self.project, ".claude/agents/conf.md")) as fh:
            self.assertEqual(fh.read(), lines_with(line_3="line three BOTH"), "a hand resolution is kept")
        with open(os.path.join(self.project, ".claude/agents/collide.md")) as fh:
            self.assertEqual(fh.read(), "kit collide\n")
        backed = [k for k in tree_state(self.project) if k.endswith("collide.md") and "backup" in k]
        self.assertTrue(backed, "--take-kit backs the replaced file up")
        before = tree_state(self.project)
        rc, doc, _, _ = self.run_migrate("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual([a for a in doc["actions"] if a["action"] in ("add", "update", "merge", "remove", "conflict", "take-kit")], [])
        after = tree_state(self.project)
        changed = {k for k in after if before.get(k) != after[k]}
        self.assertEqual({k for k in changed if ".migrate" not in k}, set(), "idempotent")

    def test_resolved_is_forgotten_when_the_file_changes_again(self):
        self.run_migrate("--apply")
        path = os.path.join(self.project, ".claude/agents/conf.md")
        with open(path, "w") as fh:
            fh.write(lines_with(line_3="resolved"))
        call(migrate.main, ["--repo", self.project, "--source", self.source, "--resolved", ".claude/agents/conf.md"])
        self.assertEqual(self.actions(self.run_migrate()[1])["conf.md"], "resolved")
        with open(path, "a") as fh:
            fh.write("edited after resolving\n")
        self.assertEqual(self.actions(self.run_migrate()[1])["conf.md"], "conflict")

    def test_human_only(self):
        before = tree_state(self.project)
        for flags in (["--apply"], ["--resolved", ".claude/agents/conf.md"], ["--take-kit", ".claude/agents/conf.md"]):
            for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                rc, doc, out, err = self.run_migrate(*flags, env={marker: "1"})
                self.assertEqual(rc, 3, flags)
                self.assertIn("refusing to run inside a Claude Code session", err)
        self.assertEqual(tree_state(self.project), before)
        rc, doc, out, err = self.run_migrate(env={"CLAUDECODE": "1"})
        self.assertEqual(rc, 1, "the dry run is fine inside a session")

    def test_a_tampered_or_incomplete_source_is_refused_before_anything_is_written(self):
        before = tree_state(self.project)
        with open(os.path.join(self.source, ".claude/agents/upd.md"), "a") as fh:
            fh.write("evil\n")
        rc, doc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("not the version its KIT_MANIFEST describes", err)
        os.remove(os.path.join(self.source, ".claude/agents/upd.md"))
        rc, _, _, err = self.run_migrate("--apply")
        self.assertEqual((rc, "incomplete" in err), (2, True))
        self.assertEqual(tree_state(self.project), before)

    def test_a_plugin_directory_is_not_a_source_and_an_unknown_version_needs_a_base(self):
        rc, _, err = call(migrate.main, ["--repo", self.project, "--source", os.path.join(self.tmp, "nokit")], {})
        self.assertEqual(rc, 2)
        self.assertIn("a plugin directory is not a source", err)
        with open(os.path.join(self.project, ".claude/KIT_VERSION"), "w") as fh:
            fh.write("0.9.0\n")
        rc, _, out, err = self.run_migrate()
        self.assertEqual(rc, 2)
        self.assertIn("no base archive for the installed version 0.9.0", err)
        rc, doc, _, err = self.run_migrate("--from-version", "1.0.0")
        self.assertEqual(rc, 1, err)

    def test_nothing_to_migrate_without_a_claude_directory(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        rc, out, err = call(migrate.main, ["--repo", empty, "--source", self.source], {})
        self.assertEqual(rc, 2)
        self.assertIn("nothing to migrate", err)

    def test_paths_on_the_command_line_must_be_project_relative(self):
        for bad in ("/etc/passwd", "../x", ".claude/agents/"):
            rc, doc, out, err = self.run_migrate("--take-kit", bad)
            self.assertEqual(rc, 2, bad)
        rc, doc, out, err = self.run_migrate("--take-kit", ".claude/agents/same.md")
        self.assertEqual(rc, 2)
        self.assertIn("not a kit file that differs", err)

    def test_a_symlinked_work_directory_stops_the_apply_before_any_write(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        os.symlink(outside, os.path.join(self.project, ".claude", ".migrate"))
        before = tree_state(self.project)
        rc, doc, out, err = self.run_migrate("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("symlink", err)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(tree_state(self.project), before)

    def test_a_hostile_base_archive_is_refused(self):
        for name, setup in (("../escape", lambda ti: None), ("/abs/path", lambda ti: None), ("link", lambda ti: (setattr(ti, "type", tarfile.SYMTYPE), setattr(ti, "linkname", "/etc/passwd")))):
            path = os.path.join(self.tmp, "bad.tar")
            with tarfile.open(path, "w") as tf:
                ti = tarfile.TarInfo(name)
                setup(ti)
                ti.size = 1 if ti.type == tarfile.REGTYPE else 0
                tf.addfile(ti, io.BytesIO(b"x") if ti.type == tarfile.REGTYPE else None)
            rc, doc, out, err = self.run_migrate("--base-archive", path)
            self.assertEqual(rc, 2, name)
            self.assertIn("cannot read the base archive", err)

    def test_abbreviated_flags_are_not_accepted(self):
        with self.assertRaises(SystemExit):
            call(migrate.main, ["--repo", self.project, "--source", self.source, "--app"], {})

    def test_a_symlinked_project_path_is_never_written_through(self):
        target = os.path.join(self.tmp, "elsewhere.md")
        self.run_migrate("--apply")
        self.assertFalse(os.path.exists(target), "the link's target was not created")
        self.assertTrue(os.path.islink(os.path.join(self.project, ".claude/agents/link.md")))


class GitTree(unittest.TestCase):
    def test_apply_refuses_a_dirty_git_tree_and_allows_it_with_the_flag(self):
        tmp = tempfile.mkdtemp(prefix="kit-migrate-git-")
        self.addCleanup(shutil.rmtree, tmp, True)
        source, project = os.path.join(tmp, "kit"), os.path.join(tmp, "proj")
        new = {".claude/agents/a.md": "new\n"}
        write_tree(source, new)
        write_tree(source, {km.MANIFEST_REL: km.dumps(km.build_manifest("2.0.0", {k: v.encode() for k, v in new.items()})), ".claude/KIT_VERSION": "2.0.0\n"})
        os.makedirs(os.path.join(source, ".claude", "migrate"))
        make_tar(os.path.join(source, ".claude", "migrate", "v1.0.0.tar"), {".claude/agents/a.md": "old\n"})
        write_tree(project, {".claude/agents/a.md": "old\n", ".claude/KIT_VERSION": "1.0.0\n"})
        git = lambda *a: subprocess.run(["git", "-C", project, "-c", "user.email=t@t", "-c", "user.name=t"] + list(a), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "v1")
        with open(os.path.join(project, ".claude/KIT_VERSION"), "w") as fh:
            fh.write("1.0.0\n# dirty\n")
        rc, out, err = call(migrate.main, ["--repo", project, "--source", source, "--apply"], {})
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes", err)
        with open(os.path.join(project, ".claude/agents/a.md")) as fh:
            self.assertEqual(fh.read(), "old\n")
        rc, out, err = call(migrate.main, ["--repo", project, "--source", source, "--apply", "--allow-dirty"], {})
        self.assertEqual(rc, 0, err)
        with open(os.path.join(project, ".claude/agents/a.md")) as fh:
            self.assertEqual(fh.read(), "new\n")


class V120Install(unittest.TestCase):
    """The exit criterion: both real installs migrate without hand-merging (report 16.1 row 9)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kit-v120-")
        cls.pristine = os.path.join(cls.tmp, "pristine")
        os.makedirs(cls.pristine)
        with tarfile.open(os.path.join(REPO_ROOT, ".claude", "migrate", "v1.2.0.tar")) as tf:
            try:
                tf.extractall(cls.pristine, filter="data")
            except TypeError:        # Python before 3.12 has no extraction filters
                tf.extractall(cls.pristine)
        cls.source = os.path.join(REPO_ROOT, "dist", "copy-install")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def project(self, forks=None):
        d = os.path.join(self.tmp, "p%d" % len(os.listdir(self.tmp)))
        shutil.copytree(self.pristine, d, symlinks=True)
        for rel, fn in (forks or {}).items():
            p = os.path.join(d, rel)
            with open(p, "r", encoding="utf-8") if os.path.exists(p) else io.StringIO("") as fh:
                text = fh.read()
            new = fn(text)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new)
        return d

    def migrate(self, project, *extra):
        rc, out, err = call(migrate.main, ["--repo", project, "--source", self.source, "--json"] + list(extra), {})
        return rc, (json.loads(out) if out.startswith("{") else None), err

    def test_the_archive_is_the_v1_2_0_install(self):
        files = km.read_tar_tree(os.path.join(REPO_ROOT, ".claude", "migrate", "v1.2.0.tar"))
        self.assertEqual(km.parse_version(files[".claude/KIT_VERSION"].decode()), "1.2.0")
        for rel in (".claude/commands/pm.md", ".claude/commands/orchestrate.md", ".claude/hooks/guard-git.sh", ".claude/agents/code-reviewer.md", "AGENTS.md"):
            self.assertIn(rel, files)
        self.assertFalse([p for p in files if p.startswith(".claude/scripts/")], "v1.2.0 had no scripts")

    def test_the_archive_is_what_git_archive_makes_of_the_tag(self):
        if subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "-q", "--verify", "v1.2.0^{commit}"], stdout=subprocess.DEVNULL).returncode != 0:
            self.skipTest("the v1.2.0 tag is not in this clone (a shallow CI checkout)")
        out = subprocess.run(["git", "-C", REPO_ROOT, "archive", "--format=tar", "v1.2.0", ".claude", "AGENTS.md", ".cursor", "skills"], stdout=subprocess.PIPE, check=True).stdout
        with open(os.path.join(REPO_ROOT, ".claude", "migrate", "v1.2.0.tar"), "rb") as fh:
            self.assertEqual(fh.read(), out)

    def test_a_pristine_install_migrates_with_no_conflict_and_ends_at_the_kit(self):
        p = self.project()
        rc, doc, err = self.migrate(p)
        self.assertEqual(rc, 0, err)
        acts = {}
        for a in doc["actions"]:
            acts.setdefault(a["action"], []).append(a["path"])
        self.assertEqual(sorted(acts.get("remove", [])), sorted(".claude/commands/%s.md" % m for m in ("pm", "architect", "tech-lead", "planner", "orchestrate")))
        self.assertNotIn("conflict", acts)
        hooks = [".claude/hooks/guard-git.sh", ".claude/hooks/guard-main-edit.sh"]
        self.assertEqual(sorted(acts.get("keep-retired", [])), hooks, "the v1 hooks stay while settings.json names them")
        rc, doc, err = self.migrate(p, "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(doc["finished"], "not finished while a retired hook is still wired")
        for rel in hooks:
            self.assertTrue(os.path.exists(os.path.join(p, rel)), rel)
        shutil.copyfile(os.path.join(self.source, ".claude", "settings.json"), os.path.join(p, ".claude", "settings.json"))   # the human switch
        rc, doc, err = self.migrate(p, "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(a["path"] for a in doc["actions"] if a["action"] == "remove"), hooks)
        self.assertTrue(doc["finished"])
        for rel in hooks:
            self.assertFalse(os.path.exists(os.path.join(p, rel)), rel)
        with open(os.path.join(self.source, km.MANIFEST_REL)) as fh:
            m = km.loads(fh.read())
        for rel, ent in m["files"].items():
            if ent["class"] == "kit":
                with open(os.path.join(p, rel), "rb") as fh:
                    self.assertEqual(km.sha256_bytes(fh.read()), ent["sha256"], rel)
        self.assertFalse(os.path.exists(os.path.join(p, ".claude", "commands")), "the empty commands directory is gone")
        self.assertTrue(os.path.exists(os.path.join(p, ".claude", "skills", "orchestrate", "SKILL.md")))
        rc, doc, err = self.migrate(p)
        self.assertEqual([a for a in doc["actions"] if a["action"] in ("add", "update", "merge", "remove", "conflict")], [], "a second run is empty")

    def test_forked_installs_merge_without_hand_work_unless_the_same_lines_changed(self):
        forks = {
            ".claude/agents/backend-engineer.md": lambda t: t + "\n## Local rule\nAlways use our ORM wrapper.\n",             # appended: merges cleanly
            ".claude/agents/app-engineer.md": lambda t: t.replace("name: app-engineer", "name: web-engineer", 1),             # same line the kit changed? no: kit adds lines elsewhere
            ".claude/hooks/guard-main-edit.sh": lambda t: t + "\n# our block list\n",     # retired in 2.0 and still wired: stays
            ".claude/loop.config.md": lambda t: t.replace("autonomy:", "autonomy:", 1) + "\n# ours\n",
            ".claude/CLAUDE.md": lambda t: t + "\nOur stack: Rails.\n",
            ".claude/commands/pm.md": lambda t: t + "\nOur PM tweak.\n",
            ".claude/agents/ours-extra.md": lambda t: "---\nname: ours-extra\n---\nours\n",
        }
        p = self.project(forks)
        rc, doc, err = self.migrate(p, "--apply")
        by = {a["path"]: a["action"] for a in doc["actions"]}
        self.assertEqual(by[".claude/agents/backend-engineer.md"], "merge")
        self.assertEqual(by[".claude/hooks/guard-main-edit.sh"], "keep-retired")
        self.assertEqual(by[".claude/CLAUDE.md"], "keep")
        self.assertEqual(by[".claude/commands/pm.md"], "keep-retired")
        self.assertNotIn(".claude/agents/ours-extra.md", by)
        with open(os.path.join(p, ".claude/agents/backend-engineer.md")) as fh:
            text = fh.read()
        self.assertIn("Always use our ORM wrapper.", text)
        self.assertIn("effort:", text, "the kit's new frontmatter arrived in the same file")
        with open(os.path.join(p, ".claude/CLAUDE.md")) as fh:
            self.assertIn("Our stack: Rails.", fh.read())
        self.assertTrue(os.path.exists(os.path.join(p, ".claude/commands/pm.md")), "a command you changed is never deleted")
        self.assertTrue(os.path.exists(os.path.join(p, ".claude/agents/ours-extra.md")))
        self.assertEqual(rc, 1 if any(v == "conflict" for v in by.values()) else 0)


if __name__ == "__main__":
    unittest.main()
