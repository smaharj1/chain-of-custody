"""evidence: the human-committed evidence gate (IMP-1)."""
from __future__ import annotations

import copy
import json
import os
import unittest

from kitlib import evidence
from tests.helpers import BaseTestCase

GOOD = {
    "schema": "evidence.v1",
    "fixture_runs": [{"id": "run-1", "clean": True}, {"id": "run-2", "clean": True}, {"id": "run-3", "clean": True}],
    "real_feature": {"id": "feat-1", "completed": True},
    "attested_by": "A. Maintainer",
    "attested_at": "2026-12-01",
    "notes": "extra fields are allowed",
}


class EvidenceTests(BaseTestCase):
    def put(self, obj, raw=None):
        repo = self.tmpdir()
        path = os.path.join(repo, evidence.EVIDENCE_PATH)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(obj))
        return repo

    def test_absent(self):
        repo = self.tmpdir()
        st = evidence.check(repo)
        self.assertFalse(st)
        self.assertFalse(evidence.passed(repo))
        self.assertIn("absent", st.reason)

    def test_complete_file_passes(self):
        repo = self.put(GOOD)
        self.assertTrue(evidence.passed(repo))
        st = evidence.check(repo)
        self.assertEqual(st.reason, "")
        self.assertEqual(st.data["attested_by"], "A. Maintainer")

    def mutate(self, fn):
        obj = copy.deepcopy(GOOD)
        fn(obj)
        return self.put(obj)

    def test_incomplete_variants_fail_with_a_reason(self):
        rows = [
            ("wrong schema", lambda o: o.update(schema="evidence.v0"), "schema"),
            ("missing schema", lambda o: o.pop("schema"), "schema"),
            ("two runs", lambda o: o["fixture_runs"].pop(), "need 3"),
            ("one run not clean", lambda o: o["fixture_runs"][0].update(clean=False), "found 2"),
            ("clean is truthy but not true", lambda o: o["fixture_runs"][0].update(clean="yes"), "found 2"),
            ("duplicate ids", lambda o: o["fixture_runs"][1].update(id="run-1"), "unique"),
            ("run without id", lambda o: o["fixture_runs"][2].pop("id"), "found 2"),
            ("runs not a list", lambda o: o.update(fixture_runs="3"), "fixture_runs"),
            ("no real feature", lambda o: o.pop("real_feature"), "real_feature"),
            ("feature not completed", lambda o: o["real_feature"].update(completed=False), "real_feature"),
            ("no attestation", lambda o: o.pop("attested_by"), "attested_by"),
            ("blank attestation", lambda o: o.update(attested_by="  "), "attested_by"),
            ("placeholder in angle brackets", lambda o: o.update(attested_at="<date>"), "placeholders"),
            ("TODO placeholder", lambda o: o["real_feature"].update(pr="TODO: add link"), "placeholders"),
            ("nested placeholder", lambda o: o["fixture_runs"][0].update(ref="<result file>"), "placeholders"),
        ]
        for label, fn, frag in rows:
            with self.subTest(label):
                repo = self.mutate(fn)
                st = evidence.check(repo)
                self.assertFalse(st)
                self.assertFalse(evidence.passed(repo))
                self.assertIn(frag, st.reason)

    def test_corrupt_and_wrong_shape(self):
        self.assertIn("not valid JSON", evidence.check(self.put(None, raw="{nope")).reason)
        self.assertIn("JSON object", evidence.check(self.put(None, raw="[1,2]")).reason)
        self.assertFalse(evidence.passed(self.put(None, raw="")))

    def test_the_real_repo_has_no_evidence_file(self):
        # By design (IMP-1): the gate cannot pass in this build.
        from tests import REPO_ROOT
        self.assertFalse(evidence.passed(REPO_ROOT))

    def test_status_is_falsy_and_truthy(self):
        self.assertFalse(evidence.EvidenceStatus(False, "x"))
        self.assertTrue(evidence.EvidenceStatus(True))


if __name__ == "__main__":
    unittest.main()
