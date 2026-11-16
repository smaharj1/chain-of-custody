"""Every human-run apply path refuses inside a Claude Code session (review fixes GD-8; the Phase 9 audit that 11a deferred).

For each script: the first thing it does is the `humanonly` check, and presence of EITHER marker counts, even empty (`CLAUDECODE=`). Five environments:
CLAUDECODE=1, CLAUDECODE= (empty), only CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_ENTRYPOINT= (empty), and both. The scripts are run for real with a PATH that has no `gh`,
so a refusal that failed to fire would have nothing to talk to and would fail differently; none of these runs touches the network.

This is a tripwire: `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT script` passes it. What stops an agent from doing that is the guard (corpus rows in
tests/guards/corpus/adversarial6.json) and the absence of credentials.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.helpers import clean_env

GH = os.path.join(SCRIPTS_DIR, "github")
ENVS = ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}, {"CLAUDE_CODE_ENTRYPOINT": ""}, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})


def _base_env(tmp: str) -> dict:
    env = clean_env(home=tmp)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["PATH"] = "/usr/bin:/bin"
    return env


class HumanOnlyApplyPaths(unittest.TestCase):
    def scripts(self, tmp: str):
        sys.path.insert(0, GH)
        try:
            import repo_script
        finally:
            sys.path.remove(GH)
        setup = os.path.join(tmp, "setup.sh")
        with open(setup, "w") as fh:
            fh.write(repo_script.generate(repo_script.Params(repo="acme/widgets", create_repo=True, boundary=True, labels_=True,
                                                             config_plan=".claude/bootstrap/p.json")))
        plan = os.path.join(tmp, "plan.json")
        with open(plan, "w") as fh:
            fh.write("{}")
        py = sys.executable
        return [
            ("apply-boundary.sh", ["sh", os.path.join(GH, "apply-boundary.sh"), "--repo", "acme/widgets", "--variant", "solo", "--apply"]),
            ("apply-boundary.sh (dry run)", ["sh", os.path.join(GH, "apply-boundary.sh"), "--repo", "acme/widgets", "--variant", "solo"]),
            ("verify-boundary.sh", ["sh", os.path.join(GH, "verify-boundary.sh"), "--repo", "acme/widgets", "--run"]),
            ("apply-labels.sh", ["sh", os.path.join(GH, "apply-labels.sh"), "--repo", "acme/widgets", "--apply"]),
            ("boundary.py apply", [py, os.path.join(GH, "boundary.py"), "apply", "--repo", "acme/widgets", "--variant", "solo", "--apply"]),
            ("boundary.py verify", [py, os.path.join(GH, "boundary.py"), "verify", "--repo", "acme/widgets", "--run"]),
            ("labels.py apply", [py, os.path.join(GH, "labels.py"), "apply", "--repo", "acme/widgets", "--apply"]),
            ("bootstrap_config.py apply", [py, os.path.join(SCRIPTS_DIR, "bootstrap_config.py"), "apply", plan, "--write"]),
            ("setup.sh local --apply", ["sh", setup, "local", "--apply"]),
            ("setup.sh remote --apply", ["sh", setup, "remote", "--apply"]),
            ("migrate.py --apply", [py, os.path.join(SCRIPTS_DIR, "migrate.py"), "--apply"]),
            ("init_project.py --apply", [py, os.path.join(SCRIPTS_DIR, "init_project.py"), "--apply"]),
        ]

    def test_each_apply_path_refuses_on_either_marker_even_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, argv in self.scripts(tmp):
                for extra in ENVS:
                    with self.subTest(script=name, env=sorted(extra.items())):
                        env = _base_env(tmp)
                        env.update(extra)
                        p = subprocess.run(argv, cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                        err = p.stderr.decode("utf-8", "replace")
                        self.assertNotEqual(p.returncode, 0, (name, err))
                        self.assertIn("refusing", err, (name, err))
                        self.assertEqual(p.stdout.decode("utf-8", "replace").strip(), "", "nothing is printed or planned before the refusal")

    def test_without_a_marker_the_check_does_not_fire(self):
        """The refusal is the markers' doing, not a script that always fails."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _base_env(tmp)
            p = subprocess.run(["sh", os.path.join(GH, "apply-boundary.sh"), "--repo", "acme/widgets", "--variant", "solo"], cwd=tmp, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertNotIn("refusing", p.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
