"""gate.py: the approver check, the main-sha check, the saved-plan hash, the plan digest and the values-free summary (RC-2, RC-4)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from infra import gate

from tests.helpers import BaseTestCase

from .helpers import GATE

SHA_A = "a" * 40
SHA_B = "b" * 40


def plan(changes, outputs=None, variables=None, timestamp="2026-10-26T10:00:00Z"):
    return {
        "format_version": "1.2", "terraform_version": "1.9.8", "timestamp": timestamp, "variables": variables or {},
        "resource_changes": [{"address": a, "mode": "managed", "type": a.split(".")[0], "name": a.split(".")[-1], "provider_name": "registry.terraform.io/hashicorp/aws",
                              "change": {"actions": acts, "before": before, "after": after, "after_unknown": {}, "before_sensitive": False, "after_sensitive": False}}
                             for a, acts, before, after in changes],
        "output_changes": outputs or {},
    }


class Authorize(BaseTestCase):
    def ok(self, actors, approvers="alice,bob", bots=()):
        gate.authorize(actors, approvers, bots)

    def refused(self, actors, approvers="alice,bob", bots=()):
        with self.assertRaises(gate.Refused) as cm:
            gate.authorize(actors, approvers, bots)
        return str(cm.exception)

    def test_a_listed_human_passes_case_insensitively(self):
        self.ok(["Alice"])
        self.ok(["@bob"], approvers="@Alice, BOB")
        self.ok(["alice", "bob"])

    def test_every_actor_must_qualify(self):
        self.assertIn("mallory", self.refused(["alice", "mallory"]))

    def test_an_unlisted_actor_is_refused(self):
        self.assertIn("not in factory_approvers", self.refused(["mallory"]))

    def test_an_empty_list_refuses_everyone(self):
        self.assertIn("empty", self.refused(["alice"], approvers=""))
        self.assertIn("empty", self.refused(["alice"], approvers=" , ,"))

    def test_no_actor_and_blank_actor_are_refused(self):
        self.refused([])
        self.refused([""])
        self.refused(["  "])

    def test_a_github_app_actor_is_a_bot_even_if_listed(self):
        self.assertIn("bot", self.refused(["factory-app[bot]"], approvers="factory-app[bot],alice"))
        self.assertIn("bot", self.refused(["Factory-App[bot]"]))

    def test_the_named_machine_user_bot_is_refused_even_if_listed(self):
        self.assertIn("bot", self.refused(["factory-bot"], approvers="alice,factory-bot", bots=["Factory-Bot"]))
        self.assertIn("bot", self.refused(["factory-bot"], bots=["factory-bot"]))

    def test_the_bot_in_the_triggering_actor_is_caught(self):
        self.refused(["alice", "factory-bot"], approvers="alice,factory-bot", bots=["factory-bot"])

    def test_blank_bot_arguments_are_ignored(self):
        self.ok(["alice"], bots=["", "  "])

    def test_separators_other_than_commas(self):
        self.ok(["bob"], approvers="alice\nbob")
        self.ok(["bob"], approvers="alice bob")


class MainSha(BaseTestCase):
    def test_equal_passes(self):
        gate.base_is_main(SHA_A, SHA_A)
        gate.base_is_main(SHA_A.upper(), " " + SHA_A + "\n")

    def test_different_is_refused_with_both_shas_in_the_reason(self):
        with self.assertRaises(gate.Refused) as cm:
            gate.base_is_main(SHA_A, SHA_B)
        self.assertIn("aaaaaaaaaaaa", str(cm.exception))
        self.assertIn("bbbbbbbbbbbb", str(cm.exception))

    def test_malformed_values_are_refused_rather_than_compared(self):
        for planned, main in (("", ""), ("abc", "abc"), (SHA_A, ""), ("", SHA_A), (SHA_A[:39], SHA_A[:39]), ("g" * 40, "g" * 40), ("HEAD", "HEAD")):
            with self.subTest(planned=planned[:8], main=main[:8]):
                with self.assertRaises(gate.Refused):
                    gate.base_is_main(planned, main)

    def test_the_ref_must_be_main(self):
        gate.require_main_ref("refs/heads/main")
        for ref in ("refs/heads/feat/x", "refs/heads/main2", "main", "refs/tags/main", "refs/pull/1/merge", ""):
            with self.subTest(ref=ref):
                with self.assertRaises(gate.Refused):
                    gate.require_main_ref(ref)


class Hashing(BaseTestCase):
    def write(self, data=b"plan bytes"):
        path = os.path.join(self.tmpdir(), "tfplan")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_a_matching_hash_passes_with_or_without_prefix(self):
        path = self.write()
        digest = gate.sha256_file(path)
        gate.verify_hash(path, digest)
        gate.verify_hash(path, "sha256:" + digest)
        gate.verify_hash(path, digest.upper())

    def test_a_changed_file_is_refused(self):
        path = self.write()
        digest = gate.sha256_file(path)
        with open(path, "ab") as fh:
            fh.write(b"x")
        with self.assertRaises(gate.Refused):
            gate.verify_hash(path, digest)

    def test_a_malformed_expected_hash_or_missing_file_is_refused(self):
        path = self.write()
        for bad in ("", "abc", "z" * 64, gate.sha256_file(path)[:-1]):
            with self.subTest(bad=bad[:10]):
                with self.assertRaises(gate.Refused):
                    gate.verify_hash(path, bad)
        with self.assertRaises(gate.Refused):
            gate.verify_hash(path + ".missing", "0" * 64)


class Digest(BaseTestCase):
    BASE = [("aws_s3_bucket.a", ["create"], None, {"bucket": "a"}), ("aws_sqs_queue.q", ["update"], {"n": 1}, {"n": 2})]

    def test_stable_across_timestamps_versions_and_order(self):
        a = plan(self.BASE)
        b = plan(list(reversed(self.BASE)), timestamp="2030-01-01T00:00:00Z")
        b["terraform_version"] = "9.9.9"
        self.assertEqual(gate.plan_digest(a), gate.plan_digest(b))
        self.assertTrue(gate.plan_digest(a).startswith("sha256:"))
        self.assertEqual(len(gate.plan_digest(a)), 7 + 64)

    def test_changes_when_anything_that_would_be_applied_changes(self):
        base = gate.plan_digest(plan(self.BASE))
        for name, changed in (
            ("an extra resource", plan(self.BASE + [("aws_vpc.v", ["create"], None, {})])),
            ("an action", plan([("aws_s3_bucket.a", ["delete"], {"bucket": "a"}, None), self.BASE[1]])),
            ("a value", plan([("aws_s3_bucket.a", ["create"], None, {"bucket": "b"}), self.BASE[1]])),
            ("an output", plan(self.BASE, outputs={"o": {"actions": ["create"]}})),
            ("a variable", plan(self.BASE, variables={"v": {"value": "x"}})),
        ):
            with self.subTest(name):
                self.assertNotEqual(gate.plan_digest(changed), base)

    def test_a_plan_that_deletes_and_recreates_differs_from_one_that_only_creates(self):
        self.assertNotEqual(gate.plan_digest(plan([("a.b", ["create"], None, {})])), gate.plan_digest(plan([("a.b", ["delete", "create"], {}, {})])))


class Summary(BaseTestCase):
    def test_lists_addresses_and_actions_and_no_values(self):
        text = gate.plan_summary(plan([("aws_s3_bucket.a", ["create"], None, {"bucket": "secret-value-1"}), ("aws_db_instance.d", ["delete", "create"], {"password": "hunter2"}, {"password": "hunter3"}),
                                      ("aws_sqs_queue.q", ["no-op"], {}, {})]), title="Preview")
        self.assertIn("aws_s3_bucket.a", text)
        self.assertIn("create", text)
        self.assertIn("replace", text)
        self.assertNotIn("aws_sqs_queue.q", text)
        for secret in ("secret-value-1", "hunter2", "hunter3"):
            self.assertNotIn(secret, text)
        self.assertTrue(text.startswith(gate.COMMENT_MARKER))
        self.assertIn("1 create, 1 replace", text)
        self.assertIn("Plan digest: `sha256:", text)

    def test_no_changes(self):
        self.assertIn("No resource changes", gate.plan_summary(plan([("a.b", ["no-op"], {}, {})])))

    def test_hostile_addresses_are_neutralised(self):
        addr = 'aws_x.y["`</code>|<img src=x onerror=1>\n@octocat"]'
        text = gate.plan_summary(plan([(addr, ["create"], None, {})]))
        row = [l for l in text.splitlines() if l.startswith("| `aws_x")][0]
        self.assertNotIn("<img", row)
        self.assertNotIn("</code>", row)
        self.assertEqual(row.count("\n"), 0)
        self.assertNotIn("`</", row)
        # the pipe is escaped so it cannot add a column
        self.assertEqual(row.replace("\\|", "").count("|"), 3)

    def test_long_lists_are_capped(self):
        text = gate.plan_summary(plan([("aws_x.r%d" % i, ["create"], None, {}) for i in range(gate.MAX_ROWS + 25)]))
        self.assertIn("25 more", text)
        self.assertLess(len(text), 60000)


class RunApproval(BaseTestCase):
    def entry(self, login, env="production", state="approved"):
        return {"state": state, "comment": "", "environments": [{"name": env}], "user": {"login": login}}

    def test_a_listed_approver_of_the_environment_counts(self):
        self.assertEqual(gate.require_run_approval([self.entry("Alice")], "production", "alice,bob", []), "alice")

    def test_no_approvals_means_the_environment_has_no_reviewers(self):
        with self.assertRaises(gate.Refused) as cm:
            gate.require_run_approval([], "production", "alice", [])
        self.assertIn("no required reviewers", str(cm.exception))

    def test_wrong_environment_state_or_person_do_not_count(self):
        for name, entries in (
            ("other environment", [self.entry("alice", env="staging")]),
            ("rejected", [self.entry("alice", state="rejected")]),
            ("not listed", [self.entry("mallory")]),
            ("a bot", [self.entry("factory-app[bot]")]),
            ("not a list", {"state": "approved"}),
            ("junk entries", [None, 3, "x", {}]),
        ):
            with self.subTest(name):
                with self.assertRaises(gate.Refused):
                    gate.require_run_approval(entries, "production", "alice,factory-app[bot]", ["factory-bot"])

    def test_the_named_bot_never_counts(self):
        with self.assertRaises(gate.Refused):
            gate.require_run_approval([self.entry("factory-bot")], "production", "alice,factory-bot", ["factory-bot"])

    def test_an_empty_approver_list_refuses(self):
        with self.assertRaises(gate.Refused):
            gate.require_run_approval([self.entry("alice")], "production", "", [])


class Cli(BaseTestCase):
    def run_gate(self, *args):
        return subprocess.run([sys.executable, GATE] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_authorize_exit_codes(self):
        ok = self.run_gate("authorize", "--actor", "alice", "--approvers", "alice", "--bot", "robot")
        self.assertEqual((ok.returncode, ok.stderr), (0, ""))
        no = self.run_gate("authorize", "--actor", "robot", "--approvers", "alice,robot", "--bot", "robot")
        self.assertEqual(no.returncode, 1)
        self.assertIn("refused", no.stderr)

    def test_base_is_main_exit_codes(self):
        self.assertEqual(self.run_gate("base-is-main", "--planned-sha", SHA_A, "--main-sha", SHA_A).returncode, 0)
        self.assertEqual(self.run_gate("base-is-main", "--planned-sha", SHA_A, "--main-sha", SHA_B).returncode, 1)

    def test_a_usage_error_is_not_success(self):
        self.assertNotEqual(self.run_gate().returncode, 0)
        self.assertNotEqual(self.run_gate("authorize").returncode, 0)
        self.assertNotEqual(self.run_gate("no-such-command").returncode, 0)

    def test_hash_digest_and_summary_round_trip(self):
        d = self.tmpdir()
        blob = os.path.join(d, "tfplan")
        with open(blob, "wb") as fh:
            fh.write(b"x")
        digest = self.run_gate("hash-file", blob).stdout.strip()
        self.assertEqual(self.run_gate("verify-hash", "--file", blob, "--sha256", digest).returncode, 0)
        self.assertEqual(self.run_gate("verify-hash", "--file", blob, "--sha256", "0" * 64).returncode, 1)
        pj = os.path.join(d, "plan.json")
        with open(pj, "w") as fh:
            json.dump(plan([("aws_s3_bucket.a", ["create"], None, {"bucket": "x"})]), fh)
        self.assertTrue(self.run_gate("plan-digest", "--plan-json", pj).stdout.startswith("sha256:"))
        self.assertIn("aws_s3_bucket.a", self.run_gate("plan-summary", "--plan-json", pj).stdout)

    def test_a_non_plan_json_is_refused(self):
        d = self.tmpdir()
        bad = os.path.join(d, "x.json")
        with open(bad, "w") as fh:
            fh.write('{"hello": 1}')
        self.assertEqual(self.run_gate("plan-digest", "--plan-json", bad).returncode, 1)
        self.assertEqual(self.run_gate("plan-digest", "--plan-json", os.path.join(d, "missing.json")).returncode, 1)

    def test_require_run_approval_cli(self):
        d = self.tmpdir()
        path = os.path.join(d, "a.json")
        with open(path, "w") as fh:
            json.dump([{"state": "approved", "environments": [{"name": "production"}], "user": {"login": "alice"}}], fh)
        ok = self.run_gate("require-run-approval", "--approvals-json", path, "--environment", "production", "--approvers", "alice")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        no = self.run_gate("require-run-approval", "--approvals-json", path, "--environment", "staging", "--approvers", "alice")
        self.assertEqual(no.returncode, 1)
        self.assertEqual(self.run_gate("require-run-approval", "--approvals-json", path + ".missing", "--environment", "production", "--approvers", "alice").returncode, 1)
