"""The hook bypass corpus (report §7.3, §15 item 2; IMP-24 item 8): table-driven, deterministic, free.

Every row in tests/guards/corpus/*.json is `{caller, command or path, expected decision, rule id}`. Files:

    git.json        guard-git: v1 rules ported, §5.9 escalations, gh, refspecs, tiers by caller
    file.json       self-modification protection (H6), guard-main-edit v2, engineer-only paths, worktree cases (D-2)
    infra.json      guard-infra: the verb classifier, tunnels, publish, credentials, MCP
    exec.json       guard-exec: claude and SDK launches, interpreters, pipes to shells, loopctl writes (RC-13)
    critic.json     guard-critic-write, guard-halt, ConfigChange
    callers.json    caller classification: roster, plugin prefix, registered and unknown sessions, --agent sessions
    adversarial.json  bypass attempts that cross guards: wrappers, quoting, env tricks, paths, symlinks, case
    allow.json      ordinary development commands that must be allowed (the false-positive corpus)

A deny row names the rule id, so a guard that denies for the wrong reason fails the test too.
"""
from __future__ import annotations

import json
import os
import unittest

from tests.guards.harness import CORPUS_DIR, Fixtures, evaluate_row

FILES = ["git.json", "file.json", "infra.json", "exec.json", "critic.json", "callers.json", "adversarial.json", "adversarial2.json", "adversarial3.json",
         "adversarial4.json", "allow.json"]


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = Fixtures()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def run_file(self, name: str) -> None:
        path = os.path.join(CORPUS_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "duplicate row ids in %s" % name)
        for row in rows:
            with self.subTest(id=row["id"], cmd=row.get("cmd") or row.get("path")):
                d = evaluate_row(self.fx, row)
                expect = row["expect"]
                got = "allow" if d is None else d.kind
                msg = "%s %s (%s): expected %s%s, got %s" % (
                    row["id"], row.get("cmd") or row.get("path") or row.get("input"), row.get("caller", "unknown"), expect,
                    " " + row["rule"] if row.get("rule") else "", (d.rule + ": " + d.message[:100]) if d else "allow")
                self.assertEqual(got, expect, msg)
                if expect != "allow":
                    self.assertEqual(d.rule, row["rule"], msg)


def _make(name):
    def test(self):
        self.run_file(name)
    return test


for _f in FILES:
    if os.path.exists(os.path.join(CORPUS_DIR, _f)):
        setattr(CorpusTests, "test_" + _f.replace(".json", "").replace("-", "_"), _make(_f))


if __name__ == "__main__":
    unittest.main()
