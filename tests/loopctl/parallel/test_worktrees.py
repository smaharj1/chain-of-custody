"""Worker worktrees (report 11.5 item 3, 19.7 L-1 / L-3, 5.4; D-2, D-3, D-22): created and removed only under `.claude/worktrees/`, and only safely."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest

from loopctl.parallel import worktrees
from loopctl import common

from ...helpers import TempRepo
from . import put, read


class Worktrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = TempRepo()
        cls.repo = cls.t.path

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def branch(self, name):
        self.t.git("branch", name, "main")
        self.addCleanup(self.t.git_rc, "branch", "-D", name)
        return name

    def test_a_worktree_is_a_checkout_of_the_item_branch_under_the_root_and_the_clone_stays_clean(self):
        br = self.branch("feat/wt-one-a1")
        path = worktrees.create(self.repo, "wt-one-a1", br)
        self.addCleanup(worktrees.remove, self.repo, path, False)
        self.assertEqual(path, os.path.join(os.path.realpath(self.repo), ".claude", "worktrees", "wt-one-a1"))
        self.assertEqual(self.t.git("-C", path, "branch", "--show-current"), br)
        self.assertEqual(self.t.git("status", "--porcelain"), "")                       # `.claude/worktrees/` is excluded in this clone
        self.assertIn("/.claude/worktrees/", read(os.path.join(self.t.common_dir(), "info", "exclude")))
        self.assertEqual([w["branch"] for w in worktrees.registered(self.repo) if w["path"] == path], [br])
        with self.assertRaises(common.Refused):
            worktrees.create(self.repo, "wt-one-a1", br)                                  # already there

    def test_names_that_are_paths_are_refused_for_both_kinds(self):
        br = self.branch("feat/wt-two-a1")
        for bad in ("../x-a1", "a/b-a1", "/abs-a1", "", "..", ".", "x y-a1", "UPPER-a1", "no-attempt", "x-a1\n", "x" * 60 + "-a1"):
            with self.subTest(bad):
                with self.assertRaises(common.Refused):
                    worktrees.create(self.repo, bad, br)
        for bad in ("../x", "a/b", "", "..", ".", "a b", "x.lock", "wt-one-a1", "a\n"):
            with self.subTest(hook=bad):
                with self.assertRaises(common.Refused):
                    worktrees.safe_path(self.repo, bad, "hook")
        self.assertTrue(worktrees.safe_path(self.repo, "my.tree_1", "hook").endswith("/.claude/worktrees/my.tree_1"))

    def test_a_branch_that_does_not_exist_or_looks_like_an_option_is_refused(self):
        for bad in ("feat/nope-a1", "--detach", "-b", "", "a..b"):
            with self.subTest(bad):
                with self.assertRaises(common.Refused):
                    worktrees.create(self.repo, "wt-three-a1", bad)
        self.assertFalse(os.path.lexists(os.path.join(self.repo, ".claude", "worktrees", "wt-three-a1")))

    def test_removing_saves_uncommitted_work_on_the_items_branch(self):
        br = self.branch("feat/wt-four-a1")
        path = worktrees.create(self.repo, "wt-four-a1", br)
        put(os.path.join(path, "half-done.txt"), "in progress\n")
        self.assertTrue(worktrees.remove(self.repo, path))
        self.assertFalse(os.path.lexists(path))
        self.assertIn("half-done.txt", self.t.git("show", "--stat", "--format=%s", br))
        self.assertIn("WIP(loopctl)", self.t.git("log", "-1", "--format=%s", br))

    def test_only_a_directory_directly_under_the_root_can_be_removed(self):
        outside = tempfile.mkdtemp(prefix="outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        keep = os.path.join(outside, "precious.txt")
        put(keep, "keep\n")
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees"), exist_ok=True)
        link = os.path.join(self.repo, ".claude", "worktrees", "evil-a1")
        os.symlink(outside, link)
        self.addCleanup(os.unlink, link)
        for target in (outside, link, self.repo, os.path.join(self.repo, ".claude", "worktrees"), os.path.join(self.repo, ".claude"), "/", os.path.join(self.repo, ".git"),
                       os.path.join(self.repo, ".claude", "worktrees", "evil-a1", "x"), os.path.join(self.repo, ".claude", "worktrees", "..", "..")):
            with self.subTest(target):
                with self.assertRaises(common.Refused):
                    worktrees.remove(self.repo, target)
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".git")))

    def test_a_symlinked_worktrees_root_is_refused_before_anything_is_made(self):
        other = TempRepo()
        self.addCleanup(other.cleanup)
        elsewhere = tempfile.mkdtemp(prefix="elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        os.makedirs(os.path.join(other.path, ".claude"))
        os.symlink(elsewhere, os.path.join(other.path, ".claude", "worktrees"))
        other.git("branch", "feat/x-a1")
        with self.assertRaises(common.Refused):
            worktrees.create(other.path, "x-a1", "feat/x-a1")
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(os.path.join(other.path, ".claude", "worktrees"))
        shutil.rmtree(os.path.join(other.path, ".claude"))
        os.symlink(elsewhere, os.path.join(other.path, ".claude"))
        with self.assertRaises(common.Refused):
            worktrees.create(other.path, "x-a1", "feat/x-a1")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_orphan_cleanup_takes_loopctl_worktrees_a_live_worker_does_not_own_and_leaves_a_persons_alone(self):
        mine = worktrees.create(self.repo, "wt-five-a1", self.branch("feat/wt-five-a1"))
        live = worktrees.create(self.repo, "wt-six-a2", self.branch("feat/wt-six-a2"))
        theirs = worktrees.create(self.repo, "my-experiment", self.branch("wt-experiment"), kind="hook")
        self.addCleanup(worktrees.remove, self.repo, theirs, False)
        self.addCleanup(worktrees.remove, self.repo, live, False)
        put(os.path.join(mine, "left.txt"), "a dead worker's file\n")
        self.assertEqual(sorted(os.path.basename(p) for p in worktrees.orphans(self.repo, [live])), ["wt-five-a1"])
        self.assertEqual([os.path.basename(p) for p in worktrees.cleanup_orphans(self.repo, [live])], ["wt-five-a1"])
        self.assertTrue(os.path.isdir(live) and os.path.isdir(theirs))
        self.assertIn("left.txt", self.t.git("show", "--stat", "--format=", "feat/wt-five-a1"))

    def test_a_directory_git_does_not_know_is_cleaned_only_by_name(self):
        junk = os.path.join(self.repo, ".claude", "worktrees", "stale-a9")
        os.makedirs(os.path.join(junk, "deep"))
        put(os.path.join(junk, "deep", "x"), "x")
        person = os.path.join(self.repo, ".claude", "worktrees", "persons-notes")
        os.makedirs(person)
        self.addCleanup(shutil.rmtree, person, True)
        self.assertEqual([os.path.basename(p) for p in worktrees.cleanup_orphans(self.repo)], ["stale-a9"])
        self.assertTrue(os.path.isdir(person))
        self.assertFalse(os.path.lexists(junk))


class Include(unittest.TestCase):
    def test_only_the_two_local_env_files_are_ever_honoured(self):
        text = "# comment\n.env.local\n.env.test\n.env.production\n*.pem\n../.env.local\n!.env.local\n.env.local \n  .env.test\n~/.aws/credentials\n"
        ok, bad = worktrees.parse_include(text)
        self.assertEqual(ok, [".env.local", ".env.test"])
        self.assertEqual(bad, [".env.production", "*.pem", "../.env.local", "!.env.local", "~/.aws/credentials"])
        self.assertEqual(worktrees.parse_include("")[0], [])

    def test_copy_is_private_skips_symlinks_missing_and_large_files(self):
        src, dst = tempfile.mkdtemp(prefix="src-"), tempfile.mkdtemp(prefix="dst-")
        self.addCleanup(shutil.rmtree, src, True)
        self.addCleanup(shutil.rmtree, dst, True)
        secret = os.path.join(src, "real-secret")
        put(secret, "TOP=SECRET\n")
        put(os.path.join(src, ".env.local"), "PORT=1\n")
        os.symlink(secret, os.path.join(src, ".env.test"))
        names = [".env.local", ".env.test", ".env.production", "../x"]
        self.assertEqual(worktrees.apply_include(src, dst, names), [".env.local"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dst, ".env.local")).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(dst)), [".env.local"])
        os.unlink(os.path.join(src, ".env.test"))
        put(os.path.join(src, ".env.test"), "x" * (worktrees.MAX_INCLUDE_FILE + 1))
        self.assertEqual(worktrees.apply_include(src, dst, [".env.test"]), [])


if __name__ == "__main__":
    unittest.main()
