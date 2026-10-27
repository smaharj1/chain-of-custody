"""The guards and loopctl agree on which commands write (report section 10.1, RC-13). A disagreement is a hole: an engineer
running a command the guard thinks is a read that actually writes state, or the reverse (a false deny)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from guards import g_exec, g_halt
from guards.invoke import analyze

from kitlib import loopctl_table

from loopctl import cli, common, gates, state as sm

from . import LoopTestCase, item_doc

SHA = "0123456789abcdef0123456789abcdef01234567"

# every command line the table promises is a read, and that must write nothing
READS = [
    ["validate"], ["validate", "--committed"], ["status"], ["status", "--json"], ["status", "--pending-human"], ["status", "--matrix"], ["status", "--bypass"],
    ["render"], ["render", "--format", "mermaid"], ["audit", "render", "A"], ["report", "check", "docs/features/x/reports/backend.md"],
    ["help"], ["version"], ["lock", "status"], ["evidence", "--check"], ["cost", "--projection"], ["reconcile", "--dry-run"],
    ["migrate-plan", "--check"], ["migrate-plan", "--dry-run"], ["run", "--dry-run"], ["abort", "--dry-run"], ["abort", "--dry-run", "--rollback-to", SHA],
    # Phase 10: the learning loop's reads
    ["retro"], ["retro", "--json"], ["retro", "--milestone", "M1"], ["harness"], ["harness", "--json"], ["gaps"], ["gaps", "--json", "--min", "1"],
    ["calibration", "report"], ["calibration", "scan"], ["calibration", "report", "--json"], ["report", "scan"], ["report", "scan", "--json"],
    # Phase 11b: the extras' reads
    ["merge-class", "A"], ["merge-class", "A", "--paths", "docs/a.md", "--json"], ["remote-lock", "status"], ["remote-lock", "status", "--json"],
    ["claim", "list"], ["claim", "show", "A"], ["manifest", "show"], ["manifest", "verify", "latest", "--expect-hash", "abc"],
]
# commands (with typical arguments) the table calls writes
WRITES = [
    ["begin", "A", "--session", "s"], ["advance", "A", "building", "--session", "s"], ["fail", "A", "--class", "item", "--reason", "tests_failed"],
    ["clarify", "A"], ["park", "A", "--code", "x"], ["approve", "plan", "--by", "Pat"], ["gate", "g6", "A"], ["spend", "record", "--item", "A", "--result", "-"],
    ["run", "--session", "s", "--nonce", "n"], ["abort", "--by", "Pat"], ["abort", "--rollback-to", SHA, "--by", "Pat"], ["reconcile", "--apply"],
    ["migrate-plan", "--write"], ["lock", "acquire", "--session", "s"], ["lock", "release", "--session", "s"], ["lock", "steal"], ["halt"], ["resume"],
    ["migrate-plan", "--check", "--write"], ["reconcile", "--dry-run", "--apply"], ["abort", "--dry-run", "--force"], ["evidence"], ["cost"],
    ["gaps", "--write"], ["gaps", "--min", "1", "--write"], ["calibration", "scan", "--write"], ["calibration", "report", "--write"],
    ["calibration", "record", "--blocker", "bk-000000000000", "--outcome", "fixed", "--by", "Pat"], ["calibration"], ["calibration", "frobnicate"],
    ["retro", "--write"], ["harness", "--apply"], ["gaps", "--force"],
    # Phase 11b: the extras' writes
    ["remote-lock", "acquire"], ["remote-lock", "renew"], ["remote-lock", "release"], ["remote-lock", "steal", "--host-dead"], ["remote-lock", "status", "--force"],
    ["claim", "take", "F1", "--by", "Pat"], ["claim", "release", "F1", "--by", "Pat"], ["claim", "show", "F1", "--force"], ["claim"], ["remote-lock"], ["manifest"],
    ["manifest", "write", "F1"], ["manifest", "verify", "latest", "--write"], ["merge-class", "F1", "--apply"],
]


def _subparsers():
    """name -> the argparse parser each loopctl subcommand builds (captured by running `<cmd> --help` with `_parser` wrapped)."""
    made = {}
    real = cli._parser

    def spy(prog, desc=""):
        ap = real(prog, desc)
        made[prog] = ap
        return ap
    cli._parser = spy
    try:
        for name, fn in cli.COMMANDS.items():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    fn(["--help"], {}, cli.Out(io.StringIO(), io.StringIO()))
            except (cli._Exit, common.LoopctlError, SystemExit):
                pass
    finally:
        cli._parser = real
    return made


def inv(args):
    ana = analyze("python3 .claude/scripts/loopctl " + " ".join(args), "/tmp")
    found = [i for i in ana.invs if i.name.startswith("python")]
    return found[0]


class Alignment(unittest.TestCase):
    def test_cli_classification_is_the_shared_table(self):
        """`loopctl/cli.py` carries no table of its own: it is a view of kitlib/loopctl_table.py, which the guards import too."""
        t = cli.classification()
        self.assertEqual(set(t["read"]), loopctl_table.READ - {"--help", "-h", "--version"})
        self.assertEqual({k: tuple(v) for k, v in t["flag_read"].items()}, loopctl_table.FLAG_READ)
        self.assertEqual(tuple(t["write_flags"]), loopctl_table.WRITE_FLAGS)
        self.assertEqual({k: set(v) for k, v in t["action_read"].items()}, loopctl_table.ACTION_READ)
        self.assertEqual({k: set(v) for k, v in t["human_only"].items()}, loopctl_table.HUMAN_ONLY)
        self.assertIs(cli.READ_COMMANDS.__class__, tuple)

    def test_the_table_agrees_with_every_subparser(self):
        """Every `action` choice of a loopctl subcommand is classified on purpose: a new action without a table entry fails here."""
        parsers = _subparsers()
        for name, ap in parsers.items():
            for act in ap._actions:
                if act.dest != "action" or not act.choices:
                    continue
                for choice in act.choices:
                    cls = loopctl_table.classify([name, choice])[0]
                    with self.subTest("%s %s" % (name, choice)):
                        if name in loopctl_table.HUMAN_ONLY and choice in loopctl_table.HUMAN_ONLY[name]:
                            self.assertEqual(cls, "human")
                        elif choice in loopctl_table.ACTION_READ.get(name, ()):
                            self.assertEqual(cls, "read")
                        elif name in loopctl_table.READ:
                            self.assertEqual(cls, "read")
                        else:
                            self.assertEqual(cls, "write")
        # the commands whose reads are action-based are exactly the ones whose parser has an action
        for name in loopctl_table.ACTION_READ:
            self.assertIn(name, parsers)
            self.assertTrue(any(a.dest == "action" for a in parsers[name]._actions), name)
        # every write flag in the table is a flag some parser knows (no stale entries)
        known = {o for ap in parsers.values() for a in ap._actions for o in a.option_strings}
        for f in loopctl_table.WRITE_FLAGS + tuple(x for fl in loopctl_table.HUMAN_FLAG.values() for x in fl) + loopctl_table.RECONCILE_FACT_FLAGS:
            self.assertIn(f, known)

    def test_every_approve_scope_and_choice(self):
        scope_choices = [a for a in cli._approve_parser()._actions if a.dest == "scope"][0].choices
        self.assertEqual(set(scope_choices), {"plan", "item", "checkpoint", "unblock", "env", "breaker", "cost", "demotion"})
        choices = ("approve", "reject", "stop", "reset", "keep", "replan")
        relay = {"checkpoint": set(choices), "item": {"reject", "stop"}, "unblock": {"keep", "replan"}}
        for scope in scope_choices:
            for ch in choices:
                with self.subTest("%s %s" % (scope, ch)):
                    want = "write" if ch in relay.get(scope, ()) else "human"
                    self.assertEqual(loopctl_table.classify(["approve", scope, "F1", "--by", "me", "--choice", ch])[0], want)
                    self.assertEqual(loopctl_table.classify(["approve", scope, "F1", "--by", "me", "--choice=" + ch])[0], want)
            # no --choice means approve
            self.assertEqual(loopctl_table.classify(["approve", scope, "F1", "--by", "me"])[0], "write" if scope == "checkpoint" else "human")

    def test_odd_approve_forms_are_human(self):
        for args in (["--cho", "stop"], ["--choice=stop", "--choice", "approve"], ["--choice", "stop", "--choice", "stop"], ["--choice"], ["--choice", "--by"],
                     ["--chocie", "stop"], ["--c=stop"]):
            with self.subTest(args):
                self.assertEqual(loopctl_table.classify(["approve", "checkpoint", "F1", "--by", "me"] + args)[0], "human")
        self.assertEqual(loopctl_table.classify(["approve", "--by", "me", "item", "F1"])[0], "human")
        self.assertEqual(loopctl_table.classify(["approve"])[0], "human")
        self.assertEqual(loopctl_table.classify(["approve", "bogus", "F1"])[0], "human")

    def test_approve_parity_with_the_cli_parser(self):
        """No parser differential: what the table reads as the scope and the choice is what `cmd_approve`'s own parser parses."""
        samples = [["checkpoint", "F1", "--by", "pat", "--note", "ok"], ["item", "F1", "--by", "pat", "--choice", "reject"],
                   ["item", "F1", "--by", "pat", "--choice=stop"], ["unblock", "F1", "--by", "pat", "--choice", "keep", "--note", "--choice reset"],
                   ["item", "F1", "--by", "pat", "--note", "--choice stop"], ["plan", "--by", "pat"], ["unblock", "F1", "--by", "pat", "--choice", "reset"]]
        for args in samples:
            with self.subTest(args):
                a = cli._approve_parser().parse_args(args)
                cls = loopctl_table.classify(["approve"] + args)[0]
                relay = {"checkpoint": None, "item": {"reject", "stop"}, "unblock": {"keep", "replan"}}
                expect = "write" if a.scope in relay and (relay[a.scope] is None or a.choice in relay[a.scope]) else "human"
                # a `--choice ...` inside a --note value is the note's text: the table may be stricter (human), never looser
                if cls == "write":
                    self.assertEqual(expect, "write")
                else:
                    self.assertIn(cls, ("human",))

    def test_human_only_commands(self):
        for args in (["resume"], ["parallel", "cleanup", "--apply"], ["parallel", "cleanup", "--appl"], ["parallel", "cascade", "A", "--apply=1"], ["migrate-plan", "--write", "--force"],
                     ["migrate-plan", "--write", "--for"], ["reconcile", "--live", "--apply"], ["reconcile", "--live", "--dry-run"], ["reconcile", "--apply", "--prs", "x.json"],
                     ["reconcile", "--apply", "--rule-suites", "s.json"], ["reconcile", "--apply", "--pr", "x.json"],
                     ["remote-lock", "acquire"], ["remote-lock", "renew"], ["remote-lock", "release"], ["remote-lock", "steal", "--host-dead"],
                     ["claim", "take", "F1"], ["claim", "release", "F1"], ["claim", "take", "F1", "--override"]):
            with self.subTest(args):
                self.assertEqual(loopctl_table.classify(args)[0], "human")
        for args in (["reconcile", "--apply"], ["reconcile", "--dry-run"], ["reconcile", "--dry-run", "--prs", "p.json"], ["migrate-plan", "--write"],
                     ["remote-lock", "status"], ["claim", "list"], ["claim", "show", "F1"]):
            with self.subTest(args):
                self.assertNotEqual(loopctl_table.classify(args)[0], "human")

    def test_the_actions_extras_refuses_under_a_session_are_the_table_human_only_set(self):
        """`loopctl_table.HUMAN_ONLY` is what the guard denies; the actions `extras` refuses with `CLAUDECODE` set are the same set."""
        from loopctl import extras
        seen = []
        real = cli._ctx

        def spy(*a, **k):
            seen.append(1)
            raise common.Refused("ctx reached")
        cli._ctx = spy
        try:
            for cmd, fn, actions in (("remote-lock", extras.cmd_remote_lock, ("status", "acquire", "renew", "release", "steal")),
                                     ("claim", extras.cmd_claim, ("list", "show", "take", "release"))):
                for action in actions:
                    for env in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
                        del seen[:]
                        refused = False
                        try:
                            fn([action] + (["F1"] if cmd == "claim" and action != "list" else []) + ["--repo", "/nonexistent"], env, cli.Out(io.StringIO(), io.StringIO()))      # the positional item first: Python 3.9's argparse reads one given after an option as unrecognized (R-A-5)
                        except common.Refused as exc:
                            refused = "refusing to run inside a Claude Code session" in str(exc)
                        with self.subTest("%s %s %s" % (cmd, action, sorted(env))):
                            human = action in loopctl_table.HUMAN_ONLY[cmd]
                            self.assertEqual(refused, human)
                            self.assertEqual(bool(seen), not human, "a human-only action must stop before any config, git or network access")
                    del seen[:]       # without a marker the action goes on to read the config
                    try:
                        fn([action] + (["F1"] if cmd == "claim" and action != "list" else []) + ["--repo", "/nonexistent"], {}, cli.Out(io.StringIO(), io.StringIO()))
                    except common.Refused:
                        pass
                    self.assertTrue(seen, "%s %s without a session marker must run" % (cmd, action))
        finally:
            cli._ctx = real

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
        for args in ([a for a in READS if a[0] not in ("audit", "abort", "report", "remote-lock", "claim", "manifest", "merge-class")]):       # the extras' reads have their own tests (they refuse, rc 3, without the opt-in)
            r = lr.ctl(*args)
            self.assertIn(r.rc, (0, 1, 3), (args, r))
            if r.rc == 3:
                self.assertIn("no state", r.err)               # nothing to read yet; never a permission problem


if __name__ == "__main__":
    unittest.main()
