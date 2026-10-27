"""The WorktreeCreate / WorktreeRemove hook script (report 11.5 item 3, D-3, D-22): it creates under `.claude/worktrees` or it refuses."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

import worktree_hook

from loopctl.parallel import mutex
from kitlib import runstate

from ...helpers import TempRepo, clean_env
from . import put


def run_hook(action, doc, env):
    out, err = io.StringIO(), io.StringIO()
    rc = worktree_hook.main([action], stdin=io.StringIO(doc if isinstance(doc, str) else json.dumps(doc)), stdout=out, stderr=err, env=env)
    return rc, out.getvalue(), err.getvalue()


class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = TempRepo()
        cls.repo = os.path.realpath(cls.t.path)
        cls.home = tempfile.mkdtemp(prefix="hook-home-")
        cls.env = clean_env(home=cls.home)
        put(os.path.join(cls.repo, ".env.local"), "PORT=1\n")
        put(os.path.join(cls.repo, ".env.production"), "KEY=prod\n")
        put(os.path.join(cls.repo, ".gitignore"), ".env*\n")
        cls.t.write(".worktreeinclude", ".env.local\n.env.production\n")
        cls.t.git("add", ".gitignore", ".worktreeinclude")
        cls.t.git("commit", "-q", "-m", "include")
        cls.tip = cls.t.head()
        cls.rs = runstate.RunState.for_path(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()
        shutil.rmtree(cls.home, ignore_errors=True)

    def create(self, name, cwd=None, **more):
        doc = {"name": name, "cwd": cwd or self.repo, "hook_event_name": "WorktreeCreate"}
        doc.update(more)
        return run_hook("create", doc, self.env)

    def test_create_cuts_the_worktree_off_the_local_integration_tip_and_prints_only_its_path(self):
        self.t.git("commit", "-q", "--allow-empty", "-m", "local only: not on any remote")
        tip = self.t.head()
        rc, out, err = self.create("hook-one")
        self.assertEqual(rc, 0, err)
        path = os.path.join(self.repo, ".claude", "worktrees", "hook-one")
        self.assertEqual(out, path + "\n")
        self.assertEqual(self.t.git("-C", path, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.t.git("-C", path, "branch", "--show-current"), "worktree-hook-one")
        self.assertEqual(sorted(f for f in os.listdir(path) if f.startswith(".env")), [".env.local"])      # .env.production is never copied
        slot = self.rs and mutex.LeaseStore(self.rs).read("ports-0")
        self.assertEqual((slot["meta"]["kind"], slot["meta"]["worktree"], slot["pid"]), ("hook", path, 0))
        self.assertEqual(self.t.git("status", "--porcelain"), "")
        rc, out, err = run_hook("remove", {"worktree_path": path, "cwd": self.repo}, self.env)
        self.assertEqual((rc, out), (0, ""), err)
        self.assertFalse(os.path.lexists(path))
        self.assertIsNone(mutex.LeaseStore(self.rs).read("ports-0"))                                       # the slot went back with the worktree

    def test_a_name_that_is_a_path_or_not_plain_is_refused_and_nothing_is_made(self):
        before = set(os.listdir(os.path.join(self.repo, ".claude", "worktrees"))) if os.path.isdir(os.path.join(self.repo, ".claude", "worktrees")) else set()
        for bad in ("../../escape", "/tmp/abs", "a/b", "", "..", "has space", "x" * 80, "../x", "a\nb", None, 7, ["a"], "slice-a-a1", "evil.lock"):
            with self.subTest(bad):
                rc, out, err = self.create(bad)
                self.assertEqual((rc, out), (2, ""), err)
                self.assertIn("refused", err)
        now = set(os.listdir(os.path.join(self.repo, ".claude", "worktrees"))) if os.path.isdir(os.path.join(self.repo, ".claude", "worktrees")) else set()
        self.assertEqual(before, now)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.repo), "escape")))

    def test_input_it_does_not_recognise_fails_closed(self):
        for doc in ("not json", "[]", "null", json.dumps({"name": "ok-name"}), json.dumps({"name": "ok-name", "cwd": "relative/dir"}),
                    json.dumps({"name": "ok-name", "cwd": "/nonexistent-dir"}), json.dumps({"name": "ok-name", "cwd": tempfile.gettempdir()}), "x" * 70000):
            with self.subTest(doc[:30]):
                rc, out, err = run_hook("create", doc, self.env)
                self.assertEqual((rc, out), (2, ""), err)
        self.assertEqual(run_hook("destroy", {}, self.env)[0], 64)
        self.assertEqual(worktree_hook.main([], stdin=io.StringIO("{}"), stdout=io.StringIO(), stderr=io.StringIO(), env=self.env), 64)

    def test_remove_refuses_anything_that_is_not_a_worktree_under_the_root(self):
        outside = tempfile.mkdtemp(prefix="outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        put(os.path.join(outside, "precious"), "keep\n")
        root = os.path.join(self.repo, ".claude", "worktrees")
        os.makedirs(root, exist_ok=True)
        link = os.path.join(root, "link-out")
        os.symlink(outside, link)
        self.addCleanup(os.unlink, link)
        for path in (outside, link, self.repo, root, os.path.join(root, ".."), "relative/path", "", None, "/", os.path.join(root, "nope", "..", "..", "..")):
            with self.subTest(path):
                rc, out, err = run_hook("remove", {"worktree_path": path, "cwd": self.repo}, self.env)
                self.assertEqual((rc, out), (2, ""), err)
        self.assertTrue(os.path.exists(os.path.join(outside, "precious")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".git")))

    def test_a_hook_run_from_inside_a_linked_worktree_still_means_the_project(self):
        rc, out, err = self.create("hook-two")
        self.assertEqual(rc, 0, err)
        inner = out.strip()
        rc, out, err = self.create("hook-three", cwd=inner)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), os.path.join(self.repo, ".claude", "worktrees", "hook-three"))      # not nested inside hook-two
        for p in (inner, out.strip()):
            self.assertEqual(run_hook("remove", {"worktree_path": p, "cwd": self.repo}, self.env)[0], 0)

    def test_nothing_is_left_half_made_when_the_port_slots_are_all_taken(self):
        store = mutex.LeaseStore(self.rs)
        held = [store.acquire(mutex.ports_lease(n), "someone") for n in range(8)]
        self.addCleanup(lambda: [store.release(l) for l in held])
        rc, out, err = self.create("hook-four")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no free port slot", err)
        self.assertFalse(os.path.lexists(os.path.join(self.repo, ".claude", "worktrees", "hook-four")))
        self.assertEqual([w for w in self.t.git("worktree", "list", "--porcelain").splitlines() if w.endswith("hook-four")], [])


if __name__ == "__main__":
    unittest.main()
