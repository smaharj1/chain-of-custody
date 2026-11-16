"""The HCL scanner on its own: tokens, strings, heredocs, comments, the block tree, object literals and the JSON syntax."""
from __future__ import annotations

import unittest

from infra import hcl


def kinds(src):
    return [(t.kind, t.val) for t in hcl.tokenize(src) if t.kind not in ("NL", "EOF")]


def tree(src):
    return list(hcl.walk_blocks(hcl.parse_native(src)))


class Tokens(unittest.TestCase):
    def test_comments_are_dropped(self):
        self.assertEqual(kinds("a = 1 # x\nb = 2 // y\n/* z */ c = 3"), [("ID", "a"), ("P", "="), ("NUM", "1"), ("ID", "b"), ("P", "="), ("NUM", "2"), ("ID", "c"), ("P", "="), ("NUM", "3")])

    def test_line_numbers_follow_every_kind_of_break(self):
        toks = hcl.tokenize('a = 1\r\nb = "x"\n/* two\nlines */\nc = 3\rd = 4')
        lines = {t.val: t.line for t in toks if t.kind == "ID"}
        self.assertEqual(lines, {"a": 1, "b": 2, "c": 5, "d": 6})

    def test_string_literals_and_escapes(self):
        toks = [t for t in hcl.tokenize('"a\\nb" "q\\"q" "\\u0041" "\\U0001F600" "$${x}" "%%{x}"') if t.kind == "STR"]
        self.assertEqual([t.val for t in toks], ["a\nb", 'q"q', "A", "\U0001F600", "${x}" if False else None, "%{x}" if False else None])

    def test_interpolation_makes_a_string_non_literal(self):
        for src in ('"a${b}c"', '"%{ if x }y%{ endif }"', '"${ "}" }"', '"${ {a = "}"}.a }"'):
            with self.subTest(src):
                self.assertIsNone([t for t in hcl.tokenize(src) if t.kind == "STR"][0].val)

    def test_escaped_interpolation_marker_is_not_literal_for_policy_purposes(self):
        # `$${` decodes to the text `${`; the scanner refuses to call any string containing that text a literal
        self.assertIsNone([t for t in hcl.tokenize('"$${x}"') if t.kind == "STR"][0].val)

    def test_heredocs(self):
        toks = hcl.tokenize("a = <<EOT\nline one\nline two\nEOT\nb = 1\n")
        here = [t for t in toks if t.kind == "HERE"][0]
        self.assertEqual(here.val, "line one\nline two")
        self.assertEqual([t.val for t in toks if t.kind == "ID"], ["a", "b"])
        toks = hcl.tokenize("a = <<-EOT\n    x\n    EOT\n")
        self.assertEqual([t for t in toks if t.kind == "HERE"][0].val, "    x")
        self.assertIsNone([t for t in hcl.tokenize("a = <<EOT\nhi ${x}\nEOT\n") if t.kind == "HERE"][0].val)

    def test_identifiers_numbers_and_operators(self):
        self.assertEqual(kinds("a-b_c1 == 1.5e3 != x => ..."), [("ID", "a-b_c1"), ("P", "=="), ("NUM", "1.5e3"), ("P", "!="), ("ID", "x"), ("P", "=>"), ("P", "...")])

    def test_errors(self):
        for src in ('"open', "/* open", "a = <<EOT\nnever", "a = <<", "a = @", 'a = "x${ y"', 'a = "\\q"', "a = `x`", 'a = "x\ny"'):
            with self.subTest(src):
                with self.assertRaises(hcl.HclError):
                    hcl.tokenize(src)

    def test_error_carries_a_line_number(self):
        with self.assertRaises(hcl.HclError) as cm:
            hcl.tokenize("a = 1\nb = 2\nc = @\n")
        self.assertEqual(cm.exception.line, 3)


class Parser(unittest.TestCase):
    def test_nested_blocks_and_labels(self):
        blocks = tree('resource "aws_instance" "x" {\n  ami = "a"\n  lifecycle {\n    create_before_destroy = true\n  }\n}\n')
        self.assertEqual([(b.type, b.labels) for b in blocks], [("resource", ["aws_instance", "x"]), ("lifecycle", [])])

    def test_attribute_values_with_nested_structures_span_lines(self):
        src = 'locals {\n  a = {\n    b = [1,\n      2]\n    c = f(\n      "x",\n    )\n  }\n}\nresource "t" "n" {}\n'
        self.assertEqual([b.type for b in tree(src)], ["locals", "resource"])

    def test_one_line_blocks(self):
        src = 'a { b = 1 }\nc "d" { e { f = 2 } }\n'
        self.assertEqual([b.type for b in tree(src)], ["a", "c", "e"])

    def test_bare_identifier_labels(self):
        self.assertEqual(tree("resource aws_instance web {\n}\n")[0].labels, ["aws_instance", "web"])

    def test_errors(self):
        for src in ("a {", "}", "a = {", "a = )", 'a "b"\n{\n}\n', "a { } b { }", 'a "x${y}" {}', "= 1", "1 = 2", "a b c"):
            with self.subTest(src):
                with self.assertRaises(hcl.HclError):
                    hcl.parse_native(src)

    def test_depth_limit(self):
        with self.assertRaises(hcl.HclError):
            hcl.parse_native("a {\n" * (hcl.MAX_DEPTH + 5) + "}\n" * (hcl.MAX_DEPTH + 5))

    def test_an_empty_file_and_a_comment_only_file(self):
        self.assertEqual(hcl.parse_native(""), [])
        self.assertEqual(hcl.parse_native("# nothing\n/* here */\n"), [])


class Values(unittest.TestCase):
    def attr(self, src, name="x"):
        return [i for i in hcl.parse_native(src) if isinstance(i, hcl.Attr) and i.name == name][0]

    def test_literal_of(self):
        self.assertEqual(hcl.literal_of(self.attr('x = "hello"\n')), "hello")
        self.assertIsNone(hcl.literal_of(self.attr('x = "a${b}"\n')))
        self.assertIsNone(hcl.literal_of(self.attr("x = var.y\n")))
        self.assertIsNone(hcl.literal_of(self.attr('x = ("a")\n')))
        self.assertIsNone(hcl.literal_of(self.attr('x = "a" "b"\n')))
        self.assertEqual(hcl.literal_of(self.attr("x = <<EOT\nhi\nEOT\n")), "hi")

    def test_object_of(self):
        obj = hcl.object_of(self.attr('x = {\n  source = "hashicorp/aws"\n  version = "~> 5.0"\n  nested = { a = "b" }\n  expr = var.y\n}\n'))
        self.assertEqual(obj, {"source": "hashicorp/aws", "version": "~> 5.0", "nested": {"a": "b"}, "expr": None})
        self.assertEqual(hcl.object_of(self.attr('x = { "quoted-key" = "v", other: "w" }\n')), {"quoted-key": "v", "other": "w"})
        self.assertIsNone(hcl.object_of(self.attr('x = "str"\n')))
        self.assertIsNone(hcl.object_of(self.attr("x = {a = 1} + {b = 2}\n")))

    def test_names_in_finds_nested_names_and_object_keys(self):
        blk = hcl.parse_native('provider "aws" {\n  a = 1\n  inner {\n    b = { c = 2 }\n  }\n}\n')[0]
        self.assertEqual(sorted(n for n, _ in hcl.names_in(blk)), ["a", "b", "c", "inner"])


class JsonSyntax(unittest.TestCase):
    def test_labelled_blocks_and_attributes(self):
        items = hcl.parse_json('{"resource": {"aws_instance": {"x": {"ami": "a", "provisioner": [{"local-exec": {"command": "id"}}]}}}, "module": {"m": {"source": "./x"}}}')
        blocks = list(hcl.walk_blocks(items))
        self.assertIn(("resource", ["aws_instance", "x"]), [(b.type, b.labels) for b in blocks])
        self.assertIn(("provisioner", ["local-exec"]), [(b.type, b.labels) for b in blocks])
        mod = [b for b in blocks if b.type == "module"][0]
        self.assertEqual(hcl.literal_of(mod.attrs("source")[0]), "./x")

    def test_interpolated_strings_are_not_literals(self):
        items = hcl.parse_json('{"module": {"m": {"source": "./${var.x}"}}}')
        mod = [b for b in hcl.walk_blocks(items) if b.type == "module"][0]
        self.assertIsNone(hcl.literal_of(mod.attrs("source")[0]))

    def test_errors(self):
        for text in ("{", "[]", '{"a": 1, "a": 2}', '{"a": [{"b": 1}, "x"]}', "nope"):
            with self.subTest(text):
                with self.assertRaises(hcl.HclError):
                    hcl.parse_json(text)

    def test_dispatch_on_file_name(self):
        self.assertEqual(len(hcl.parse_file_text('{"locals": {"a": 1}}', "x.TF.JSON")), 2)
        self.assertEqual(len(hcl.parse_file_text("a = 1\n", "x.tf")), 1)


if __name__ == "__main__":
    unittest.main()
