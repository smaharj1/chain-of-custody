"""paths.agent_authored (review fixes D1 item 7, GD-6): a tracked, clean file is "reviewed" only when it is also unchanged from a trusted base.

The base is the active run's `baseSha` (run.json), else the merge-base of HEAD with the protected branch. A conductor commit on a feat/*
branch must not turn an engineer's script into a reviewed one; with no determinable base the file is agent-authored (fail closed).
"""
from __future__ import annotations

import json
import os
import unittest

from guards import paths
from kitlib import runstate

from tests.helpers.gitrepo import TempRepo


class AgentAuthoredTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.commit_file("scripts/reviewed.py", "print('reviewed')\n", "reviewed on main")

    def aa(self, rel: str) -> bool:
        return paths.agent_authored(os.path.join(self.repo.path, rel))

    def test_tracked_and_unchanged_on_main_is_reviewed(self):
        self.assertFalse(self.aa("scripts/reviewed.py"))

    def test_untracked_and_modified_are_agent_authored(self):
        self.repo.write("scripts/new.py", "x = 1\n")
        self.assertTrue(self.aa("scripts/new.py"))
        self.repo.write("scripts/reviewed.py", "print('changed')\n")
        self.assertTrue(self.aa("scripts/reviewed.py"))

    def test_a_script_committed_on_a_feature_branch_is_agent_authored(self):
        self.repo.git("checkout", "-q", "-b", "feat/x")
        self.repo.commit_file("scripts/engineer.py", "print('written by an engineer, committed by the conductor')\n", "conductor commit")
        self.assertTrue(self.aa("scripts/engineer.py"), "tracked and clean, but changed since the merge-base with main")
        self.assertFalse(self.aa("scripts/reviewed.py"), "unchanged from the base")

    def test_a_reviewed_script_edited_and_committed_on_the_branch_is_agent_authored(self):
        self.repo.git("checkout", "-q", "-b", "feat/x")
        self.repo.commit_file("scripts/reviewed.py", "print('edited')\n", "edit")
        self.assertTrue(self.aa("scripts/reviewed.py"))

    def test_a_run_base_sha_adds_a_check_and_cannot_remove_one(self):
        base = self.repo.head()
        self.repo.git("checkout", "-q", "-b", "feat/x")
        self.repo.commit_file("scripts/mid.py", "print('mid')\n", "committed on the branch")
        rs = runstate.RunState(self.repo.common_dir()).ensure()
        # run.json is writable from the sandbox: a forged baseSha at HEAD must not launder the script (it is still changed since the merge-base)
        rs.write_run({"runId": "r1", "baseSha": self.repo.head()})
        self.assertTrue(self.aa("scripts/mid.py"))
        # a real base: a script committed during the run is agent-authored; one unchanged since the base is reviewed
        rs.write_run({"runId": "r1", "baseSha": base})
        self.assertTrue(self.aa("scripts/mid.py"))
        self.assertFalse(self.aa("scripts/reviewed.py"))
        # main moves on after the run began (a human merged a reviewed change): the run base still flags it
        self.repo.git("checkout", "-q", "main")
        self.repo.commit_file("scripts/later_on_main.py", "print('later')\n", "human merge on main")
        rs.write_run({"runId": "r1", "baseSha": base})
        self.assertTrue(self.aa("scripts/later_on_main.py"), "changed since the run's base")
        rs.write_run({"runId": "r1", "baseSha": self.repo.head()})
        self.assertFalse(self.aa("scripts/later_on_main.py"))

    def test_no_determinable_base_fails_closed(self):
        self.repo.git("branch", "-q", "-m", "main", "trunk")      # no main, master or origin/HEAD
        self.assertTrue(self.aa("scripts/reviewed.py"))

    def test_a_bad_base_sha_in_run_json_fails_closed(self):
        rs = runstate.RunState(self.repo.common_dir()).ensure()
        rs.write_run({"runId": "r1", "baseSha": "deadbeefdeadbeef"})
        self.assertTrue(self.aa("scripts/reviewed.py"))

    def test_outside_a_repository(self):
        self.assertTrue(paths.agent_authored("/nonexistent/dir/x.py"))


if __name__ == "__main__":
    unittest.main()
