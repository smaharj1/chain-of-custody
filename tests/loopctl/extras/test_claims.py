"""Claims: two humans, one item (F-25), against a local bare remote. Advisory: names are labels, the first push wins, expired claims lapse."""
from __future__ import annotations

import json
import threading
import unittest

from loopctl import claims as cm_mod, remote_lock as rl
from loopctl.common import Refused, UsageError

from . import Clock, Fleet, MemRemote, mem_claims


def claims(machine, plan, clock, ttl_h=72.0):
    return cm_mod.Claims(machine.path, "origin", plan, ttl_h=ttl_h, clock=clock, host="h-" + machine.path[-4:])


class Pure(unittest.TestCase):
    NOW = 1_800_000_000.0

    def doc(self, **over):
        d = {"schema": cm_mod.SCHEMA, "plan": "demo", "item": "F1", "by": "alice", "token": "a" * 32, "host": "laptop", "claimed": "2026-10-26T10:00:00Z",
             "expires": self.NOW + 3600, "ttl_s": 72 * 3600.0}
        d.update(over)
        return d

    def test_a_claim_record_is_judged_strictly(self):
        self.assertIsNone(cm_mod.validate_claim(self.doc(), "demo", "F1", self.NOW))
        bad = {"another item": self.doc(item="F2"), "another plan": self.doc(plan="x"), "unknown field": self.doc(extra=1), "name with a space": self.doc(by="a b"),
               "name with shell": self.doc(by="a;b"), "no name": self.doc(by=""), "token": self.doc(token="x"), "expires never": self.doc(expires=self.NOW + 10 ** 9),
               "expires nan": self.doc(expires=float("nan")), "ttl tiny": self.doc(ttl_s=5), "ttl bool": self.doc(ttl_s=True), "note long": self.doc(note="x" * 300),
               "override not a flag": self.doc(override="yes")}
        for label, d in bad.items():
            with self.subTest(label):
                self.assertIsNotNone(cm_mod.validate_claim(d, "demo", "F1", self.NOW), label)

    def test_refs_are_built_from_validated_names_only(self):
        self.assertEqual(cm_mod.claim_ref("demo", "F1"), "refs/loopctl/claim/demo/F1")
        for plan, item in (("a/b", "F1"), ("demo", "../x"), ("demo", "F 1"), ("demo", ""), ("Demo", "F1")):
            with self.assertRaises(UsageError):
                cm_mod.claim_ref(plan, item)


class Rules(unittest.TestCase):
    """The rules, over an in-memory remote."""

    def setUp(self):
        self.mem = MemRemote()
        self.clock = Clock()
        self.alice = mem_claims(self.mem, "demo", self.clock)
        self.bob = mem_claims(self.mem, "demo", self.clock)

    def test_the_first_claim_wins_the_second_human_is_refused_by_name_and_the_same_name_renews(self):
        r = self.alice.take("F1", "alice", note="doing the schema")
        self.assertEqual((r["action"], r["claim"]["by"], r["claim"]["note"]), ("claimed", "alice", "doing the schema"))
        self.assertNotIn("token", json.dumps(r))
        with self.assertRaises(Refused) as cm:
            self.bob.take("F1", "bob")
        self.assertIn("alice", str(cm.exception))
        self.assertEqual(self.alice.get("F1")["claim"]["by"], "alice")
        self.clock.advance(3600)
        again = self.alice.take("F1", "alice")
        self.assertEqual(again["action"], "renewed")
        self.assertEqual(again["claim"]["note"], "doing the schema")                          # a renewal keeps the note and the original time
        self.assertEqual(again["claim"]["claimed"], r["claim"]["claimed"])

    def test_two_humans_who_claim_at_the_same_moment_cannot_both_get_the_item(self):
        self.bob.before_push = lambda: self.alice.take("F1", "alice")                          # alice's push lands between bob's read and bob's push
        with self.assertRaises(Refused) as cm:
            self.bob.take("F1", "bob")
        self.assertIn("first", str(cm.exception))
        self.assertEqual(self.alice.get("F1")["claim"]["by"], "alice")

    def test_an_expired_claim_can_be_taken_over_and_says_whose_it_was_and_a_live_one_only_with_override(self):
        self.alice.take("F1", "alice")
        self.clock.advance(73 * 3600)
        self.assertEqual(self.bob.get("F1")["state"], "expired")
        took = self.bob.take("F1", "bob")
        self.assertIn("took over an expired claim of alice", took["action"])
        self.assertEqual(took["claim"]["over"], "alice")
        with self.assertRaises(Refused):
            self.alice.take("F1", "alice")                                                     # bob's is live now
        over = self.alice.take("F1", "alice", override=True)
        self.assertIn("overrode the claim of bob", over["action"])
        self.assertTrue(over["claim"]["override"])                                             # the record says it was an override

    def test_release_is_by_the_holder_or_by_override_and_a_released_item_is_free(self):
        self.alice.take("F1", "alice")
        with self.assertRaises(Refused):
            self.bob.release("F1", "bob")
        self.assertEqual(self.bob.release("F1", "bob", override=True)["was"], "alice")
        self.assertEqual(self.alice.get("F1")["state"], "none")
        with self.assertRaises(Refused):
            self.alice.release("F1", "alice")                                                  # nothing left to release
        self.assertEqual(self.bob.take("F1", "bob")["action"], "claimed")

    def test_an_unreadable_claim_counts_as_claimed_and_never_as_free(self):
        self.mem.refs[cm_mod.claim_ref("demo", "F1")] = ("a" * 40, None, "its tree is not exactly claim.json")
        self.assertEqual(self.alice.get("F1")["state"], "corrupt")
        with self.assertRaises(Refused) as cm:
            self.alice.take("F1", "alice")
        self.assertIn("not a loopctl claim", str(cm.exception))
        self.assertIn("F1", self.alice.held_by_others("alice"))                               # the driver skips it
        r = self.alice.take("F1", "alice", override=True)
        self.assertIn("unreadable", r["action"])
        self.assertEqual(self.alice.get("F1")["state"], "live")
        self.mem.refs[cm_mod.claim_ref("demo", "F2")] = ("b" * 40, {"schema": cm_mod.SCHEMA, "plan": "demo", "item": "F2", "by": "mallory", "token": "a" * 32, "host": "h",
                                                                   "claimed": "2026-10-26T10:00:00Z", "expires": self.clock() + 10 ** 9, "ttl_s": 3600.0}, None)
        self.assertEqual(self.alice.get("F2")["state"], "corrupt")                            # a claim that would never lapse is not a claim

    def test_the_listing_and_what_the_driver_skips(self):
        self.alice.take("F1", "alice")
        self.bob.take("F2", "bob")
        rows = {r["item"]: r for r in self.alice.listing()}
        self.assertEqual({k: v["state"] for k, v in rows.items()}, {"F1": "live", "F2": "live"})
        self.assertNotIn("token", json.dumps(rows))
        self.assertEqual(sorted(self.alice.held_by_others("alice")), ["F2"])
        self.assertEqual(sorted(self.alice.held_by_others("carol")), ["F1", "F2"])
        self.clock.advance(100 * 3600)
        self.assertEqual(self.alice.held_by_others("carol"), {})                              # expired claims do not block anyone

    def test_the_driver_filter_removes_claimed_items_remembers_the_answer_and_never_falls_back_to_everything(self):
        self.bob.take("F2", "bob")
        said = []
        flt = rl.ClaimsFilter(self.alice, "alice", every_s=30.0, say=said.append, clock=self.clock)
        self.assertEqual(flt(["F1", "F2", "F3"]), ["F1", "F3"])
        self.assertTrue(any("F2" in s and "bob" in s for s in said))
        self.bob.release("F2", "bob")
        self.assertEqual(flt(["F1", "F2"]), ["F1"])                                           # cached: not asked again inside 30 s
        self.clock.advance(31)
        self.assertEqual(flt(["F1", "F2"]), ["F1", "F2"])
        # an unreachable remote with nothing cached means nothing is eligible, not everything
        broken = rl.ClaimsFilter(self.alice, "alice", clock=self.clock, say=said.append)

        def down(me):
            raise rl.RemoteUnreachable("no route")
        self.alice.held_by_others = down
        self.assertEqual(broken(["F1"]), [])


class Git(unittest.TestCase):
    """What git does, against a local bare remote."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    @classmethod
    def tearDownClass(cls):
        cls.fleet.cleanup()

    def setUp(self):
        self.plan = self.fleet.plan()
        self.clock = Clock()
        self.alice = claims(self.fleet.a, self.plan, self.clock)
        self.bob = claims(self.fleet.b, self.plan, self.clock)

    def test_the_first_push_wins_and_the_listing_reads_what_the_other_machine_wrote(self):
        self.alice.take("F1", "alice")
        with self.assertRaises(Refused) as cm:
            self.bob.take("F1", "bob")
        self.assertIn("alice", str(cm.exception))
        self.bob.take("F2", "bob")
        rows = {r["item"]: r for r in self.alice.listing()}
        self.assertEqual({k: (v["state"], v["claim"]["by"]) for k, v in rows.items()}, {"F1": ("live", "alice"), "F2": ("live", "bob")})
        self.assertNotIn("token", json.dumps(rows))
        self.assertEqual(sorted(self.alice.held_by_others("alice")), ["F2"])
        self.assertEqual(self.alice.release("F1", "alice")["action"], "released")
        self.assertEqual(self.bob.get("F1")["state"], "none")

    def test_two_humans_who_claim_at_the_same_moment_cannot_both_get_the_item(self):
        self.bob.before_push = lambda: self.alice.take("F1", "alice")                          # alice's push lands between bob's read and bob's push
        with self.assertRaises(Refused) as cm:
            self.bob.take("F1", "bob")
        self.assertIn("first", str(cm.exception))
        self.assertEqual(self.alice.get("F1")["claim"]["by"], "alice")
        barrier, results = threading.Barrier(2), []

        def go(c, who):
            barrier.wait()
            try:
                c.take("F2", who)
                results.append(who)
            except Refused:
                results.append("no-" + who)
        ts = [threading.Thread(target=go, args=(self.alice, "alice")), threading.Thread(target=go, args=(self.bob, "bob"))]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(sorted(r[:2] == "no" for r in results), [False, True], results)

    def test_a_flood_of_claim_refs_is_refused_not_fetched(self):
        fl = self.fleet
        old, cm_mod.MAX_CLAIMS = cm_mod.MAX_CLAIMS, 2                                           # the real limit is 400; three refs show the same thing for the price of one push
        self.addCleanup(setattr, cm_mod, "MAX_CLAIMS", old)
        fl.b.git("push", "-q", "origin", *["HEAD:refs/loopctl/claim/%s/F%d" % (self.plan, i) for i in (1, 2, 3)])
        with self.assertRaises(rl.RemoteUnreachable) as cm:
            self.alice.listing()
        self.assertIn("flood", str(cm.exception))
        self.assertEqual(rl.ClaimsFilter(self.alice, "alice", clock=self.clock, say=lambda m: None)(["F1"]), [])        # the driver then runs nothing

    def test_a_forged_claim_ref_counts_as_claimed_and_never_as_free(self):
        fl = self.fleet
        fl.forge(fl.b, cm_mod.claim_ref(self.plan, "F1"), blob="whatever")
        self.assertEqual(self.alice.get("F1")["state"], "corrupt")
        with self.assertRaises(Refused):
            self.alice.take("F1", "alice")
        self.assertEqual(self.alice.take("F1", "alice", override=True)["action"], "overrode an unreadable claim")
        self.assertEqual(self.alice.get("F1")["state"], "live")


if __name__ == "__main__":
    unittest.main()
