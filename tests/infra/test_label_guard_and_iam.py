"""IN-15 (label-guard and gate.py normalise logins and labels the same way) and IN-13 (plan-readonly policy shape)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from infra import gate

from tests import REPO_ROOT

LABEL_GUARD = os.path.join(REPO_ROOT, "templates", "github", "workflows", "label-guard.yml")
POLICY = os.path.join(REPO_ROOT, "templates", "security", "iam", "plan-readonly.policy.json")


def guard_script():
    with open(LABEL_GUARD, "r", encoding="utf-8") as fh:
        text = fh.read()
    block = text.split("run: |\n", 1)[1]
    return "\n".join(l[10:] if l.startswith("          ") else l for l in block.split("\n"))


class LabelGuardMatchesGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = guard_script()
        cls.dir = tempfile.mkdtemp()
        cls.log = os.path.join(cls.dir, "calls.log")
        with open(os.path.join(cls.dir, "gh"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> %s\n' % cls.log)
        os.chmod(os.path.join(cls.dir, "gh"), 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def guard_keeps(self, label, sender, approvers):
        """True when label-guard leaves the label alone (it treats `sender` as an approver)."""
        if os.path.exists(self.log):
            os.unlink(self.log)
        env = {"PATH": self.dir + os.pathsep + "/usr/bin:/bin", "REPO": "acme/widgets", "LABEL": label, "SENDER": sender, "NUMBER": "7", "APPROVERS": approvers}
        p = subprocess.run(["sh", "-c", self.script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return not os.path.exists(self.log)

    @staticmethod
    def gate_approves(sender, approvers):
        try:
            gate.authorize([sender], approvers, [])
            return True
        except gate.Refused:
            return False

    CASES = [("Alice", "alice"), ("alice", "Alice"), ("ALICE", "alice, bob"), ("@alice", "alice"), ("alice", "@Alice"), ("bob", "Alice, BOB"),
             ("carol", "alice, bob"), ("alic", "alice"), ("alice", "alicia"), ("alice", ""), ("alice", "alice bob"), ("bob", "alice bob"), ("Bob", "alice\tbob"),
             ("alice[bot]", "alice"), ("al ice", "alice")]

    def test_the_guard_and_gate_py_agree_on_who_is_an_approver(self):
        for sender, approvers in self.CASES:
            with self.subTest(sender=sender, approvers=approvers):
                self.assertEqual(self.guard_keeps("factory:go", sender, approvers), self.gate_approves(sender, approvers))

    def test_a_label_spelled_in_another_case_is_still_guarded(self):
        for label in ("Factory:GO", "FACTORY:go", "Plan-OK", "PREVIEW", "factory:Feature"):
            with self.subTest(label):
                self.assertFalse(self.guard_keeps(label, "mallory", "alice"))
                self.assertTrue(self.guard_keeps(label, "ALICE", "alice"))

    def test_a_label_the_guard_does_not_own_is_left_alone(self):
        for label in ("bug", "Factory:Requested", "FACTORY:REQUESTED", "plan-okay"):
            with self.subTest(label):
                self.assertTrue(self.guard_keeps(label, "mallory", "alice"))

    def test_an_upper_case_factory_go_from_a_stranger_becomes_requested(self):
        self.guard_keeps("Factory:GO", "mallory", "alice")
        with open(self.log) as fh:
            calls = fh.read().strip().split("\n")
        self.assertEqual(calls[0], "api -X DELETE repos/acme/widgets/issues/7/labels/Factory%3AGO")
        self.assertIn("labels[]=factory:requested", calls[1])

    def test_a_glob_character_in_the_approver_list_matches_no_file(self):
        self.assertFalse(self.guard_keeps("factory:go", "mallory", "*"))
        self.assertFalse(self.guard_keeps("factory:go", "x", "?"))


class PlanReadonlyPolicy(unittest.TestCase):
    def setUp(self):
        with open(POLICY, "r", encoding="utf-8") as fh:
            self.doc = json.load(fh)
        self.deny = [s for s in self.doc["Statement"] if s["Effect"] == "Deny"]

    def test_no_deny_depends_on_a_resource_arn_condition(self):
        self.assertNotIn("aws:ResourceArn", json.dumps(self.doc))
        for st in self.deny:
            self.assertNotIn("Condition", st)

    def test_secret_values_are_denied_everywhere(self):
        st = [s for s in self.deny if "secretsmanager:GetSecretValue" in s["Action"]][0]
        self.assertEqual(st["Resource"], "*")
        for a in ("secretsmanager:GetSecretValue", "ssm:GetParameter*", "kms:Decrypt"):
            self.assertIn(a, st["Action"])
        self.assertNotIn("s3:GetObject", st["Action"])

    def test_object_reads_are_denied_outside_the_state_bucket_with_notresource(self):
        st = [s for s in self.deny if s["Action"] == ["s3:GetObject"]][0]
        self.assertEqual(st["NotResource"], ["arn:aws:s3:::acme-terraform-state/*"])
        self.assertNotIn("Resource", st)

    def test_the_state_bucket_stays_readable(self):
        allow = [s for s in self.doc["Statement"] if s["Effect"] == "Allow" and "s3:GetObject" in s["Action"]]
        self.assertEqual(len(allow), 1)
        self.assertIn("arn:aws:s3:::acme-terraform-state/*", allow[0]["Resource"])

    def test_it_allows_nothing_that_writes(self):
        import re
        for st in self.doc["Statement"]:
            if st["Effect"] == "Allow":
                # a write verb at the start of the action name (`lambda:GetFunctionEventInvokeConfig` is a read, `lambda:InvokeFunction` is not), or a secret read anywhere
                self.assertFalse([a for a in st["Action"]
                                  if re.search(r"^[\w-]+:(Create|Put|Update|Delete|Attach|Run|Start|Terminate|Invoke)", a) or re.search(r"(Decrypt|GetSecretValue)", a)])


if __name__ == "__main__":
    unittest.main()
