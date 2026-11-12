"""The push tier's lanes and environment (review-push F2, F5, F7): the lanes the guard pushes equal the ones pr_prepare names,
GH_REPO in the guard's own environment is refused for gh, and the guard reads git config with its own HOME."""
from __future__ import annotations

import ast
import os
import unittest
from unittest import mock

from guards import engine, g_git
from guards.lib import Ctx
from tests import REPO_ROOT
from tests.guards.harness import Fixtures, build_hook, config_for

PUSH = {"git_autonomy": "branch+commit+push", "delivery": "draft-pr"}


def _pr_prepare_prefixes():
    """pr_prepare._BRANCH_PREFIXES read from the source (pr_prepare is not imported: it is a script with its own sys.path set-up)."""
    with open(os.path.join(REPO_ROOT, "scripts", "pr_prepare.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "_BRANCH_PREFIXES" for t in targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("pr_prepare.py has no _BRANCH_PREFIXES")


class LaneTests(unittest.TestCase):
    def test_lanes_match_pr_prepare(self) -> None:
        self.assertEqual(set(g_git.LANE_PREFIXES), set(_pr_prepare_prefixes()))

    def test_every_lane_is_a_push_ref(self) -> None:
        for lane in g_git.LANE_PREFIXES:
            self.assertTrue(g_git.FEAT_RE.match(lane + "x"), lane)
        for bad in ("dead/x", "hotfix/x", "feat/", "feat/x.lock", "quick/a..b", "main"):
            self.assertFalse(g_git.FEAT_RE.match(bad), bad)


class EnvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = Fixtures()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def _eval(self, cmd: str, env: dict):
        row = {"caller": "conductor", "repo": "feat", "cmd": cmd, "config": PUSH}
        hook = build_hook(self.fx, row)
        ctx = Ctx(hook, env=env, state=self.fx.state_for("feat"), config=config_for(row))
        return engine.evaluate(hook, ctx=ctx)

    def test_gh_repo_in_the_guard_environment(self) -> None:
        with open(os.path.join(self.fx.feat.path, "pr-body.md"), "w") as fh:
            fh.write("body\n")
        cmd = "gh pr create --draft --base main --head feat/x --title t --body-file pr-body.md"
        base = {"HOME": self.fx.home, "PATH": "/usr/bin:/bin"}
        self.assertIsNone(self._eval(cmd, base))
        d = self._eval(cmd, dict(base, GH_REPO="other/repo"))
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, "guard-git:git-env")

    def test_config_is_read_with_the_guard_home(self) -> None:
        evil = os.path.join(self.fx.root, "evil-home")
        os.makedirs(evil, exist_ok=True)
        with open(os.path.join(evil, ".gitconfig"), "w") as fh:
            fh.write('[remote "origin"]\n\tmirror = true\n')
        cmd = "git push origin feat/x"
        good = {"HOME": self.fx.home, "PATH": "/usr/bin:/bin"}
        with mock.patch.dict(os.environ, {"HOME": evil, "XDG_CONFIG_HOME": evil}):
            self.assertIsNone(self._eval(cmd, good), "the guard read a global config from a HOME that is not its own")
        d = self._eval(cmd, dict(good, HOME=evil))
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, "guard-git:push-url")


if __name__ == "__main__":
    unittest.main()
