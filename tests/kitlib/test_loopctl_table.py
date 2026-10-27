"""loopctl_table: the shared read/write/human classification of loopctl commands (review fixes D1b, D3, D11)."""
from __future__ import annotations

import shlex
import unittest

from kitlib import loopctl_table as T


def cls(cmd: str) -> str:
    return T.classify(shlex.split(cmd))[0]


class Classify(unittest.TestCase):
    def check(self, want: str, cmds) -> None:
        for c in cmds:
            with self.subTest(c):
                self.assertEqual(cls(c), want)

    def test_reads(self):
        self.check("read", ["", "--help", "-h", "--version", "status", "status --json", "validate", "merge-class F1", "lock status",
                            "remote-lock status", "claim list", "claim show F1", "manifest verify", "unattended check", "parallel status",
                            "parallel leases", "parallel cleanup --dry-run", "calibration scan", "reconcile --dry-run",
                            "reconcile --dry-run --prs prs.json", "migrate-plan --check", "cost --projection", "evidence --check",
                            "run --dry-run", "abort --dry-run"])

    def test_writes(self):
        self.check("write", ["begin F1", "advance F1 merged", "reconcile --apply", "reconcile --apply --by conductor", "migrate-plan --write",
                             "migrate-plan --check --write", "migrate-plan --check --wri", "lock steal", "parallel run",
                             "approve checkpoint F1 --by pat --note ok", "approve checkpoint F1 --by pat --choice reject",
                             "approve item F1 --by pat --choice reject", "approve item F1 --by pat --choice=stop",
                             "approve unblock F1 --by pat --choice keep", "approve unblock F1 --by pat --choice replan",
                             "--repo . status", "status --apply", "status --a"])

    def test_option_in_action_position_is_not_the_action(self):
        # the review's six commands: an option before the action word must not turn a write into a read
        self.check("write", ["lock --session status steal", "claim --by list take X", "manifest --by show write",
                             "unattended --by check intake", "parallel --x status run"])
        self.assertEqual(cls("remote-lock --owner status steal"), "write")

    def test_human_only(self):
        self.check("human", [
            "remote-lock acquire", "remote-lock renew", "remote-lock release", "remote-lock steal --host-dead", "claim take F1",
            "claim take F1 --override", "claim release F1", "resume", "parallel cleanup --apply", "parallel cascade A --apply --by me",
            "parallel check --apply", "parallel cleanup --app", "migrate-plan --write --force", "migrate-plan --write --for",
            "reconcile --live --apply", "reconcile --live --dry-run", "reconcile --apply --prs prs.json", "reconcile --apply --rule-suites s.json",
            "reconcile --apply --pr x.json", "reconcile --apply=1 --prs=p.json",
            "approve plan --by me", "approve plan --by me --seed-v1 build-plan.json", "approve item F1 --by me",
            "approve item F1 --by me --choice approve", "approve item F1 --by me --choice=reset", "approve item F1 --by me --choice keep",
            "approve unblock F1 --by me --choice reset", "approve unblock F1 --by me", "approve env --by me", "approve breaker --by me",
            "approve cost --by me", "approve demotion --by me", "approve checkpoint F1 --by me --cho stop",
            "approve checkpoint F1 --by me --choice stop --choice approve", "approve --by me item F1", "approve item F1 --by me --choice",
            "approve item F1 --by me --choice --note x", "approve", "approve nonsense F1 --by me", "Approve item F1 --by me"])

    def test_returns_sub_and_action(self):
        self.assertEqual(T.classify(["remote-lock", "steal", "--host-dead"]), ("human", "remote-lock", "steal"))
        self.assertEqual(T.classify(["lock", "--session", "status"]), ("write", "lock", None))


if __name__ == "__main__":
    unittest.main()
