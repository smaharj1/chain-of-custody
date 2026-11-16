"""redact: secret patterns (report §5.4). Secret-shaped test values are assembled at runtime so
no scanner (or GitHub push protection) sees a literal token in the repo."""
from __future__ import annotations

import time
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
    # UN-2: a key name is an identifier, not a word (`\\b` missed every compound name)
    ("env style password", "DB_PASSWORD=hunter2hunter2", ["hunter2hunter2"], ["DB_PASSWORD="]),
    ("env style secret", "API_SECRET=abc12345xyz", ["abc12345xyz"], ["API_SECRET="]),
    ("stripe secret key name", "STRIPE_SECRET_KEY=zzzzzzzzzzzzzz", ["zzzzzzzzzzzzzz"], ["STRIPE_SECRET_KEY="]),
    ("lower snake password", "db_password=hunter2xx", ["hunter2xx"], ["db_password="]),
    ("camelCase secret", "apiSecret=abc123456", ["abc123456"], ["apiSecret="]),
    ("camelCase quoted", "dbPassword: 'abc123456'", ["abc123456"], ["dbPassword:"]),
    ("prefixed json field", '{"client_secret_id": "abcd1234"}', ["abcd1234"], ["client_secret_id"]),
    ("glued lower-case names", "clientsecret=abcdefgh1 dbpassword: abcdefgh2 SECRETKEY=abcdefgh3", ["abcdefgh1", "abcdefgh2", "abcdefgh3"], ["clientsecret=", "SECRETKEY="]),
    ("cli flag", "--token=abc12345", ["abc12345"], ["--token="]),
    # UN-3: token prefixes
    ("anthropic key", "k " + "sk-" + "ant-" + "api03-" + "abcdefghijklmnopqrstuvwx", ["abcdefghijklmnopqrstuvwx"], ["[REDACTED:anthropic-key]"]),
    ("openai project key", "OPENAI_API_KEY=" + "sk-" + "proj-" + "abcdefghijklmnopqrstuv", ["abcdefghijklmnopqrstuv"], ["[REDACTED:openai-key]"]),
    ("gitlab token", "glp" + "at-" + "abcdefghijklmnopqrst", ["abcdefghijklmnopqrst"], ["[REDACTED:gitlab-token]"]),
    ("stripe webhook secret", "whs" + "ec_" + "abcdefghijklmnop0123", ["abcdefghijklmnop0123"], ["[REDACTED:stripe-webhook-secret]"]),
    ("pypi token", "pypi-" + "AgEIcHlwaS5vcmc" + "abcdefghijklmnopqrstuvwx", ["abcdefghijklmnopqrstuvwx"], ["[REDACTED:pypi-token]"]),
    ("azure account key", "Account" + "Key=" + "abcdEFGH0123456789abcdEFGH0123456789ab==", ["abcdEFGH0123456789"], ["AccountKey=", "[REDACTED:azure-account-key]"]),
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
    "max_tokens=4000",
    "input_tokens: 1200",
    "auth: required",
    "passwordless=true",
    "token_count = 5",
    "maxtoken=500",
    "secretary: Pat",
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
        self.assertLess(time.time() - t, 30.0)


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

    def test_camel_case_and_env_style_keys(self):
        out = redact.redact_obj({"dbPassword": "x", "apiSecret": "y", "DB_PASSWORD": "z", "inputTokens": "n", "tokenBudget": "b"})
        self.assertEqual([out[k] for k in ("dbPassword", "apiSecret", "DB_PASSWORD")], ["[REDACTED:secret-key]"] * 3)
        self.assertEqual((out["inputTokens"], out["tokenBudget"]), ("n", "b"))

    def test_idempotent(self):
        once = redact.redact_obj({"token": "abc", "x": ["password=zzzzzz"]})
        self.assertEqual(redact.redact_obj(once), once)


V = "Ab12" + "Cd34Ef56" + "Gh78"


class RedactSecondReview(unittest.TestCase):
    """R-GATES-3 and the url-credentials regex of R-GATES-6."""

    def test_more_key_names(self):
        for text, key in (("secret_key_base: %s" % V, "secret_key_base:"), ("SECRET_KEY_BASE=%s" % V, "SECRET_KEY_BASE="), ("SIGNING_KEY=%s" % V, "SIGNING_KEY="),
                          ("ENCRYPTION_KEY=%s" % V, "ENCRYPTION_KEY="), ("signingKey: %s" % V, "signingKey:"), ("master_key=%s" % V, "master_key=")):
            with self.subTest(text):
                out = redact.redact(text)
                self.assertNotIn(V, out)
                self.assertIn(key, out)

    def test_command_line_secrets(self):
        for text, kept in (("mysql --password %s -h x" % V, "-h x"), ("tool --db-token %s --verbose" % V, "--verbose"), ("sshpass -p %s ssh host" % V, "ssh host"),
                           ("mysql -u root -p%s db" % V, "db"), ("run --api-key '%s' now" % V, "now"), ("run --secret \"%s\" now" % V, "now")):
            with self.subTest(text):
                out = redact.redact(text)
                self.assertNotIn(V, out)
                self.assertIn(kept, out)
                self.assertEqual(redact.redact(out), out)

    def test_command_lines_that_are_not_secrets_stay(self):
        for text in ("--token $GITHUB_TOKEN", "--token-budget 5000", "--password-file /run/secret", "mkdir -p build/out", "docker run -p 8080:80 img", "psql -p 5432 db",
                     "mysql -P 3306 -u root", "sshpass -p $PW ssh h", "tail -n 20 --secret-name foo", "git push --no-verify"):
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)

    def test_xml_elements_and_attributes(self):
        for text in ("<password>%s</password>" % V, "<db:Password attr='x'>\n  %s\n</db:Password>" % V, '<add key="DbPassword" value="%s"/>' % V,
                     "<add name='api_key' value='%s' />" % V):
            with self.subTest(text):
                out = redact.redact(text)
                self.assertNotIn(V, out)
                self.assertEqual(redact.redact(out), out)

    def test_xml_that_is_not_a_secret_stays(self):
        for text in ("<passwordPolicy>strict</passwordPolicy>", "<tokenBudget>150000</tokenBudget>", '<add key="Timeout" value="30"/>', "<password></password>"):
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)

    def test_dockerfile_env_without_an_equals_sign(self):
        for text in ("ENV DB_PASSWORD %s\nRUN x" % V, "  env API_SECRET_KEY\t%s" % V, "ENV DB_PASSWORD '%s'" % V, 'ENV DB_PASSWORD "%s"' % V):
            with self.subTest(text):
                out = redact.redact(text)
                self.assertNotIn(V, out)
                self.assertEqual(redact.redact(out), out)
        self.assertIn("RUN x", redact.redact("ENV DB_PASSWORD %s\nRUN x" % V))

    def test_dockerfile_env_lines_that_are_not_secrets_stay(self):
        for text in ("ENV PATH /usr/local/bin", "ENV TOKEN_BUDGET 5000", "ENV LANG C.UTF-8", "RUN echo ENV DB_PASSWORD"):
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)

    def test_a_long_password_in_a_url(self):
        out = redact.redact("https://u:%s@h/" % ("p" * 3000))
        self.assertNotIn("p" * 3000, out)

    def test_redact_obj_replaces_lists_dicts_and_numbers_under_a_secret_key(self):
        out = redact.redact_obj({"password": ["a1", "b2"], "api_key": {"id": "x", "n": 4}, "secret": 123456, "token_budget": 5000, "tokens": {"in": 1, "out": 2},
                                 "title": ["x"], "password_min_length": 12, "nested": {"credentials": [{"user": "u", "pass": "p"}]}})
        self.assertEqual(out["password"], ["[REDACTED:secret-key]"] * 2)
        self.assertEqual(out["api_key"], {"id": "[REDACTED:secret-key]", "n": "[REDACTED:secret-key]"})
        self.assertEqual(out["secret"], "[REDACTED:secret-key]")
        self.assertEqual((out["token_budget"], out["tokens"], out["title"], out["password_min_length"]), (5000, {"in": 1, "out": 2}, ["x"], 12))
        self.assertEqual(out["nested"]["credentials"], [{"user": "[REDACTED:secret-key]", "pass": "[REDACTED:secret-key]"}])
        self.assertEqual(redact.redact_obj(out), out)

    def test_redact_obj_keeps_booleans_none_and_empty_strings(self):
        out = redact.redact_obj({"password": None, "secret": True, "token": "", "api_key": []})
        self.assertEqual(out, {"password": None, "secret": True, "token": "", "api_key": []})


class RedactIsLinear(unittest.TestCase):
    """Every pattern is linear on hostile input (a 1 MB hyphenated line took minutes with the old url-credentials regex). The bound is generous so
    a loaded machine does not make it flaky; a quadratic regex misses it by orders of magnitude."""

    def timed(self, text, limit=1.0):
        t = time.time()
        redact.redact(text)
        took = time.time() - t
        self.assertLess(took, limit, "%d characters took %.1fs" % (len(text), took))

    def test_hostile_inputs(self):
        n = 50000
        cases = {"hyphens": "-" * n, "hyphenated words": "a-" * (n // 2), "ab-": "ab-" * (n // 3), "scheme-ish": "a-a-a-://" * (n // 9), "url without at": "a://" + "b:" * (n // 2),
                 "long password": "a://u:" + "p" * n, "repeated urls": "a://b:" * (n // 6), "dots": "a." * (n // 2), "plus": "a+" * (n // 2), "keyword runs": "password-" * (n // 9),
                 "cli": "--password " * (n // 11), "mysql": "mysql -p" * (n // 8), "tags": "<password>" * (n // 10), "attrs": 'key="password" value="' * (n // 24),
                 "underscore words": "a_" * (n // 2) + "token", "spaces": " " * n + "password:", "newlines": "token\n" * (n // 6)}
        for name, text in cases.items():
            with self.subTest(name):
                self.timed(text)

    def test_a_secret_element_with_a_long_run_of_blanks_after_the_value(self):
        """`<password>x` followed by blanks and no closing tag: the trailing `[ \\t\\r\\n]*` after a lazy body of up to 500 characters made this 500 times
        the length of the run. The run is now bounded (it is a single pass now; a 200k run is read in a few hundredths of a second)."""
        n = 200000
        for name, text in (("spaces", "<password>x" + " " * n), ("newlines", "<password>x" + "\n" * n), ("tabs and CRs", "<password>x" + "\t\r" * (n // 2)),
                           ("blank run after a word", "<password>word" + " " * n + "more")):
            with self.subTest(name):
                self.timed(text, limit=0.5)

    def test_dockerfile_env_lines_are_linear(self):
        n = 50000
        for name, text in (("blanks after ENV", "ENV" + " " * n), ("blanks after the key", "ENV password" + " " * n), ("many lines", "ENV password\n" * (n // 13)),
                           ("blank lines", ("ENV\n" + " " * 20 + "\n") * (n // 25)), ("long key", "ENV " + "a_" * (n // 2) + "password x")):
            with self.subTest(name):
                self.timed(text)

    def test_a_secret_element_and_a_long_password_still_work(self):
        out = redact.redact("<password>%s</password>\n<db:Password attr='x'>\n  %s\n</db:Password>" % (V, V))
        self.assertNotIn(V, out)
        out = redact.redact("<password>%s%s</password>" % (V, " " * 100))
        self.assertNotIn(V, out)

    def test_the_url_credential_shape_still_works_with_long_parts(self):
        out = redact.redact("git clone https://%s:%s@example.com/r.git" % ("u" * 150, "p" * 600))
        self.assertNotIn("p" * 600, out)


class RedactPaddedElement(unittest.TestCase):
    """R3-GATES-1: a value followed by more blanks than a bounded blank run allows was not redacted (the earlier bound of 64 let `<password>…` + 560 blanks through)."""

    def test_a_value_padded_with_blanks_or_newlines_is_redacted(self):
        for pad in (0, 1, 63, 64, 65, 500, 560, 600, 4000, 5000, 100000):
            for blank in (" ", "\n", "\t", "\r\n"):
                with self.subTest(pad=pad, blank=repr(blank)):
                    text = "<password>" + V + blank * pad + "</password>"
                    out = redact.redact(text)
                    self.assertNotIn(V, out)
                    self.assertEqual(redact.redact(out), out)
                    self.assertTrue(out.startswith("<password>[REDACTED:secret-element]"))
                    self.assertTrue(out.endswith("</password>"))

    def test_the_blanks_around_the_value_are_kept(self):
        self.assertEqual(redact.redact("<password>\n  %s \n</password>" % V), "<password>\n  [REDACTED:secret-element] \n</password>")
        self.assertEqual(redact.redact("<password>%s%s</password>" % (V, " " * 700)), "<password>[REDACTED:secret-element]%s</password>" % (" " * 700))

    def test_a_value_with_inner_blanks_and_a_padded_end(self):
        out = redact.redact("<password>%s %s%s</password>" % (V, V, " " * 900))
        self.assertNotIn(V, out)
        self.assertEqual(out, "<password>[REDACTED:secret-element]%s</password>" % (" " * 900))

    def test_find_secrets_spans_stop_before_the_trailing_blanks(self):
        text = "<password>%s%s</password>" % (V, " " * 300)
        spans = [(s, e) for kind, s, e in redact.find_secrets(text) if kind == "secret-element"]
        self.assertTrue(spans)
        for s, e in spans:
            self.assertEqual(text[s:e], V)

    def test_hostile_padding_is_linear(self):
        n = 100000
        cases = {"blanks": "<password>" + V + " " * n, "blanks and closer": "<password>" + V + " " * n + "</password>", "leading blanks": "<password>" + " " * n + "</password>"}
        cases["many openers, padded"] = ("<password>" + V + " " * 4000 + "<x>") * (n // 4030)
        cases["many openers, long bodies"] = ("<password>" + "a b " * 1100 + "<x>") * (n // 4420)
        cases["openers without closers"] = "<password>a" * (n // 11)
        cases["body at the cap"] = ("<password>" + "a" * 4097 + "<x>") * (n // 4110)
        cases["nested tags"] = "<password><password>" * (n // 20)
        for name, text in cases.items():
            with self.subTest(name):
                t = time.time()
                redact.redact(text)
                redact.find_secrets(text)
                self.assertLess(time.time() - t, 1.0, name)


class RedactRound4(unittest.TestCase):
    """R4: the bounds redact, escaped quotes, the new key names, CDATA, and a reader that fails keeps nothing."""

    def test_a_body_past_the_bound_is_redacted_not_left_in_the_clear(self):
        out = redact.redact("<password>correct horse battery%s</password>" % (" " * 5000))
        self.assertNotIn("correct horse", out)
        self.assertEqual(redact.redact(out), out)

    def test_a_quoted_value_past_the_bound_is_redacted(self):
        # R5-B1: the bound on a quoted value is the reader's; past it the value is redacted to the end of the line, never left in the clear
        for n in (4100, 5000):
            for text in ('password = "%s"' % ("Ab1" * (n // 3)), "password: '%s'" % ("Ab1" * (n // 3)), '--password "%s"' % ("Ab1" * (n // 3))):
                with self.subTest("%d %s" % (n, text[:12])):
                    out = redact.redact(text)
                    self.assertNotIn("Ab1Ab1Ab1", out)
                    self.assertEqual(redact.redact(out), out)

    def test_escaped_quotes_in_a_value(self):
        for text in ('password = "A1b2\\"Cd34Ef56Gh78"', "password = 'A1b2\\'Cd34Ef56Gh78'", 'password: "A1b2\\"Cd34Ef56Gh78"', '--password "A1b2\\"Cd34Ef56Gh78"'):
            with self.subTest(text):
                self.assertNotIn("Ef56Gh78", redact.redact(text))

    def test_pass_and_auth_key(self):
        for text in ("db_pass: " + V, "MYSQL_PASS=" + V, "AUTH_KEY=" + V, "--pass " + V, "pass: " + V, "  - pass: " + V, "export PASS=" + V, '{"pass": "%s"}' % V,
                     "Server=db;User=u;Pass=" + V, "https://h/x?user=u&pass=" + V, "docker run -e PASS=" + V + " img", "run --env 'PASS=" + V + "'",
                     "{pass: " + V + "}", "{ pass: '" + V + "' }"):
            with self.subTest(text):
                self.assertNotIn(V, redact.redact(text))
        for text in ("pass_through: required", "bypass: true", "passes: 12", "auth: required", "| Verify (G6) | PASS: g6 passed |", "the run says PASS: 3 tests",
                     "tests: pass=12 fail=0", "x, pass: 3 passed", "if a == pass"):
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)
        self.assertEqual(redact.redact_obj({"pass": "x", "pass_count": 3}), {"pass": "[REDACTED:secret-key]", "pass_count": 3})

    def test_cdata_and_attribute_on_a_new_line(self):
        self.assertEqual(redact.redact("<password><![CDATA[%s]]></password>" % V), "<password><![CDATA[[REDACTED:secret-element]]]></password>")
        self.assertNotIn(V, redact.redact('<password\n type="x">%s</password>' % V))

    def test_a_pattern_that_fails_keeps_nothing(self):
        class Boom(object):
            def finditer(self, text):
                raise RecursionError("deep")
        saved = redact._PATTERNS
        redact._PATTERNS = [("x", Boom(), 0)]
        try:
            self.assertEqual(redact.redact("token=" + V), "[REDACTED:unreadable]")
            self.assertEqual(redact.find_secrets("abc"), [("unreadable", 0, 3)])
        finally:
            redact._PATTERNS = saved
        deep = cur = {}
        for _ in range(5000):
            cur["a"] = {}
            cur = cur["a"]
        self.assertEqual(redact.redact_obj(deep), "[REDACTED:unreadable]")

    def test_200_kilobytes_of_hostile_text(self):
        n = 200000
        for name, text in (("padded element", "<password>%s%s" % (V, " " * n)), ("openers", "<password>a" * (n // 11)), ("escapes", 'password = "' + '\\"' * (n // 2)),
                           ("assignments", "password=" * (n // 9)), ("cdata", "<password><![CDATA[" * (n // 19))):
            with self.subTest(name):
                t = time.time()
                redact.redact(text)
                redact.find_secrets(text)
                self.assertLess(time.time() - t, 2.0, name)


if __name__ == "__main__":
    unittest.main()
