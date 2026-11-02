"""The injection corpus (report §5.3 rules 1 and 7, §15 item 4): poisoned README, issue, web page, MCP result, PR comment.

Detection is a guardrail: it annotates and logs, it never blocks. Taint is the part with teeth: a session (or subagent)
that has read untrusted content is marked, and the PreToolUse guards then withhold authority from it
(corpus rows taint-001..004 and exec-107/108 in callers.json and exec.json). Safety cases run at threshold 1.0 in the evals.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from guards import g_taint, lib
from guards.lib import Ctx
from kitlib import runstate

from tests.guards.harness import Fixtures, load_corpus
from tests.helpers.fakehome import clean_env

from tests import REPO_ROOT


class InjectionCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = Fixtures()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.cleanup()

    def hook_for(self, row):
        sid = "sess-inj-%s" % row["id"]
        h = {"session_id": sid, "cwd": self.fx.main.path, "hook_event_name": "PostToolUse", "tool_name": row["tool"],
             "tool_input": self.fx.sub(row["input"]), "tool_response": row["response"]}
        if row.get("caller") == "engineer":
            h["agent_id"] = "agent-inj-%s" % row["id"]
            h["agent_type"] = "backend-engineer"
        return h

    def test_corpus(self) -> None:
        rows = load_corpus("injection.json")
        for row in rows:
            with self.subTest(id=row["id"], what=row["what"]):
                hook = self.hook_for(row)
                ctx = Ctx(hook, env={"HOME": self.fx.home}, state=self.fx.state, config=None)
                self.assertFalse(lib.is_tainted(self.fx.state, hook), "fresh session")
                res = g_taint.check(ctx)
                self.assertEqual(res["tainted"], row["taint"], row["what"])
                self.assertEqual(lib.is_tainted(self.fx.state, hook), row["taint"])
                self.assertEqual(bool(res["hits"]), row["flag"], "%s: hits=%s" % (row["what"], res["hits"]))
                for p in row["patterns"]:
                    self.assertIn(p, res["hits"], row["what"])
                if row["flag"]:
                    self.assertIn("NOTICE", res["context"])
                    self.assertIn("untrusted", res["context"])
                    self.assertNotIn(row["response"] if isinstance(row["response"], str) else "\u0000", res["context"], "the notice does not echo the payload")

    BENIGN = [
        "You should run npm install before building the project.",
        "Run the following commands:\n  npm install\n  npm test",
        "To use this package you must first create an API key in the dashboard.",
        "Assistant professors may apply: the form is at /apply.",
        "The system: a web server and a worker. User: any logged-in person.",
        "Set the environment variables DATABASE_URL and PORT, then start the server.",
        "See the credentials page of the AWS console for how to rotate keys.",
        "If you are an AI language model, this paragraph describes how tokens are counted.",
        "Please read the contributing guide and open a pull request.",
        "git push is how you publish your branch; ask your maintainer first.",
    ]

    def test_ordinary_prose_is_not_flagged(self) -> None:
        for text in self.BENIGN:
            with self.subTest(text=text[:50]):
                hits = g_taint.scan(text)
                self.assertEqual(hits, [] if "git push" not in text else ["push-to-main"], text)

    def test_taint_does_not_leak_between_sessions_or_agents(self) -> None:
        a = {"session_id": "sess-a", "agent_id": "agent-a", "agent_type": "backend-engineer"}
        b = {"session_id": "sess-a", "agent_id": "agent-b", "agent_type": "backend-engineer"}
        lib.mark_tainted(self.fx.state, a, "test")
        self.assertTrue(lib.is_tainted(self.fx.state, a))
        self.assertFalse(lib.is_tainted(self.fx.state, b), "a sibling subagent is not tainted by its sibling")
        lib.mark_tainted(self.fx.state, {"session_id": "sess-parent"}, "test")
        self.assertTrue(lib.is_tainted(self.fx.state, {"session_id": "sess-parent", "agent_id": "child"}), "a subagent inherits its parent session's taint")
        self.assertFalse(lib.is_tainted(self.fx.state, {"session_id": "sess-other"}))

    def test_taint_file_cannot_be_path_traversal(self) -> None:
        self.assertFalse(lib.mark_tainted(self.fx.state, {"session_id": "../../x"}, "test"))
        self.assertFalse(lib.is_tainted(self.fx.state, {"session_id": "../../x"}))

    def test_hook_output_through_the_real_entry_point(self) -> None:
        row = [r for r in load_corpus("injection.json") if r["id"] == "inj-005"][0]
        hook = self.hook_for(row)
        hook["session_id"] = "sess-real-entry"
        proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "run.py"), "PostToolUse", "--id", "post-untrusted"],
                              input=json.dumps(hook).encode(), cwd=self.fx.main.path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=clean_env(self.fx.main.path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.decode())["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("NOTICE", out["additionalContext"])
        events = self.fx.state.read_events("s-sess-real-entry")
        self.assertTrue(any(e.get("event") == "injection_suspected" for e in events))
        self.assertTrue(os.path.exists(os.path.join(self.fx.state.root, "taint", "s-sess-real-entry")))

    def test_clean_page_is_silent_but_taints(self) -> None:
        hook = {"session_id": "sess-quiet", "cwd": self.fx.main.path, "hook_event_name": "PostToolUse", "tool_name": "WebFetch",
                "tool_input": {"url": "https://example.com"}, "tool_response": "Just documentation."}
        proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "run.py"), "PostToolUse", "--id", "post-untrusted"],
                              input=json.dumps(hook).encode(), cwd=self.fx.main.path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=clean_env(self.fx.main.path))
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))
        self.assertTrue(os.path.exists(os.path.join(self.fx.state.root, "taint", "s-sess-quiet")))


if __name__ == "__main__":
    unittest.main()
