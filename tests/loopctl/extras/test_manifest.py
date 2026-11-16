"""The reproducibility manifest (F-26): sealed with a hash, checked against git at the recorded commits."""
from __future__ import annotations

import copy
import json
import os
import unittest

from kitlib import kitpaths
from loopctl import kit, manifest as mf
from loopctl.common import Refused, UsageError

from .. import LoopTestCase, item_doc
from . import EVIDENCE

LOCK = "lockfile v1\n"
CFG = "verify_tests: python3 -m unittest\nrunner: sequential\ngates:\n  g6: report\n"


class Manifest(LoopTestCase):
    @classmethod
    def build_repo(cls, tc):
        lr = tc.lr(items=[item_doc("A")], extra_cfg=CFG)
        for p, text in (("package-lock.json", LOCK), (".nvmrc", "20.11.0\n"), (".tool-versions", "python 3.12.1\n"), ("docs/baseline/evidence.json", json.dumps(EVIDENCE)),
                        (".claude/KIT_VERSION", "2.0.0-rc.1 (2026-10-26)\nclaude_code_min: 2.1.302\n"),     # an old copy install's: never read
                        ("package.json", json.dumps({"name": "x", "engines": {"node": ">=20"}})), ("pyproject.toml", '[project]\nrequires-python = ">=3.9"\n')):
            lr.repo.write(p, text)
        lr.commit_all("pins and lockfile")
        return lr

    def written(self, lr=None, item="A"):
        lr = lr or self.build_repo(self)
        r = lr.ctl("manifest", "write", item, "--json")
        self.assertEqual(r.rc, 0, r)
        path = r.json["path"]
        return lr, path, mf.read_file(path)

    def reseal(self, doc):
        return mf.seal(doc)

    def test_a_written_manifest_records_the_facts_and_verifies(self):
        lr, path, doc = self.written()
        self.assertEqual(doc["schema"], mf.SCHEMA)
        self.assertEqual(doc["repo"]["head"], lr.repo.head())
        self.assertEqual(doc["plan"]["item"], "A")
        self.assertEqual(doc["toolchain"][".nvmrc"], "20.11.0")
        self.assertEqual(doc["toolchain"]["package.json#engines"], {"node": ">=20"})
        self.assertEqual(doc["toolchain"]["pyproject.toml#requires-python"], ">=3.9")
        self.assertEqual(list(doc["lockfiles"]), ["package-lock.json"])
        with open(kitpaths.kit("KIT_VERSION"), encoding="utf-8") as fh:                      # the running plugin's, not the project's
            self.assertEqual(doc["kit"]["version"], fh.read().split("\n", 1)[0].strip())
        self.assertNotEqual(doc["kit"]["version"], "2.0.0-rc.1 (2026-10-26)")
        self.assertEqual({k: doc["kit"][k] for k in ("plugin", "fingerprint")}, {"plugin": kit.version(), "fingerprint": kit.fingerprint()})
        self.assertEqual(doc["config"]["gates"], {"g6": "report"})
        self.assertIn("verify_tests", doc["verify"])
        self.assertIsNotNone(doc["evidence"]["blob"])
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        v = lr.ctl("manifest", "verify", "--json")
        self.assertEqual(v.rc, 0, v)
        self.assertTrue(v.json["ok"] and v.json["seal"] and v.json["anchored"])
        self.assertEqual(v.json["facts"], [])
        self.assertEqual(v.json["not_rederivable"], ["gates", "environment"])
        again = lr.ctl("manifest", "write", "A").out
        self.assertIn("manifest written", again)
        # the hash covers `created`, so a write in a later second has a new one: compare with what this write stored
        self.assertIn("hash: " + mf.read_file(path)["hash"], again.replace("  ", " "))

    def test_editing_the_recorded_hash_or_any_field_breaks_the_seal(self):
        lr, path, doc = self.written()
        for label, edit in (("hash", lambda d: d.__setitem__("hash", "0" * 64)),
                            ("base sha", lambda d: d["repo"].__setitem__("base", "1" * 40)),
                            ("config sha", lambda d: d["config"].__setitem__("sha256", "2" * 64)),
                            ("a pin", lambda d: d["toolchain"].__setitem__(".nvmrc", "18.0.0")),
                            ("a gate", lambda d: d.__setitem__("gates", {"g6": {"status": "pass"}})),
                            ("environment", lambda d: d["environment"].__setitem__("platform", "plan9")),
                            ("an added field", lambda d: d.__setitem__("approved", True))):
            with self.subTest(label):
                d = copy.deepcopy(doc)
                edit(d)
                res = mf.verify(d, lr.path)
                self.assertFalse(res["ok"], label)
                self.assertFalse(res["seal"], label)

    def test_a_manifest_edited_and_sealed_again_still_has_to_match_what_git_holds(self):
        lr, path, doc = self.written()
        lies = {
            "a config the base never had": lambda d: d["config"].__setitem__("sha256", "3" * 64),
            "a gate mode the config never set": lambda d: d["config"]["gates"].__setitem__("g6", "on"),
            "another plan": lambda d: d["plan"].__setitem__("digest", "4" * 64),
            "another item": lambda d: d["plan"].__setitem__("item_digest", "5" * 64),
            "a pin the head does not hold": lambda d: d["toolchain"].__setitem__(".nvmrc", "18.0.0"),
            "a lockfile that is not there": lambda d: d["lockfiles"].__setitem__("yarn.lock", "6" * 40),
            "a lockfile hash that is wrong": lambda d: d["lockfiles"].__setitem__("package-lock.json", "7" * 40),
            "no evidence": lambda d: d["evidence"].__setitem__("blob", None),
            "another kit": lambda d: d["kit"].__setitem__("version", "9.9.9"),
            "a verify command that was never configured": lambda d: d["verify"].__setitem__("verify_build", {"sha256": "8" * 64, "head": "make"}),
        }
        for label, edit in lies.items():
            with self.subTest(label):
                d = copy.deepcopy(doc)
                edit(d)
                res = mf.verify(self.reseal(d), lr.path)
                self.assertTrue(res["seal"], label)                                      # a clean re-seal ...
                self.assertFalse(res["ok"], label)                                       # ... still does not match the repository
                self.assertTrue(res["facts"], label)
        # recorded-only fields can be re-sealed without git noticing: that is what --expect-hash is for
        d = copy.deepcopy(doc)
        d["gates"] = {"g6": {"status": "pass", "mode": "on", "head": doc["repo"]["head"]}}
        forged = self.reseal(d)
        self.assertTrue(mf.verify(forged, lr.path)["ok"])
        res = mf.verify(forged, lr.path, expect_hash=doc["hash"])
        self.assertFalse(res["ok"])
        self.assertFalse(res["expect"])
        self.assertTrue(mf.verify(doc, lr.path, expect_hash=doc["hash"])["expect"])

    def test_a_manifest_from_another_repository_cannot_be_rederived_here(self):
        lr, path, doc = self.written()
        d = copy.deepcopy(doc)
        d["repo"]["head"] = "9" * 40
        res = mf.verify(self.reseal(d), lr.path)
        self.assertFalse(res["ok"])
        self.assertIn("does not have commit", " ".join(res["problems"]))

    def test_the_command_reports_a_failed_check_and_the_anchor_is_this_machines_log(self):
        lr, path, doc = self.written()
        bad = copy.deepcopy(doc)
        bad["toolchain"][".nvmrc"] = "18.0.0"
        bad = self.reseal(bad)
        elsewhere = os.path.join(os.path.dirname(path), "..", "foreign.json")
        with open(elsewhere, "w") as fh:
            json.dump(bad, fh)
        r = lr.ctl("manifest", "verify", "--file", elsewhere)
        self.assertEqual(r.rc, 1, r)
        self.assertIn("DOES NOT HOLD", r.out)
        self.assertIn("DIFFERS  toolchain..nvmrc", r.out)
        self.assertIn("not in this machine's event log", r.out)
        j = lr.ctl("manifest", "verify", "--file", elsewhere, "--json").json
        self.assertFalse(j["anchored"])
        self.assertEqual(lr.ctl("manifest", "verify", "--expect-hash", "0" * 64).rc, 1)

    def test_show_find_and_the_files_it_will_read(self):
        lr, path, doc = self.written()
        self.assertIn(doc["hash"], lr.ctl("manifest", "show").out)
        self.assertIn("lockfiles", lr.ctl("manifest", "show", os.path.basename(path)).out)
        self.assertEqual(lr.ctl("manifest", "show", "--json").json["hash"], doc["hash"])
        self.assertEqual(lr.ctl("manifest", "show", "nonsense").rc, 2)                       # a stored name is plain.json; anything else needs --file
        self.assertEqual(lr.ctl("manifest", "show", "../x.json").rc, 2)
        elsewhere = os.path.join(lr.home, "m.json")
        os.symlink(path, elsewhere)
        self.assertEqual(lr.ctl("manifest", "show", "--file", elsewhere).rc, 3)             # a symlink is refused
        big = os.path.join(lr.home, "big.json")
        with open(big, "w") as fh:
            fh.write(" " * (mf.MAX_BYTES + 1))
        with self.assertRaises(Refused):
            mf.read_file(big)
        junk = os.path.join(lr.home, "junk.json")
        for text in ("{", '{"schema": "other"}', "[]", '{"schema": "%s", "schema": "%s"}' % (mf.SCHEMA, mf.SCHEMA), '{"schema": "%s", "n": NaN}' % mf.SCHEMA):
            with open(junk, "w") as fh:
                fh.write(text)
            with self.assertRaises(Refused, msg=text):
                mf.read_file(junk)
        os.unlink(path)
        self.assertEqual(lr.ctl("manifest", "show").rc, 3)                                   # nothing stored

    def test_arguments_are_validated(self):
        lr = self.build_repo(self)
        self.assertEqual(lr.ctl("manifest", "write", "ZZ").rc, 2)
        self.assertEqual(lr.ctl("manifest", "write", "A", "--head", "--x").rc, 2)
        with self.assertRaises(UsageError):
            mf.collect(lr.path, "nope", lr.repo.head(), "A")
        r = lr.ctl("manifest", "write", "--json")                                           # the whole plan, no item
        self.assertEqual(r.rc, 0, r)
        self.assertIsNone(mf.read_file(r.json["path"])["plan"]["item"])

    def test_the_hash_is_stable_for_the_same_document(self):
        lr, path, doc = self.written()
        again = mf.seal({k: v for k, v in doc.items() if k != "hash"})
        self.assertEqual(again["hash"], doc["hash"])
        self.assertEqual(mf.body_hash(doc), doc["hash"])


if __name__ == "__main__":
    unittest.main()
