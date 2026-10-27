"""tests/helpers/gitfast.py answers read-only git questions from the repository's files: prove it prints exactly what git prints.

A fixture repo (merges, annotated and light tags, remote-tracking refs, a symbolic remote HEAD, nested and look-alike directories,
binary/empty/executable/symlink files, a root commit) is asked every shape the shim handles, in a loose, a ref-packed and a fully
packed layout; the answer must equal real git's (return code and stdout), or the shim must decline (`Unsupported`) so that git runs.
The loose layout must be answered without git for every shape: if it is not, the shim has stopped earning its place.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from typing import Dict, List, Optional

from . import REPO_ROOT  # noqa: F401  (puts .claude/scripts on sys.path)
from .helpers import TempRepo
from .helpers import gitfast
from .helpers.gitrepo import git_test_env


def real_git(args: List[str], cwd: str, input_text: Optional[str] = None):
    proc = subprocess.run(["git"] + args, cwd=cwd, env=git_test_env(), input=input_text.encode() if input_text is not None else None,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout


class Fixture(object):
    def __init__(self) -> None:
        self.repo = TempRepo()
        r = self.repo
        r.write("src/a.txt", "a\n")
        r.write("src/sub/b.txt", "b\n")
        r.write("srcx/c.txt", "c\n")
        r.write("plan/items/A.json", "{}\n")
        r.write("plan2/x", "x\n")
        r.write("empty", "")
        r.write("bin.dat", "")
        with open(os.path.join(r.path, "bin.dat"), "wb") as fh:
            fh.write(b"\0\1\2\xff\xfe binary \0")
        r.write("run.sh", "#!/bin/sh\n", mode=0o755)
        os.symlink("src/a.txt", os.path.join(r.path, "link"))
        r.write("name with space.txt", "s\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "tree one")
        r.git("switch", "-q", "-c", "feat/x")
        r.commit_file("src/new.txt", "n\n", "on feat")
        r.git("switch", "-q", "main")
        r.commit_file("src/main-only.txt", "m\n", "on main")
        r.git("merge", "--no-ff", "-q", "-m", "merge feat", "feat/x")
        r.git("tag", "light")
        r.git("tag", "-a", "-m", "annotated", "v1")
        r.git("update-ref", "refs/remotes/origin/main", r.head())
        r.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        r.git("update-ref", "refs/loopctl/state/demo", r.head())
        r.git("switch", "-q", "-c", "side", "main~1")
        r.commit_file("side.txt", "s\n", "side commit")
        r.git("switch", "-q", "--detach", "main")
        r.git("switch", "-q", "main")
        r.git("switch", "-q", "--orphan", "orphan")
        r.git("commit", "-q", "--allow-empty", "-m", "root of an unrelated history")
        r.git("switch", "-q", "main")
        self.shas = {n: r.git("rev-parse", n) for n in ("main", "main~1", "main~2", "feat/x", "side", "orphan", "v1^{commit}")}
        self.shas["tag"] = r.git("rev-parse", "v1")
        self.shas["tree"] = r.git("rev-parse", "main^{tree}")
        self.shas["blob"] = r.git("rev-parse", "main:src/a.txt")

    def cases(self) -> List[List[str]]:
        s = self.shas
        c = [["rev-parse", "--verify", "-q", ref] for ref in
             ("HEAD", "HEAD^{commit}", "HEAD^{object}", "HEAD^{commit}^{object}", "refs/heads/main", "refs/heads/main^{commit}",
              "refs/heads/main^{object}", "refs/heads/nope", "refs/heads/feat/x", "refs/heads/feat/x^{commit}^{object}", "refs/tags/light",
              "refs/tags/v1", "refs/tags/v1^{commit}", "refs/remotes/origin/main", "refs/remotes/origin/HEAD", "refs/loopctl/state/demo^{commit}",
              "refs/loopctl/state/missing^{commit}", "refs/heads/feat", "refs/heads", s["main"], s["main"] + "^{commit}", s["main"] + "^{object}",
              s["tag"] + "^{commit}", s["tree"] + "^{object}", s["tree"] + "^{commit}", "0" * 40 + "^{object}", "main", "HEAD~1", "refs/heads/ma*")]
        c += [["rev-parse", "--verify", "refs/heads/main"], ["rev-parse", "--verify", "refs/heads/nope"]]
        c += [["rev-parse", "--show-toplevel"], ["rev-parse", "--git-common-dir"], ["rev-parse", "--git-dir"],
              ["rev-parse", "--show-toplevel", "--git-common-dir"], ["rev-parse", "--abbrev-ref", "HEAD"]]
        c += [["symbolic-ref", "--short", "-q", "HEAD"], ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
              ["symbolic-ref", "-q", "HEAD"], ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], ["symbolic-ref", "-q", "--short", "refs/heads/main"],
              ["symbolic-ref", "-q", "--short", "refs/remotes/none/HEAD"], ["symbolic-ref", "--short", "HEAD"]]
        c += [["show-ref", "--verify", "-q", ref] for ref in ("refs/heads/main", "refs/heads/nope", "refs/tags/v1", "refs/loopctl/state/demo")]
        c += [["for-each-ref", "--format=%(refname)"] + pats for pats in
              ([], ["refs/heads/"], ["refs/heads"], ["refs/heads/main"], ["refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/heads/main",
               "refs/heads/master"], ["refs/tags"], ["refs/loopctl"], ["refs/heads/feat/"], ["refs/nothing"], ["refs/heads/m*"])]
        for a, b in (("HEAD", "refs/heads/main"), ("HEAD", "refs/heads/side"), ("HEAD", "refs/heads/orphan"), (s["main~1"], "refs/heads/main"),
                     ("refs/heads/feat/x", "refs/heads/main"), ("refs/heads/side", "refs/heads/feat/x"), ("refs/tags/v1", "HEAD"),
                     (s["main"], s["main"]), ("refs/heads/nope", "HEAD")):
            c += [["merge-base", a, b], ["merge-base", "--is-ancestor", a, b], ["merge-base", "--is-ancestor", b, a]]
        base = ["ls-tree", "-z", "-l", "--full-tree"]
        for rev in (s["main"], "HEAD", "refs/heads/feat/x", s["main~2"], s["tree"], s["orphan"], "refs/heads/nope"):
            c += [base + [rev, "--"], base + ["-r", rev, "--"], ["ls-tree", "-r", "-z", rev], ["ls-tree", "-z", rev], ["ls-tree", "-z", "-l", "-r", rev]]
        for specs in (["plan"], ["plan/"], ["plan/items"], ["plan/items/A.json"], ["src"], ["src/sub"], ["srcx", "plan", "empty"], ["nope"], ["pla"],
                      ["plan2"], ["link"], ["run.sh", "bin.dat"], ["name with space.txt"], ["."], ["src/*"], [":(top)src"]):
            c += [base + ["-r", s["main"], "--"] + specs]
        c += [["cat-file", "--batch"]]
        c += [["show", "%s:%s" % (s["main"], p)] for p in ("src/a.txt", "empty", "bin.dat", "run.sh", "link", "src", "src/sub/b.txt", "nope", "plan/items/A.json",
                                                              "name with space.txt")]
        c += [["show", "%s:%s" % (s["tree"], "src/a.txt")], ["show", "HEAD:src/a.txt"], ["show", s["main"]]]
        return c

    def batch_inputs(self) -> List[str]:
        s = self.shas
        names = [s["main"], s["main"] + "^", s["main~2"] + "^", s["orphan"] + "^", s["tree"], s["blob"], s["tag"], "0" * 40, s["main"] + ":src/a.txt",
                 s["main"] + ":src", s["main"] + ":nope", s["main"] + "^:src/a.txt", s["main"] + ":src/sub/b.txt", s["main"] + ":empty", s["main"] + ":bin.dat",
                 s["main"] + ":link", s["tree"] + ":src/a.txt"]
        return ["\n".join(names) + "\n", s["main"] + "\n", "\n".join(names[:3]) + "\n"]


class Gitfast(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = Fixture()
        cls.addClassCleanup(cls.fx.repo.cleanup)

    def compare(self, layout: str, require_answers: bool) -> int:
        fx, path = self.fx, self.fx.repo.path
        answered = 0
        env = dict(git_test_env())
        for args in fx.cases():
            inputs = fx.batch_inputs() if args == ["cat-file", "--batch"] else [None]
            for inp in inputs:
                want = real_git(args, path, inp)
                try:
                    got = gitfast.emulate(args, path, env, inp)
                except gitfast.Unsupported:
                    continue
                answered += 1
                self.assertEqual(got, want, "%s: git %s (input %r)" % (layout, " ".join(args), inp))
        return answered

    def test_every_answer_equals_gits_and_a_loose_repository_is_answered_without_git(self):
        n = self.compare("loose", True)
        self.assertGreater(n, 115, "the shim answered only %d questions without git" % n)

    def test_each_supported_shape_is_answered_in_a_loose_repository(self):
        env = dict(git_test_env())
        path = self.fx.repo.path
        s = self.fx.shas
        shapes = [["rev-parse", "--verify", "-q", "refs/loopctl/state/demo^{commit}"], ["rev-parse", "--verify", "-q", "HEAD^{commit}^{object}"],
                  ["rev-parse", "--verify", "-q", "refs/heads/nope"], ["rev-parse", "--show-toplevel", "--git-common-dir"], ["rev-parse", "--git-common-dir"],
                  ["rev-parse", "--abbrev-ref", "HEAD"], ["symbolic-ref", "--short", "-q", "HEAD"], ["show-ref", "--verify", "-q", "refs/heads/main"],
                  ["for-each-ref", "--format=%(refname)", "refs/remotes/origin/HEAD", "refs/heads/main"], ["merge-base", "HEAD", "refs/heads/main"],
                  ["merge-base", "--is-ancestor", s["main~1"], "refs/heads/main"], ["ls-tree", "-z", "-l", "--full-tree", "-r", s["main"], "--", "plan", "src"],
                  ["ls-tree", "-z", "-l", "--full-tree", s["main"], "--"], ["ls-tree", "-r", "-z", s["main"]], ["show", s["main"] + ":src/a.txt"],
                  ["-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat", "rev-parse", "--verify", "-q", "HEAD^{object}"]]
        for args in shapes:
            gitfast.emulate(args, path, env)                       # raises Unsupported if the shim declines
        gitfast.emulate(["cat-file", "--batch"], path, env, s["main"] + "\n" + s["main"] + ":src/a.txt\n")

    def test_the_objects_it_writes_are_the_ones_git_writes(self):
        parent = tempfile.mkdtemp(prefix="gitfast-w-")
        self.addCleanup(shutil.rmtree, parent, True)
        path = os.path.join(parent, "repo")
        shutil.copytree(self.fx.repo.path, path, symlinks=True)
        s = self.fx.shas
        env = dict(git_test_env())
        for text in ("", "x\n", "{\"a\": 1}\n" * 50, "caf\u00e9 \u2713\n", "no newline"):
            want = real_git(["hash-object", "--stdin"], path, text)
            got = gitfast.emulate(["hash-object", "-w", "--stdin"], path, env, text)
            self.assertEqual(got, want)
            self.assertEqual(real_git(["cat-file", "blob", got[1].decode().strip()], path)[1], text.encode("utf-8"))
        blob = s["blob"]
        lines = ["100644 blob %s\tstate.json\n" % blob, "100644 blob %s\ta.txt\n100755 blob %s\tb\n" % (blob, blob),
                 "100644 blob %s\tz\n040000 tree %s\tz.d\n100644 blob %s\tz.e\n" % (blob, s["tree"], blob),
                 "100644 blob %s\ta\n040000 tree %s\ta-b\n040000 tree %s\ta0\n" % (blob, s["tree"], s["tree"]), ""]
        for text in lines:
            self.assertEqual(gitfast.emulate(["mktree"], path, env, text), real_git(["mktree"], path, text), text)
            tree = real_git(["mktree"], path, text)[1].decode().strip()
            self.assertEqual(real_git(["cat-file", "-t", tree], path), (0, b"tree\n"))
            self.assertEqual(real_git(["ls-tree", tree], path)[0], 0)
        tree = real_git(["mktree"], path, lines[0])[1].decode().strip()
        for date in ("2026-01-01T00:00:00+0000", "2026-03-04T05:06:07-0700", "2026-12-31T23:59:59+0545", "2020-02-29T12:00:00+0100"):
            wenv = dict(env, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_AUTHOR_NAME="loopctl", GIT_AUTHOR_EMAIL="loopctl@localhost.invalid",
                        GIT_COMMITTER_NAME="loopctl", GIT_COMMITTER_EMAIL="loopctl@localhost.invalid")
            for args in (["commit-tree", tree, "-m", "approve plan demo by Pat [seq 1]"], ["commit-tree", tree, "-p", s["main"], "-m", "advance A -> building"],
                         ["commit-tree", tree, "-p", s["main"], "-p", s["side"], "-m", "two parents\nand a body\n"], ["commit-tree", tree, "-m", "caf\u00e9"]):
                want = subprocess.run(["git"] + args, cwd=path, env=wenv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                got = gitfast.emulate(args, path, wenv)
                self.assertEqual(got, (want.returncode, want.stdout), " ".join(args))
                self.assertEqual(real_git(["cat-file", "-t", got[1].decode().strip()], path)[1], b"commit\n")
        # and a commit made without a pinned date is a well-formed commit stamped now
        now_env = dict(env, GIT_AUTHOR_NAME="n", GIT_AUTHOR_EMAIL="e@x.invalid", GIT_COMMITTER_NAME="n", GIT_COMMITTER_EMAIL="e@x.invalid")
        for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            now_env.pop(k)
        sha = gitfast.emulate(["commit-tree", tree, "-m", "now"], path, now_env)[1].decode().strip()
        self.assertEqual(real_git(["cat-file", "-t", sha], path), (0, b"commit\n"))
        self.assertIn(b"author n <e@x.invalid> ", real_git(["cat-file", "commit", sha], path)[1])

    def test_it_declines_instead_of_guessing(self):
        env = dict(git_test_env())
        path = self.fx.repo.path
        s = self.fx.shas
        declined = [["rev-parse", "main"], ["rev-parse", "--verify", "-q", "main"], ["rev-parse", "--verify", "HEAD~1"],
                    ["rev-parse", "--verify", "refs/heads/nope"], ["log", "-1"], ["status", "--porcelain"], ["commit", "-m", "x"],
                    ["update-ref", "refs/heads/x", s["main"]], ["ls-tree", s["main"], "src"], ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                    ["merge-base", "--is-ancestor", "HEAD"], ["show", "HEAD"], ["cat-file", "-p", s["main"]]]
        for args in declined:
            with self.assertRaises(gitfast.Unsupported, msg=" ".join(args)):
                gitfast.emulate(args, path, env)
        with self.assertRaises(gitfast.Unsupported):
            gitfast.emulate(["rev-parse", "--show-toplevel"], path, dict(env, GIT_DIR="/nonexistent"))
        with self.assertRaises(gitfast.Unsupported):
            gitfast.emulate(["rev-parse", "--show-toplevel"], os.path.join(path, ".git"), env)
        with self.assertRaises(gitfast.Unsupported):
            gitfast.emulate(["rev-parse", "--show-toplevel"], os.path.join(path, "nowhere"), env)
        with self.assertRaises(gitfast.Unsupported):
            gitfast.emulate(["rev-parse", "--show-toplevel"], tempfile.gettempdir(), env)

    def copy_fixture(self) -> str:
        parent = tempfile.mkdtemp(prefix="gitfast-")
        self.addCleanup(shutil.rmtree, parent, True)
        dst = os.path.join(parent, "repo")
        shutil.copytree(self.fx.repo.path, dst, symlinks=True)
        return dst

    def test_refs_packed_into_packed_refs_are_read_the_same(self):
        dst = self.copy_fixture()
        subprocess.run(["git", "pack-refs", "--all", "--prune"], cwd=dst, env=git_test_env(), check=True)
        self.assertFalse(os.path.exists(os.path.join(dst, ".git", "refs", "heads", "main")))
        fx, env = self.fx, dict(git_test_env())
        answered = 0
        for args in fx.cases():
            if args[0] in ("cat-file", "ls-tree", "show"):          # object questions: the loose layout and the gc layout cover them
                continue
            want = real_git(args, dst)
            try:
                got = gitfast.emulate(args, dst, env)
            except gitfast.Unsupported:
                continue
            answered += 1
            self.assertEqual(got, want, "packed refs: git " + " ".join(args))
        self.assertGreater(answered, 60)

    def test_objects_in_a_pack_are_left_to_git(self):
        dst = self.copy_fixture()
        subprocess.run(["git", "gc", "-q", "--prune=now"], cwd=dst, env=git_test_env(), check=True)
        env = dict(git_test_env())
        for args in self.fx.cases():
            if args[0] not in ("cat-file", "ls-tree", "show", "merge-base"):      # object questions only: refs do not change under gc
                continue
            inputs = self.fx.batch_inputs() if args == ["cat-file", "--batch"] else [None]
            for inp in inputs:
                try:
                    got = gitfast.emulate(args, dst, env, inp)
                except gitfast.Unsupported:
                    continue
                self.assertEqual(got, real_git(args, dst, inp), "after gc: git " + " ".join(args))

    def test_installed_it_wraps_gitutil_run_git_and_a_miss_still_runs_git(self):
        from kitlib import gitutil
        path = self.fx.repo.path
        original = gitutil.run_git
        gitfast.install()
        try:
            self.assertIsNot(gitutil.run_git, original)
            before = dict(gitfast.stats)
            fast = gitutil.run_git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd=path, isolated=True)
            self.assertEqual((fast.returncode, fast.stdout), (0, self.fx.shas["main"] + "\n"))
            self.assertEqual(gitfast.stats["emulated"], before["emulated"] + 1)
            slow = gitutil.run_git(["log", "-1", "--format=%s"], cwd=path, isolated=True)
            self.assertEqual(slow.stdout.strip(), "merge feat")
            self.assertEqual(gitfast.stats["fallback"], before["fallback"] + 1)
            raw = gitutil.run_git(["show", self.fx.shas["main"] + ":src/a.txt"], cwd=path, isolated=True, binary=True)
            self.assertEqual(raw.stdout, b"a\n")
            with self.assertRaises(gitutil.GitError):
                gitutil.run_git(["rev-parse", "--verify", "-q", "refs/heads/nope"], cwd=path, isolated=True, check=True)
        finally:
            gitfast.uninstall()
        self.assertIs(gitutil.run_git, original)

    def test_the_switch_turns_it_off(self):
        from kitlib import gitutil
        gitfast.install()
        old = os.environ.get("TESTS_GITFAST")
        os.environ["TESTS_GITFAST"] = "0"
        try:
            before = dict(gitfast.stats)
            gitutil.run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=self.fx.repo.path)
            self.assertEqual(gitfast.stats["emulated"], before["emulated"])
        finally:
            gitfast.uninstall()
            if old is None:
                os.environ.pop("TESTS_GITFAST", None)
            else:
                os.environ["TESTS_GITFAST"] = old


if __name__ == "__main__":
    unittest.main()
