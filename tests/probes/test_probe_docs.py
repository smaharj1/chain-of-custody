"""The probe snippets, README, assumptions and results template agree with each other and with the IMP-7 table."""
from __future__ import annotations

import json
import os
import re
import unittest

from tests import REPO_ROOT
from tests.probes import FILES_DIR, PROBES_DIR, README, SETTINGS_DIR, load_tool

ms = load_tool("make_scratch")
IMP = os.path.join(REPO_ROOT, "docs", "design", "v2-implementation-decisions.md")
ASSUMPTIONS = os.path.join(REPO_ROOT, "docs", "design", "probe-assumptions.md")
TEMPLATE = os.path.join(REPO_ROOT, "docs", "design", "probe-results.template.md")
REPORT = os.path.join(REPO_ROOT, "docs", "design", "review-2026-10.md")

CLOSED = ("V2", "V10", "V11")
PRIORITY = ["V1", "V5", "V9", "V18", "V19", "V22", "V24", "V27"]
REQUIRED_SNIPPETS = ["V1", "V5", "V7", "V9", "V18", "V19", "V20", "V22", "V23", "V24"]
KNOWN_EVENTS = set(ms.ALL_EVENTS) | {"SessionStart"}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def norm(text):
    return " ".join(text.split())


def sections(text):
    """README level-2 sections: heading -> body."""
    out, name = {}, None
    for line in text.splitlines():
        m = re.match(r"^## (V\d+)\s*$", line)
        if m:
            name = m.group(1)
            out[name] = []
        elif line.startswith("## "):
            name = None
        elif name:
            out[name].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def snippet_paths():
    return sorted(os.path.join(SETTINGS_DIR, f) for f in os.listdir(SETTINGS_DIR) if f.endswith(".json"))


class Snippets(unittest.TestCase):
    def test_every_snippet_is_a_json_object_named_for_a_known_probe(self):
        self.assertTrue(snippet_paths())
        for path in snippet_paths():
            name = os.path.basename(path)
            with self.subTest(name):
                self.assertRegex(name, r"^V\d+(-[a-z0-9][a-z0-9-]*)?\.json$")
                self.assertIn(name.split("-")[0].split(".")[0], ms.PROBES)
                data = json.loads(read(path))
                self.assertIsInstance(data, dict)
                for ph in re.findall(r"\{\{(\w+)\}\}", read(path)):
                    self.assertIn(ph, ms.PLACEHOLDERS)

    def test_the_snippets_the_decision_lists_exist(self):
        have = {os.path.basename(p).split("-")[0].split(".")[0] for p in snippet_paths()}
        for v in REQUIRED_SNIPPETS:
            self.assertIn(v, have)

    def test_hook_events_and_handlers_are_well_formed(self):
        for path in snippet_paths():
            data = json.loads(read(path))
            for event, entries in data.get("hooks", {}).items():
                with self.subTest(os.path.basename(path), event=event):
                    self.assertIn(event, KNOWN_EVENTS | {"SubagentStop"})
                    for entry in entries:
                        for h in entry["hooks"]:
                            self.assertEqual(h["type"], "command")
                            self.assertTrue(h["command"])
                            self.assertNotIn("http", h["command"])  # no network in a probe

    def test_v9_has_three_variants_each_on_the_three_events_with_on_failure_block(self):
        for variant in ("missing-interpreter", "crash", "timeout"):
            data = json.loads(read(os.path.join(SETTINGS_DIR, "V9-%s.json" % variant)))
            with self.subTest(variant):
                self.assertEqual(sorted(data["hooks"]), ["ConfigChange", "PermissionRequest", "PreToolUse"])
                for event, entries in data["hooks"].items():
                    handler = entries[0]["hooks"][0]
                    self.assertEqual(handler["onFailure"], "block", event)
                    if variant == "missing-interpreter":
                        exe = handler["command"].split()[0]
                        self.assertTrue(exe.startswith("/nonexistent/"))
                        self.assertFalse(os.path.exists(exe))
                    elif variant == "crash":
                        self.assertIn(" crash ", handler["command"])
                    else:
                        m = re.search(r"--seconds (\d+)", handler["command"])
                        self.assertIn(" sleep ", handler["command"])
                        self.assertLess(handler["timeout"], int(m.group(1)))
                self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "Bash")
                self.assertEqual(data["hooks"]["PermissionRequest"][0]["matcher"], "Write")

    def test_v19_and_v24_sandbox_keys_follow_the_report(self):
        v19 = json.loads(read(os.path.join(SETTINGS_DIR, "V19.json")))["sandbox"]
        self.assertEqual((v19["enabled"], v19["failIfUnavailable"], v19["allowUnsandboxedCommands"]), (True, True, False))
        v24 = json.loads(read(os.path.join(SETTINGS_DIR, "V24.json")))["sandbox"]
        self.assertEqual(v24["filesystem"]["denyRead"], ["{{SCRATCH}}/heldout"])

    def test_v20_injects_only_on_compact_and_v18_sets_the_fork_env(self):
        v20 = json.loads(read(os.path.join(SETTINGS_DIR, "V20.json")))["hooks"]["SessionStart"]
        self.assertEqual([e["matcher"] for e in v20], ["compact"])
        on = json.loads(read(os.path.join(SETTINGS_DIR, "V18-fork-on.json")))["env"]
        off = json.loads(read(os.path.join(SETTINGS_DIR, "V18-fork-off.json")))["env"]
        self.assertEqual((on["CLAUDE_CODE_FORK_SUBAGENT"], off["CLAUDE_CODE_FORK_SUBAGENT"]), ("1", "0"))
        nobg = json.loads(read(os.path.join(SETTINGS_DIR, "V18-fork-off-nobg.json")))["env"]
        self.assertEqual(nobg["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_v23_variants_differ_only_in_the_workflow_allow(self):
        allow = json.loads(read(os.path.join(SETTINGS_DIR, "V23-allow.json")))["permissions"]["allow"]
        noallow = json.loads(read(os.path.join(SETTINGS_DIR, "V23-noallow.json")))["permissions"]["allow"]
        self.assertIn("Workflow", allow)
        self.assertNotIn("Workflow", noallow)

    def test_every_probe_file_directory_in_the_table_exists(self):
        for probe, spec in ms.PROBES.items():
            for d in spec.get("files", []):
                self.assertTrue(os.path.isdir(os.path.join(FILES_DIR, d)), "%s: files/%s" % (probe, d))
        self.assertTrue(os.path.isfile(os.path.join(FILES_DIR, "common", "probe_hook.py")))

    def test_no_closed_probe_is_in_the_table(self):
        for v in CLOSED:
            self.assertNotIn(v, ms.PROBES)
            self.assertIn(v, ms.CLOSED)


class Readme(unittest.TestCase):
    def setUp(self):
        self.text = read(README)
        self.secs = sections(self.text)

    def test_one_section_per_open_item_and_none_for_closed(self):
        self.assertEqual(sorted(self.secs, key=lambda v: int(v[1:])),
                         sorted(ms.PROBES, key=lambda v: int(v[1:])))
        for v in CLOSED:
            self.assertNotIn(v, self.secs)
            self.assertIn("**%s**" % v, self.text)  # the one-line note

    def test_open_items_are_exactly_report_section_17_minus_closed(self):
        report = read(REPORT)
        part = report[report.index("## 17. Verify before building"):report.index("## 18. Decisions")]
        listed = set(re.findall(r"^\| (V\d+) \|", part, re.M))
        self.assertEqual(listed - set(CLOSED), set(ms.PROBES))

    def test_priority_items_come_first_in_order(self):
        order = list(self.secs)
        self.assertEqual(order[:len(PRIORITY)], PRIORITY)
        rest = [int(v[1:]) for v in order[len(PRIORITY):]]
        self.assertEqual(rest, sorted(rest))

    def test_each_section_has_purpose_blocks_assumption_observe_and_record(self):
        for v, body in self.secs.items():
            with self.subTest(v):
                for word in ("**Purpose.**", "**Blocks.**", "**Observe.**", "**Record.**"):
                    self.assertIn(word, body)
                self.assertIn("assumes", body)
                self.assertIn("analyze.py", body) if v not in ("V15", "V27") else None

    def test_sections_with_a_scratch_name_their_prepare_command(self):
        for v, body in self.secs.items():
            if ms.PROBES[v].get("scratch") is False:
                continue
            with self.subTest(v):
                self.assertRegex(body, r"make-scratch\.sh \"\$HOME/probes/[^\"]+\"\s+--probe %s\b" % v)

    def test_every_variant_is_prepared_somewhere_in_its_section(self):
        for v in ms.PROBES:
            for variant in ms.variants_of(v):
                if v == "V21":
                    continue
                self.assertIn("--variant %s" % variant, self.secs[v], "%s %s" % (v, variant))

    def test_every_prompt_file_block_is_quoted_verbatim(self):
        seen = 0
        for dirpath, _dirs, files in os.walk(FILES_DIR):
            if "PROMPT.txt" not in files:
                continue
            v = os.path.basename(dirpath)
            blocks = re.split(r"^== .+ ==\n", read(os.path.join(dirpath, "PROMPT.txt")), flags=re.M)
            for block in [b.strip() for b in blocks if b.strip()]:
                seen += 1
                with self.subTest(v, block=block[:40]):
                    self.assertIn(norm(block), norm(self.secs[v]))
        self.assertGreater(seen, 20)

    def test_probe_prompts_exist_for_every_interactive_scratch_probe(self):
        for v, spec in ms.PROBES.items():
            if spec.get("scratch") is False or v in ("V3", "V17", "V21"):
                continue
            has = any(os.path.isfile(os.path.join(FILES_DIR, d, "PROMPT.txt")) for d in spec.get("files", []))
            self.assertTrue(has, v)

    def test_commands_use_only_the_documented_variables(self):
        for name in re.findall(r"\$\{?([A-Z_]+)", self.text):
            self.assertIn(name, {"SCRATCH", "LOG", "HOME", "CLAUDE_PLUGIN_ROOT"}, name)


class Assumptions(unittest.TestCase):
    def setUp(self):
        self.text = read(ASSUMPTIONS)

    def imp7_items(self):
        imp = read(IMP)
        part = imp[imp.index("## IMP-7."):imp.index("## IMP-8.")]
        rows = re.findall(r"^\| (V[^|]+) \|", part, re.M)
        items = set()
        for row in rows:
            items |= set("V" + n for n in re.findall(r"V(\d+)", row))
        return items

    def test_every_imp7_row_has_a_section(self):
        heads = set(re.findall(r"^### (V\d+)\.", self.text, re.M))
        self.assertTrue(self.imp7_items())
        self.assertLessEqual(self.imp7_items(), heads)

    def test_every_report_item_has_a_section(self):
        heads = set(re.findall(r"^### (V\d+)\.", self.text, re.M))
        self.assertEqual(heads, set("V%d" % n for n in range(1, 28)))

    def test_open_items_state_assumption_location_and_consequence(self):
        parts = re.split(r"^### ", self.text, flags=re.M)[1:]
        for part in parts:
            v = part.split(".")[0]
            if v in CLOSED:
                continue
            with self.subTest(v):
                if v in ("V10", "V11", "V2"):
                    continue
                self.assertIn("**Assumption.**", part)
                self.assertIn("**Encoded in.**", part)
                self.assertRegex(part, r"\*\*If (the probe shows )?otherwise\.\*\*|\*\*If the probe shows otherwise\.\*\*")

    def test_v1_points_at_the_real_caller_module(self):
        part = self.text[self.text.index("### V1."):self.text.index("### V2.")]
        self.assertIn(".claude/scripts/kitlib/caller.py", part)
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, ".claude", "scripts", "kitlib", "caller.py")))

    def test_v9_records_that_the_field_is_unverified_and_not_depended_on(self):
        part = self.text[self.text.index("### V9."):self.text.index("### V10.")]
        self.assertIn("unverified", part)
        self.assertIn("nothing depends on them", part)

    def test_unbuilt_paths_are_marked_planned(self):
        for m in re.finditer(r"`((?:\.claude|tests|tools|docs)/[^`\s]+)`( \*\(planned[^)]*\)\*)?", self.text):
            path, planned = m.group(1), m.group(2)
            if "<" in path or "*" in path:
                continue
            exists = os.path.exists(os.path.join(REPO_ROOT, path))
            if not exists:
                # the annotation may follow a list of paths in the same bullet; accept it anywhere in the paragraph
                start = self.text.rfind("\n- ", 0, m.start())
                end = self.text.find("\n- ", m.end())
                para = self.text[start:end if end != -1 else len(self.text)]
                self.assertTrue(planned or "(planned" in para or "planned)" in para or "*(planned" in para,
                                "missing path not marked planned: %s" % path)


class Template(unittest.TestCase):
    def test_header_columns_and_a_row_per_open_item(self):
        text = read(TEMPLATE)
        self.assertIn("| V-item | Variant / cell | Date | Claude Code version | OS | Observation | Verdict | Follow-up |", text)
        rows = set(re.findall(r"^\| (V\d+) \|", text, re.M))
        self.assertEqual(rows, set(ms.PROBES))
        for word in ("confirms", "overturns", "inconclusive"):
            self.assertIn(word, text)

    def test_priority_rows_cover_their_cells(self):
        text = read(TEMPLATE)
        self.assertEqual(len(re.findall(r"^\| V9 \|", text, re.M)), 10)  # 3 variants x 3 events + control
        self.assertEqual(len(re.findall(r"^\| V27 \|", text, re.M)), 3)
        self.assertIn("devcontainer", text)


class Tracked(unittest.TestCase):
    def test_no_probe_file_is_git_ignored(self):
        """A file the scratch needs must be committable (e.g. a settings.local.json would silently vanish)."""
        import subprocess
        paths = []
        for dirpath, dirnames, filenames in os.walk(PROBES_DIR):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            paths += [os.path.relpath(os.path.join(dirpath, f), REPO_ROOT) for f in filenames if not f.endswith(".pyc")]
        self.assertGreater(len(paths), 50)
        proc = subprocess.run(["git", "-C", REPO_ROOT, "check-ignore", "--stdin"], input="\n".join(paths).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.stdout.decode().split(), [])

    def test_shell_scripts_in_files_are_executable_and_parse(self):
        import subprocess
        for dirpath, _d, filenames in os.walk(FILES_DIR):
            for f in filenames:
                if f.endswith(".sh"):
                    path = os.path.join(dirpath, f)
                    self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0, path)
