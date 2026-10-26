"""gitutil: resolution, worktrees, CAS against temp repos and a temp bare remote (report §10.3)."""
from __future__ import annotations

import os
import unittest

from kitlib import gitutil
from tests.helpers import BaseTestCase


class Resolution(BaseTestCase):
    def test_toplevel_and_common_dir_in_main_checkout(self):
        repo = self.repo()
        self.assertEqual(gitutil.toplevel(repo.path), os.path.realpath(repo.path))
        self.assertEqual(gitutil.common_dir(repo.path), os.path.join(os.path.realpath(repo.path), ".git"))
        self.assertFalse(gitutil.is_linked_worktree(repo.path))

    def test_subdirectory_and_not_yet_existing_file(self):
        repo = self.repo()
        repo.write("src/a/b.txt", "x")
        self.assertEqual(gitutil.toplevel(os.path.join(repo.path, "src", "a")), os.path.realpath(repo.path))
        new_file = os.path.join(repo.path, "does", "not", "exist", "yet.py")
        self.assertEqual(gitutil.toplevel(new_file), os.path.realpath(repo.path))
        self.assertEqual(gitutil.common_dir(new_file), gitutil.common_dir(repo.path))

    def test_linked_worktree_has_own_toplevel_but_shared_common_dir(self):
        repo = self.repo()
        wt = repo.add_worktree("wt1", branch="feat/x")
        self.assertEqual(gitutil.toplevel(wt), wt)
        self.assertNotEqual(gitutil.toplevel(wt), gitutil.toplevel(repo.path))
        self.assertEqual(gitutil.common_dir(wt), gitutil.common_dir(repo.path))
        self.assertNotEqual(gitutil.git_dir(wt), gitutil.common_dir(wt))
        self.assertTrue(gitutil.is_linked_worktree(wt))
        self.assertEqual(gitutil.current_branch(wt), "feat/x")
        self.assertEqual(gitutil.current_branch(repo.path), "main")

    def test_outside_any_repo(self):
        d = self.tmpdir()
        self.assertIsNone(gitutil.toplevel(d))
        self.assertIsNone(gitutil.common_dir(os.path.join(d, "nope", "file")))
        self.assertIsNone(gitutil.current_branch(d))

    def test_git_dir_env_cannot_redirect_resolution(self):
        repo = self.repo()
        other = self.repo()
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(other.path, ".git")
        try:
            self.assertEqual(gitutil.toplevel(repo.path), os.path.realpath(repo.path))
        finally:
            if old is None:
                del os.environ["GIT_DIR"]
            else:
                os.environ["GIT_DIR"] = old

    def test_detached_head_has_no_branch(self):
        repo = self.repo()
        repo.git("checkout", "-q", "--detach")
        self.assertIsNone(gitutil.current_branch(repo.path))

    def test_rev_parse_show_file_ancestor_clean(self):
        repo = self.repo()
        a = repo.head()
        b = repo.commit_file("f.txt", "one", "add f")
        self.assertEqual(gitutil.rev_parse("HEAD", repo.path), b)
        self.assertIsNone(gitutil.rev_parse("nope", repo.path))
        self.assertEqual(gitutil.show_file(b, "f.txt", repo.path), "one")
        self.assertIsNone(gitutil.show_file(a, "f.txt", repo.path))
        self.assertTrue(gitutil.is_ancestor(a, b, repo.path))
        self.assertFalse(gitutil.is_ancestor(b, a, repo.path))
        self.assertTrue(gitutil.is_clean(repo.path))
        repo.write("untracked.txt", "x")
        self.assertFalse(gitutil.is_clean(repo.path))

    def test_option_looking_revisions_are_refused(self):
        repo = self.repo()
        with self.assertRaises(ValueError):
            gitutil.rev_parse("--output=/tmp/x", repo.path)
        with self.assertRaises(ValueError):
            gitutil.show_file("--x", "f", repo.path)


class UpdateRefCas(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.repo_ = self.repo()
        self.a = self.repo_.head()
        self.b = self.repo_.commit_file("f.txt", "1", "second")
        self.c = self.repo_.commit_file("f.txt", "2", "third")
        self.ref = "refs/loopctl/state/plan1"

    def test_create_only_then_conflicting_create(self):
        r = gitutil.update_ref_cas(self.ref, self.a, None, self.repo_.path)
        self.assertTrue(r)
        self.assertEqual(self.repo_.git("rev-parse", self.ref), self.a)
        again = gitutil.update_ref_cas(self.ref, self.b, None, self.repo_.path)
        self.assertFalse(again)
        self.assertEqual(self.repo_.git("rev-parse", self.ref), self.a)

    def test_swap_succeeds_with_right_old_and_fails_with_stale_old(self):
        gitutil.update_ref_cas(self.ref, self.a, None, self.repo_.path)
        self.assertTrue(gitutil.update_ref_cas(self.ref, self.b, self.a, self.repo_.path))
        stale = gitutil.update_ref_cas(self.ref, self.c, self.a, self.repo_.path)
        self.assertFalse(stale)
        self.assertEqual(self.repo_.git("rev-parse", self.ref), self.b)

    def test_swap_of_missing_ref_with_an_old_value_fails(self):
        self.assertFalse(gitutil.update_ref_cas(self.ref, self.b, self.a, self.repo_.path))
        self.assertEqual(self.repo_.git_rc("rev-parse", "--verify", "-q", self.ref)[0], 1)

    def test_delete_cas(self):
        gitutil.update_ref_cas(self.ref, self.a, None, self.repo_.path)
        self.assertFalse(gitutil.delete_ref_cas(self.ref, self.b, self.repo_.path))
        self.assertTrue(gitutil.delete_ref_cas(self.ref, self.a, self.repo_.path))
        self.assertIsNone(gitutil.rev_parse(self.ref, self.repo_.path))

    def test_works_from_a_linked_worktree_on_the_shared_ref_store(self):
        wt = self.repo_.add_worktree("wt", branch="feat/y")
        self.assertTrue(gitutil.update_ref_cas(self.ref, self.a, None, wt))
        self.assertEqual(self.repo_.git("rev-parse", self.ref), self.a)
        self.assertFalse(gitutil.update_ref_cas(self.ref, self.b, None, wt))

    def test_unsafe_arguments_are_rejected_before_git_runs(self):
        bad_refs = ["HEAD", "refs/heads/../x", "refs/heads/a b", "refs/x;rm", "-x", "refs/", "refs/heads/x.lock",
                    "refs/heads/x\nfoo", "refs/heads/x\n", "refs/heads/-"]
        for ref in bad_refs[:-1]:
            with self.subTest(ref):
                with self.assertRaises(ValueError):
                    gitutil.update_ref_cas(ref, self.a, None, self.repo_.path)
        for new in ("HEAD", "abc", "--stdin", self.a[:10], ""):
            with self.subTest(new):
                with self.assertRaises(ValueError):
                    gitutil.update_ref_cas(self.ref, new, None, self.repo_.path)
        with self.assertRaises(ValueError):
            gitutil.update_ref_cas(self.ref, self.a, "HEAD", self.repo_.path)

    def test_repo_hooks_do_not_run(self):
        hook = os.path.join(self.repo_.path, ".git", "hooks", "reference-transaction")
        marker = os.path.join(self.repo_.parent, "hook-ran")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch '%s'\n" % marker)
        os.chmod(hook, 0o755)
        gitutil.update_ref_cas(self.ref, self.a, None, self.repo_.path)
        self.assertFalse(os.path.exists(marker))


class PushCas(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.repo_, self.bare = self.repo_with_remote()
        self.a = self.repo_.head()
        self.b = self.repo_.commit_file("f.txt", "1", "second")
        self.c = self.repo_.commit_file("f.txt", "2", "third")
        self.ref = "refs/loopctl/lock/plan1"
        self.remote = self.bare.path

    def test_create_only_succeeds_once(self):
        first = gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path)
        self.assertTrue(first, first.stderr)
        self.assertEqual(self.bare.ref(self.ref), self.a)
        second = gitutil.push_cas(self.remote, self.ref, self.b, None, self.repo_.path)
        self.assertFalse(second)  # a plain push would have fast-forwarded; create-only must not
        self.assertEqual(self.bare.ref(self.ref), self.a)

    def test_swap_with_correct_and_stale_expectation(self):
        gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path)
        self.assertTrue(gitutil.push_cas(self.remote, self.ref, self.b, self.a, self.repo_.path))
        self.assertEqual(self.bare.ref(self.ref), self.b)
        stale = gitutil.push_cas(self.remote, self.ref, self.c, self.a, self.repo_.path)
        self.assertFalse(stale)
        self.assertEqual(self.bare.ref(self.ref), self.b)

    def test_expectation_is_what_the_caller_read_not_the_last_fetch(self):
        # Another writer moves the remote ref. We never fetched, so a bare --force-with-lease would
        # compare against stale tracking info; the explicit <ref>:<old> form must still refuse.
        gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path)
        self.repo_.git("push", "-q", self.remote, "%s:%s" % (self.b, self.ref))  # someone else moved it
        self.assertEqual(self.bare.ref(self.ref), self.b)
        mine = gitutil.push_cas(self.remote, self.ref, self.c, self.a, self.repo_.path)
        self.assertFalse(mine)
        self.assertEqual(self.bare.ref(self.ref), self.b)

    def test_ls_remote_ref(self):
        self.assertIsNone(gitutil.ls_remote_ref(self.remote, self.ref, self.repo_.path))
        gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path)
        self.assertEqual(gitutil.ls_remote_ref(self.remote, self.ref, self.repo_.path), self.a)

    def test_unsafe_arguments(self):
        with self.assertRaises(ValueError):
            gitutil.push_cas("--receive-pack=evil", self.ref, self.a, None, self.repo_.path)
        with self.assertRaises(ValueError):
            gitutil.push_cas(self.remote, "refs/heads/main --force", self.a, None, self.repo_.path)
        with self.assertRaises(ValueError):
            gitutil.push_cas(self.remote, self.ref, "main", None, self.repo_.path)
        with self.assertRaises(ValueError):
            gitutil.push_cas(self.remote, self.ref, self.a, "main", self.repo_.path)

    def test_only_the_named_remote_receives_the_push(self):
        gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path)
        self.assertEqual(self.repo_.git("remote").split(), ["origin"])
        self.assertEqual(self.bare.ref(self.ref), self.a)

    def test_pre_push_hook_does_not_run(self):
        hook = os.path.join(self.repo_.path, ".git", "hooks", "pre-push")
        marker = os.path.join(self.repo_.parent, "pre-push-ran")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch '%s'\nexit 1\n" % marker)
        os.chmod(hook, 0o755)
        self.assertTrue(gitutil.push_cas(self.remote, self.ref, self.a, None, self.repo_.path))
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
