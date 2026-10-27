"""caller: the §5.7 classifier. Rows live in tests/data/caller_cases.json."""
from __future__ import annotations

import json
import os
import unittest

from kitlib import caller

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "caller_cases.json")


class CallerTable(unittest.TestCase):
    def test_table(self):
        with open(DATA) as fh:
            data = json.load(fh)
        reg = data["registry"]
        for row in data["rows"]:
            with self.subTest(row["name"]):
                c = caller.classify(row["input"], reg)
                self.assertEqual(c.kind, row["kind"], c.reason)
                self.assertIn(c.kind, caller.KINDS)
                if "role" in row:
                    self.assertEqual(c.role, row["role"])
                self.assertEqual(c.is_conductor, row["kind"] == "conductor")
                self.assertEqual(c.restricted, row["kind"] in ("unknown", "unknown-subagent"))
                self.assertTrue(c.reason)

    def test_custom_roster_extends_the_defaults(self):
        roster = dict(caller.DEFAULT_ROSTER)
        roster["acceptance-author"] = caller.ENGINEER
        roster["house-reviewer"] = caller.CRITIC
        c = caller.classify({"session_id": "s", "agent_id": "a", "agent_type": "house-reviewer"}, {}, roster)
        self.assertEqual(c.kind, caller.CRITIC)
        c = caller.classify({"session_id": "s", "agent_id": "a", "agent_type": "acceptance-author"}, {}, roster)
        self.assertEqual(c.kind, caller.ENGINEER)
        # with the default roster the same name is just an unknown subagent: engineer rules
        c = caller.classify({"session_id": "s", "agent_id": "a", "agent_type": "house-reviewer"}, {})
        self.assertEqual(c.kind, caller.ENGINEER)

    def test_roster_cannot_make_a_subagent_a_conductor(self):
        c = caller.classify({"session_id": "s", "agent_id": "a", "agent_type": "conductor"}, {"s": {"role": "conductor"}},
                            {"conductor": caller.CONDUCTOR})
        self.assertNotEqual(c.kind, caller.CONDUCTOR)

    def test_normalize_agent_type(self):
        self.assertEqual(caller.normalize_agent_type("plugin:backend-engineer"), "backend-engineer")
        self.assertEqual(caller.normalize_agent_type("a:b:c"), "c")
        self.assertEqual(caller.normalize_agent_type(" x "), "x")
        self.assertIsNone(caller.normalize_agent_type("plugin:"))
        self.assertIsNone(caller.normalize_agent_type(None))
        self.assertIsNone(caller.normalize_agent_type(3))

    def test_to_dict_is_json_serialisable(self):
        c = caller.classify({"session_id": "s", "agent_id": "a", "agent_type": "app-engineer"}, {})
        self.assertEqual(json.loads(json.dumps(c.to_dict()))["kind"], "engineer")

    def test_registry_values_are_not_trusted_blindly(self):
        # a registry record that is not a mapping must not crash the guard
        c = caller.classify({"session_id": "s"}, {"s": {"role": ["conductor"]}})
        self.assertEqual(c.kind, caller.UNKNOWN)
        c = caller.classify({"session_id": "s"}, {"s": "conductor"})
        self.assertEqual(c.kind, caller.UNKNOWN)
        c = caller.classify({"session_id": "s"}, None)
        self.assertEqual(c.kind, caller.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
