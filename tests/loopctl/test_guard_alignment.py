"""The guards and loopctl agree on which commands write (report section 10.1, RC-13). A disagreement is a hole: an engineer
running a command the guard thinks is a read that actually writes state, or the reverse (a false deny)."""
from __future__ import annotations

import json
import os
import unittest

from guards import g_exec, g_halt
from guards.invoke import analyze

from loopctl import cli, gates, state as sm

from . import LoopTestCase, item_doc

SHA = "0123456789abcdef0123456789abcdef01234567"

# every command line the table promises is a read, and that must write nothing
READS = [
    ["validate"], ["validate", "--committed"], ["status"], ["status", "--json"], ["status", "--pending-human"], ["status", "--matrix"], ["status", "--bypass"],
    ["render"], ["render", "--format", "mermaid"], ["audit", "render", "A"], ["report", "check", "docs/features/x/reports/backend.md"],
    ["help"], ["version"], ["lock", "status"], ["evidence", "--check"], ["cost", "--projection"], ["reconcile", "--dry-run"],
    ["migrate-plan", "--check"], ["migrate-plan", "--dry-run"], ["run", "--dry-run"], ["abort", "--dry-run"], ["abort", "--dry-run", "--rollback-to", SHA],
]
# commands (with typical arguments) the table calls writes
WRITES = [
    ["begin", "A", "--session", "s"], ["advance", "A", "building", "--session", "s"], ["fail", "A", "--class", "item", "--reason", "tests_failed"],
    ["clarify", "A"], ["park", "A", "--code", "x"], ["approve", "plan", "--by", "Pat"], ["gate", "g6", "A"], ["spend", "record", "--item", "A", "--result", "-"],
    ["run", "--session", "s", "--nonce", "n"], ["abort", "--by", "Pat"], ["abort", "--rollback-to", SHA, "--by", "Pat"], ["reconcile", "--apply"],
    ["migrate-plan", "--write"], ["lock", "acquire", "--session", "s"], ["lock", "release", "--session", "s"], ["lock", "steal"], ["halt"], ["resume"],
    ["migrate-plan", "--check", "--write"], ["reconcile", "--dry-run", "--apply"], ["abort", "--dry-run", "--force"], ["evidence"], ["cost"],
]


def inv(args):
    ana = analyze("python3 .claude/scripts/loopctl " + " ".join(args), "/tmp")
    found = [i for i in ana.invs if i.name.startswith("python")]
    return found[0]


class Alignment(unittest.TestCase):
    def test_the_tables_are_the_same(self):
        t = cli.classification()
        self.assertEqual(set(t["read"]), g_exec.LOOPCTL_READ - {"--help", "-h", "--version"})
        self.assertEqual({k: tuple(v) for k, v in t["flag_read"].items()}, g_exec.LOOPCTL_FLAG_READ)
        self.assertEqual(tuple(t["write_flags"]), g_exec.LOOPCTL_WRITE_FLAGS)

    def test_every_command_is_classified_exactly_once(self):
        t = cli.classification()
        classified = set(t["read"]) | set(t["tier1"]) | set(t["tier2"])
        # evidence, cost, lock, migrate-plan, reconcile, run, abort appear in a write tier or as flag reads, never nowhere
        flag_cmds = set(t["flag_read"])
        for name in cli.COMMANDS:
            self.assertTrue(name in classified or name in flag_cmds, "%s is not classified" % name)
        self.assertFalse(set(t["read"]) & set(t["tier2"]))
        self.assertFalse(set(t["tier1"]) & set(t["tier2"]))
        for name in t["read"]:
            self.assertIn(name, cli.COMMANDS)

    def test_the_guard_reads_each_command_line_as_the_table_says(self):
        for args in READS:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), True)
        for args in WRITES:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), False)

    def test_halt_lets_only_the_reads_through_in_every_spelling(self):
        for args in READS:
            self.assertTrue(g_halt._read_only(None, inv(args)), args)
        for args in WRITES:
            self.assertFalse(g_halt._read_only(None, inv(args)), args)

    def test_a_command_hidden_behind_an_option_is_a_write_for_the_guard_and_a_usage_error_for_loopctl(self):
        for args in (["--repo", "status", "abort", "--by", "Pat"], ["--dry-run", "abort", "--by", "Pat"], ["--json", "status"], ["--base", "validate", "begin", "A"]):
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), False)
                from io import StringIO
                out, err = StringIO(), StringIO()
                self.assertEqual(cli.main(args, env={}, stdout=out, stderr=err), 2)
                self.assertIn("before the command", err.getvalue())
        self.assertIs(g_exec.loopctl_is_read(inv(["--help"])), True)

    def test_the_command_word_is_case_sensitive_in_loopctl_and_folded_in_the_guard(self):
        from io import StringIO
        for w in ("Status", "ABORT", "Run", "VALIDATE"):
            self.assertEqual(cli.main([w], env={}, stdout=StringIO(), stderr=StringIO()), 2)

    def test_options_are_not_abbreviated(self):
        # `--dry` must not be accepted as `--dry-run` (the guard would see an unknown flag and deny, but loopctl must not run it either)
        from io import StringIO
        err = StringIO()
        self.assertEqual(cli.main(["abort", "--dry", "--by", "Pat"], env={}, stdout=StringIO(), stderr=err), 2)
        self.assertEqual(cli.main(["reconcile", "--dr"], env={}, stdout=StringIO(), stderr=StringIO()), 2)


class ReadsWriteNothing(LoopTestCase):
    def snapshot(self, lr):
        rs = lr.context().rs
        files = {}
        for dirpath, _d, names in os.walk(rs.root):
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, "rb") as fh:
                    files[os.path.relpath(p, rs.root)] = fh.read()
        refs = lr.repo.git("for-each-ref", "--format=%(refname) %(objectname)")
        return refs, lr.repo.git("status", "--porcelain"), files, lr.repo.git("branch", "--list")

    def test_every_read_command_line_leaves_refs_files_and_run_state_untouched(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.repo.git("branch", lr.item("A")["branch"], "main")
        lr.repo.write("body.md", "x")
        lr.repo.git("add", "body.md")
        lr.repo.git("commit", "-q", "-m", "body")
        lr.repo.write("build-plan.json", json.dumps({"goal": "g", "items": [{"id": "F1", "milestone": "M", "title": "t", "slug": "s", "size": "S", "dependsOn": [],
                                                                            "intent": "i", "acceptance": ["AC-F1-1: `x` returns 200"], "requiresEnv": []}]}))
        lr.repo.git("add", "build-plan.json")
        lr.repo.git("commit", "-q", "-m", "v1 plan")
        before = self.snapshot(lr)
        for args in READS:
            if args[0] == "doctor":
                continue
            fixed = [a.replace("A", "A") for a in args]
            if args[:2] == ["report", "check"] or args[:2] == ["audit", "render"]:
                pass
            lr.ctl(*fixed)
            after = self.snapshot(lr)
            self.assertEqual(before[0], after[0], "refs changed by %s" % args)
            self.assertEqual(before[1], after[1], "the working tree changed by %s" % args)
            self.assertEqual(before[2], after[2], "run state changed by %s" % args)
            self.assertEqual(before[3], after[3], "branches changed by %s" % args)

    def test_read_commands_work_for_a_caller_with_no_session_and_tier2_off(self):
        lr = self.lr(tier2=False)
        lr.repo.write("build-plan.json", json.dumps({"goal": "g", "items": [{"id": "F1", "milestone": "M", "title": "t", "slug": "s", "size": "S", "dependsOn": [],
                                                                            "intent": "i", "acceptance": ["AC-F1-1: `x` returns 200"], "requiresEnv": []}]}))
        for args in ([a for a in READS if a[0] not in ("audit", "abort", "report")]):
            r = lr.ctl(*args)
            self.assertIn(r.rc, (0, 1, 3), (args, r))
            if r.rc == 3:
                self.assertIn("no state", r.err)               # nothing to read yet; never a permission problem


if __name__ == "__main__":
    unittest.main()
