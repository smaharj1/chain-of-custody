"""tools/probes/analyze.py against synthetic logs: each probe's answer must come out of the fields."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

from tests.helpers import BaseTestCase, clean_env
from tests.probes import PROBES_DIR, LogBuilder, load_tool

ANALYZE = os.path.join(PROBES_DIR, "analyze.py")
analyze = load_tool("analyze")


class AnalyzeCase(BaseTestCase):
    def report(self, probe, builder, manifest=None, as_json=False):
        """Run analyze.main on the builder's log; return (exit code, stdout)."""
        d = self.tmpdir()
        log = builder.write(os.path.join(d, "scratch.observe.jsonl"))
        args = [probe, log]
        if manifest is not None:
            mpath = os.path.join(d, "m.json")
            with open(mpath, "w") as fh:
                json.dump(manifest, fh)
            args += ["--manifest", mpath]
        if as_json:
            args.append("--json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = analyze.main(args)
        return code, buf.getvalue()

    def facts(self, probe, builder, manifest=None):
        code, out = self.report(probe, builder, manifest, as_json=True)
        self.assertEqual(code, 0)
        return json.loads(out)["facts"]


class V1(AnalyzeCase):
    def test_agent_id_without_agent_type_at_depth_2_means_agent_blind_is_required(self):
        b = LogBuilder()
        b.event("SessionStart", source="startup")
        b.bash("echo probe-v1-depth-1", agent_id="a1", agent_type="probe-outer")
        b.bash("echo probe-v1-depth-2", agent_id="a2")
        b.bash("echo probe-v1-depth-3", agent_id="a3")
        code, out = self.report("V1", b)
        self.assertEqual(code, 0)
        self.assertIn("probe-v1-depth-2: agent_id present, agent_type ABSENT", out)
        self.assertIn("agent-blind rule is REQUIRED", out)
        f = self.facts("V1", b)["by_label"]
        self.assertEqual(f["probe-v1-depth-1"], {"agent_id_present": True, "agent_type": "probe-outer", "n": 1})
        self.assertIsNone(f["probe-v1-depth-3"]["agent_type"])

    def test_both_fields_at_every_depth(self):
        b = LogBuilder()
        for i, role in enumerate(["probe-outer", "probe-middle", "probe-leaf"], 1):
            b.bash("echo probe-v1-depth-%d" % i, agent_id="a%d" % i, agent_type=role)
        _, out = self.report("V1", b)
        self.assertIn("roster-aware guards can go beyond depth 1", out)

    def test_missing_agent_id_at_depth_is_a_blocker(self):
        b = LogBuilder()
        b.bash("echo probe-v1-depth-1", agent_id="a1", agent_type="probe-outer")
        b.bash("echo probe-v1-depth-2")
        _, out = self.report("V1", b)
        self.assertIn("ABSENT at a nested depth", out)

    def test_driver_child_shows_agent_type_without_agent_id(self):
        b = LogBuilder()
        b.event("SessionStart", session="child", source="startup", agent_type="probe-outer")
        b.bash("echo probe-v1-depth-1", session="child", agent_type="probe-outer")
        f = self.facts("V1", b)
        self.assertEqual(f["session_start_agent_types"], [{"session": "child", "agent_type": "probe-outer"}])
        self.assertFalse(f["by_label"]["probe-v1-depth-1"]["agent_id_present"])

    def test_empty_log_is_reported_not_crashed(self):
        code, out = self.report("V1", LogBuilder())
        self.assertEqual(code, 0)
        self.assertIn("The log is EMPTY", out)
        self.assertIn("no probe-v1 call reached the log", out)


class V9(AnalyzeCase):
    def log(self, bash_outcome, read_outcome, read_output):
        b = LogBuilder()
        b.marker("v9-crash-pretooluse")
        b.bash("echo probe-v9-bash", outcome=bash_outcome, output="probe-v9-bash")
        b.event("PermissionRequest", tool_name="Write", tool_input={"file_path": "probe-v9-write.txt"})
        b.event("ConfigChange", source="local_settings")
        b.tool("Read", {"file_path": "probe-v9-secret.txt"}, outcome=read_outcome, output=read_output)
        return b

    def test_fail_closed_when_the_command_never_ran(self):
        _, out = self.report("V9", self.log("no-post", "ok", "secret-marker-v9"), {"variant": "crash"})
        self.assertIn("did NOT run", out)
        self.assertIn("secret visible", out)
        f = self.facts("V9", self.log("no-post", "ok", "secret-marker-v9"), {"variant": "crash"})
        self.assertEqual(f["pretooluse_bash"], ["no-post"])
        self.assertEqual(f["permissionrequest_write_events"], 1)
        self.assertEqual(f["configchange_events"], 1)

    def test_fail_open_when_the_command_ran(self):
        _, out = self.report("V9", self.log("ok", "failed", "denied by rule"), {"variant": "timeout", "control": True})
        self.assertIn("FAIL-OPEN", out)
        self.assertIn("CONTROL", out)
        self.assertIn("no secret in output", out)


class V18(AnalyzeCase):
    def test_parent_kept_going_while_the_child_was_slow(self):
        c = LogBuilder()
        c.event("SessionStart", session="p")
        c.event("PreToolUse", "p", 0.5, tool_name="Bash", tool_input={"command": "sh .claude/probe/slow.sh probe-v18-slow-done"},
                tool_use_id="slow1", agent_id="c1", agent_type="probe-slow")
        c.bash("echo probe-v18-parent-after", session="p", dt=2)
        c.event("PostToolUse", "p", 20, tool_name="Bash", tool_input={"command": "sh .claude/probe/slow.sh probe-v18-slow-done"},
                tool_use_id="slow1", tool_response={"stdout": "probe-v18-slow-done"}, agent_id="c1", agent_type="probe-slow")
        f = self.facts("V18", c, {"variant": "fork-on"})
        row = f["sessions"][0]
        self.assertFalse(row["parent_waited"])
        self.assertLess(row["after_minus_slow_end_s"], 0)
        _, out = self.report("V18", c, {"variant": "fork-on"})
        self.assertIn("parent_waited: False", out)
        self.assertIn("BEFORE the slow child finished", out)

    def test_parent_waited_and_nested_driver_shape(self):
        b = LogBuilder()
        b.event("SessionStart", session="d", agent_type="probe-spawner", source="startup")
        b.bash("sh .claude/probe/slow.sh probe-v18-slow-done", session="d", agent_id="c1", agent_type="probe-slow",
               run_seconds=20)
        b.bash("echo probe-v18-spawner-after", session="d", agent_id="s1", agent_type="probe-spawner", dt=1)
        f = self.facts("V18", b, {"variant": "fork-off"})
        row = f["sessions"][0]
        self.assertTrue(row["parent_waited"])
        self.assertIn("nested (spawner)", row["shape"])
        self.assertIn("driver child --agent probe-spawner", row["shape"])

    def test_two_sessions_are_separate_rows(self):
        b = LogBuilder()
        for s in ("one", "two"):
            b.bash("sh .claude/probe/slow.sh probe-v18-slow-done", session=s, agent_id="c", agent_type="probe-slow", run_seconds=3)
            b.bash("echo probe-v18-parent-after", session=s)
        self.assertEqual(len(self.facts("V18", b)["sessions"]), 2)


class V20(AnalyzeCase):
    def test_compact_fired_and_the_model_quoted_the_nonce(self):
        b = LogBuilder()
        b.event("SessionStart", source="startup")
        b.bash("echo probe-v20-none")
        b.event("SessionStart", source="compact", dt=5)
        b.marker("v20-compact-inject", source="compact", hook_event_name="SessionStart",
                 output={"hookSpecificOutput": {"additionalContext": "PROBE-V20-NONCE V20-7f3a91"}})
        b.bash("echo probe-v20-nonce V20-7f3a91")
        f = self.facts("V20", b)
        self.assertTrue(f["compact_session_start_fired"])
        self.assertTrue(f["inject_hook_ran_on_compact"])
        self.assertTrue(f["nonce_seen_by_model_after_compact"])
        self.assertFalse(f["nonce_seen_before_compact"])
        _, out = self.report("V20", b)
        self.assertIn("additionalContext works on compact", out)

    def test_compact_never_fires(self):
        b = LogBuilder()
        b.event("SessionStart", source="startup")
        b.bash("echo probe-v20-none")
        f = self.facts("V20", b)
        self.assertFalse(f["compact_session_start_fired"])
        _, out = self.report("V20", b)
        self.assertIn("does not fire on /compact", out)

    def test_fired_but_not_seen(self):
        b = LogBuilder()
        b.event("SessionStart", source="compact")
        b.marker("v20-compact-inject", source="compact")
        b.bash("echo probe-v20-none")
        f = self.facts("V20", b)
        self.assertTrue(f["compact_session_start_fired"])
        self.assertFalse(f["nonce_seen_by_model_after_compact"])


class V24(AnalyzeCase):
    SECRET = "HELDOUT-SECRET-V24\nprobe-v24-x"

    def build(self, plain, loose, strict, main="denied"):
        b = LogBuilder()
        script = "sh .claude/probe/read-heldout.sh probe-v24-%s %s"
        def one(who, readable, agent=True, file="heldout/secret-v24.txt"):
            out = "HELDOUT-SECRET-V24\n" if (readable and file != "README.md") else (
                "PROBE-SCRATCH-README\n" if readable else "cat: Operation not permitted")
            b.bash(script % (who, file), agent_id="a-" + who if agent else None,
                   agent_type="probe-sb-" + who if agent else None, outcome="ok", output=out)
        one("main", main == "read", agent=False)
        one("plain", plain)
        one("loose", loose)
        one("strict", strict, file="README.md")
        return b

    def test_shared_sandbox_is_the_assumed_answer(self):
        b = self.build(plain=False, loose=False, strict=True)
        f = self.facts("V24", b)["results"]
        self.assertEqual(f, {"main": "DENIED/ERROR", "plain": "DENIED/ERROR", "loose": "DENIED/ERROR", "strict": "READ"})
        _, out = self.report("V24", b)
        self.assertIn("shares the parent's sandbox", out)
        self.assertIn("frontmatter sandbox keys are ignored", out)

    def test_subagent_escape_and_per_subagent_sandbox(self):
        b = self.build(plain=True, loose=True, strict=False)
        _, out = self.report("V24", b)
        self.assertIn("NOT under the parent's sandbox", out)
        self.assertIn("boundary hole", out)
        self.assertIn("frontmatter sandbox keys ARE honoured", out)

    def test_main_read_invalidates_the_setup(self):
        _, out = self.report("V24", self.build(True, True, True, main="read"))
        self.assertIn("not enforcing denyRead", out)


class V19(AnalyzeCase):
    def test_refusal_to_start_leaves_an_empty_log(self):
        code, out = self.report("V19", LogBuilder(), {"control": False})
        self.assertEqual(code, 0)
        self.assertIn("REFUSED to start", out)

    def test_unsandboxed_write_succeeds(self):
        b = LogBuilder()
        b.event("SessionStart")
        b.bash("touch probe-v19-inside.txt")
        b.bash("touch ~/.probe-v19-outside")
        _, out = self.report("V19", b)
        self.assertIn("SUCCEEDED", out)

    def test_blocked_write(self):
        b = LogBuilder()
        b.event("SessionStart")
        b.bash("touch ~/.probe-v19-outside", outcome="failed", output="Operation not permitted")
        _, out = self.report("V19", b)
        self.assertIn("blocked/failed", out)


class V5V6V7V8(AnalyzeCase):
    def test_v5_lists_markers_permissions_and_command_results_per_session(self):
        b = LogBuilder()
        b.event("SessionStart", session="child", agent_type="probe-role", source="startup")
        b.marker("v5-project-hook", session="child")
        b.marker("v5-child-settings-hook", session="child")
        b.bash("echo probe-v5-start", session="child", output="probe-v5-start")
        b.bash("git branch --show-current", session="child", output="feature/probe-v5")
        b.bash("touch ~/.probe-v5-outside", session="child", outcome="failed", output="Operation not permitted")
        f = self.facts("V5", b, {"facts": {"feature/probe-v5": "abc1234"}})["sessions"]["child"]
        self.assertEqual(f["markers"], {"v5-project-hook": 1, "v5-child-settings-hook": 1})
        self.assertEqual(f["touch_outside_home"], "failed")
        self.assertEqual(f["commands"]["git branch --show-current"]["output"].count("feature"), 1)

    def test_v6_maps_the_worktree_head_to_the_recorded_commit(self):
        b = LogBuilder()
        b.bash("git rev-parse --short HEAD", agent_id="wt1", agent_type="probe-wt", output="b0f78c6\n")
        manifest = {"facts": {"main": "cd7baa6", "origin/main": "cd7baa6", "feature/probe": "b0f78c6"}}
        f = self.facts("V6", b, manifest)
        self.assertEqual(f["matches"], ["feature/probe"])
        _, out = self.report("V6", b, manifest)
        self.assertIn("branched from the current HEAD", out)

    def test_v7_finds_the_dispatch_nonce(self):
        b = LogBuilder()
        b.event("SubagentStart", agent_id="a1", agent_type="probe-v7", prompt="NONCE-V7-91c4: go")
        self.assertTrue(self.facts("V7", b)["subagent_start_has_prompt"])
        c = LogBuilder()
        c.event("SubagentStart", agent_id="a1", agent_type="probe-v7")
        c.tool("Agent", {"prompt": "NONCE-V7-91c4: go", "subagent_type": "probe-v7"})
        self.assertFalse(self.facts("V7", c)["subagent_start_has_prompt"])
        _, out = self.report("V7", c)
        self.assertIn("does not carry the dispatch prompt", out)

    def test_v8_spots_cost_like_fields(self):
        b = LogBuilder()
        b.event("SubagentStop", agent_id="a1", agent_type="x", last_assistant_message="hi", transcript_path="/t")
        f = self.facts("V8", b)["cost_like_fields"]
        self.assertEqual(f["SubagentStop"], [])
        c = LogBuilder()
        c.event("SubagentStop", agent_id="a1", usage={"input_tokens": 5}, model="m")
        self.assertEqual(self.facts("V8", c)["cost_like_fields"]["SubagentStop"], ["model", "usage", "usage.input_tokens"])
        s = LogBuilder()
        s.marker("v8-statusline", stdin_keys=["cost", "model"], stdin={"cost": {"total_cost_usd": 1}, "model": {"id": "m"}})
        self.assertIn("statusLine", self.facts("V8", s)["cost_like_fields"])


class OtherProbes(AnalyzeCase):
    def test_v4_clean_vs_saw(self):
        b = LogBuilder()
        b.bash("echo probe-v4-clean", agent_id="a", agent_type="probe-clean-agent")
        self.assertIn("starts clean", self.report("V4", b)[1])
        c = LogBuilder()
        c.bash("echo probe-v4-saw-codeword", agent_id="a")
        self.assertIn("does not start clean", self.report("V4", c)[1])

    def test_v12_labels(self):
        b = LogBuilder()
        b.bash("echo probe-v12-keep-knows", agent_id="a")
        b.bash("echo probe-v12-omit-blank", agent_id="b")
        self.assertEqual(self.facts("V12", b)["labels"], ["probe-v12-keep-knows", "probe-v12-omit-blank"])

    def test_v13_plugin_hook_for_plugin_and_project_agents(self):
        b = LogBuilder()
        b.marker("v13-plugin-hook", agent_id="a1", agent_type="probe13:plugin-agent")
        b.marker("v13-plugin-hook", agent_id="a2", agent_type="project-agent")
        b.marker("v13-plugin-root-expanded", env_CLAUDE_PLUGIN_ROOT="/p/probe-plugin13")
        f = self.facts("V13", b)
        self.assertEqual(f["plugin_hook_fired_for_agent_types"], ["probe13:plugin-agent", "project-agent"])
        self.assertEqual(f["root_expanded"], ["/p/probe-plugin13"])

    def test_v14_counts_denials(self):
        b = LogBuilder()
        for i in range(4):
            b.marker("v14-deny")
            b.bash("echo probe-v14-%d" % (i + 1), outcome="no-post")
        f = self.facts("V14", b)
        self.assertEqual((f["deny_hook_invocations"], f["calls_attempted"]), (4, 4))

    def test_v22_rows_per_session(self):
        b = LogBuilder()
        b.event("SessionStart", session="one", agent_type="inline-role")
        b.bash("echo probe-v22-inline-role", session="one")
        b.event("SessionStart", session="two")
        f = self.facts("V22", b)["sessions"]
        self.assertEqual(f[0]["agent_type"], "inline-role")
        self.assertEqual(f[0]["labels"], ["probe-v22-inline-role"])
        self.assertEqual(f[1]["agent_type"], "-")

    def test_v23_workflow_from_a_subagent(self):
        b = LogBuilder()
        b.tool("Workflow", {"name": "probe-v23"}, agent_id="a1", agent_type="probe-wf-caller", outcome="no-post")
        b.bash("echo probe-v23-caller-finished", agent_id="a1", agent_type="probe-wf-caller")
        f = self.facts("V23", b, {"variant": "allow"})
        self.assertEqual(f["workflow_calls"], [{"from_subagent": True, "result": "no-post"}])
        self.assertFalse(f["workflow_ran_marker"])

    def test_v26_and_v21_and_v16(self):
        b = LogBuilder()
        b.bash("echo probe-v26-project-skill")
        self.assertTrue(self.facts("V26", b)["project_skill_ran"])
        c = LogBuilder()
        c.marker("v21-managed-hook")
        c.bash("echo probe-v21-denied-1", outcome="no-post")
        self.assertEqual(self.facts("V21", c)["denied_command_outcomes"], ["no-post"])
        d = LogBuilder()
        d.event("SubagentStart", agent_id="x")
        d.event("TeammateIdle")
        self.assertEqual(self.facts("V16", d)["events"], {"SubagentStart": 1, "TeammateIdle": 1})

    def test_v3_lists_the_pwd_each_agent_saw(self):
        b = LogBuilder()
        b.bash("pwd", agent_id="a1", agent_type="x", output="/tmp/wt-1\n")
        b.bash("echo probe-v3-isolated", agent_id="a1", agent_type="x")
        _, out = self.report("V3", b)
        self.assertIn("/tmp/wt-1", out)
        self.assertIn("probe-v3-isolated", out)

    def test_v17_lists_push_commands(self):
        b = LogBuilder()
        b.bash("git push origin bg-probe", output="ok")
        b.bash("git push origin main", outcome="no-post")
        _, out = self.report("V17", b, {"remote": "/x.remote.git"})
        self.assertIn("git push origin main", out)
        self.assertIn("no-post", out)
        self.assertIn("/x.remote.git", out)

    def test_v25_is_the_v6_analysis_under_its_own_name(self):
        b = LogBuilder()
        b.bash("git rev-parse --short HEAD", agent_id="wt1", agent_type="probe-wt", output="cd7baa6\n")
        manifest = {"facts": {"main": "cd7baa6", "feature/probe": "b0f78c6"}}
        self.assertEqual(self.facts("V25", b, manifest)["matches"], ["main"])
        self.assertIn("V25: worktree.baseRef", self.report("V25", b, manifest)[1])

    def test_non_log_probes_say_so(self):
        for p in ("V15", "V27"):
            code, out = self.report(p, LogBuilder())
            self.assertEqual(code, 0)
            self.assertIn("not settled by the hook log", out)


class LoadingAndCli(AnalyzeCase):
    def test_bad_lines_and_exact_duplicates_are_counted(self):
        b = LogBuilder()
        b.event("SessionStart", source="startup")
        path = b.write(os.path.join(self.tmpdir(), "x.jsonl"))
        with open(path, "a") as fh:
            fh.write("not json\n")
            fh.write("[1,2]\n")
            fh.write(json.dumps(b.rows[0]) + "\n")  # identical event: the same hook from two sources
            fh.write("\n")
        log = analyze.load(path)
        self.assertEqual((len(log.events), log.bad_lines, log.duplicates), (1, 2, 1))

    def test_summary_counts_events_and_identity(self):
        b = LogBuilder()
        b.event("SessionStart")
        b.bash("echo hi", agent_id="a1", agent_type="t")
        f = self.facts("summary", b)
        self.assertEqual(f["event_counts"], {"PostToolUse": 1, "PreToolUse": 1, "SessionStart": 1})

    def test_manifest_found_next_to_the_log(self):
        d = self.tmpdir()
        LogBuilder().write(os.path.join(d, "s.observe.jsonl"))
        with open(os.path.join(d, "s.probe.json"), "w") as fh:
            json.dump({"probe": "V9", "variant": "crash"}, fh)
        self.assertEqual(analyze.load_manifest(None, os.path.join(d, "s.observe.jsonl"))["variant"], "crash")
        self.assertEqual(analyze.load_manifest(None, "/nonexistent/x.observe.jsonl"), {})

    def test_cli_exit_codes_and_text(self):
        log = LogBuilder().bash("echo probe-v1-depth-1", agent_id="a", agent_type="t").write(os.path.join(self.tmpdir(), "l.jsonl"))
        env = clean_env(home=self.tmpdir())
        ok = subprocess.run([sys.executable, ANALYZE, "v1", log], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(b"V1: hook input agent_id", ok.stdout)
        bad = subprocess.run([sys.executable, ANALYZE, "V99", log], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(bad.returncode, 2)
        gone = subprocess.run([sys.executable, ANALYZE, "V1", log + ".nope"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(gone.returncode, 2)

    def test_every_probe_with_a_scratch_has_an_analyzer(self):
        scratch = load_tool("make_scratch")
        for probe in scratch.PROBES:
            self.assertIn(probe, analyze.ANALYZERS, probe)
