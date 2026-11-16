"""The pushing extras are human-only (review fixes D3, GD-8): under a Claude Code session marker they refuse before any git or config access."""
from __future__ import annotations

import json
import os
import unittest

from loopctl import cli, lgit

from .. import LoopTestCase, item_doc
from ...helpers import make_bare_remote
from . import EVIDENCE

MARKERS = ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}, {"CLAUDE_CODE_ENTRYPOINT": ""})
HUMAN = (("remote-lock", "acquire"), ("remote-lock", "renew"), ("remote-lock", "release"), ("remote-lock", "steal", "--host-dead"),
         ("claim", "take", "F1"), ("claim", "take", "F1", "--override"), ("claim", "release", "F1"))


class HumanOnly(LoopTestCase):
    def repo(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")], extra_cfg="remote_lock: origin\n")
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
        lr.commit_all("evidence")
        return lr, bare

    def test_each_pushing_action_refuses_under_a_session_marker_and_starts_no_git(self):
        lr, bare = self.repo()
        calls = []
        real = lgit.run_git
        lgit.run_git = lambda *a, **k: calls.append(a) or real(*a, **k)
        self.addCleanup(setattr, lgit, "run_git", real)
        for args in HUMAN:
            for marker in MARKERS:
                with self.subTest(args=args, marker=marker):
                    del calls[:]
                    r = lr.ctl(*args, env=marker)
                    self.assertEqual(r.rc, 3, r)
                    self.assertIn("refusing to run inside a Claude Code session", r.err)
                    self.assertEqual(calls, [], "no git process may start")
        self.assertIsNone(bare.ref("refs/loopctl/lock/demo"))
        self.assertEqual(bare.ref("refs/loopctl/claim/demo/A"), None)

    def test_the_reads_still_work_under_a_marker_and_the_pushes_work_without_one(self):
        lr, bare = self.repo()
        for marker in MARKERS:
            self.assertEqual(lr.ctl("remote-lock", "status", env=marker).rc, 0)
            self.assertEqual(lr.ctl("claim", "list", env=marker).rc, 0)
            self.assertEqual(lr.ctl("claim", "show", "A", env=marker).rc, 0)
        self.assertEqual(lr.ctl("remote-lock", "acquire", "--owner", "alice").rc, 0)
        self.assertEqual(lr.ctl("claim", "take", "A", "--by", "alice").rc, 0)
        self.assertEqual(lr.ctl("claim", "release", "A", "--by", "alice").rc, 0)
        self.assertEqual(lr.ctl("remote-lock", "release").rc, 0)


if __name__ == "__main__":
    unittest.main()
