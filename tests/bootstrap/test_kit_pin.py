"""The CI templates run the kit's code from the kit's own repository at a pinned release, never from the tree under test.

A workflow that ran `python3 scripts/verify.py` (or `.claude/scripts/...`) from its checkout would let a pull request rewrite the
gate that judges it. Each job that runs kit code fetches the kit into `$RUNNER_TEMP/kit` at KIT_REF and refuses to go on unless the
checkout is at KIT_SHA; the bootstrap setup script fills both placeholders when it writes the file.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR

TEMPLATE_DIRS = (os.path.join(REPO_ROOT, "templates", "github"), os.path.join(REPO_ROOT, "templates", "infra"))
WORKFLOW_DIRS = tuple(os.path.join(d, "workflows") for d in TEMPLATE_DIRS)
KIT_USE = "$RUNNER_TEMP/kit/scripts/"


def template_files():
    for base in TEMPLATE_DIRS:
        for dirpath, _, names in os.walk(base):
            for n in sorted(names):
                yield os.path.join(dirpath, n)


def workflows():
    for d in WORKFLOW_DIRS:
        for n in sorted(os.listdir(d)):
            if n.endswith((".yml", ".yaml")):
                yield os.path.join(d, n)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def code(text):
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("#"))


def job_blocks(text):
    """{job id: text} of the jobs: section (two-space-indented ids)."""
    body = code(text).split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z_][A-Za-z0-9_-]*):\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def steps(job):
    return re.split(r"(?m)^      - ", job)[1:]


def is_fetch(step):
    return 'git init -q "$RUNNER_TEMP/kit"' in step


class NoKitCodeFromTheTree(unittest.TestCase):
    def test_no_template_names_a_copy_install_path_or_archives_the_tree(self):
        for path in template_files():
            text = read(path)
            self.assertNotIn(".claude/scripts", text, path)
            self.assertNotIn(".claude/templates", text, path)
            self.assertNotIn("git archive", text, path)

    def test_no_kit_script_runs_from_the_workspace(self):
        tree = re.compile(r"python3?\s+(?:-\S+\s+)*\"?(?:\./|\$GITHUB_WORKSPACE/|\$\{\{\s*github\.workspace\s*\}\}/|base/)?scripts/")
        for path in workflows():
            for ln in code(read(path)).split("\n"):
                self.assertIsNone(tree.search(ln), "%s: %s" % (os.path.basename(path), ln.strip()))

    def test_kit_code_is_run_only_from_the_fetched_kit(self):
        used = 0
        for path in workflows():
            for m in re.finditer(r"(?m)python3?\s+\"?(\S+?/scripts/)", code(read(path))):
                self.assertEqual(m.group(1), KIT_USE, path)
                used += 1
        self.assertGreater(used, 5)


class TheKitIsPinned(unittest.TestCase):
    def test_every_job_that_runs_kit_code_fetches_the_pinned_kit_first(self):
        jobs_using = 0
        for path in workflows():
            for name, job in job_blocks(read(path)).items():
                where = "%s job %s" % (os.path.basename(path), name)
                st = steps(job)
                uses = [i for i, s in enumerate(st) if KIT_USE in s and not is_fetch(s)]
                if not uses:
                    continue
                jobs_using += 1
                fetch = [i for i, s in enumerate(st) if is_fetch(s)]
                self.assertEqual(len(fetch), 1, where)
                self.assertLess(fetch[0], uses[0], where)
                f = st[fetch[0]]
                self.assertIn('KIT_REF: "{{KIT_REF}}"', f, where)
                self.assertIn('KIT_SHA: "{{KIT_SHA}}"', f, where)
                self.assertIn('fetch -q --depth 1 --no-tags "$KIT_REPO" "refs/tags/$KIT_REF"', f, where)
                self.assertIn('got="$(git -C "$RUNNER_TEMP/kit" rev-parse HEAD)"', f, where)
                self.assertIn('if [ "$got" != "$KIT_SHA" ]; then', f, where)
                # an unfilled placeholder is not a commit, so the step fails closed
                self.assertIn('case "$KIT_SHA" in', f, where)
                self.assertRegex(f, r'\*\[!0-9a-f\]\*\|""\) .*exit 1', where)
                after = f.split('if [ "$got" != "$KIT_SHA" ]; then', 1)[1]
                self.assertIn("exit 1", after.split("fi", 1)[0], where)
        self.assertGreater(jobs_using, 5)

    def test_the_templates_fetch_from_the_kit_repository_the_plugin_names(self):
        sys.path.insert(0, os.path.join(SCRIPTS_DIR, "github"))
        import repo_script
        with open(os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["repository"].rstrip("/") + ".git", repo_script.KIT_REPO)
        repos = set()
        for path in workflows():
            repos.update(re.findall(r"KIT_REPO: (\S+)", read(path)))
        self.assertEqual(repos, {repo_script.KIT_REPO})

    def test_a_fetched_kit_is_never_given_credentials(self):
        for path in workflows():
            for name, job in job_blocks(read(path)).items():
                for s in steps(job):
                    if is_fetch(s):
                        self.assertNotIn("secrets.", s, "%s job %s" % (os.path.basename(path), name))
                        self.assertNotIn("GITHUB_TOKEN", s, "%s job %s" % (os.path.basename(path), name))


if __name__ == "__main__":
    unittest.main()
