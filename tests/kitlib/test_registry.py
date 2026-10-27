"""Registry loader: merging, duplicates, field validation (IMP-11)."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import registry
from tests.helpers import BaseTestCase


def key(name, **kw):
    base = {"name": name, "type": "int", "default": 1, "owner_phase": "t", "h6_protected": False,
            "evidence_gated": False, "deprecated_by": None, "section": "s", "doc": ["d"]}
    base.update(kw)
    return base


class RegistryFiles(BaseTestCase):
    def write(self, d, name, keys, sections=None, **top):
        doc = {"schema_version": 1, "phase": name, "sections": sections if sections is not None else [{"id": "s", "title": "S"}],
               "keys": keys}
        doc.update(top)
        with open(os.path.join(d, name + ".json"), "w") as fh:
            json.dump(doc, fh)

    def test_real_registry_loads_and_has_the_v1_keys(self):
        reg = registry.load_registry()
        v1 = [k.name for k in reg if k.owner_phase == "v1"]
        self.assertEqual(len(v1), 16)
        self.assertIn("gates.*", reg.keys)
        for k in reg:
            self.assertTrue(k.doc or k.name.endswith(".*"), k.name)

    def test_merges_files_v1_first_then_alphabetical(self):
        d = self.tmpdir()
        self.write(d, "b", [key("b_key")])
        self.write(d, "a", [key("a_key")], sections=[{"id": "s", "title": "S"}])
        self.write(d, "v1", [key("v1_key")], sections=[{"id": "s1", "title": "S1"}])
        with self.assertRaises(registry.RegistryError):  # section "s" defined twice (a and b)
            registry.load_registry(d)
        os.unlink(os.path.join(d, "b.json"))
        self.write(d, "b", [key("b_key", section="s1")], sections=[])
        reg = registry.load_registry(d)
        self.assertEqual(reg.names(), ["v1_key", "a_key", "b_key"])

    def test_duplicate_key_names_are_rejected(self):
        d = self.tmpdir()
        self.write(d, "v1", [key("same")])
        self.write(d, "p2", [key("same")], sections=[])
        with self.assertRaises(registry.RegistryError) as cm:
            registry.load_registry(d)
        self.assertIn("duplicate key 'same'", str(cm.exception))

    def test_bad_entries_are_rejected(self):
        bad = [
            ("missing field", {k: v for k, v in key("x").items() if k != "doc"}),
            ("unknown field", key("x", surprise=1)),
            ("bad name", key("Bad-Name")),
            ("bad type", key("x", type="float")),
            ("default wrong type", key("x", default="one")),
            ("bool is not an int", key("x", default=True)),
            ("enum without choices", key("x", type="enum", default="a")),
            ("default not in choices", key("x", type="enum", choices=["a"], default="b")),
            ("choices on non-enum", key("x", choices=["a"])),
            ("h6 not bool", key("x", h6_protected="yes")),
            ("evidence_gated not bool/list", key("x", evidence_gated="yes")),
            ("empty owner", key("x", owner_phase="")),
            ("unknown section", key("x", section="nope")),
            ("deprecated_by unknown", key("x", deprecated_by="ghost")),
            ("example bad value", key("x", example={"value": "str"})),
            ("example unknown field", key("x", example={"nope": 1})),
        ]
        for label, entry in bad:
            with self.subTest(label):
                d = self.tmpdir()
                self.write(d, "v1", [entry])
                with self.assertRaises(registry.RegistryError):
                    registry.load_registry(d)

    def test_deprecated_chain_and_leaf_parent_are_rejected(self):
        d = self.tmpdir()
        self.write(d, "v1", [key("a", deprecated_by="b"), key("b", deprecated_by="c"), key("c")])
        with self.assertRaises(registry.RegistryError):
            registry.load_registry(d)
        d = self.tmpdir()
        self.write(d, "v1", [key("a"), key("a.b")])
        with self.assertRaises(registry.RegistryError):
            registry.load_registry(d)

    def test_invalid_json_and_wrong_version(self):
        d = self.tmpdir()
        with open(os.path.join(d, "v1.json"), "w") as fh:
            fh.write("{nope")
        with self.assertRaises(registry.RegistryError):
            registry.load_registry(d)
        with open(os.path.join(d, "v1.json"), "w") as fh:
            json.dump({"schema_version": 2}, fh)
        with self.assertRaises(registry.RegistryError):
            registry.load_registry(d)


class KeySemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry.load_registry()

    def test_wildcard_lookup(self):
        self.assertIsNotNone(self.reg.get("gates.g6"))
        self.assertIsNone(self.reg.get("gates"))
        self.assertIsNone(self.reg.get("gates.g6.extra"))
        self.assertTrue(self.reg.is_h6("gates.anything"))

    def test_type_checks(self):
        k = self.reg.get("max_items_per_run")
        self.assertIsNone(k.check_value(5))
        self.assertIsNotNone(k.check_value("5"))
        self.assertIsNotNone(k.check_value(True))
        self.assertIsNotNone(k.check_value(1.5))
        e = self.reg.get("autonomy")
        self.assertIsNone(e.check_value("unattended"))
        self.assertIn("per-feature", e.check_value("nope"))

    def test_evidence_gated_values(self):
        a = self.reg.get("autonomy")
        self.assertTrue(a.is_gated_value("unattended"))
        self.assertFalse(a.is_gated_value("per-feature"))
        self.assertFalse(self.reg.get("max_items_per_run").is_gated_value(1000))
        # later phases register their own gated keys (autonomy_level, parallel_mode, ...), so check membership, not equality
        self.assertIn("autonomy", self.reg.evidence_gated_keys())
        self.assertNotIn("max_items_per_run", self.reg.evidence_gated_keys())

    def test_h6_keys_include_caps_and_gate_toggles_not_db_local(self):
        h6 = self.reg.h6_keys()
        for name in ("max_items_per_run", "max_tool_calls_per_run", "security_review", "gates.*", "verify_tests"):
            self.assertIn(name, h6)
        self.assertNotIn("db_local", h6)

    def test_suggest(self):
        self.assertEqual(self.reg.suggest("max_item_per_run")[0], "max_items_per_run")
        self.assertEqual(self.reg.suggest("zzzzzzzz"), [])

    def test_gated_evidence_true_means_any_non_default(self):
        k = registry.Key(key("x", type="enum", choices=["off", "worktree"], default="off", evidence_gated=True), "t")
        self.assertTrue(k.is_gated_value("worktree"))
        self.assertFalse(k.is_gated_value("off"))


if __name__ == "__main__":
    unittest.main()
