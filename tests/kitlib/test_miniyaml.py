"""miniyaml accept/reject tables (IMP-11). Rows live in tests/data/miniyaml_cases.json."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import miniyaml

CASES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "miniyaml_cases.json")


def load_cases():
    with open(CASES, "r", encoding="utf-8") as fh:
        return json.load(fh)


class AcceptRejectTables(unittest.TestCase):
    def test_accept_table(self):
        for row in load_cases()["accept"]:
            with self.subTest(row["name"]):
                self.assertEqual(miniyaml.parse(row["yaml"]), row["expect"])

    def test_reject_table_reports_line_numbers(self):
        for row in load_cases()["reject"]:
            with self.subTest(row["name"]):
                with self.assertRaises(miniyaml.YamlError) as cm:
                    miniyaml.parse(row["yaml"])
                self.assertEqual(cm.exception.lineno, row["line"], str(cm.exception))
                self.assertIn(row["contains"], cm.exception.message)
                self.assertIn("line %d:" % row["line"], str(cm.exception))


class LineOffsetAndKeyLines(unittest.TestCase):
    def test_line_offset_is_added_to_errors(self):
        with self.assertRaises(miniyaml.YamlError) as cm:
            miniyaml.parse("a: 1\na: 2\n", line_offset=10)
        self.assertEqual(cm.exception.lineno, 12)

    def test_key_lines(self):
        data, lines = miniyaml.parse_with_lines("a: 1\n\n# c\ng:\n  x: 1\n  y: 2\n", line_offset=5)
        self.assertEqual(lines[("a",)], 6)
        self.assertEqual(lines[("g",)], 9)
        self.assertEqual(lines[("g", "y")], 11)


class DumpScalar(unittest.TestCase):
    def test_round_trip(self):
        values = [True, False, None, 0, 25, -3, 1.5, "plain", "per-milestone", "<command>", "pnpm test",
                  "cp ./a /tmp/x-$ID.db", "", " lead", "trail ", "has # hash", "a: b", "ends:", "true", "null",
                  "123", "1.5", "- dash", "[x]", "{y}", "q\"uote", "back\\slash", "&anchor", "*alias", "!tag",
                  "|pipe", ">fold", "'single'", "tab\tin", "on", "~", "x#y", ["a", "b c", 1], [], {}]
        for v in values:
            with self.subTest(repr(v)):
                text = miniyaml.dump_scalar(v)
                self.assertEqual(miniyaml.parse_scalar(text), v, text)
                self.assertEqual(miniyaml.parse("k: " + text + "\n"), {"k": v}, text)

    def test_multiline_and_nonempty_mapping_are_refused(self):
        with self.assertRaises(ValueError):
            miniyaml.dump_scalar("a\nb")
        with self.assertRaises(ValueError):
            miniyaml.dump_scalar({"a": 1})


def _emit(obj, indent=0, out=None):
    """A tiny block-style dumper used only to fuzz the parser (seeded, deterministic)."""
    out = [] if out is None else out
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and v:
                out.append("%s%s:" % (pad, miniyaml.dump_scalar(k)))
                _emit(v, indent + 2, out)
            elif isinstance(v, list) and v and any(isinstance(x, (dict, list)) for x in v):
                out.append("%s%s:" % (pad, miniyaml.dump_scalar(k)))
                _emit(v, indent + 2 if (indent // 2) % 2 else indent, out)  # alternate indented / same-indent sequences
            else:
                out.append("%s%s: %s" % (pad, miniyaml.dump_scalar(k), miniyaml.dump_scalar(v)))
    else:
        for item in obj:
            if isinstance(item, dict) and item:
                sub = []
                _emit(item, indent + 2, sub)
                out.append(pad + "- " + sub[0][indent + 2:])
                out.extend(sub[1:])
            else:
                out.append("%s- %s" % (pad, miniyaml.dump_scalar(item)))
    return out


class Fuzz(unittest.TestCase):
    POOL = ["a", "b c", "x#y", "it's", "q\"q", "1", "1.5", "true", "null", "~", "<command>", "$ID", "a:b", "- x",
            "[z]", "", " pad ", "-", "dash-ed", "ünï", "tab\tx", "http://h/p?q=1#f", "k: v", "end:", "%x", "@at",
            "pnpm test", "per-milestone", "back\\slash", "on", "off"]

    def gen(self, rnd, depth):
        kind = rnd.random()
        if depth >= 3 or kind < 0.45:
            c = rnd.random()
            if c < 0.2:
                return rnd.randint(-5, 10 ** 6)
            if c < 0.3:
                return rnd.choice([True, False, None])
            if c < 0.35:
                return [rnd.choice(self.POOL) for _ in range(rnd.randint(0, 3))]
            return rnd.choice(self.POOL)
        if kind < 0.8:
            return {"k%d_%s" % (i, rnd.choice(["a", "b-c", "d_e"])): self.gen(rnd, depth + 1) for i in range(rnd.randint(1, 4))}
        items = []
        for _ in range(rnd.randint(1, 4)):
            v = self.gen(rnd, depth + 1)
            if isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
                v = "nested sequences are outside the subset"
            items.append(v)
        return items

    def test_round_trip_random_documents(self):
        import random
        rnd = random.Random(20261026)
        done = 0
        for n in range(400):
            doc = {"top%d" % i: self.gen(rnd, 0) for i in range(rnd.randint(1, 4))}
            text = "\n".join(_emit(doc)) + "\n"
            with self.subTest(n=n):
                self.assertEqual(miniyaml.parse(text), doc, text)
            done += 1
        self.assertEqual(done, 400)


class RealConfig(unittest.TestCase):
    def test_parses_the_v1_config_block(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "data", "v1", "loop.config.md"), "r") as fh:
            text = fh.read()
        start = text.index("```yaml\n") + len("```yaml\n")
        end = text.index("```", start)
        data = miniyaml.parse(text[start:end])
        self.assertEqual(data["max_tool_calls_per_run"], 600)
        self.assertEqual(data["verify_tests"], "<command>")
        self.assertIs(data["enforce_engineer_git_hook"], True)


if __name__ == "__main__":
    unittest.main()
