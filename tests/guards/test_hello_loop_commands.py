"""Zero guard false positives on a hello-loop re-run (report §16.1 Phase 2 exit criterion).

The live re-run needs a real `claude` and is pending-human. What can be proved offline is that every command the fixture's
own documents tell an agent to run, and every git step the conductor takes around an item, is allowed for the caller that runs
it. The fixture's commands are read from `examples/hello-loop/` itself, so a change to the fixture that a guard would refuse
fails here.
"""
from __future__ import annotations

import json
import os
import re
import unittest

from tests import REPO_ROOT
from tests.guards.harness import Fixtures, evaluate_row

HL = os.path.join(REPO_ROOT, "examples", "hello-loop")


def fenced_commands(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    out = []
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S):
        for line in block.split("\n"):
            line = line.split("   #")[0].strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def config_commands(path):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    for line in lines:
        m = re.match(r"^(verify_tests|verify_run|db_ephemeral):\s*(.*?)\s*(#.*)?$", line)
        if m and not m.group(2).startswith("<"):
            out.append(m.group(2).replace("$ITEM_ID", "F1"))
    return out


class HelloLoopCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = Fixtures()
        cmds = []
        cmds += fenced_commands(os.path.join(HL, "kit-overlay", ".claude", "context", "local-dev.md"))
        cmds += config_commands(os.path.join(HL, "kit-overlay", ".claude", "loop.config.md"))
        with open(os.path.join(HL, "verify.json")) as fh:
            v = json.load(fh)
        cmds += [v["verify_tests"], v["verify_run"]]
        for item in v["items"].values():
            cmds += [item["tests"], item["acceptance"]]
        cls.commands = sorted(set(cmds))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def test_the_fixture_documents_some_commands(self) -> None:
        self.assertGreaterEqual(len(self.commands), 10)

    def test_every_documented_command_is_allowed_for_engineers_and_the_conductor(self) -> None:
        for cmd in self.commands:
            for caller in ("engineer", "conductor", "critic"):
                if caller == "critic" and re.search(r"(rm |migrate|seed|ephemeral_db|server)", cmd):
                    continue        # critics run verify commands, not the local-dev reset
                with self.subTest(cmd=cmd, caller=caller):
                    d = evaluate_row(self.fx, {"cmd": cmd, "caller": caller, "repo": "feat"})
                    self.assertIsNone(d, "%s: %s" % (caller, d))

    def test_the_conductors_git_steps_around_an_item(self) -> None:
        steps = [
            "git status --short", "git checkout -b feat/f1-schema-a1", "git add -N .", "git add -A && git commit -m 'feat(F1): schema' -m 'Loop-Item: F1' -m 'Loop-Run: r1' -m 'Loop-Attempt: 1'",
            "git diff --stat main...feat/f1-schema-a1", "git log --oneline -5", "git checkout main", "git merge --no-ff feat/f1-schema-a1 -m 'Merge F1'",
            "git branch -d feat/f1-schema-a1", "git branch -D dead/f2-bookmarks-a1", "git branch -m feat/f2-a1 blocked/f2-a1",
        ]
        for cmd in steps:
            with self.subTest(cmd=cmd):
                repo = "main" if cmd.startswith(("git merge", "git branch -d", "git checkout main")) else "feat"
                d = evaluate_row(self.fx, {"cmd": cmd, "caller": "conductor", "repo": repo})
                self.assertIsNone(d, str(d))

    def test_the_discard_path(self) -> None:
        for cmd in ("git reset --hard && git clean -fd", "git branch -m feat/f2-a1 dead/f2-a1"):
            with self.subTest(cmd=cmd):
                d = evaluate_row(self.fx, {"cmd": cmd, "caller": "conductor", "repo": "wt"})
                self.assertIsNone(d, str(d))

    def test_engineer_reads_and_edits(self) -> None:
        for cmd in ("git diff", "git status", "git log --oneline -3", "ls hello_loop", "cat hello_loop/server.py", "python3 -m hello_loop.migrate"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate_row(self.fx, {"cmd": cmd, "caller": "engineer", "repo": "feat"}))
        for path in ("{FEAT}/hello_loop/handlers/bookmarks.py", "{FEAT}/tests/test_bookmarks_api.py", "{FEAT}/hello_loop/migrations/0001_create_bookmarks.up.sql",
                     "{FEAT}/docs/features/bookmarks/reports/backend.md"):
            with self.subTest(path=path):
                self.assertIsNone(evaluate_row(self.fx, {"path": path, "caller": "engineer", "repo": "feat"}))


if __name__ == "__main__":
    unittest.main()
