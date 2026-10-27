"""The unattended start: every precondition decided from committed facts, and the guards' view of the new command (report 6.2, 6.3, 16.1 row 7)."""
from __future__ import annotations

import json
import os
import time
import unittest

from ... import SCRIPTS_DIR  # noqa: F401
from ..fault_injection import render_config

from guards import g_exec, g_halt
from guards.invoke import analyze
from kitlib import config as kconfig
from loopctl import cli, unattended

from . import EVIDENCE, UNATTENDED_CFG, UnattendedRepo


def ids(rows, ok):
    return sorted(r["id"].split(".", 1)[1] for r in rows if r["ok"] is ok)


class Rows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lr = UnattendedRepo()
        cls.addClassCleanup(cls.lr.cleanup)
        cls.lr.approve()

    def setUp(self):
        self.ctx = self.lr.context()
        self.state = self.ctx.store().read()[0]

    def cfg(self, **over):
        full = dict(UNATTENDED_CFG, notify_command=self.lr.notifier.path)
        full.update(over)
        self.ctx.cfg = kconfig.load_text(render_config(full))

    def rows(self, level="L4", **kw):
        return unattended.rows(self.ctx, kw.pop("state", self.state), lambda c: level, **kw)

    def only_bad(self, rows, *want):
        self.assertEqual(ids(rows, False), sorted(want), [r for r in rows if not r["ok"]])

    def test_a_properly_set_up_install_is_ready(self):
        rows = self.rows()
        self.assertTrue(unattended.ready(rows), [r for r in rows if not r["ok"]])
        self.assertEqual(ids(rows, True), sorted(["evidence", "autonomy", "runner", "budget", "notify", "level", "demotion", "parked", "work", "watch"]))
        self.assertIn("next: A", [r for r in rows if r["id"].endswith("work")][0]["message"])

    def test_each_precondition_is_its_own_refusal(self):
        for label, over, want in (("autonomy", {"autonomy": "per-feature"}, "autonomy"), ("level configured", {"autonomy_level": "L3"}, "autonomy"),
                                  ("runner", {"runner": "sequential"}, "runner"), ("no run ceiling", {"run_budget_usd": "0"}, "budget"),
                                  ("no day ceiling", {"day_budget_usd": "0"}, "budget"), ("no notify", {"notify_command": '""'}, "notify"),
                                  ("shell syntax", {"notify_command": '"%s; id"' % self.lr.notifier.path}, "notify"),
                                  ("program in the repo", {"notify_command": os.path.join(self.lr.path, "x.sh")}, "notify")):
            with self.subTest(label):
                self.cfg(**over)
                self.only_bad(self.rows(), want)

    def test_the_effective_level_is_the_doctors_and_an_unreadable_doctor_is_a_refusal(self):
        self.cfg()
        self.only_bad(self.rows(level="L3"), "level")
        self.only_bad(self.rows(level="garbage"), "level")
        self.assertTrue(unattended.ready(self.rows(level="L5")))
        rows = unattended.rows(self.ctx, self.state, lambda c: (_ for _ in ()).throw(RuntimeError("doctor exploded")))
        self.only_bad(rows, "level")
        self.assertTrue(unattended.ready(unattended.rows(self.ctx, self.state, lambda c: None)))      # the test double: not computed

    def test_the_evidence_must_exist_be_complete_and_be_the_merged_copy(self):
        self.cfg()
        path = os.path.join(self.lr.path, "docs", "baseline", "evidence.json")

        def restore():
            self.lr.repo.git("checkout", "--", "docs/baseline/evidence.json")
        self.addCleanup(restore)
        os.unlink(path)
        self.only_bad(self.rows(), "evidence")                                                    # absent
        restore()
        with open(path, "w") as fh:
            json.dump(dict(EVIDENCE, attested_by="<name>"), fh)
        self.only_bad(self.rows(), "evidence")                                                    # a template placeholder left in
        restore()
        with open(path, "w") as fh:
            json.dump(dict(EVIDENCE, attested_by="Mallory"), fh)                                  # complete, but not the one a human merged
        rows = self.rows()
        self.only_bad(rows, "evidence")
        self.assertIn("not the committed copy", [r for r in rows if r["id"].endswith("evidence")][0]["message"])
        restore()
        self.ctx.base_sha = self.lr.repo.git("rev-parse", "HEAD~1")                               # a base from before the evidence was merged
        self.only_bad(self.rows(), "evidence")

    def test_a_demotion_a_parked_run_and_nothing_to_do_each_stop_the_start(self):
        self.cfg()
        st = self.ctx.store().read()[0]
        st.header["demotion"] = {"to": "L3", "reason": "revert", "at": "2026-10-20T00:00:00Z"}
        self.only_bad(unattended.rows(self.ctx, st, lambda c: "L4"), "demotion")
        st.header["demotion"] = None
        for status in ("parked", "stopped", "aborted"):
            st.header["runStatus"] = status
            self.only_bad(unattended.rows(self.ctx, st, lambda c: "L4"), "parked")
        st.header["runStatus"] = "idle"
        for i in st.items.values():
            i["status"] = "needs_human"
            i["blockReason"] = "x"
        self.only_bad(unattended.rows(self.ctx, st, lambda c: "L4"), "work")
        self.only_bad(unattended.rows(self.ctx, None, lambda c: "L4"), "state")

    def test_an_unattended_start_needs_the_external_watchdog_to_have_checked_in(self):
        self.cfg()
        path = unattended.watch_path(self.lr.rs)
        self.assertTrue(os.path.getsize(path) > 0)
        self.addCleanup(self.lr.checkin)
        os.unlink(path)
        rows = self.rows()
        self.only_bad(rows, "watch")                                                               # a killed driver cannot notify anyone: nobody watching, no start
        self.assertIn("unattended watch", [r for r in rows if r["id"].endswith("watch")][0]["message"])
        unattended.jsonio.atomic_write_json(path, {"ts": time.time() - 3600})
        self.only_bad(self.rows(), "watch")                                                        # an hour old is not checked in
        unattended.jsonio.atomic_write_json(path, {"ts": time.time() + 3600})
        self.only_bad(self.rows(), "watch")                                                        # nor is one from the future
        unattended.jsonio.atomic_write_json(path, {"ts": "now"})
        self.only_bad(self.rows(), "watch")
        self.lr.checkin()
        self.assertTrue(unattended.ready(self.rows()))
        self.assertTrue(self.lr.ctl("unattended", "watch").rc == 0)                                # and `watch` itself is what checks in
        self.assertGreater(unattended.read_watch(self.lr.rs)["ts"], time.time() - 60)


class Cli(unittest.TestCase):
    def test_check_status_and_the_dry_run_agree_and_a_run_without_evidence_is_refused(self):
        lr = UnattendedRepo()
        self.addCleanup(lr.cleanup)
        lr.approve()
        r = lr.ctl("unattended", "check", "--json")
        self.assertEqual(r.rc, 0, r)
        self.assertTrue(r.json["ready"])
        st = lr.ctl("unattended", "status", "--json").json
        self.assertEqual((st["wanted"], st["ready"], st["demotion"], st["watchdog"]["status"]), (True, True, None, "idle"))
        dry = lr.ctl("run", "--dry-run", "--json").json
        self.assertIn("unattended.notify", [p["id"] for p in dry["preflight"]])                  # the dry run shows what a start would check
        self.assertTrue(dry["ready"])
        text = lr.ctl("unattended", "status").out
        self.assertIn("wanted by the config", text)

        bare = UnattendedRepo(evidence=False)                                                      # promotion without evidence: the config asks for L4, the file is not there
        self.addCleanup(bare.cleanup)
        ap = bare.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(ap.rc, 3, ap)                                                              # every Tier 2 command refuses an evidence-gated value
        self.assertIn("evidence gate", ap.err)
        rc = bare.run()
        self.assertEqual(rc.rc, 3, rc)
        self.assertIn("evidence gate", rc.err)
        self.assertEqual(bare.calls(), [])
        self.assertEqual(bare.notifier.calls(), [])
        chk = bare.ctl("unattended", "check")
        self.assertEqual(chk.rc, 1)
        self.assertIn("unattended.evidence", chk.out)
        self.assertEqual(bare.ctl("unattended", "check", "--quiet").rc, 1)


def inv(args):
    ana = analyze("python3 .claude/scripts/loopctl " + " ".join(args), "/tmp")
    return [i for i in ana.invs if i.name.startswith("python")][0]


READS = [["unattended", "check"], ["unattended", "check", "--quiet"], ["unattended", "status", "--json"], ["unattended", "promotion", "--facts", "f.json"],
         ["unattended", "intake", "--in", "t.json", "--source", "issue#1"]]
WRITES = [["unattended"], ["unattended", "watch"], ["unattended", "notify-test"], ["unattended", "intake", "--in", "t.json", "--source", "issue#1", "--write"],
          ["unattended", "check", "--write"], ["unattended", "status", "--force"], ["unattended", "frobnicate"], ["approve", "demotion", "--by", "Pat"],
          ["--json", "unattended", "check"]]


class Guards(unittest.TestCase):
    def test_the_guards_and_loopctl_agree_on_which_unattended_actions_write(self):
        t = cli.classification()
        self.assertIn("unattended", t["tier1"])
        self.assertEqual(tuple(t["flag_read"]["unattended"]), g_exec.LOOPCTL_FLAG_READ["unattended"])
        for args in READS:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), True)
                self.assertTrue(g_halt._read_only(None, inv(args)))
        for args in WRITES:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), False)
                self.assertFalse(g_halt._read_only(None, inv(args)))

    def test_every_action_the_command_accepts_is_either_a_read_word_or_a_write(self):
        actions = ("check", "status", "promotion", "intake", "watch", "notify-test")
        reads = set(g_exec.LOOPCTL_FLAG_READ["unattended"])
        self.assertEqual(reads, {"check", "status", "promotion", "intake"})
        self.assertTrue(set(actions) - reads == {"watch", "notify-test"})                          # the two that run the human's command are writes


if __name__ == "__main__":
    unittest.main()
