"""Telemetry (report §7.3): one file per agent_id, redacted, diagnostic only, no hash chain; heartbeat; never blocks."""
from __future__ import annotations

import hashlib
import io
import json
import os
import unittest

from guards import cli, telemetry
from guards.lib import Ctx
from kitlib import runstate

from tests.helpers import BaseTestCase


def _read(path):
    with open(path) as fh:
        return fh.read()


class TelemetryTests(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo_ = self.repo()
        self.st = runstate.RunState.for_path(self.repo_.path).ensure()

    def fire(self, event, **extra):
        hook = {"session_id": "sess-1", "cwd": self.repo_.path, "hook_event_name": event}
        hook.update(extra)
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main([event, "--id", {"PostToolUse": "post-telemetry", "SubagentStart": "subagent-start", "SubagentStop": "subagent-stop",
                                        "PermissionDenied": "permission-denied"}[event]], stdin=io.StringIO(json.dumps(hook)), stdout=out, stderr=err)
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_each_agent_id_writes_its_own_file(self) -> None:
        self.fire("PostToolUse", tool_name="Read", agent_id="a1", agent_type="backend-engineer", tool_use_id="t1", tool_response={})
        self.fire("PostToolUse", tool_name="Read", agent_id="a2", agent_type="backend-engineer", tool_use_id="t2", tool_response={})
        self.fire("PostToolUse", tool_name="Bash", tool_use_id="t3", tool_response={})
        names = sorted(os.listdir(self.st.events_dir))
        self.assertEqual(names, ["a-a1.jsonl", "a-a2.jsonl", "s-sess-1.jsonl"])
        ev = self.st.read_events("a-a1")[0]
        self.assertEqual((ev["event"], ev["tool"], ev["ok"], ev["agent_id"]), ("tool", "Read", True, "a1"))

    def test_no_hash_chain_and_no_command_text(self) -> None:
        self.fire("PostToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "echo hunter2 ghp_" + "A" * 36}, tool_response={"stdout": "hunter2"})
        raw = _read(self.st.event_path("s-sess-1"))
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("prev", json.loads(raw))
        self.assertNotIn("hash", json.loads(raw))

    def test_secrets_are_redacted_at_write_time(self) -> None:
        self.fire("PostToolUse", tool_name="Bash", tool_use_id="ghp_" + "B" * 36, tool_response={})
        raw = _read(self.st.event_path("s-sess-1"))
        self.assertNotIn("ghp_" + "B" * 36, raw)
        self.assertIn("REDACTED", raw)

    def test_error_result_is_ok_false(self) -> None:
        self.fire("PostToolUse", tool_name="Bash", tool_use_id="t1", tool_response={"is_error": True})
        self.assertFalse(self.st.read_events("s-sess-1")[0]["ok"])

    def test_subagent_start_classifies_the_parent_per_5_7(self) -> None:
        self.fire("SubagentStart", agent_id="a1", agent_type="backend-engineer", model="opus")
        ev = self.st.read_events("a-a1")[0]
        self.assertEqual((ev["event"], ev["agent_type"], ev["model"], ev["parent_kind"], ev["parent_session"]),
                         ("subagent_start", "backend-engineer", "opus", "unknown", "sess-1"))
        self.st.register_session("sess-1", "conductor")
        self.fire("SubagentStart", agent_id="a2", agent_type="code-reviewer")
        self.assertEqual(self.st.read_events("a-a2")[0]["parent_kind"], "conductor")

    def test_subagent_stop_records_the_hash_and_the_report_path(self) -> None:
        msg = "Done.\n{\"status\": \"APPROVED\", \"report_path\": \"docs/features/x/reports/backend.md\", \"iteration\": 1}"
        self.fire("SubagentStop", agent_id="a1", agent_type="backend-engineer", last_assistant_message=msg)
        ev = self.st.read_events("a-a1")[0]
        self.assertEqual(ev["last_message_sha256"], hashlib.sha256(msg.encode()).hexdigest())
        self.assertEqual(ev["report_path"], "docs/features/x/reports/backend.md")
        self.fire("SubagentStop", agent_id="a3", agent_type="backend-engineer", last_assistant_message="no trailer")
        self.assertIsNone(self.st.read_events("a-a3")[0]["report_path"])

    def test_permission_denied_is_the_stall_signal(self) -> None:
        self.fire("PermissionDenied", tool_name="Bash", reason="denied by rule")
        ev = self.st.read_events("s-sess-1")[0]
        self.assertEqual((ev["event"], ev["tool"]), ("permission_denied", "Bash"))

    def test_post_tool_use_stamps_the_lock_heartbeat_of_the_owner_only(self) -> None:
        self.st.acquire_lock({"runId": "r1", "sessionId": "sess-1", "heartbeat": "2020-01-01T00:00:00Z", "startedAt": "2020-01-01T00:00:00Z"})
        self.fire("PostToolUse", tool_name="Read", agent_id="a1", agent_type="backend-engineer", tool_response={})
        self.assertEqual(self.st.read_lock()["heartbeat"], "2020-01-01T00:00:00Z", "a subagent's activity does not stamp the conductor's heartbeat")
        self.fire("PostToolUse", tool_name="Read", tool_response={})
        self.assertNotEqual(self.st.read_lock()["heartbeat"], "2020-01-01T00:00:00Z")
        self.st.release_lock("sess-1")
        self.st.acquire_lock({"runId": "r2", "sessionId": "someone-else", "heartbeat": "2020-01-01T00:00:00Z", "startedAt": "2020-01-01T00:00:00Z"})
        self.fire("PostToolUse", tool_name="Read", tool_response={})
        self.assertEqual(self.st.read_lock()["heartbeat"], "2020-01-01T00:00:00Z")

    def test_telemetry_never_blocks_when_the_state_dir_is_unwritable(self) -> None:
        os.chmod(self.st.events_dir, 0o500)
        try:
            ctx = Ctx({"session_id": "sess-1", "cwd": self.repo_.path, "tool_name": "Bash"})
            telemetry.record_deny(ctx, "guard-git:push")          # swallowed
        finally:
            os.chmod(self.st.events_dir, 0o700)

    def test_outside_a_repository_nothing_is_written_and_nothing_fails(self) -> None:
        d = self.tmpdir()
        hook = {"session_id": "sess-1", "cwd": d, "hook_event_name": "PostToolUse", "tool_name": "Read"}
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["PostToolUse", "--id", "post-telemetry"], stdin=io.StringIO(json.dumps(hook)), stdout=out, stderr=err), 0)
        self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
