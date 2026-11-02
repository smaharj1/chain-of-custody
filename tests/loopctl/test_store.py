"""The state ref: compare-and-swap, concurrent writers, forged refs, the remote form (report sections 10.3, RC-13, RC-15, RC-28)."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest

from loopctl import common, state as sm
from loopctl.state import StateCorrupt

from . import LoopTestCase

REF = "refs/loopctl/state/demo"


class Store(LoopTestCase):
    def test_every_write_is_a_commit_on_the_ref_and_the_log_is_the_audit_trail(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        log = lr.repo.git("log", "--format=%s", REF).splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(log[0].startswith("begin A"), log[0])
        self.assertEqual(lr.repo.git("ls-tree", "--name-only", REF), "state.json")
        seqs = [json.loads(lr.repo.git("show", "%s~%d:state.json" % (REF, k)))["seq"] for k in range(len(log))]
        self.assertEqual(seqs, [2, 1])
        self.assertEqual(lr.loop().store.verify_chain(), [])

    def test_a_concurrent_writer_forces_a_retry_that_reapplies_the_mutation(self):
        lr = self.lr()
        store = lr.fast_start().store
        raced = []

        def race():
            if not raced:
                raced.append(1)
                lr.context().store().update(lambda st: st.header.__setitem__("breaker", 1), "someone else")
        store._before_cas = race
        st, _ = store.update(lambda st: st.header.__setitem__("runStatus", "parked"), "mine")
        self.assertEqual(raced, [1])
        self.assertEqual((st.header["breaker"], st.header["runStatus"]), (1, "parked"))      # both writes survived
        self.assertEqual(lr.state().header["breaker"], 1)

    def test_a_losing_writer_whose_precondition_no_longer_holds_fails_instead_of_overwriting(self):
        lr = self.lr()
        loop_a = lr.fast_start()
        loop_a.begin("A", lr.session)
        loop_b = lr.loop(fresh=True)
        loop_a.advance("A", "design_review", lr.session)
        # B's mutation is evaluated against the fresh state inside the CAS loop, where A has already moved
        with self.assertRaises(common.Refused):
            loop_b.advance("A", "design_review", lr.session)
        self.assertEqual(lr.item("A")["status"], "design_review")

    def test_threads_racing_to_begin_the_same_item_exactly_one_wins(self):
        lr = self.lr()
        lr.fast_start()
        results = []

        def go():
            try:
                lr.context() and __import__("loopctl.state", fromlist=["Loop"]).Loop(lr.context()).begin("A", lr.session)
                results.append("ok")
            except common.Refused:
                results.append("refused")
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["ok", "refused", "refused", "refused"])
        self.assertEqual(len([h for h in lr.item("A")["history"] if h["to"] == "designing"]), 1)

    def test_exhausted_retries_leave_the_state_untouched(self):
        lr = self.lr()
        store = lr.fast_start().store
        store.max_tries = 3

        def always_race():
            lr.context().store().update(lambda st: st.header.__setitem__("breaker", st.header["breaker"] + 1), "noise")
        store._before_cas = always_race
        with self.assertRaises(common.Refused) as cm:
            store.update(lambda st: st.header.__setitem__("runStatus", "mine"), "mine")
        self.assertIn("moved under this command", str(cm.exception))
        self.assertNotEqual(lr.state().header["runStatus"], "mine")

    def test_a_missing_ref_is_an_error_unless_the_caller_creates_it(self):
        lr = self.lr()
        store = lr.context().store()
        self.assertEqual(store.read(), (None, None))
        with self.assertRaises(common.Refused):
            store.update(lambda st: None, "x")


class ForgedRef(LoopTestCase):
    def read(self, lr):
        return lr.context().store().read()

    def forge(self, lr, text, name="state.json", parent=None, extra=None):
        blob = lr.repo.git("hash-object", "-w", "--stdin", input_text=text)
        entries = "100644 blob %s\t%s\n" % (blob, name)
        if extra:
            entries += extra
        tree = lr.repo.git("mktree", input_text=entries)
        args = ["commit-tree", tree, "-m", "forged"]
        if parent:
            args += ["-p", parent]
        sha = lr.repo.git(*args)
        lr.repo.git("update-ref", REF, sha)
        return sha

    def test_the_ref_pointing_at_an_ordinary_commit_is_refused_not_read_as_empty(self):
        lr = self.lr()
        lr.repo.git("update-ref", REF, "HEAD")
        with self.assertRaises(StateCorrupt):
            self.read(lr)
        self.assertIn("STATE UNREADABLE", lr.ctl("status").out)
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 2)        # and it does not overwrite the forged ref
        self.assertEqual(lr.repo.git("rev-parse", REF), lr.repo.head())

    def test_the_ref_pointing_at_a_blob_or_a_tree(self):
        lr = self.lr()
        lr.repo.git("update-ref", REF, lr.repo.git("hash-object", "-w", "--stdin", input_text="{}"))
        with self.assertRaises(StateCorrupt):
            self.read(lr)
        lr.repo.git("update-ref", REF, lr.repo.git("rev-parse", "HEAD^{tree}"))
        with self.assertRaises(StateCorrupt):
            self.read(lr)

    def test_wrong_tree_shapes(self):
        lr = self.lr()
        text = json.dumps(sm.State.new("demo").d)
        self.forge(lr, text, name="other.json")
        with self.assertRaises(StateCorrupt):
            self.read(lr)
        self.forge(lr, text, extra="100644 blob %s\textra.txt\n" % lr.repo.git("hash-object", "-w", "--stdin", input_text="x"))
        with self.assertRaises(StateCorrupt):
            self.read(lr)
        self.forge(lr, text)                     # a well-formed first state is accepted
        self.assertIsNotNone(self.read(lr)[0])

    def test_hostile_json_in_state(self):
        lr = self.lr()
        for text in ("not json", "[]", '{"schema": "loopctl.state.v1", "schema": "x"}', '{"seq": NaN}', "{" * 5000, ""):
            with self.subTest(text[:20]):
                self.forge(lr, text)
                with self.assertRaises(StateCorrupt):
                    self.read(lr)

    def test_a_forged_state_for_another_plan(self):
        lr = self.lr()
        self.forge(lr, json.dumps(sm.State.new("someone-elses-plan").d))
        with self.assertRaises(StateCorrupt):
            self.read(lr)

    def test_a_rewritten_history_with_a_broken_sequence(self):
        lr = self.lr()
        lr.fast_start()
        real = lr.repo.git("rev-parse", REF)
        doc = json.loads(lr.repo.git("show", REF + ":state.json"))
        doc["seq"] += 7                          # does not follow its parent
        self.forge(lr, json.dumps(doc), parent=real)
        with self.assertRaises(StateCorrupt) as cm:
            self.read(lr)
        self.assertIn("sequence", str(cm.exception))
        self.assertEqual(lr.ctl("begin", "A", "--session", lr.session).rc, 2)

    def test_a_forged_merged_item_is_caught_by_reconcile_and_its_dependents_do_not_start(self):
        lr = self.lr()
        loop = lr.fast_start()
        real = lr.repo.git("rev-parse", REF)
        doc = json.loads(lr.repo.git("show", REF + ":state.json"))
        doc["items"]["A"].update(status="merged", mergeCommit="1" * 40)
        doc["seq"] += 1
        self.forge(lr, json.dumps(doc), parent=real)
        self.assertEqual(self.read(lr)[0].items["A"]["status"], "merged")        # structurally valid: the forgery is plausible
        r = lr.ctl("reconcile", "--dry-run")
        self.assertIn("reconcile.merge-missing", r.out)
        self.assertEqual(r.rc, 1)
        self.assertEqual(lr.ctl("reconcile", "--apply").rc, 0)
        self.assertEqual((lr.item("A")["status"], lr.item("A")["blockReason"]), ("needs_human", "merge_missing"))
        with self.assertRaises(common.Refused):
            lr.loop(fresh=True).begin("B", lr.session)


class Remote(LoopTestCase):
    def lr_remote(self, extra="state_remote: origin"):
        from tests.helpers import make_bare_remote
        lr = self.lr(extra_cfg=extra)
        bare = make_bare_remote()
        self.addCleanup(bare.cleanup)
        lr.repo.add_remote("origin", bare.path)
        lr.repo.git("push", "-q", "origin", "main")
        return lr, bare

    def test_state_is_pushed_by_compare_and_swap_to_a_local_bare_remote(self):
        lr, bare = self.lr_remote()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        self.assertEqual(bare.ref(REF), lr.repo.git("rev-parse", REF))
        self.assertEqual(lr.item("A")["status"], "designing")

    def test_a_remote_that_moved_underneath_is_fast_forwarded_into_the_local_ref(self):
        from tests.helpers.gitrepo import run
        from loopctl.state import StateStore
        lr, bare = self.lr_remote()
        lr.fast_start()
        clone = tempfile.mkdtemp(prefix="loopctl-clone-")
        self.addCleanup(shutil.rmtree, clone, True)
        run(["clone", "-q", bare.path, clone + "/c"], clone)
        run(["fetch", "-q", "origin", "+%s:%s" % (REF, REF)], clone + "/c")
        other = StateStore(clone + "/c", "demo", "origin")           # another machine writes
        other.update(lambda st: st.header.__setitem__("breaker", 1), "from the other machine")
        st, _ = lr.context().store().update(lambda s: s.header.__setitem__("runStatus", "parked"), "mine")
        self.assertEqual((st.header["breaker"], st.header["runStatus"]), (1, "parked"))
        self.assertEqual(bare.ref(REF), lr.repo.git("rev-parse", REF))

    def test_a_diverged_remote_is_refused(self):
        lr, bare = self.lr_remote()
        lr.fast_start()
        other = lr.repo.git("commit-tree", lr.repo.git("rev-parse", "HEAD^{tree}"), "-m", "unrelated")
        lr.repo.git("push", "-q", "--force", "origin", "%s:%s" % (other, REF))
        with self.assertRaises(common.LoopctlError):
            lr.context().store().update(lambda s: None, "x")

    def test_the_remote_must_be_a_configured_name_never_a_url(self):
        lr = self.lr(extra_cfg="state_remote: origin")           # origin is not configured
        r = lr.ctl("approve", "plan", "--by", "Pat")
        self.assertEqual(r.rc, 3, r)
        self.assertIn("not a configured remote", r.err)
        for bad, msg in (("https://example.invalid/x.git", "not a remote name"), ("-oProxyCommand=evil", "not a remote name")):
            lr2 = self.lr(extra_cfg="state_remote: '%s'" % bad)
            r = lr2.ctl("approve", "plan", "--by", "Pat")
            self.assertEqual(r.rc, 3, r)
            self.assertIn(msg, r.err)


if __name__ == "__main__":
    unittest.main()
