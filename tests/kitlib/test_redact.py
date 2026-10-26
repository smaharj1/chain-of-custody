"""redact: secret patterns (report §5.4). Secret-shaped test values are assembled at runtime so
no scanner (or GitHub push protection) sees a literal token in the repo."""
from __future__ import annotations

import unittest

from kitlib import redact

AWS_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
GHP = "gh" + "p_" + "a1B2c3D4" * 5
GHO = "gh" + "o_" + "Z" * 36
GHS = "gh" + "s_" + "y" * 36
GHPAT = "github" + "_pat_" + "11ABCDEFG0" + "x" * 30
SLACK = "xo" + "xb-" + "1234567890-" + "abcdefghijkl"
SLACK_HOOK = "https://hooks." + "slack.com/services/T000/B000/" + "XXXXXXXXXXXXXXXXXXXXXXXX"
KEY_BLOCK = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA " + "PRIVATE KEY-----"
JWT = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
STRIPE = "sk_" + "live_" + "a" * 24

# (name, input, must_not_contain, must_contain)
TABLE = [
    ("aws access key id", "id=%s done" % AWS_ID, [AWS_ID], ["[REDACTED:aws-access-key]", "done"]),
    ("aws session key id", "ASIA" + "ABCDEFGHIJKLMNOP", ["ASIAABCDEFGHIJKLMNOP"], ["[REDACTED:aws-access-key]"]),
    ("aws secret in ini", "aws_secret_access_key = %s" % AWS_SECRET, [AWS_SECRET], ["aws_secret_access_key"]),
    ("aws secret in env style", "AWS_SECRET_ACCESS_KEY=%s" % AWS_SECRET, [AWS_SECRET], ["AWS_SECRET_ACCESS_KEY"]),
    ("aws session token", "aws_session_token: %s" % ("F" * 40), ["F" * 40], ["aws_session_token"]),
    ("github ghp", "token " + GHP, [GHP], ["[REDACTED"]),
    ("github gho", "x " + GHO + " y", [GHO], ["[REDACTED:github-token]", " y"]),
    ("github ghs", GHS, [GHS], ["[REDACTED:github-token]"]),
    ("github fine-grained pat", "GH_TOKEN=%s" % GHPAT, [GHPAT], ["GH_TOKEN"]),
    ("slack bot token", "SLACK=%s" % SLACK, [SLACK], ["[REDACTED"]),
    ("slack webhook", "post to " + SLACK_HOOK, [SLACK_HOOK], ["[REDACTED:slack-webhook]"]),
    ("private key block", "before\n%s\nafter" % KEY_BLOCK, ["MIIBOgIBAAJB", "BEGIN RSA"], ["before", "after", "[REDACTED:private-key]"]),
    ("truncated private key block", "x\n-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAAB3Nza", ["AAAAB3Nza"], ["[REDACTED:private-key]"]),
    ("bearer header", "Authorization: Bearer abcdefghijklmnop1234567", ["abcdefghijklmnop1234567"], ["Authorization: Bearer"]),
    ("basic auth header", "Authorization: Basic dXNlcjpwYXNzd29yZA==", ["dXNlcjpwYXNzd29yZA"], ["Authorization:"]),
    ("password equals", "db password=hunter2hunter2 ok", ["hunter2hunter2"], ["password=", "ok"]),
    ("password colon quoted", 'password: "correct horse battery"', ["correct horse battery"], ["password:"]),
    ("json secret field", '{"api_key": "abc123def456", "name": "x"}', ["abc123def456"], ['"name": "x"']),
    ("client secret", "client_secret = 's3cr3t-value'", ["s3cr3t-value"], ["client_secret"]),
    ("token assignment", "auth_token=abcdef0123456789", ["abcdef0123456789"], ["auth_token="]),
    ("url credentials", "git clone https://alice:pa55w0rd@example.com/r.git", ["pa55w0rd"], ["alice:", "@example.com"]),
    ("jwt", "t=%s" % JWT, [JWT], ["[REDACTED:jwt]"]),
    ("stripe live key", STRIPE, [STRIPE], ["[REDACTED:stripe-key]"]),
    ("google api key", "AIza" + "S" * 35, ["AIza" + "S" * 35], ["[REDACTED:google-api-key]"]),
    ("npm token", "npm_" + "a" * 36, ["npm_" + "a" * 36], ["[REDACTED:npm-token]"]),
    ("several in one line", "%s and %s" % (AWS_ID, GHP), [AWS_ID, GHP], ["and"]),
]

# Text that must come through unchanged.
UNCHANGED = [
    "nothing secret here",
    "the password policy requires 12 characters",
    "token_budget_per_item: 150000",
    "max_tool_calls_per_run: 600",
    "see docs/security/secrets.md for the secret manager",
    "https://example.com/path?x=1",
    "git commit -m 'fix auth token refresh'",
    "AKIA-short",
    "ghp_short",
    "",
]


class RedactTable(unittest.TestCase):
    def test_table(self):
        for name, text, gone, kept in TABLE:
            with self.subTest(name):
                out = redact.redact(text)
                for g in gone:
                    self.assertNotIn(g, out)
                for k in kept:
                    self.assertIn(k, out)

    def test_idempotent(self):
        for name, text, _, _ in TABLE:
            with self.subTest(name):
                once = redact.redact(text)
                self.assertEqual(redact.redact(once), once)

    def test_unchanged(self):
        for text in UNCHANGED:
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)

    def test_non_strings_pass_through(self):
        self.assertIsNone(redact.redact(None))
        self.assertEqual(redact.redact(5), 5)

    def test_find_secrets_reports_spans(self):
        text = "a %s b" % AWS_ID
        spans = redact.find_secrets(text)
        self.assertEqual(spans[0][0], "aws-access-key")
        self.assertEqual(text[spans[0][1]:spans[0][2]], AWS_ID)

    def test_large_input_is_fast(self):
        import time
        text = ("lorem ipsum token password " * 20000) + "x" * 100000
        t = time.time()
        redact.redact(text)
        self.assertLess(time.time() - t, 5.0)


class RedactObj(unittest.TestCase):
    def test_walks_nested_structures(self):
        obj = {"cmd": "curl -H 'Authorization: Bearer %s'" % ("a" * 30), "args": ["x", "password=%s" % ("p" * 10)],
               "nested": {"AWS_SECRET_ACCESS_KEY": AWS_SECRET, "n": 3, "ok": True}}
        out = redact.redact_obj(obj)
        flat = str(out)
        self.assertNotIn("a" * 30, flat)
        self.assertNotIn("p" * 10, flat)
        self.assertNotIn(AWS_SECRET, flat)
        self.assertEqual(out["nested"]["n"], 3)
        self.assertIs(out["nested"]["ok"], True)
        self.assertEqual(obj["args"][1], "password=%s" % ("p" * 10))  # the input is not mutated

    def test_secret_looking_keys_are_replaced_wholesale(self):
        out = redact.redact_obj({"Authorization": "Basic zzz", "api-key": "k", "password": "", "title": "x"})
        self.assertEqual(out["Authorization"], "[REDACTED:secret-key]")
        self.assertEqual(out["api-key"], "[REDACTED:secret-key]")
        self.assertEqual(out["password"], "")
        self.assertEqual(out["title"], "x")

    def test_idempotent(self):
        once = redact.redact_obj({"token": "abc", "x": ["password=zzzzzz"]})
        self.assertEqual(redact.redact_obj(once), once)


if __name__ == "__main__":
    unittest.main()
