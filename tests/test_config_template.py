"""The committed config templates are generated from the registry (IMP-11).

* `.claude/loop.config.md` and `examples/loop.config.md` match what `tools/gen-config.py`
  would write
* v1 round trip: the 16 v1 keys, their defaults and every human comment survive. The frozen
  v1 files in `tests/data/v1/` are the reference; only the column of a few continuation-comment
  lines differs (v1 had 33 and 34 spaces mixed; the generator uses 33)
"""
from __future__ import annotations

import os
import re
import unittest

from . import REPO_ROOT
from kitlib import config, configgen, miniyaml, registry

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "v1")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def normalize(text):
    """Collapse the indentation of comment-only lines (the one known v1 inconsistency)."""
    return "\n".join(ln.lstrip() if ln.lstrip().startswith("#") else ln for ln in text.split("\n"))


def v1_values(path):
    text = read(path)
    data = miniyaml.parse(config.extract_yaml_block(text)[0])
    return data


class GeneratedFilesInSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry.load_registry()

    def test_full_config_in_sync(self):
        want = configgen.render(self.reg, example=False)
        got = read(os.path.join(REPO_ROOT, ".claude", "loop.config.md"))
        self.assertEqual(got, want, "run: python3 tools/gen-config.py --write")

    def test_example_config_in_sync(self):
        want = configgen.render(self.reg, example=True)
        got = read(os.path.join(REPO_ROOT, "examples", "loop.config.md"))
        self.assertEqual(got, want, "run: python3 tools/gen-config.py --write")

    def test_generated_files_load_clean(self):
        for rel in (os.path.join(".claude", "loop.config.md"), os.path.join("examples", "loop.config.md")):
            cfg = config.load_file(os.path.join(REPO_ROOT, rel), self.reg)
            self.assertEqual(cfg.issues, [], rel)


class V1RoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = registry.load_registry()

    def test_v1_full_config_round_trips(self):
        v1 = v1_values(os.path.join(DATA, "loop.config.md"))
        self.assertEqual(len(v1), 16)
        defaults = self.reg.defaults()
        for name, value in v1.items():
            self.assertIn(name, self.reg, name)
            self.assertEqual(self.reg.get(name).owner_phase, "v1")
            self.assertEqual(defaults[name], value, "default of %s changed" % name)
        # same key order as the v1 file
        generated = miniyaml.parse(config.extract_yaml_block(configgen.render(self.reg))[0])
        v1_order = list(v1)
        self.assertEqual([k for k in generated if k in v1], v1_order)

    def test_v1_full_config_text_matches_apart_from_comment_column(self):
        want = normalize(read(os.path.join(DATA, "loop.config.md")))
        # later phases add keys; compare only the v1 part by rendering a registry of v1 alone
        v1_only = registry.load_files([os.path.join(registry.SCHEMA_DIR, "v1.json")])
        got = normalize(configgen.render(v1_only, example=False))
        self.assertEqual(got, want)

    def test_v1_example_config_round_trips_exactly(self):
        v1_only = registry.load_files([os.path.join(registry.SCHEMA_DIR, "v1.json")])
        self.assertEqual(configgen.render(v1_only, example=True), read(os.path.join(DATA, "loop.config.example.md")))

    def test_v1_files_load_with_current_registry_and_defaults_fill_new_keys(self):
        cfg = config.load_text(read(os.path.join(DATA, "loop.config.md")), self.reg)
        self.assertEqual(cfg.errors, [])
        self.assertTrue(cfg.is_placeholder("verify_tests"))
        self.assertEqual(cfg.get("autonomy"), "per-milestone")
        for name, value in self.reg.defaults().items():
            self.assertEqual(cfg.get(name), value if name not in cfg.explicit else cfg.explicit[name])

    def test_every_comment_line_of_v1_is_present(self):
        v1_only = registry.load_files([os.path.join(registry.SCHEMA_DIR, "v1.json")])
        got = set(l.strip() for l in configgen.render(v1_only).split("\n"))
        for line in read(os.path.join(DATA, "loop.config.md")).split("\n"):
            if line.strip().startswith("#") or "#" in line:
                self.assertIn(line.strip(), got)


class GeneratorBehaviour(unittest.TestCase):
    def _reg(self, tmp_keys):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, "x.json")
        with open(p, "w") as fh:
            json.dump({"schema_version": 1, "phase": "x",
                       "sections": [{"id": "s", "title": "Section S", "comment": ["intro line"]}],
                       "keys": tmp_keys}, fh)
        return registry.load_files([p])

    def _key(self, name, **kw):
        base = {"name": name, "type": "bool", "default": True, "owner_phase": "x", "h6_protected": False,
                "evidence_gated": False, "deprecated_by": None, "section": "s", "doc": ["does a thing"]}
        base.update(kw)
        return base

    def test_nested_dotted_keys_render_as_a_mapping(self):
        reg = self._reg([self._key("gates.g1", type="enum", choices=["on", "off"], default="on"),
                         self._key("gates.g2", type="enum", choices=["on", "off"], default="off")])
        body = configgen.render_body(reg)
        self.assertIn("gates:\n  g1: on", body)
        data = miniyaml.parse(body)
        self.assertEqual(data, {"gates": {"g1": "on", "g2": "off"}})

    def test_wildcard_keys_are_not_rendered(self):
        reg = self._reg([self._key("fam.*", type="enum", choices=["on", "off"], default="on"), self._key("plain")])
        body = configgen.render_body(reg)
        self.assertNotIn("fam", body)
        self.assertIn("plain: true", body)

    def test_value_comment_alignment_and_continuation(self):
        reg = self._reg([self._key("k", doc=["first", "second"])])
        lines = configgen.render_body(reg).split("\n")
        self.assertEqual(lines[2], "k: true" + " " * (33 - 7) + "# first")
        self.assertEqual(lines[3], " " * 33 + "# second")

    def test_noncontiguous_parent_is_an_error(self):
        reg = self._reg([self._key("a.x"), self._key("b"), self._key("a.y")])
        with self.assertRaises(registry.RegistryError):
            configgen.render_body(reg)

    def test_key_without_section_cannot_be_rendered(self):
        k = self._key("lonely")
        del k["section"]
        reg = self._reg([k])
        with self.assertRaises(registry.RegistryError):
            configgen.render_body(reg)

    def test_example_file_lists_only_keys_with_an_example(self):
        reg = self._reg([self._key("with_ex", example={"value": False, "doc": ["short"]}), self._key("without")])
        body = configgen.render_body(reg, example=True)
        self.assertIn("with_ex: false", body)
        self.assertNotIn("without", body)


if __name__ == "__main__":
    unittest.main()
