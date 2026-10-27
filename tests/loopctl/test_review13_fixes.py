"""The second-review fixes to the loopctl core (LC-1 to LC-14 of the review of commit 45f2204): the relayable reject and stop never lift a stop or
leave a merge approval standing, a merge commit holds exactly the judged head, a Claude session's `run` pushes nothing, the printed next steps
work, the gate head is the checkout's own, an approval can assert the head, and crash recovery respects human gates.

These use the real `Loop`, the real CLI parser and a temp repository.
"""
from __future__ import annotations

import io
import os
import re
import shlex
import types
import unittest

from loopctl import cli, gates, lgit, launch, startrun, state as sm, unattended
from loopctl.common import Refused, UsageError

from . import LoopTestCase, item_doc


class Base(LoopTestCase):
    def setUp(self):
        for n in list(gates._registered):
            gates.unregister(n)
        self.addCleanup(lambda: [gates.unregister(n) for n in list(gates._registered)])

    def at_awaiting(self, **kw):
        lr = self.lr(items=[item_doc("A", **kw), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        return lr, loop

    def parked(self, code="security_blocked"):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.park("A", code, "sec", lr.session)
        return lr, loop


class RelayCannotLiftAStop(Base):
    """LC-1: `approve item --choice reject|stop` on a needs_human item must not un-park it (the un-park is human-only, D11)."""

    def test_reject_and_stop_on_a_parked_item_are_refused_and_change_nothing(self):
        for choice in ("reject", "stop"):
            with self.subTest(choice):
                lr, loop = self.parked()
                before = lr.item("A")
                r = lr.ctl("approve", "item", "A", "--by", "conductor", "--choice", choice, "--note", "x")
                self.assertEqual(r.rc, 3, r)
                self.assertIn("approve unblock A", r.err)
                after = lr.item("A")
                self.assertEqual((after["status"], after["blockReason"], after["counters"], after["branch"]),
                                 ("needs_human", "security_blocked", before["counters"], before["branch"]))
                self.assertEqual([a for a in lr.state().d["approvals"] if a["scope"] == "item"], [])

    def test_the_human_unblock_and_the_human_approve_still_lift_it(self):
        lr, loop = self.parked()
        loop.approve("unblock", "A", "Pat", "reset", "looked")
        self.assertEqual(lr.item("A")["status"], "pending")
        lr, loop = self.parked("ESCALATION_NEEDED")
        ap = loop.approve("item", "A", "Pat", "approve", "looked")
        self.assertEqual((ap["kind"], lr.item("A")["status"]), ("unpark", "pending"))

    def test_reject_from_an_approvable_state_still_works(self):
        lr, loop = self.at_awaiting()
        loop.approve("item", "A", "Pat", "reject", "not like this")
        self.assertEqual(lr.item("A")["status"], "pending")
        self.assertEqual(lr.item("A")["rework"][-1]["note"], "not like this")


class StopWithdrawsTheApproval(Base):
    """LC-2: `stop` is how the human says "do not merge"; an earlier merge approval must not survive it."""

    def test_stop_voids_a_standing_merge_approval(self):
        lr, loop = self.at_awaiting()
        loop.approve("item", "A", "Pat")
        st = lr.state()
        self.assertIsNotNone(sm._find_approval(st, "A", st.items["A"]["attempt"], st.items["A"]["headSha"]))
        loop.approve("item", "A", "Pat", "stop", "wait, do not merge")
        st = lr.state()
        self.assertIsNone(sm._find_approval(st, "A", st.items["A"]["attempt"], st.items["A"]["headSha"]))
        merge = [a for a in st.d["approvals"] if a.get("kind") == "merge"][0]
        self.assertEqual((merge["used"], merge["voided"]), (True, "stop"))
        mc = lr.do_merge("A")
        with self.assertRaises(Refused):
            loop.advance("A", "merged", lr.session, merge_commit=mc)


class ApproveAssertsTheHead(Base):
    """LC-9: `approve item --head` is refused when the branch moved since the line was printed."""

    def test_a_stale_head_is_refused_and_a_matching_prefix_is_accepted(self):
        lr, loop = self.at_awaiting()
        head = lr.item("A")["headSha"]
        r = lr.ctl("approve", "item", "A", "--by", "Pat", "--head", "0" * 12)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("branch moved", r.err)
        self.assertEqual([a for a in lr.state().d["approvals"] if a["scope"] == "item"], [])
        r = lr.ctl("approve", "item", "A", "--by", "Pat", "--head", head[:12])
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.state().d["approvals"][-1]["headSha"], head)

    def test_a_malformed_head_or_one_on_another_scope_is_a_usage_error(self):
        lr, loop = self.at_awaiting()
        for args in (("item", "A", "--head", "abc"), ("item", "A", "--head", "zzzzzzzzzz"), ("item", "A", "--head", "ABCDEF123456"),
                     ("unblock", "A", "--head", "a" * 12), ("checkpoint", "A", "--head", "a" * 12)):
            with self.subTest(args):
                self.assertEqual(lr.ctl("approve", *args, "--by", "Pat").rc, 2)

    def test_the_human_step_line_carries_the_head(self):
        lr, loop = self.at_awaiting()
        head = lr.item("A")["headSha"]
        pend = lr.ctl("status", "--pending-human", "--json").json["pending"]
        step = [p for p in pend if p["kind"] == "approve-item"][0]["human_step"]
        self.assertTrue(step.endswith("approve item A --by <you> --head %s" % head[:12]), step)
        self.assertIn("HUMAN STEP: python3 .claude/scripts/loopctl approve item A --by <you> --head %s" % head[:12], lr.ctl("status", "--pending-human").out)


class MergeCommitHoldsExactlyTheJudgedHead(Base):
    """LC-3: the merge commit has two parents and the tree of a plain merge of them; an evil merge or an octopus merge is refused."""

    def at_reviewing(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        return lr, loop

    def test_an_evil_merge_is_refused(self):
        lr, loop = self.at_reviewing()
        br = lr.item("A")["branch"]
        lr.repo.git("merge", "--no-ff", "--no-commit", br)
        with open(os.path.join(lr.path, "evil.txt"), "w") as fh:
            fh.write("unreviewed payload\n")
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "Merge branch '%s'\n\nLoop-Item: A\nLoop-Attempt: 1" % br)
        mc = lr.repo.head()
        r = lr.ctl("advance", "A", "merged", "--merge-commit", mc, "--session", lr.session)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("changes more than the judged head", r.err)
        self.assertEqual(lr.item("A")["status"], "reviewing")

    def test_an_octopus_merge_is_refused(self):
        lr, loop = self.at_reviewing()
        br = lr.item("A")["branch"]
        lr.repo.git("switch", "-q", "-c", "other", "main")
        lr.repo.commit_file("src/other.txt", "o\n", "other")
        lr.repo.git("switch", "-q", "main")
        lr.repo.git("merge", "--no-ff", "-q", "-m", "Merge branch '%s'\n\nLoop-Item: A\nLoop-Attempt: 1" % br, br, "other")
        mc = lr.repo.head()
        self.assertEqual(len(lr.repo.git("rev-list", "--parents", "-n", "1", mc).split()), 4)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertIn("two-parent", str(cm.exception))

    def test_a_clean_merge_of_the_head_is_accepted(self):
        lr, loop = self.at_reviewing()
        mc = lr.do_merge("A")
        loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertEqual(lr.item("A")["status"], "merged")

    def test_a_git_without_merge_tree_write_tree_refuses_rather_than_accepts(self):
        lr, loop = self.at_reviewing()
        mc = lr.do_merge("A")
        real = lgit.run_git

        def old_git(args, *a, **k):
            if "merge-tree" in list(args):
                return types.SimpleNamespace(returncode=129, stdout="", stderr="usage: git merge-tree [<options>] <branch1> <branch2>\n")
            return real(args, *a, **k)
        lgit.run_git = old_git
        self.addCleanup(setattr, lgit, "run_git", real)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "merged", lr.session, merge_commit=mc)
        self.assertIn("git 2.38", str(cm.exception))
        self.assertEqual(lr.item("A")["status"], "reviewing")


class NestedRunPushesNothing(LoopTestCase):
    """LC-4: a Claude session's `loopctl run` is refused before the run lock, the remote lock or any push."""

    def test_the_keeper_is_never_started_and_no_lock_is_taken(self):
        import json
        from tests.helpers import make_bare_remote
        from tests.loopctl.extras import EVIDENCE
        from loopctl import remote_lock
        lr = self.lr(items=[item_doc("A"), item_doc("B")], extra_cfg="remote_lock: origin\nrunner: driver\nrun_budget_usd: 5\n")
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
        lr.commit_all("evidence")
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 0)
        calls = []
        real = remote_lock.Keeper.start
        remote_lock.Keeper.start = lambda self, *a, **k: calls.append("start")
        self.addCleanup(setattr, remote_lock.Keeper, "start", real)
        for marker in ({"CLAUDECODE": "1"}, {"CLAUDECODE": ""}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(marker):
                r = lr.ctl("run", env=marker)
                self.assertEqual(r.rc, 3, r)
                self.assertIn("nesting", r.err)
                self.assertEqual(calls, [])
                self.assertFalse(lr.context().lock().status()["held"])
                self.assertIsNone(bare.ref("refs/loopctl/lock/demo"))

    def test_the_dry_run_shows_the_nesting_row(self):
        lr = self.lr(items=[item_doc("A")], extra_cfg="runner: driver\nrun_budget_usd: 5\n")
        rows = {p["id"]: p for p in startrun.preflight(lr.context(), None, None, {"CLAUDECODE": "1"})}
        self.assertFalse(rows["nesting"]["ok"])
        rows = {p["id"]: p for p in startrun.preflight(lr.context(), None, None, {})}
        self.assertTrue(rows["nesting"]["ok"])


class NextStepsWork(unittest.TestCase):
    """LC-7: every command the unattended closing message prints parses with the real CLI parser, and an unblock names a choice that exists."""

    def commands(self):
        out = []
        for code in sorted(unattended._NEXT):
            out.append((code, unattended.next_step("stop", code, "A")))
        out.append(("demoted", unattended.next_step("demoted", None, "A")))
        return out

    def test_every_printed_approve_command_parses_and_unblock_names_a_real_choice(self):
        seen = 0
        for code, text in self.commands():
            for m in re.finditer(r"loopctl (approve [^,;]*)", text):
                toks = shlex.split(m.group(1).replace(" if it is parked", ""))[1:]
                args, k, positional = [], 0, 0
                while k < len(toks):
                    if toks[k].startswith("--"):
                        args += toks[k:k + 2]
                        k += 2
                    elif positional < 2 and not any(x.startswith("--") for x in args):      # the scope, then the target, before any option
                        args.append(toks[k])
                        positional += 1
                        k += 1
                    else:
                        break                                           # prose after the command
                with self.subTest(code=code, cmd=args):
                    a = cli._approve_parser().parse_args(args)
                    seen += 1
                    if a.scope == "unblock":
                        self.assertIn(a.choice, ("reset", "keep", "replan"))
                    if a.scope == "item":
                        self.assertIn(a.choice, ("approve", "reject", "stop"))
        self.assertGreaterEqual(seen, 8)

    def test_the_printed_unblock_really_unparks_an_item(self):
        lr = self.lr_()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        loop.park("A", "merge_refused", "x", lr.session)
        text = unattended.next_step("stop", "merge_refused", "A")
        cmd = re.search(r"loopctl (approve unblock .*)", text).group(1).replace("NAME", "Pat")
        self.assertEqual(lr.ctl(*shlex.split(cmd)).rc, 0)
        self.assertEqual(lr.item("A")["status"], "pending")

    def lr_(self):
        from . import LoopRepo
        r = LoopRepo(items=[item_doc("A"), item_doc("B")])
        self.addCleanup(r.cleanup)
        return r


class GateHeadIsTheCheckouts(Base):
    """LC-8: `gate --head` cannot name a head the checkout is not at."""

    def test_a_made_up_head_is_refused_and_the_real_one_is_recorded(self):
        gates.register(types.SimpleNamespace(NAME="g6", OWNER_METRIC="x", run=lambda ctx: gates.GateResult.passed(note="ok"), __name__="fake_g6"))
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        r = lr.ctl("gate", "g6", "A", "--head", "f" * 40, "--session", lr.session)
        self.assertEqual(r.rc, 3, r)
        self.assertIn("not the HEAD", r.err)
        self.assertNotIn("g6", lr.item("A")["gates"])
        real = lr.repo.head()
        r = lr.ctl("gate", "g6", "A", "--head", real[:10], "--session", lr.session)
        self.assertEqual(r.rc, 0, r)
        self.assertEqual(lr.item("A")["gates"]["g6"]["head"], real)
        r = lr.ctl("gate", "g6", "A", "--session", lr.session)
        self.assertEqual(lr.item("A")["gates"]["g6"]["head"], real)
        self.assertEqual(lr.ctl("gate", "g6", "A", "--head", "abc", "--session", lr.session).rc, 3)          # too short to be a head


class UnblockAndRejectResetThePlatformCounter(Base):
    """LC-11: a human decision resets the platform counter too, so the first platform failure after it does not park at once."""

    def test_unblock_reset_and_reject_clear_it_and_a_discard_keeps_it(self):
        lr, loop = self.parked("platform_outage")

        def set_platform(st):
            st.items["A"]["counters"]["platform"] = 3
            st.items["A"]["counters"]["discards"] = 1
        loop.store.update(set_platform, "test")
        loop.approve("unblock", "A", "Pat", "reset")
        self.assertEqual((lr.item("A")["counters"]["platform"], lr.item("A")["counters"]["discards"]), (0, 1))
        lr, loop = self.at_awaiting()
        loop.store.update(set_platform, "test")
        loop.approve("item", "A", "Pat", "reject", "again")
        self.assertEqual((lr.item("A")["counters"]["platform"], lr.item("A")["counters"]["discards"]), (0, 1))


class CheckpointAnswersAreVoidedWhenThePauseIsClearedAnotherWay(Base):
    """LC-10: an unused `approve checkpoint` answer must not survive an `approve breaker` or an abort and answer a later checkpoint."""

    def test_breaker_voids_the_answer(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        lr.complete("A")

        def park(st):
            st.header["parked"] = {"item": "A", "reason": "checkpoint", "at": "2026-01-01T00:00:00Z"}
            st.header["runStatus"] = "parked"
        loop.store.update(park, "test")
        loop.approve("checkpoint", "A", "Pat", "approve", "ok")
        self.assertTrue([a for a in lr.state().d["approvals"] if a["kind"] == "checkpoint" and not a["used"]])
        loop.approve("breaker", "breaker", "Pat")
        left = [a for a in lr.state().d["approvals"] if a["kind"] == "checkpoint"]
        self.assertEqual([(a["used"], a["voided"]) for a in left], [(True, "breaker")])


class CrashRecoveryRespectsHumanGates(Base):
    """LC-13: `reconcile` recovers a merge lost to a crash only when a human-gated item has a merge approval for that merge's head."""

    def crashed(self, approve):
        lr = self.lr(items=[item_doc("A", migrations=[{"name": "m1", "dataLossClass": "lossy", "noDownReason": "drops a column"}]), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing", "awaiting_human")
        if approve:
            loop.approve("item", "A", "Pat")
        lr.do_merge("A")                                                      # the crash window: merged in git, not recorded in the state
        return lr

    def test_an_unapproved_gated_merge_is_not_recovered(self):
        lr = self.crashed(approve=False)
        r = lr.ctl("reconcile", "--apply", "--by", "Pat")
        self.assertIn("recover-unapproved", r.out + r.err)
        self.assertEqual(lr.item("A")["status"], "awaiting_human")

    def test_an_approved_gated_merge_is_recovered(self):
        lr = self.crashed(approve=True)
        r = lr.ctl("reconcile", "--apply", "--by", "Pat")
        self.assertEqual(lr.item("A")["status"], "merged", r)

    def test_an_ungated_merge_is_recovered_as_before(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        lr.do_merge("A")
        lr.ctl("reconcile", "--apply", "--by", "Pat")
        self.assertEqual(lr.item("A")["status"], "merged")

    def test_a_merge_of_some_other_commit_than_the_judged_head_is_not_recovered(self):
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying", "reviewing")
        br = lr.item("A")["branch"]
        lr.repo.git("switch", "-q", br)
        lr.repo.commit_file("src/more.txt", "after the review\n", "work added after the head was judged")
        lr.repo.git("switch", "-q", "main")
        lr.repo.git("merge", "--no-ff", "-q", "-m", "Merge branch '%s'\n\nLoop-Item: A\nLoop-Attempt: 1" % br, br)
        r = lr.ctl("reconcile", "--apply", "--by", "Pat")
        self.assertIn("recover-unapproved", r.out + r.err)
        self.assertEqual(lr.item("A")["status"], "reviewing")


class OrphansAreLedgered(Base):
    """LC-6: a child found dead or killed after its driver died spent money nobody recorded; each gets an unknown ledger entry."""

    def test_a_reaped_or_found_dead_child_parks_the_run_until_a_human_accepts_the_cost(self):
        from loopctl import driver
        lr = self.lr(items=[item_doc("A"), item_doc("B")])
        loop = lr.fast_start()
        fake = types.SimpleNamespace(ctx=loop.ctx, loop=loop, run_id="run-2", ev=lambda *a, **k: None)
        reaped = [{"sessionId": "s-one", "item": "A", "role": "backend-engineer", "killed": True, "runId": "run-1"},
                  {"sessionId": "s-two", "item": None, "role": None, "killed": False, "runId": "../bad"}]
        driver.Driver.ledger_orphans(fake, reaped)
        driver.Driver.ledger_orphans(fake, [])
        ents = [e for e in loop.ctx.ledger().entries()[0] if e["type"] == "spend"]
        self.assertEqual([(e["item"], e["runId"], e["unknown"], e["source"]) for e in ents], [("A", "run-1", True, "orphan"), ("orphan", "run-2", True, "orphan")])
        v = loop.ctx.ledger().verdict(loop.ctx.cfg, "run-3")
        self.assertEqual((v.ok, v.codes), (False, ["cost_unknown"]))
        loop.approve("cost", "cost", "Pat")
        self.assertTrue(loop.ctx.ledger().verdict(loop.ctx.cfg, "run-3").ok)


class ProseMatchesCode(unittest.TestCase):
    """LC-10 (prose): the orchestrate skill and loop.md do not offer a checkpoint reject that loopctl always refuses."""

    def test_no_relay_form_offers_checkpoint_reject(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for rel in (".claude/skills/orchestrate/SKILL.md", ".claude/skills/orchestrate/loop.md"):
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(rel):
                self.assertNotRegex(text, r"approve checkpoint[^\n`]*--choice approve\|reject")
                self.assertNotRegex(text, r"approve checkpoint <id> --by <name> \[--choice approve\|reject")


if __name__ == "__main__":
    unittest.main()
