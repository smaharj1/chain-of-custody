"""tools/check-refs.py, tools/gen-config.py and tools/test.sh."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from . import REPO_ROOT
from tests.helpers import BaseTestCase

CHECK_REFS = os.path.join(REPO_ROOT, "tools", "check-refs.py")
GEN_CONFIG = os.path.join(REPO_ROOT, "tools", "gen-config.py")


def run(argv, **kw):
    p = subprocess.run([sys.executable] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


class CheckRefs(BaseTestCase):
    def make(self, files, allow=None):
        repo = self.repo()
        for rel, text in files.items():
            repo.write(rel, text)
        if allow is not None:
            repo.write("tools/check-refs.allow", allow)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "files")
        return repo

    def check(self, repo, *extra):
        return run([CHECK_REFS, "--repo", repo.path] + list(extra))

    def test_repo_passes(self):
        rc, out, err = run([CHECK_REFS])
        self.assertEqual(rc, 0, out + err)

    def test_missing_code_span_path_is_reported_with_file_and_line(self):
        repo = self.make({"README.md": "intro\nsee `docs/missing.md` and `tools/ok.py`\n", "tools/ok.py": "x"})
        rc, out, _ = self.check(repo)
        self.assertEqual(rc, 1)
        self.assertIn("README.md:2: code reference to missing path docs/missing.md", out)
        self.assertNotIn("tools/ok.py", out)

    def test_existing_paths_and_directories_pass(self):
        repo = self.make({"README.md": "`docs/` `docs/a.md` `./docs/a.md` `.claude/x/` [a](docs/a.md)\n",
                          "docs/a.md": "x", ".claude/x/y.md": "y"})
        self.assertEqual(self.check(repo)[0], 0)

    def test_links_resolve_relative_to_the_file(self):
        repo = self.make({"docs/a.md": "[b](b.md) [up](../README.md) [x](nope.md) [web](https://example.com/x) [anchor](#top)\n",
                          "docs/b.md": "x", "README.md": "r"})
        rc, out, _ = self.check(repo)
        self.assertEqual(rc, 1)
        self.assertIn("link reference to missing path docs/nope.md", out)
        self.assertEqual(out.count("missing path"), 1)

    def test_link_anchors_and_line_suffixes_are_stripped(self):
        repo = self.make({"README.md": "[a](docs/a.md#section) `docs/a.md:12` `docs/a.md:12-30` `docs/a.md`.\n", "docs/a.md": "x"})
        self.assertEqual(self.check(repo)[0], 0)

    def test_patterns_and_fenced_blocks_are_skipped(self):
        text = ("`docs/features/<slug>/x.md` `docs/*.md` `tools/{a,b}.py` `$HOME/docs/x`\n"
                "```\n`docs/in-a-fence.md`\ndocs/also-fenced.md\n```\n")
        repo = self.make({"README.md": text})
        self.assertEqual(self.check(repo)[0], 0)

    def test_non_prefixed_tokens_are_ignored(self):
        repo = self.make({"README.md": "`src/app.py` `foo.md` `git commit` `docs` `a/docs/x.md`\n"})
        self.assertEqual(self.check(repo)[0], 0)

    def test_tokens_inside_longer_code_spans_are_checked(self):
        repo = self.make({"README.md": "run `git mv docs/gone.md .claude/context/`\n"})
        rc, out, _ = self.check(repo)
        self.assertEqual(rc, 1)
        self.assertIn("docs/gone.md", out)

    def test_exclusions(self):
        repo = self.make({
            "docs/design/review-2026-10.md": "`docs/future.md`\n",
            "docs/design/v2-implementation-decisions.md": "`docs/future.md`\n",
            "changelog.d/x.md": "`docs/future.md`\n",
            ".claude/worktrees/t/README.md": "`docs/future.md`\n",
        })
        self.assertEqual(self.check(repo)[0], 0)

    def test_allowlist_and_unused_entries(self):
        repo = self.make({"README.md": "`docs/features/x/req.md` `.claude/loop.jsonl`\n"},
                         allow="# comment\ndocs/features/*   # user project\n.claude/loop.jsonl\nnever/used\n")
        rc, out, _ = self.check(repo, "--list-unused-allow")
        self.assertEqual(rc, 0, out)
        self.assertIn("unused allowlist entry: never/used", out)
        self.assertNotIn("unused allowlist entry: docs/features/*", out)

    def test_untracked_files_are_not_scanned(self):
        repo = self.make({"README.md": "ok\n"})
        repo.write("NEW.md", "`docs/missing.md`\n")  # not added
        self.assertEqual(self.check(repo)[0], 0)

    def test_mdc_files_are_scanned(self):
        repo = self.make({".cursor/rules/x.mdc": "read `.claude/nope.md`\n"})
        self.assertEqual(self.check(repo)[0], 1)


class GenConfigCli(BaseTestCase):
    def test_check_passes_on_the_repo(self):
        rc, out, err = run([GEN_CONFIG, "--check"])
        self.assertEqual(rc, 0, out + err)

    def test_stdout_matches_committed_files(self):
        for flag, rel in (([], ".claude/loop.config.md"), (["--example"], "examples/loop.config.md")):
            rc, out, _ = run([GEN_CONFIG, "--stdout"] + flag)
            self.assertEqual(rc, 0)
            with open(os.path.join(REPO_ROOT, rel)) as fh:
                self.assertEqual(out, fh.read())

    def test_check_fails_and_write_repairs_a_drifted_copy(self):
        d = self.tmpdir()
        os.makedirs(os.path.join(d, ".claude"))
        os.makedirs(os.path.join(d, "examples"))
        for rel in (".claude/loop.config.md", "examples/loop.config.md"):
            with open(os.path.join(d, rel), "w") as fh:
                fh.write("hand edited\n")
        rc, out, err = run([GEN_CONFIG, "--check", "--repo", d])
        self.assertEqual(rc, 1)
        self.assertIn("OUT OF SYNC", err)
        rc, out, _ = run([GEN_CONFIG, "--write", "--repo", d])
        self.assertEqual(rc, 0)
        self.assertEqual(run([GEN_CONFIG, "--check", "--repo", d])[0], 0)
        rc, out, _ = run([GEN_CONFIG, "--write", "--repo", d])
        self.assertIn("unchanged", out)

    def test_requires_a_mode(self):
        self.assertEqual(run([GEN_CONFIG])[0], 2)


class TestSh(unittest.TestCase):
    def test_script_is_executable_and_runs_unittest_discover(self):
        path = os.path.join(REPO_ROOT, "tools", "test.sh")
        self.assertTrue(os.access(path, os.X_OK))
        with open(path) as fh:
            text = fh.read()
        self.assertIn("python3 -m unittest discover -s tests -t .", text)
        self.assertTrue(text.startswith("#!/bin/sh"))

    def test_workflow_matrix_and_hardening(self):
        with open(os.path.join(REPO_ROOT, ".github", "workflows", "kit-tests.yml")) as fh:
            text = fh.read()
        self.assertIn('python: ["3.9", "3.13"]', text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("pull_request_target", text)
        for line in text.split("\n"):
            if "uses:" in line:
                self.assertIn("# pin to SHA", line)


if __name__ == "__main__":
    unittest.main()
