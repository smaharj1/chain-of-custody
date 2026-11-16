"""How the guards read a `python` command line (second review, R-A-2/R-A-3): `-mNAME` and `-cCODE` take the rest of the word as their value, as getopt does, and
a glob in a `cd` or in a not-yet-existing file name is expanded against the file system the way the shell would."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from guards import invoke, paths


def mods(cmd: str, cwd: str = "/tmp"):
    return [i for i in invoke.analyze(cmd, cwd).invs if i.name.startswith("py-module:")]


def codes(cmd: str, cwd: str = "/tmp"):
    return [c.code for c in invoke.analyze(cmd, cwd).inline]


class PythonOptions(unittest.TestCase):
    def test_joined_m_is_a_module(self):
        for cmd, mod, rest in (("python3 -mloopctl approve item F1", "loopctl", ["approve", "item", "F1"]),
                               ("python3 -m loopctl approve", "loopctl", ["approve"]),
                               ("python3 -Bmloopctl x", "loopctl", ["x"]),
                               ("python3 -Wignore -X dev -mguards.run set-halt", "guards.run", ["set-halt"]),
                               ("python3 -W error -u -m guards.run set-halt", "guards.run", ["set-halt"]),
                               ("python3 -m\"loopctl\" status", "loopctl", ["status"])):
            with self.subTest(cmd=cmd):
                got = mods(cmd)
                self.assertEqual([(g.name, g.args) for g in got], [("py-module:" + mod, rest)])

    def test_a_value_that_contains_an_m_is_not_a_module(self):
        # -Wmodule::x: the W option takes the word, the m inside it is part of the warning filter
        self.assertEqual(mods("python3 -Wmodule script.py"), [])
        self.assertEqual(mods("python3 -Xmodule script.py"), [])

    def test_joined_c_is_code(self):
        self.assertEqual(codes("python3 '-cprint(1)'"), ["print(1)"])
        self.assertEqual(codes("python3 -c 'print(2)'"), ["print(2)"])
        self.assertEqual(codes("python3 -Bc 'print(3)'"), ["print(3)"])

    def test_pytest_and_friends_are_not_modules_the_guards_judge(self):
        for cmd in ("python3 -m pytest tests", "python3 -mpytest -k loopctl", "python3 -m unittest discover", "python3 -m json.tool x.json"):
            with self.subTest(cmd=cmd):
                self.assertEqual(mods(cmd), [])

    def test_coverage_is_a_runner_module(self):
        self.assertEqual([m.name for m in mods("python3 -m coverage run script.py")], ["py-module:coverage"])


class Globs(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="kit-glob-"))
        os.makedirs(os.path.join(self.root, ".git", "loopctl", "sessions"))
        os.makedirs(os.path.join(self.root, "scripts", "github"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_glob_in_the_directory_part_of_a_new_file_is_expanded(self):
        hits = paths.expand_glob(".g?t/loopctl/sessions/new.json", self.root)
        self.assertEqual(hits, [os.path.join(self.root, ".git", "loopctl", "sessions", "new.json")])

    def test_a_glob_that_matches_nothing_stays_literal(self):
        self.assertEqual(paths.expand_glob("nothing*/x.json", self.root), ["nothing*/x.json"])

    def test_cd_into_a_single_glob_match_is_followed_and_an_ambiguous_one_is_unknown(self):
        os.makedirs(os.path.join(self.root, "scripts", "guards"))
        one = invoke.analyze("cd scripts/gith* && python3 labels.py", self.root).invs
        self.assertEqual([i.cwd for i in one if i.name.startswith("python")], [os.path.join(self.root, "scripts", "github")])
        many = invoke.analyze("cd scripts/g* && python3 labels.py", self.root).invs
        self.assertTrue(all(i.cwd_unknown for i in many if i.name.startswith("python")))


if __name__ == "__main__":
    unittest.main()
