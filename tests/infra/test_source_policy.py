"""The pre-plan source policy (report §8.1 Layer 5, RC-3, IMP-25): each ban, the bypass attempts against it, and the fixture repos."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from infra import source_policy

from tests import REPO_ROOT

from .helpers import BASE_FILES, FIXTURES, SOURCE_POLICY, TreeCase, write_tree

RES = 'resource "aws_instance" "x" {\n  ami = "ami-1"\n%s}\n'
LOCAL_EXEC = '  provisioner "local-exec" {\n    command = "id"\n  }\n'


def ex(text):
    return {"infra/extra.tf": text}


# (name, extra.tf text, exact set of error rules). These are the bypass attempts: every row either must be caught
# (the rule is listed) or must NOT be flagged (a ban word inside a comment, a string or a heredoc).
HCL_ROWS = [
    # --- the plain bans ---
    ("external data", 'data "external" "e" {\n  program = ["sh", "-c", "id"]\n}\n', {"data.external", "provider.banned"}),
    ("http data", 'data "http" "h" {\n  url = "https://example.com"\n}\n', {"data.http", "provider.banned"}),
    ("local-exec", RES % LOCAL_EXEC, {"provisioner.local-exec"}),
    ("remote-exec", RES % '  provisioner "remote-exec" {\n    inline = ["id"]\n  }\n', {"provisioner.remote-exec"}),
    ("file provisioner", RES % '  provisioner "file" {\n    source = "a"\n    destination = "b"\n  }\n', {"provisioner.other"}),
    ("provisioner in a module-call-free null resource still needs a new provider",
     'resource "null_resource" "n" {\n  provisioner "local-exec" {\n    command = "id"\n  }\n}\n', {"provisioner.local-exec", "provider.new"}),
    # --- odd spacing and syntax that still means the same thing ---
    ("no spaces around labels", 'data"external""e"{program=["x"]}\n', {"data.external", "provider.banned"}),
    ("tabs between tokens", 'data\t"external"\t"e"\t{\n\tprogram\t=\t["x"]\n}\n', {"data.external", "provider.banned"}),
    ("bare identifier labels", 'data external e {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("bare provisioner label", RES % '  provisioner local-exec {\n    command = "id"\n  }\n', {"provisioner.local-exec"}),
    ("one-line resource with a one-line provisioner", 'resource "aws_instance" "x" { provisioner "local-exec" { command = "id" } }\n', {"provisioner.local-exec"}),
    ("a one-line nested block that is not a provisioner", RES % '  lifecycle { create_before_destroy = true }\n', set()),
    ("dynamic provisioner", RES % '  dynamic "provisioner" {\n    for_each = [1]\n    content {}\n  }\n', {"provisioner.other"}),
    ("CRLF line endings", 'data "external" "e" {\r\n  program = ["x"]\r\n}\r\n', {"data.external", "provider.banned"}),
    ("CR-only line endings", 'x = 1\rdata "external" "e" {\r  program = ["x"]\r}\r', {"data.external", "provider.banned"}),
    ("byte order mark", '\ufeffdata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("unicode line separator ends a comment", '# note\u2028data "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("vertical tab ends a comment", '# note\x0bdata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    # --- comments hide nothing real and reveal nothing fake ---
    ("hash comment", '# data "external" "e" {}\n# provisioner "local-exec" {}\n', set()),
    ("slash comment", '// data "external" "e" {}\n', set()),
    ("block comment", '/* data "external" "e" {\n   provisioner "local-exec" {}\n*/\n', set()),
    ("code after a block comment on the same line", '/* harmless */ data "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("code after a multi-line block comment", '/* a\n b */ data "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("comment markers inside strings do not start comments",
     'locals {\n  a = "# not a comment"\n  b = "/* not a comment"\n  c = "https://example.com"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("block-comment opener in a string then real code then a closer in another string",
     'locals {\n  a = "/*"\n}\ndata "external" "e" {\n  program = ["x"]\n}\nlocals {\n  b = "*/"\n}\n', {"data.external", "provider.banned"}),
    ("escaped quote does not end the string",
     'locals {\n  a = "say \\" # still inside"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("escaped backslash then quote ends the string",
     'locals {\n  a = "end\\\\"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("trailing comment after an attribute", 'locals {\n  a = 1 # data "external" "e" {}\n}\n', set()),
    # --- strings, interpolation and templates ---
    ("interpolation with a nested quoted brace",
     'locals {\n  a = "x${ lookup({ "}" = 1 }, "}", "d") }y"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("escaped interpolation", 'locals {\n  a = "$${ not interpolation"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("template directive", 'locals {\n  a = "%{ if true }a%{ else }b%{ endif }"\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("unicode escape in a label", 'data "\\u0065xternal" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("a label that only contains the word", 'resource "aws_sns_topic" "external" {\n  name = "external"\n}\n', set()),
    ("ban words in ordinary strings", 'locals {\n  a = "data external local-exec provisioner"\n}\n', set()),
    # --- heredocs ---
    ("ban words in a heredoc", 'locals {\n  a = <<EOT\ndata "external" "e" {}\nprovisioner "local-exec" {}\nEOT\n}\n', set()),
    ("indented heredoc", 'locals {\n  a = <<-EOT\n    data "external" "e" {}\n    EOT\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("heredoc marker prefix is not the terminator", 'locals {\n  a = <<EOT\nEOTX\ndata "external" "e" {}\nEOT\n}\n', set()),
    ("code right after a heredoc", 'locals {\n  a = <<EOT\ntext\nEOT\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("heredoc with braces and quotes in it", 'locals {\n  a = <<EOT\n}}} " \' /* \nEOT\n}\ndata "external" "e" {\n  program = ["x"]\n}\n', {"data.external", "provider.banned"}),
    ("heredoc as a function argument", 'locals {\n  a = trimspace(<<EOT\ntext\nEOT\n  )\n}\nresource "aws_instance" "x" {\n  provisioner "local-exec" {\n    command = "id"\n  }\n}\n', {"provisioner.local-exec"}),
    # --- fail closed ---
    ("unterminated string", 'locals {\n  a = "oops\n}\n', {"parse.unparsed"}),
    ("unterminated block comment", '/* never closed\ndata "external" "e" {}\n', {"parse.unparsed"}),
    ("unterminated heredoc", 'locals {\n  a = <<EOT\ntext\n', {"parse.unparsed"}),
    ("unterminated block", 'resource "aws_instance" "x" {\n', {"parse.unparsed"}),
    ("stray closing brace", '}\n', {"parse.unparsed"}),
    ("comment inside an interpolation", 'locals {\n  a = "x${ foo # }\n  }"\n}\n', {"parse.unparsed"}),
    ("heredoc inside an interpolation", 'locals {\n  a = "x${ <<EOT\n  }\nEOT\n }"\n}\n', {"parse.unparsed"}),
    ("brace on the next line", 'data "external" "e"\n{\n  program = ["x"]\n}\n', {"parse.unparsed"}),
    ("two statements on one line", 'locals { a = 1 } locals { b = 2 }\n', {"parse.unparsed"}),
    ("unexpected character", 'locals {\n  a = 1;\n}\n', {"parse.unparsed"}),
    ("non-breaking space as whitespace", 'locals {\n  a\u00a0= 1\n}\n', {"parse.unparsed"}),
    ("NUL byte", 'locals {\n  a = 1\n}\x00\n', {"parse.unparsed"}),
    ("interpolated block label", 'data "ext${var.x}" "e" {}\n', {"parse.unparsed"}),
    ("bad escape", 'locals {\n  a = "\\q"\n}\n', {"parse.unparsed"}),
    ("unbalanced bracket in an expression", 'locals {\n  a = [1, 2\n}\n', {"parse.unparsed"}),
    ("invalid unicode escape", 'locals {\n  a = "\\u12"\n}\n', {"parse.unparsed"}),
    ("nesting too deep", "a {\n" * 80 + "}\n" * 80, {"parse.unparsed"}),
    # --- endpoints, proxies, exec ---
    ("provider endpoints block", 'provider "aws" {\n  alias = "w"\n  endpoints {\n    s3 = "https://evil.example"\n  }\n}\n', {"provider.endpoint-override"}),
    ("provider endpoint attribute", 'provider "aws" {\n  alias = "w"\n  sts_endpoint = "https://evil.example"\n}\n', {"provider.endpoint-override"}),
    ("provider proxy", 'provider "aws" {\n  alias = "w"\n  http_proxy = "http://evil.example:3128"\n}\n', {"provider.endpoint-override"}),
    ("provider endpoint through an object constructor", 'provider "aws" {\n  alias = "w"\n  x = { endpoints = "e" }\n}\n', {"provider.endpoint-override"}),
    ("backend endpoint", 'terraform {\n  backend "s3" {\n    bucket = "acme-state"\n    key = "main.tfstate"\n    region = "us-east-1"\n    endpoint = "https://evil.example"\n  }\n}\n', {"provider.endpoint-override"}),
    ("remote state endpoint", 'data "terraform_remote_state" "o" {\n  backend = "s3"\n  config = {\n    bucket = "b"\n    endpoints = { s3 = "https://evil.example" }\n  }\n}\n', {"provider.endpoint-override"}),
    ("use_fips_endpoint is not an override", 'provider "aws" {\n  alias = "w"\n  use_fips_endpoint = true\n}\n', set()),
    ("a resource attribute named endpoint is fine", 'resource "aws_sns_topic_subscription" "s" {\n  topic_arn = "t"\n  protocol = "https"\n  endpoint = "https://example.com/hook"\n}\n', set()),
    ("kubernetes provider exec", 'provider "kubernetes" {\n  exec {\n    api_version = "client.authentication.k8s.io/v1"\n    command = "aws"\n  }\n}\n', {"provider.exec", "provider.new"}),
    # --- providers ---
    ("a new declared provider", 'terraform {\n  required_providers {\n    datadog = { source = "datadog/datadog" }\n  }\n}\n', {"provider.new"}),
    ("an implied provider from a resource type", 'resource "random_pet" "p" {}\n', {"provider.new"}),
    ("an implied provider from a provider block", 'provider "tls" {}\n', {"provider.new"}),
    ("a provider named by the meta-argument", 'terraform {\n  required_providers {\n    evil = { source = "x/evil" }\n  }\n}\nresource "aws_instance" "x" {\n  provider = evil.west\n}\n', {"provider.new"}),
    ("a new provider declared over several lines with no commas", 'terraform {\n  required_providers {\n    datadog = {\n      source  = "datadog/datadog"\n      version = "3.0.0"\n    }\n  }\n}\n', {"provider.new"}),
    ("a decoy source in a comment above the real one", 'terraform {\n  required_providers {\n    datadog = {\n      # source = "hashicorp/aws"\n      source = "datadog/datadog"\n    }\n  }\n}\n', {"provider.new"}),
    ("a decoy source in a string above the real one", 'terraform {\n  required_providers {\n    datadog = {\n      version = "source = hashicorp/aws"\n      source = "datadog/datadog"\n    }\n  }\n}\n', {"provider.new"}),
    ("a quoted object key", 'terraform {\n  required_providers {\n    datadog = {\n      "source" = "datadog/datadog"\n    }\n  }\n}\n', {"provider.new"}),
    ("a multi-line declaration of a known provider", 'terraform {\n  required_providers {\n    aws = {\n      source  = "hashicorp/aws"\n      version = ">= 5.40"\n    }\n  }\n}\n', set()),
    ("a repeated source key in the object", 'terraform {\n  required_providers {\n    aws = {\n      source = "hashicorp/aws"\n      source = "evil/aws"\n    }\n  }\n}\n', {"provider.non-literal-source"}),
    ("a changed source for an existing local name", 'terraform {\n  required_providers {\n    aws = { source = "evil/aws" }\n  }\n}\n', {"provider.new"}),
    ("a different registry host for the same name", 'terraform {\n  required_providers {\n    aws = { source = "example.com/hashicorp/aws" }\n  }\n}\n', {"provider.new"}),
    ("the same provider, different case and full address", 'terraform {\n  required_providers {\n    aws2 = { source = "Registry.Terraform.io/HashiCorp/AWS" }\n  }\n}\n', set()),
    ("a version change is not a new provider", 'terraform {\n  required_providers {\n    aws = { source = "hashicorp/aws", version = "~> 6.0" }\n  }\n}\n', set()),
    ("external provider by alias", 'terraform {\n  required_providers {\n    ext = { source = "hashicorp/external" }\n  }\n}\n', {"provider.banned"}),
    ("http provider by an unusual namespace", 'terraform {\n  required_providers {\n    h = { source = "someone/http" }\n  }\n}\n', {"provider.banned"}),
    ("non-literal provider source", 'terraform {\n  required_providers {\n    x = { source = var.src }\n  }\n}\n', {"provider.non-literal-source"}),
    ("legacy shorthand for a new provider", 'terraform {\n  required_providers {\n    random = ">= 3.0"\n  }\n}\n', {"provider.new"}),
    # --- reading files the plan job's credentials live near ---
    ("file() of an absolute path", 'locals {\n  a = file("/proc/self/environ")\n}\n', {"expr.file-outside-tree"}),
    ("file() of the home directory", 'locals {\n  a = filebase64("~/.aws/credentials")\n}\n', {"expr.file-outside-tree"}),
    ("file() that climbs out", 'locals {\n  a = file("../../.git/config")\n}\n', {"expr.file-outside-tree"}),
    ("file() that climbs out through path.module", 'locals {\n  a = file("${path.module}/../../../etc/passwd")\n}\n', {"expr.file-outside-tree"}),
    ("templatefile() of an absolute path", 'locals {\n  a = templatefile("/proc/self/environ", {})\n}\n', {"expr.file-outside-tree"}),
    ("file() inside the module is fine", 'locals {\n  a = file("${path.module}/policy.json")\n  b = file("policy.json")\n  c = templatefile("${path.module}/t/x.tpl", { n = 1 })\n}\n', set()),
    ("file() with a computed path is a warning, not an error", 'locals {\n  a = file(var.p)\n  b = file("${path.module}/${var.n}.json")\n}\n', set()),
    ("file() nested in another call", 'locals {\n  a = jsondecode(base64decode(file("/etc/shadow")))\n}\n', {"expr.file-outside-tree"}),
    ("local_file data source outside the tree", 'data "local_file" "f" {\n  filename = "/proc/self/environ"\n}\n', {"expr.file-outside-tree", "provider.new"}),
    ("a function named file that is not a call", 'locals {\n  file = "/etc/passwd"\n}\n', set()),
    ("remote state over http", 'data "terraform_remote_state" "o" {\n  backend = "http"\n  config = {\n    address = "https://example.invalid/state"\n  }\n}\n', {"data.remote-state-http"}),
    ("remote state with a computed backend", 'data "terraform_remote_state" "o" {\n  backend = var.b\n}\n', {"data.remote-state-http"}),
    ("remote state over s3 is fine", 'data "terraform_remote_state" "o" {\n  backend = "s3"\n  config = {\n    bucket = "acme-state"\n    key    = "other.tfstate"\n    region = "us-east-1"\n  }\n}\n', set()),
    ("a data source inside a check block", 'check "c" {\n  data "http" "h" {\n    url = "https://example.com"\n  }\n}\n', {"data.http", "provider.banned"}),
    # --- backends ---
    ("a new backend type", 'terraform {\n  backend "http" {\n    address = "https://state.example"\n  }\n}\n', {"backend.changed"}),
    ("terraform cloud block", 'terraform {\n  cloud {\n    organization = "acme"\n  }\n}\n', {"backend.changed"}),
]


class HclRows(TreeCase):
    def test_every_row(self):
        for name, text, expected in HCL_ROWS:
            with self.subTest(name):
                got = self.errors(self.run_policy(ex(text)))
                self.assertEqual(set(got), expected, "%s: %s" % (name, [str(f) for f in self.run_policy(ex(text))]))

    def test_the_base_tree_alone_is_clean(self):
        self.assertEqual(self.errors(self.run_policy({})), [])


DECOYS = [
    '# provisioner "local-exec" {}\n',
    '// provisioner "local-exec" {}\n',
    '/* provisioner "local-exec" {\n} */\n',
    'locals {\n  a = "provisioner \\"local-exec\\" {"\n}\n',
    'locals {\n  a = <<EOT\nprovisioner "local-exec" {}\nEOT\n}\n',
    'locals {\n  a = <<-EOT\n    provisioner "local-exec" {\n    EOT\n}\n',
    'locals {\n  a = "/* "\n}\nlocals {\n  b = " */"\n}\n',
    'locals {\n  a = "${ lookup({ "}" = "provisioner" }, "}", "d") }"\n}\n',
    'locals {\n  a = "$${ provisioner \\"local-exec\\" }"\n}\n',
    'locals {\n  # provisioner "local-exec" {}\n  a = 1 /* provisioner "local-exec" {} */\n}\n',
    'resource "aws_sns_topic" "provisioner" {\n  name = "local-exec"\n}\n',
]
REAL = 'resource "aws_instance" "r%d" {\n  ami = "a"\n  provisioner "local-exec" {\n    command = "id"\n  }\n}\n'


class Generated(TreeCase):
    def test_exactly_the_real_provisioners_are_found_among_decoys(self):
        import random
        rng = random.Random(20261026)
        for trial in range(60):
            pieces = [rng.choice(DECOYS) for _ in range(rng.randint(3, 9))]
            reals = rng.randint(0, 3)
            for k in range(reals):
                pieces.insert(rng.randint(0, len(pieces)), REAL % k)
            text = "".join(pieces)
            got = [f for f in self.run_policy(ex(text)) if f.rule.startswith("provisioner.")]
            self.assertEqual(len(got), reals, "trial %d:\n%s" % (trial, text))


MODULE_ROWS = [
    # (name, module body, error rules)
    ("registry without a version", 'source = "terraform-aws-modules/s3-bucket/aws"', {"module.registry-no-version"}),
    ("registry exact version", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = "4.1.2"', set()),
    ("registry exact version with equals", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = "= 4.1.2"', set()),
    ("registry pessimistic range", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = "~> 4.1"', {"module.registry-range"}),
    ("registry open range", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = ">= 4.0, < 5.0"', {"module.registry-range"}),
    ("registry two-part version", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = "4.1"', {"module.registry-range"}),
    ("registry version from a variable", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = var.v', {"module.registry-nonliteral"}),
    ("registry with a host", 'source = "app.terraform.io/acme/thing/aws"\n  version = "1.0.0"', set()),
    ("registry with a host and no version", 'source = "app.terraform.io/acme/thing/aws"', {"module.registry-no-version"}),
    ("registry subdirectory", 'source = "terraform-aws-modules/s3-bucket/aws//modules/object"\n  version = "4.1.2"', set()),
    ("registry source with a query", 'source = "terraform-aws-modules/s3-bucket/aws?ref=x"\n  version = "4.1.2"', {"module.source-unknown"}),
    ("git with a full sha", 'source = "git::https://example.com/acme/mod.git?ref=0123456789abcdef0123456789abcdef01234567"', set()),
    ("git with an upper-case sha", 'source = "git::https://example.com/acme/mod.git?ref=0123456789ABCDEF0123456789ABCDEF01234567"', set()),
    ("git with a subdirectory and a sha", 'source = "git::https://example.com/acme/mod.git//sub/dir?ref=0123456789abcdef0123456789abcdef01234567"', set()),
    ("git with depth and a sha", 'source = "git::https://example.com/acme/mod.git?depth=1&ref=0123456789abcdef0123456789abcdef01234567"', set()),
    ("git with no ref", 'source = "git::https://example.com/acme/mod.git"', {"module.git-unpinned"}),
    ("git with a branch", 'source = "git::https://example.com/acme/mod.git?ref=main"', {"module.git-tag"}),
    ("git with a tag", 'source = "git::https://example.com/acme/mod.git?ref=v1.2.3"', {"module.git-tag"}),
    ("git with a short sha", 'source = "git::https://example.com/acme/mod.git?ref=0123456"', {"module.git-tag"}),
    ("git with a 39 character sha", 'source = "git::https://example.com/acme/mod.git?ref=0123456789abcdef0123456789abcdef0123456"', {"module.git-tag"}),
    ("git with an upper-case parameter name", 'source = "git::https://example.com/acme/mod.git?REF=0123456789abcdef0123456789abcdef01234567"', {"module.git-unpinned"}),
    ("git with two refs", 'source = "git::https://example.com/acme/mod.git?ref=0123456789abcdef0123456789abcdef01234567&ref=main"', {"module.git-unpinned"}),
    ("git with an encoded sha", 'source = "git::https://example.com/acme/mod.git?ref=%30123456789abcdef0123456789abcdef01234567"', set()),
    ("git ssh form", 'source = "git@github.com:acme/mod.git?ref=0123456789abcdef0123456789abcdef01234567"', set()),
    ("git ssh form without a ref", 'source = "git@github.com:acme/mod.git"', {"module.git-unpinned"}),
    ("github shorthand without a ref", 'source = "github.com/acme/mod"', {"module.git-unpinned"}),
    ("github shorthand with a sha", 'source = "github.com/acme/mod?ref=0123456789abcdef0123456789abcdef01234567"', set()),
    ("github shorthand with a branch", 'source = "github.com/acme/mod?ref=HEAD"', {"module.git-tag"}),
    ("http archive", 'source = "https://example.com/mod.zip"', {"module.archive-unpinned"}),
    ("http archive with a checksum", 'source = "https://example.com/mod.zip?checksum=sha256:' + "a" * 64 + '"', set()),
    ("http archive with a weak checksum", 'source = "https://example.com/mod.zip?checksum=md5:' + "a" * 32 + '"', {"module.archive-unpinned"}),
    ("forced s3 archive", 'source = "s3::https://s3.amazonaws.com/bucket/mod.zip"', {"module.archive-unpinned"}),
    ("mercurial without a revision", 'source = "hg::https://example.com/mod"', {"module.archive-unpinned"}),
    ("file scheme", 'source = "file:///etc"', {"module.local-escape"}),
    ("forced file", 'source = "file::/etc"', {"module.local-escape"}),
    ("unknown forced scheme", 'source = "evil::https://example.com/x"', {"module.source-unknown"}),
    ("absolute path", 'source = "/opt/modules/x"', {"module.source-unknown"}),
    ("bare relative path", 'source = "modules/thing"', {"module.source-unknown"}),
    ("dot only", 'source = "."', {"module.source-unknown"}),
    ("local inside the tree", 'source = "./modules/thing"', set()),
    ("local that goes up and back down", 'source = "./modules/../modules/thing"', set()),
    ("local that leaves the tree", 'source = "../../../outside"', {"module.local-escape"}),
    ("local that leaves through a nested path", 'source = "./modules/../../../../outside"', {"module.local-escape"}),
    ("variable source", 'source = var.src', {"module.source-nonliteral"}),
    ("interpolated source", 'source = "git::https://example.com/acme/mod.git?ref=${var.ref}"', {"module.source-nonliteral"}),
    ("heredoc source with a branch ref", 'source = <<EOT\ngit::https://example.com/acme/mod.git?ref=main\nEOT', {"module.git-tag"}),
    ("parenthesised literal", 'source = ("./modules/thing")', {"module.source-nonliteral"}),
    ("conditional source", 'source = true ? "./a" : "./b"', {"module.source-nonliteral"}),
    ("padded source", 'source = " ./modules/thing"', {"module.source-unknown"}),
    ("no source at all", 'version = "1.0.0"', {"module.source-nonliteral"}),
    ("source given twice, harmless first", 'source = "./modules/thing"\n  source = "git::https://example.com/acme/mod.git"', {"module.source-nonliteral"}),
    ("source given twice, harmless last", 'source = "git::https://example.com/acme/mod.git"\n  source = "./modules/thing"', {"module.source-nonliteral"}),
    ("version given twice", 'source = "terraform-aws-modules/s3-bucket/aws"\n  version = "4.1.2"\n  version = ">= 1"', {"module.registry-nonliteral"}),
]


class ModuleRows(TreeCase):
    def test_every_row(self):
        for name, body, expected in MODULE_ROWS:
            with self.subTest(name):
                text = 'module "m" {\n  %s\n}\n' % body
                got = self.errors(self.run_policy(ex(text)))
                self.assertEqual(set(got), expected, [str(f) for f in self.run_policy(ex(text))])

    def test_a_local_path_is_relative_to_the_calling_file(self):
        # infra/sub/main.tf calling ../thing resolves to infra/thing (inside), ../../x resolves to x at the root (still inside), ../../../x leaves.
        ok = self.run_policy({"infra/sub/main.tf": 'module "a" {\n  source = "../modules/thing"\n}\nmodule "b" {\n  source = "../../x"\n}\n'})
        self.assertEqual(self.errors(ok), [])
        bad = self.run_policy({"infra/sub/main.tf": 'module "a" {\n  source = "../../../x"\n}\n'})
        self.assertEqual(self.errors(bad), ["module.local-escape"])

    def test_the_policy_file_can_allow_ranges_and_tags_but_nothing_else(self):
        text = ex('module "a" {\n  source = "terraform-aws-modules/s3-bucket/aws"\n  version = "~> 4.1"\n}\nmodule "b" {\n  source = "git::https://example.com/acme/mod.git?ref=v1.0.0"\n}\nmodule "c" {\n  source = "git::https://example.com/acme/mod.git"\n}\n')
        self.assertEqual(self.errors(self.run_policy(text)), ["module.git-tag", "module.git-unpinned", "module.registry-range"])
        got = self.run_policy(text, policy={"schema": "infra-policy.v1", "allow_version_ranges": True, "allow_git_tag_refs": True})
        self.assertEqual(self.errors(got), ["module.git-unpinned"])

    def test_a_module_in_json_syntax_is_judged_the_same(self):
        doc = {"module": {"m": {"source": "git::https://example.com/acme/mod.git?ref=main"}, "n": {"source": "./modules/thing"}}}
        got = self.run_policy({"infra/mods.tf.json": json.dumps(doc)})
        self.assertEqual(self.errors(got), ["module.git-tag"])


class JsonSyntax(TreeCase):
    def run_json(self, doc, name="infra/evil.tf.json"):
        return self.run_policy({name: json.dumps(doc) if not isinstance(doc, str) else doc})

    def test_provisioner_forms(self):
        for name, doc in (
            ("list of objects", {"resource": {"aws_instance": {"x": {"ami": "a", "provisioner": [{"local-exec": {"command": "id"}}]}}}}),
            ("single object", {"resource": {"aws_instance": {"x": {"provisioner": {"local-exec": {"command": "id"}}}}}}),
            ("resource as a list", {"resource": [{"aws_instance": {"x": {"provisioner": [{"local-exec": {"command": "id"}}]}}}]}),
        ):
            with self.subTest(name):
                self.assertEqual(self.errors(self.run_json(doc)), ["provisioner.local-exec"])

    def test_remote_exec_and_unknown_provisioners(self):
        doc = {"resource": {"aws_instance": {"x": {"provisioner": [{"remote-exec": {"inline": ["id"]}}, {"chef": {}}]}}}}
        self.assertEqual(self.errors(self.run_json(doc)), ["provisioner.other", "provisioner.remote-exec"])

    def test_banned_data_sources(self):
        doc = {"data": {"external": {"e": {"program": ["sh"]}}, "http": {"h": {"url": "https://example.com"}}}}
        self.assertEqual(self.errors(self.run_json(doc)), ["data.external", "data.http", "provider.banned"])

    def test_required_providers_and_backends(self):
        doc = {"terraform": {"required_providers": {"datadog": {"source": "datadog/datadog"}}, "backend": {"http": {"address": "https://x"}}}}
        self.assertEqual(self.errors(self.run_json(doc)), ["backend.changed", "provider.new"])

    def test_provider_endpoints(self):
        doc = {"provider": {"aws": [{"alias": "w", "endpoints": {"s3": "https://evil.example"}}]}}
        self.assertEqual(self.errors(self.run_json(doc)), ["provider.endpoint-override"])

    def test_a_comment_key_is_ignored(self):
        self.assertEqual(self.errors(self.run_json({"//": "provisioner local-exec data external", "locals": {"a": 1}})), [])

    def test_fail_closed(self):
        for name, text in (
            ("not json", "{ nope"),
            ("top level is a list", "[1]"),
            ("duplicate keys hide the first", '{"resource": {"aws_instance": {"x": {"provisioner": [{"local-exec": {"command": "id"}}], "provisioner": []}}}}'),
            ("mixed list", '{"resource": {"aws_instance": {"x": {"things": [{"provisioner": [{"local-exec": {}}]}, "junk"]}}}}'),
            ("odd block shape", '{"resource": {"aws_instance": "oops"}}'),
        ):
            with self.subTest(name):
                self.assertEqual(self.errors(self.run_json(text)), ["parse.unparsed"])

    def test_interpolation_in_a_json_source_is_not_a_literal(self):
        got = self.run_json({"module": {"m": {"source": "./modules/${var.x}"}}})
        self.assertEqual(self.errors(got), ["module.source-nonliteral"])

    def test_tofu_files_are_scanned_too(self):
        got = self.run_policy({"infra/x.tofu": 'data "external" "e" {}\n', "infra/y.tofu.json": json.dumps({"data": {"http": {"h": {}}}})})
        self.assertIn("data.external", self.errors(got))
        self.assertIn("data.http", self.errors(got))


class TreeRules(TreeCase):
    def test_suffix_match_is_case_insensitive_and_includes_hidden_and_vendored_dirs(self):
        for rel in ("infra/EVIL.TF", "infra/.hidden/x.tf", "infra/node_modules/m/x.tf", "infra/override.tf", "infra/z_override.tf", "x.tf"):
            with self.subTest(rel):
                self.assertIn("data.external", self.errors(self.run_policy({rel: 'data "external" "e" {}\n'})))

    def test_a_committed_dot_terraform_directory(self):
        got = self.run_policy({"infra/.terraform/providers/registry.terraform.io/hashicorp/aws/5.0.0/linux_amd64/terraform-provider-aws": b"\x7fELF"})
        self.assertEqual(self.errors(got), ["tree.dot-terraform"])

    def test_a_symlinked_tf_file_and_a_symlinked_directory(self):
        pr, base = self.trees({})
        outside = self.tmpdir("outside-")
        write_tree(outside, {"evil.tf": 'data "external" "e" {}\n'})
        os.symlink(os.path.join(outside, "evil.tf"), os.path.join(pr, "infra", "linked.tf"))
        os.symlink(outside, os.path.join(pr, "infra", "linkdir"))
        got = source_policy.run(pr, base, None, False, False)
        self.assertEqual(self.errors(got), ["tree.symlink"])
        self.assertEqual(len([f for f in got if f.rule == "tree.symlink"]), 2)

    def test_a_fifo_named_like_terraform_does_not_hang_or_pass(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no mkfifo")
        pr, base = self.trees({})
        os.mkfifo(os.path.join(pr, "infra", "pipe.tf"))
        self.assertEqual(self.errors(source_policy.run(pr, base, None, False, False)), ["tree.special-file"])

    def test_invalid_utf8_and_oversized_files_are_unparsed(self):
        self.assertEqual(self.errors(self.run_policy({"infra/bad.tf": b'locals {\n  a = "\xff\xfe"\n}\n'})), ["parse.unparsed"])
        big = "# " + "x" * (2 * 1024 * 1024 + 10) + "\n"
        self.assertEqual(self.errors(self.run_policy({"infra/big.tf": big})), ["parse.unparsed"])

    def test_plugin_directories_and_provider_binaries(self):
        self.assertEqual(self.errors(self.run_policy({"infra/terraform.d/plugins/linux_amd64/terraform-provider-evil_v1.0.0": b"\x7fELF"})), ["tree.plugin-dir"])
        self.assertEqual(self.errors(self.run_policy({"infra/.terraform.d/plugins/x": b"x"})), ["tree.plugin-dir"])
        self.assertEqual(self.errors(self.run_policy({"infra/bin/terraform-provider-evil": b"\x7fELF"})), ["tree.provider-binary"])

    def test_terragrunt_and_test_files(self):
        self.assertEqual(self.errors(self.run_policy({"infra/terragrunt.hcl": 'terraform {\n  source = "../x"\n}\n'})), ["tree.terragrunt"])
        self.assertEqual(self.errors(self.run_policy({"infra/env/prod.terragrunt.hcl": ''})), ["tree.terragrunt"])
        got = self.run_policy({"infra/x.tftest.hcl": 'run "r" {\n  command = apply\n}\nresource "aws_instance" "x" {\n  provisioner "local-exec" {}\n}\n'})
        self.assertEqual(self.errors(got), ["provisioner.local-exec"])

    def test_file_calls_with_computed_paths_warn(self):
        got = self.run_policy(ex('locals {\n  a = file(var.p)\n}\n'))
        self.assertEqual(self.errors(got), [])
        self.assertIn("expr.file-dynamic", self.rules(got, "warn"))

    def test_file_calls_in_json_syntax(self):
        got = self.run_policy({"infra/x.tf.json": json.dumps({"locals": {"a": '${file("/proc/self/environ")}', "b": '${file("${path.module}/x.json")}', "c": '${file(var.p)}'}})})
        self.assertEqual(self.errors(got), ["expr.file-outside-tree"])
        self.assertIn("expr.file-dynamic", self.rules(got, "warn"))

    def test_cli_config_files_warn(self):
        got = self.run_policy({"infra/.terraformrc": 'provider_installation {}\n'})
        self.assertEqual(self.errors(got), [])
        self.assertIn("tree.cli-config", self.rules(got, "warn"))

    def test_the_lock_file_changing_warns(self):
        got = self.run_policy({"infra/.terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/aws" {\n  version = "5.40.0"\n}\n'})
        self.assertIn("lock.changed", self.rules(got, "warn"))
        self.assertEqual(self.errors(got), [])

    def test_a_lock_file_that_cannot_be_read_is_unparsed(self):
        self.assertEqual(self.errors(self.run_policy({"infra/.terraform.lock.hcl": 'provider "x" {'})), ["parse.unparsed"])

    def test_a_missing_root_fails(self):
        got = source_policy.run("/nonexistent-root-for-test", None, None, False, True)
        self.assertIn("tree.missing", self.errors(got))


class Baseline(TreeCase):
    def test_without_a_base_the_run_fails_unless_initial(self):
        got = self.run_policy({}, use_base=False)
        self.assertEqual(self.errors(got), ["provider.no-baseline"])
        got = self.run_policy({}, use_base=False, initial=True)
        self.assertEqual(self.errors(got), [])
        self.assertIn("provider.initial", self.rules(got, "warn"))

    def test_initial_still_applies_every_other_ban(self):
        got = self.run_policy(ex('data "external" "e" {}\n'), use_base=False, initial=True)
        self.assertEqual(set(self.errors(got)), {"data.external", "provider.banned"})

    def test_a_missing_base_directory_does_not_pass(self):
        pr, _ = self.trees({})
        got = source_policy.run(pr, "/nonexistent-base-for-test", None, False, False)
        self.assertIn("tree.missing", self.errors(got))
        self.assertIn("provider.no-baseline", self.errors(got))

    def test_the_policy_file_allows_a_named_provider_and_backend(self):
        pr = {"infra/more.tf": 'terraform {\n  required_providers {\n    datadog = { source = "datadog/datadog" }\n  }\n  backend "gcs" {\n    bucket = "b"\n  }\n}\n'}
        self.assertEqual(set(self.errors(self.run_policy(pr))), {"provider.new", "backend.changed"})
        got = self.run_policy(pr, policy={"schema": "infra-policy.v1", "allowed_providers": ["datadog/datadog"], "allowed_backends": ["gcs"]})
        self.assertEqual(self.errors(got), [])

    def test_allowing_a_provider_never_allows_external_or_http(self):
        pr = ex('data "external" "e" {}\n')
        got = self.run_policy(pr, policy={"schema": "infra-policy.v1", "allowed_providers": ["hashicorp/external"]})
        self.assertEqual(set(self.errors(got)), {"data.external", "provider.banned"})

    def test_providers_in_the_base_lock_file_are_known(self):
        base = dict(BASE_FILES)
        base["infra/.terraform.lock.hcl"] += 'provider "registry.terraform.io/hashicorp/random" {\n  version = "3.6.0"\n}\n'
        got = self.run_policy({"infra/r.tf": 'resource "random_pet" "p" {}\n'}, base_files=base)
        self.assertEqual(self.errors(got), [])

    def test_required_providers_resolve_per_module_directory(self):
        # In infra/modules/thing the local name `aws` is not declared, so it means hashicorp/aws (known). In infra/other it is evil/aws.
        files = {"infra/other/versions.tf": 'terraform {\n  required_providers {\n    aws = { source = "evil/aws" }\n  }\n}\n', "infra/other/main.tf": 'resource "aws_vpc" "v" {}\n'}
        self.assertEqual(self.errors(self.run_policy(files)), ["provider.new"])

    def test_backend_settings_changing_warns(self):
        files = {"infra/versions.tf": BASE_FILES["infra/versions.tf"].replace("acme-state", "other-state")}
        got = self.run_policy(files)
        self.assertEqual(self.errors(got), [])
        self.assertIn("backend.attrs-changed", self.rules(got, "warn"))

    def test_an_unparsable_base_file_makes_the_baseline_untrustworthy(self):
        base = dict(BASE_FILES)
        base["infra/broken.tf"] = 'resource "x" "y" {'
        got = self.run_policy({}, base_files=base)
        self.assertIn("base.unparsed", self.errors(got))


class PolicyFile(TreeCase):
    def test_bad_policy_files_fail_and_allow_nothing(self):
        for name, content in (
            ("unknown key", '{"schema": "infra-policy.v1", "allow_everything": true}'),
            ("wrong type", '{"schema": "infra-policy.v1", "allowed_providers": "hashicorp/aws"}'),
            ("not an object", '[]'),
            ("not json", '{'),
        ):
            with self.subTest(name):
                d = self.tmpdir("pol-")
                path = os.path.join(d, "p.json")
                with open(path, "w") as fh:
                    fh.write(content)
                pr, base = self.trees({})
                got = source_policy.run(pr, base, path, False, False)
                self.assertIn("policy.invalid", self.errors(got))

    def test_a_missing_policy_file_fails_closed(self):
        pr, base = self.trees({})
        self.assertIn("policy.invalid", self.errors(source_policy.run(pr, base, "/nonexistent/policy.json", False, False)))


class PostInit(TreeCase):
    CACHE = "infra/.terraform/modules/net/main.tf"

    def test_downloaded_modules_get_the_hard_bans(self):
        got = self.run_policy({self.CACHE: RES % LOCAL_EXEC}, post_init=True)
        errs = [f for f in got if f.severity == "error"]
        self.assertEqual([f.rule for f in errs], ["provisioner.local-exec"])
        self.assertTrue(errs[0].file.startswith("infra/.terraform/modules"), errs[0].file)

    def test_downloaded_modules_get_unpinned_nested_sources_as_warnings_only(self):
        got = self.run_policy({self.CACHE: 'module "inner" {\n  source = "terraform-aws-modules/iam/aws"\n  version = ">= 5.0"\n}\n'}, post_init=True)
        self.assertEqual(self.errors(got), [])
        self.assertIn("module.registry-range", self.rules(got, "warn"))

    def test_downloaded_modules_cannot_bring_a_new_provider(self):
        got = self.run_policy({self.CACHE: 'resource "datadog_monitor" "m" {}\n'}, post_init=True)
        self.assertEqual(self.errors(got), ["provider.new"])

    def test_downloaded_external_data_is_denied(self):
        got = self.run_policy({self.CACHE: 'data "external" "e" {}\n'}, post_init=True)
        self.assertIn("data.external", self.errors(got))

    def test_the_cache_is_not_a_committed_dot_terraform_in_post_init_mode(self):
        got = self.run_policy({self.CACHE: 'resource "aws_vpc" "v" {}\n'}, post_init=True)
        self.assertEqual(self.errors(got), [])

    def test_a_missing_cache_warns(self):
        self.assertIn("tree.no-module-cache", self.rules(self.run_policy({}, post_init=True), "warn"))


class Fixtures(TreeCase):
    """tests/fixtures/infra: `base/` is main, `clean/` and `harmless/` are overlays that must pass, and each `planted/<name>/` holds the
    files of one planted change plus expect.json (the exact error rules it must trip)."""
    BASE = os.path.join(FIXTURES, "base")
    POLICY = os.path.join(FIXTURES, "base", ".claude", "infra-policy.json")

    def pr_with(self, *overlays):
        pr = self.tmpdir("fx-pr-")
        shutil.copytree(self.BASE, pr, dirs_exist_ok=True)
        for o in overlays:
            shutil.copytree(o, pr, dirs_exist_ok=True, ignore=shutil.ignore_patterns("expect.json"))
        return pr

    def judge(self, pr):
        return source_policy.run(pr, self.BASE, self.POLICY, False, False)

    def test_the_base_passes_against_itself(self):
        self.assertEqual([str(f) for f in self.judge(self.pr_with()) if f.severity == "error"], [])

    def test_an_ordinary_change_passes(self):
        got = self.judge(self.pr_with(os.path.join(FIXTURES, "clean")))
        self.assertEqual([str(f) for f in got if f.severity == "error"], [])

    def test_scary_looking_but_harmless_source_passes(self):
        got = self.judge(self.pr_with(os.path.join(FIXTURES, "harmless")))
        self.assertEqual([str(f) for f in got if f.severity == "error"], [])

    def test_every_planted_change_is_refused_for_the_expected_rules(self):
        names = sorted(os.listdir(os.path.join(FIXTURES, "planted")))
        self.assertGreaterEqual(len(names), 18)
        for name in names:
            with self.subTest(name):
                d = os.path.join(FIXTURES, "planted", name)
                with open(os.path.join(d, "expect.json"), "r") as fh:
                    expect = json.load(fh)
                got = self.judge(self.pr_with(d))
                self.assertEqual(sorted({f.rule for f in got if f.severity == "error"}), sorted(expect["errors"]), [str(f) for f in got])

    def test_a_planted_change_next_to_an_ordinary_one_is_still_refused(self):
        got = self.judge(self.pr_with(os.path.join(FIXTURES, "clean"), os.path.join(FIXTURES, "planted", "local-exec")))
        self.assertEqual(sorted({f.rule for f in got if f.severity == "error"}), ["provisioner.local-exec"])


class Cli(TreeCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, SOURCE_POLICY] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_exit_codes_and_json(self):
        pr, base = self.trees({})
        ok = self.run_cli("--root", pr, "--base", base, "--json")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["schema"], "source_policy.v1")
        pr, base = self.trees(ex('data "external" "e" {}\n'))
        bad = self.run_cli("--root", pr, "--base", base)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("data.external", bad.stdout)

    def test_usage_error_is_two(self):
        self.assertEqual(self.run_cli("--nope").returncode, 2)

    def test_no_base_without_initial_is_a_failure(self):
        pr, _ = self.trees({})
        self.assertEqual(self.run_cli("--root", pr).returncode, 1)
        self.assertEqual(self.run_cli("--root", pr, "--initial").returncode, 0)

    def test_python_39_compatible_syntax_marker(self):
        # the whole-repo test_py39_syntax covers the files; this only checks the entry point is executable as a script
        self.assertTrue(os.path.isfile(SOURCE_POLICY))
