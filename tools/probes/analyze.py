#!/usr/bin/env python3
"""Read the hook-input log of a probe run and print the fields that answer the probe (IMP-7).

    python3 tools/probes/analyze.py V1 /path/to/scratch.observe.jsonl [--json] [--manifest FILE]
    python3 tools/probes/analyze.py summary /path/to/scratch.observe.jsonl

The log is the JSON-lines file tools/baseline/observe.py writes (one `{"observed_at", "event"}`
line per hook event) plus the `{"observed_at", "marker"}` lines tools/probes/files/common/
probe_hook.py writes. This script only reads. It states what the log shows and a "reading" for
each answer; the verdict is the human's, recorded in docs/design/probe-results.template.md.
Exit codes: 0 report printed (even if the log is empty: that is a finding), 2 usage or unreadable log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

AGENT_TOOLS = ("Agent", "Task")
POST_EVENTS = ("PostToolUse", "PostToolUseFailure")


# ---------------------------------------------------------------- loading

class Rec(object):
    """One log line: kind is 'event' (hook stdin from observe.py) or 'marker' (probe_hook.py)."""
    __slots__ = ("at", "t", "kind", "data", "idx")

    def __init__(self, at: str, kind: str, data: Dict[str, Any], idx: int) -> None:
        self.at = at
        self.t = parse_time(at)
        self.kind = kind
        self.data = data
        self.idx = idx

    @property
    def name(self) -> str:
        return str(self.data.get("hook_event_name") or "") if self.kind == "event" else str(self.data.get("name") or "")

    @property
    def session(self) -> str:
        return str(self.data.get("session_id") or "")

    @property
    def agent_id(self) -> Optional[str]:
        v = self.data.get("agent_id")
        return v if isinstance(v, str) and v else None

    @property
    def agent_type(self) -> Optional[str]:
        v = self.data.get("agent_type")
        return v if isinstance(v, str) and v else None

    @property
    def tool(self) -> str:
        return str(self.data.get("tool_name") or "")

    @property
    def tool_use_id(self) -> Optional[str]:
        v = self.data.get("tool_use_id")
        return v if isinstance(v, str) and v else None

    @property
    def command(self) -> str:
        ti = self.data.get("tool_input")
        if self.tool == "Bash" and isinstance(ti, dict) and isinstance(ti.get("command"), str):
            return ti["command"]
        return ""


def parse_time(s: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return 0.0


class Log(object):
    def __init__(self) -> None:
        self.recs = []  # type: List[Rec]
        self.bad_lines = 0
        self.duplicates = 0

    @property
    def events(self) -> List[Rec]:
        return [r for r in self.recs if r.kind == "event"]

    @property
    def markers(self) -> List[Rec]:
        return [r for r in self.recs if r.kind == "marker"]


def load(path: str) -> Log:
    log = Log()
    seen = set()  # exact duplicate events (the same hook registered from two settings sources)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                log.bad_lines += 1
                continue
            if not isinstance(row, dict):
                log.bad_lines += 1
                continue
            at = str(row.get("observed_at") or "")
            if isinstance(row.get("event"), dict):
                key = json.dumps(row["event"], sort_keys=True)
                if key in seen:
                    log.duplicates += 1
                    continue
                seen.add(key)
                log.recs.append(Rec(at, "event", row["event"], n))
            elif isinstance(row.get("marker"), dict):
                log.recs.append(Rec(at, "marker", row["marker"], n))
            else:
                log.bad_lines += 1
    log.recs.sort(key=lambda r: (r.t, r.idx))
    return log


# ---------------------------------------------------------------- helpers

def short(s: Any, n: int = 90) -> str:
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False, sort_keys=True)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 3] + "..."


def text_of(rec: Optional[Rec]) -> str:
    if rec is None:
        return ""
    d = rec.data
    for k in ("tool_response", "error", "tool_result"):
        if k in d:
            v = d[k]
            if isinstance(v, dict) and any(isinstance(v.get(x), str) for x in ("stdout", "stderr")):
                return "\n".join(v[x] for x in ("stdout", "stderr") if isinstance(v.get(x), str) and v[x])
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return ""


def label_in(cmd: str, n: int) -> Optional[str]:
    m = re.search(r"probe-v%d-[A-Za-z0-9_.-]+" % n, cmd)
    return m.group(0) if m else None


def post_for(pre: Rec, posts: List[Rec], used: set) -> Optional[Rec]:
    """The PostToolUse(Failure) that belongs to a PreToolUse: by tool_use_id, else by command order."""
    for p in posts:
        if id(p) in used:
            continue
        if pre.tool_use_id and p.tool_use_id:
            if pre.tool_use_id == p.tool_use_id:
                used.add(id(p))
                return p
            continue
        if p.session == pre.session and p.tool == pre.tool and p.t >= pre.t and \
                p.data.get("tool_input") == pre.data.get("tool_input"):
            used.add(id(p))
            return p
    return None


class Call(object):
    """A tool call as the log shows it: the PreToolUse and, if it ran, its Post."""
    def __init__(self, pre: Rec, post: Optional[Rec]) -> None:
        self.pre = pre
        self.post = post

    @property
    def outcome(self) -> str:
        if self.post is None:
            return "no-post"  # blocked or denied before it ran, or still running when the log was read
        return "failed" if self.post.name == "PostToolUseFailure" else "ok"

    @property
    def output(self) -> str:
        return text_of(self.post)

    def who(self) -> str:
        r = self.pre
        return "%s/%s" % (short(r.agent_id or "-", 12), r.agent_type or "-")


def calls(log: Log, tool: Optional[str] = None) -> List[Call]:
    pres = [r for r in log.events if r.name == "PreToolUse" and (tool is None or r.tool == tool)]
    posts = [r for r in log.events if r.name in POST_EVENTS and (tool is None or r.tool == tool)]
    used = set()  # type: set
    return [Call(p, post_for(p, posts, used)) for p in pres]


def labelled(log: Log, n: int) -> List[Tuple[str, Call]]:
    out = []
    for c in calls(log, "Bash"):
        lab = label_in(c.pre.command, n)
        if lab:
            out.append((lab, c))
    return out


def table(header: List[str], rows: List[List[str]]) -> List[str]:
    if not rows:
        return ["  (no rows)"]
    cols = [header] + rows
    widths = [max(len(str(r[i])) for r in cols) for i in range(len(header))]
    return ["  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in cols]


def keys_deep(obj: Any, prefix: str = "") -> List[str]:
    out = []  # type: List[str]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(prefix + str(k))
            out.extend(keys_deep(v, prefix + str(k) + "."))
    elif isinstance(obj, list):
        for v in obj[:3]:
            out.extend(keys_deep(v, prefix + "[]."))
    return out


def contains(obj: Any, needle: str) -> bool:
    return needle in json.dumps(obj, ensure_ascii=False)


class Report(object):
    def __init__(self, probe: str, title: str) -> None:
        self.probe = probe
        self.title = title
        self.lines = []  # type: List[str]
        self.facts = {}  # type: Dict[str, Any]

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def fact(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def reading(self, text: str) -> None:
        self.lines.append("reading: " + text)


def call_rows(cs: Iterable[Tuple[str, Call]]) -> List[List[str]]:
    return [[lab, c.who(), c.pre.session[:8], c.outcome, short(c.output, 60)] for lab, c in cs]


CALL_HEADER = ["label", "agent_id/agent_type", "session", "result", "output"]


# ---------------------------------------------------------------- probes

def a_v1(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V1", "hook input agent_id / agent_type per nesting depth")
    cs = labelled(log, 1)
    r.add("Bash calls tagged probe-v1-* (depth is in the label):")
    r.add(*table(CALL_HEADER, call_rows(cs)))
    per = {}  # type: Dict[str, Dict[str, Any]]
    for lab, c in cs:
        d = per.setdefault(lab, {"agent_id_present": False, "agent_type": None, "n": 0})
        d["n"] += 1
        d["agent_id_present"] = d["agent_id_present"] or bool(c.pre.agent_id)
        d["agent_type"] = d["agent_type"] or c.pre.agent_type
    r.fact("by_label", per)
    starts = [(x.agent_id, x.agent_type) for x in log.events if x.name == "SubagentStart"]
    r.add("", "SubagentStart events (agent_id, agent_type):")
    r.add(*table(["agent_id", "agent_type"], [[short(a or "-", 24), t or "-"] for a, t in starts]))
    r.fact("subagent_start", [{"agent_id": a, "agent_type": t} for a, t in starts])
    mains = sorted({(x.session[:8], x.agent_type or "-") for x in log.events
                    if x.name == "SessionStart"})
    r.add("", "SessionStart events (session, agent_type): a session with agent_type and no agent_id is a main session started with --agent:")
    r.add(*table(["session", "agent_type"], [[s, t] for s, t in mains]))
    r.fact("session_start_agent_types", [{"session": s, "agent_type": t} for s, t in mains])
    deep = [lab for lab in per if lab.endswith("depth-2") or lab.endswith("depth-3")]
    ids = {lab: per[lab]["agent_id_present"] for lab in per}
    types = {lab: per[lab]["agent_type"] for lab in per}
    r.add("")
    for lab in sorted(per):
        r.add("%s: agent_id %s, agent_type %s" % (lab, "present" if ids[lab] else "ABSENT", types[lab] or "ABSENT"))
    if not per:
        r.reading("no probe-v1 call reached the log: hooks did not load, or the agents did not run (check the transcript).")
    elif any(ids[d] and not types[d] for d in deep):
        r.reading("agent_id without agent_type at depth 2-3: the agent-blind rule is REQUIRED (the build assumption holds).")
    elif deep and all(ids[d] and types[d] for d in deep):
        r.reading("agent_id and agent_type are both present at depth 2-3: roster-aware guards can go beyond depth 1 (a later optimisation; the build stays agent-blind until then).")
    elif deep and any(not ids[d] for d in deep):
        r.reading("agent_id ABSENT at a nested depth: a nested subagent looks like its parent session; the registered-session rule would misclassify it. Record this as a blocker.")
    else:
        r.reading("depth 2 or 3 never ran; rerun with CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=3 or read the transcript for the refusal.")
    return r


def a_v3(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V3", "Workflow per-agent isolation and subagent_type")
    cs = labelled(log, 3)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    pwds = [c for c in calls(log, "Bash") if c.pre.command.strip() == "pwd"]
    r.add("", "pwd calls (the working directory each workflow agent saw):")
    r.add(*table(["agent_id/agent_type", "session", "result", "output"],
                 [[c.who(), c.pre.session[:8], c.outcome, short(c.output, 80)] for c in pwds]))
    r.reading("compare the pwd each workflow agent printed with the scratch repo path; a worktree path under .claude/worktrees or a temp dir means isolation worked. Record the workflow script options you used.")
    return r


def a_v4(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V4", "context: fork + agent: skill starts clean")
    cs = labelled(log, 4)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    saw = [lab for lab, _ in cs if lab.endswith("saw-codeword")]
    clean = [lab for lab, _ in cs if lab.endswith("clean")]
    r.fact("saw_codeword", len(saw))
    r.fact("clean", len(clean))
    if saw:
        r.reading("the forked skill SAW the earlier conversation: it does not start clean. No kit critique skill may rely on it.")
    elif clean:
        r.reading("the forked skill did not see the earlier codeword: it starts clean.")
    else:
        r.reading("neither marker ran: the skill did not run its Bash step; read the transcript.")
    return r


def a_v5(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V5", "driver child inheritance (hooks, settings, sandbox, branch point)")
    sessions = {}  # type: Dict[str, List[Rec]]
    for x in log.recs:
        if x.session:
            sessions.setdefault(x.session, []).append(x)
    allcalls = calls(log, "Bash")
    n = 0
    out = {}  # type: Dict[str, Any]
    for sid, recs in sorted(sessions.items(), key=lambda kv: kv[1][0].t):
        n += 1
        starts = [x for x in recs if x.name == "SessionStart"]
        mk = {}  # type: Dict[str, int]
        for x in recs:
            if x.kind == "marker":
                mk[x.name] = mk.get(x.name, 0) + 1
        cs = [c for c in allcalls if c.pre.session == sid]
        perms = [x for x in recs if x.name == "PermissionRequest"]
        r.add("session %d (%s): started %s, SessionStart agent_type=%s source=%s" % (
            n, sid[:8], recs[0].at, (starts[0].agent_type if starts else None) or "-",
            (starts[0].data.get("source") if starts else None) or "-"))
        r.add("  marker hooks fired: %s" % (", ".join("%s x%d" % kv for kv in sorted(mk.items())) or "none"))
        r.add("  PermissionRequest events: %d (a prompt would have blocked a -p child)" % len(perms))
        r.add(*table(["command", "result", "output"], [[short(c.pre.command, 50), c.outcome, short(c.output, 60)] for c in cs]))
        facts = {"markers": mk, "permission_requests": len(perms),
                 "commands": {c.pre.command: {"result": c.outcome, "output": short(c.output, 120)} for c in cs}}
        out[sid] = facts
        outside = [c for c in cs if "probe-v5-outside" in c.pre.command]
        if outside:
            facts["touch_outside_home"] = outside[0].outcome
    r.fact("sessions", out)
    if not sessions:
        r.reading("no session reached the log: the hook did not load in the child (itself an answer for --setting-sources user).")
    else:
        r.reading("per session: (1) v5-project-hook present = project settings hooks ran in the child; (2) v5-child-settings-hook present = the --settings file's hooks ran; (3) echo probe-v5-start ok without --allowedTools = project permissions.allow was applied; (4) touch ~/.probe-v5-outside ok = no sandbox in the child; (5) git branch / rev-parse outputs vs the facts in the manifest = where the child branched from; (6) printenv shows whether the parent's environment was inherited.")
    if mf.get("facts"):
        r.add("", "manifest facts (commits at prepare time): " + ", ".join("%s=%s" % kv for kv in sorted(mf["facts"].items())))
    return r


def a_v6(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V6", "isolation: worktree base-branch resolution")
    facts = mf.get("facts") or {}
    by_hash = {}  # type: Dict[str, List[str]]
    for k, v in facts.items():
        by_hash.setdefault(v, []).append(k)
    rows = []
    heads = []
    for c in calls(log, "Bash"):
        if not c.pre.agent_id:
            continue
        out = c.output
        rows.append([short(c.pre.command, 40), c.who(), c.outcome, short(out, 70)])
        if "rev-parse" in c.pre.command:
            m = re.search(r"\b[0-9a-f]{7,40}\b", out)
            if m:
                heads.append(m.group(0))
    r.add("Bash calls made inside subagents:")
    r.add(*table(["command", "agent_id/agent_type", "result", "output"], rows))
    names = []
    for h in heads:
        for k, names_ in by_hash.items():
            if h.startswith(k) or k.startswith(h):
                names.extend(names_)
    r.fact("worktree_head", heads)
    r.fact("matches", names)
    r.add("", "facts from the manifest: " + (", ".join("%s=%s" % kv for kv in sorted(facts.items())) or "(none; pass --manifest)"))
    if heads:
        r.reading("the worktree agent's HEAD equals: %s. 'feature/probe' = it branched from the current HEAD; 'main'/'origin/main' = from the default branch." % (", ".join(names) or "none of the recorded commits"))
    else:
        r.reading("no git rev-parse output from a subagent reached the log (the agent may not have run in a worktree, or was denied).")
    return r


def a_v7(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V7", "does SubagentStart see the dispatch prompt")
    nonce = "NONCE-V7-91c4"
    starts = [x for x in log.events if x.name == "SubagentStart"]
    r.add("SubagentStart events: %d" % len(starts))
    has = False
    for x in starts:
        found = contains(x.data, nonce)
        has = has or found
        r.add("  fields: %s" % ", ".join(sorted(x.data.keys())))
        r.add("  dispatch nonce present in the event: %s" % ("YES" if found else "no"))
    pre = [x for x in log.events if x.name == "PreToolUse" and x.tool in AGENT_TOOLS]
    for x in pre:
        ti = x.data.get("tool_input")
        r.add("PreToolUse(%s) tool_input fields: %s; nonce present: %s" % (
            x.tool, ", ".join(sorted(ti.keys())) if isinstance(ti, dict) else "?", "YES" if contains(x.data, nonce) else "no"))
    r.fact("subagent_start_has_prompt", has)
    r.fact("subagent_start_fields", sorted({k for x in starts for k in x.data}))
    if not starts:
        r.reading("no SubagentStart event: the hook did not fire or the subagent was not dispatched.")
    elif has:
        r.reading("SubagentStart carries the dispatch prompt: brief-path enforcement at SubagentStart is possible (D-16).")
    else:
        r.reading("SubagentStart does not carry the dispatch prompt: keep enforcing the brief path by report parse and prose; PreToolUse on the Agent tool is the only place the prompt is visible.")
    return r


def a_v8(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V8", "SubagentStop / statusLine expose model, cost, tokens")
    wanted = ("cost", "token", "model", "usage", "duration")

    def hits(obj: Any) -> List[str]:
        return sorted({k for k in keys_deep(obj) if any(w in k.lower() for w in wanted)})
    found = {}  # type: Dict[str, List[str]]
    for x in log.events:
        if x.name in ("SubagentStop", "SubagentStart") or (x.name == "PostToolUse" and x.tool in AGENT_TOOLS):
            label = x.name + ("(%s)" % x.tool if x.tool else "")
            found.setdefault(label, [])
            found[label] = sorted(set(found[label]) | set(hits(x.data)))
            r.add("%s fields: %s" % (label, ", ".join(sorted(x.data.keys()))))
    sl = [x for x in log.markers if x.name == "v8-statusline"]
    for x in sl[:1]:
        keys = x.data.get("stdin_keys") or []
        found["statusLine"] = sorted(set(hits(x.data.get("stdin") or {})))
        r.add("statusLine stdin fields: %s" % ", ".join(keys))
    r.add("", "fields that look like model/cost/token/usage/duration:")
    for k, v in sorted(found.items()):
        r.add("  %s: %s" % (k, ", ".join(v) or "none"))
    r.fact("cost_like_fields", found)
    stop = found.get("SubagentStop")
    if stop is None:
        r.reading("no SubagentStop event: nothing to judge for the subagent; check the hook loaded.")
    elif stop:
        r.reading("SubagentStop carries cost-like fields (%s); verify they are per-subagent numbers before relying on them." % ", ".join(stop))
    else:
        r.reading("SubagentStop has no model/cost/token fields: per-child cost comes from the -p JSON output only (the build assumption).")
    return r


def a_v9(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V9", "onFailure: block with a missing interpreter, a crash, a timeout")
    variant = mf.get("variant") or "?"
    r.add("variant: %s%s" % (variant, "  (CONTROL: onFailure removed)" if mf.get("control") else ""))
    mk = {}  # type: Dict[str, int]
    for x in log.markers:
        mk[x.name] = mk.get(x.name, 0) + 1
    r.add("probe hook markers (a missing-interpreter hook can never write one): " + (", ".join("%s x%d" % kv for kv in sorted(mk.items())) or "none"))
    # PreToolUse(Bash)
    bash = [c for c in calls(log, "Bash") if "probe-v9-bash" in c.pre.command]
    # PermissionRequest(Write)
    perms = [x for x in log.events if x.name == "PermissionRequest" and x.tool == "Write"]
    writes = [c for c in calls(log, "Write")]
    cfg = [x for x in log.events if x.name == "ConfigChange"]
    reads = [c for c in calls(log, "Read") if "probe-v9-secret" in json.dumps(c.pre.data.get("tool_input", {}))]
    r.add("")
    r.add("PreToolUse(Bash echo probe-v9-bash): reached the log %d time(s); result: %s" % (
        len(bash), ", ".join(c.outcome for c in bash) or "n/a"))
    r.add("PermissionRequest(Write): %d event(s); Write tool calls that ran: %s" % (
        len(perms), ", ".join(c.outcome for c in writes) or "none reached PreToolUse"))
    r.add("ConfigChange: %d event(s); Read probe-v9-secret.txt after the settings edit: %s" % (
        len(cfg), ", ".join("%s (%s)" % (c.outcome, "secret visible" if "secret-marker-v9" in c.output else "no secret in output") for c in reads) or "not attempted"))
    r.fact("variant", variant)
    r.fact("control", bool(mf.get("control")))
    r.fact("pretooluse_bash", [c.outcome for c in bash])
    r.fact("permissionrequest_write_events", len(perms))
    r.fact("write_calls", [c.outcome for c in writes])
    r.fact("configchange_events", len(cfg))
    r.fact("read_after_config_edit", [c.outcome for c in reads])
    r.fact("markers", mk)
    if bash:
        if all(c.outcome == "ok" for c in bash):
            r.reading("PreToolUse: the command RAN despite the failing hook = FAIL-OPEN for this form.")
        elif all(c.outcome == "no-post" for c in bash):
            r.reading("PreToolUse: the command did NOT run = the failing hook blocked it (fail-closed for this form). Confirm the transcript shows a denial, not a prompt you answered.")
        else:
            r.reading("PreToolUse: mixed outcomes; read the transcript.")
    else:
        r.reading("PreToolUse: no Bash call reached the log; the observe hook or the session did not run.")
    r.add("reading: PermissionRequest and ConfigChange cannot be settled from the log alone. PermissionRequest: record whether a prompt still appeared, whether Write ran, and the denial text. ConfigChange: if the Read of probe-v9-secret.txt shows the secret, the deny-rule edit did NOT take effect (the change was blocked); if the Read was denied, the edit applied (the hook did not block). Compare with the --control run.")
    return r


def a_v12(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V12", "omitClaudeMd effect on subagents")
    cs = labelled(log, 12)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    labs = [lab for lab, _ in cs]
    r.fact("labels", labs)
    keep = "probe-v12-keep-knows" in labs
    omit = "probe-v12-omit-knows" in labs
    if labs:
        r.reading("default subagent knew the CLAUDE.md codeword: %s; omitClaudeMd subagent knew it: %s. (keep=yes, omit=no is the documented behaviour.)" % (keep, omit))
    else:
        r.reading("no probe-v12 call reached the log.")
    return r


def a_v13(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V13", "plugin hooks.json for plugin and project agents; CLAUDE_PLUGIN_ROOT")
    rows = []
    for x in log.markers:
        if x.name.startswith("v13-"):
            rows.append([x.name, x.agent_type or "-", short(x.agent_id or "-", 12), x.session[:8],
                         short(x.data.get("env_CLAUDE_PLUGIN_ROOT") or "", 60)])
    r.add("plugin hook markers:")
    r.add(*table(["marker", "agent_type", "agent_id", "session", "env CLAUDE_PLUGIN_ROOT"], rows))
    cs = labelled(log, 13)
    r.add("", "Bash calls tagged probe-v13-*:")
    r.add(*table(CALL_HEADER, call_rows(cs)))
    fired_for = sorted({x.agent_type for x in log.markers if x.name == "v13-plugin-hook" and x.agent_id})
    r.fact("plugin_hook_fired_for_agent_types", fired_for)
    r.fact("root_expanded", sorted({str(x.data.get("env_CLAUDE_PLUGIN_ROOT")) for x in log.markers
                                    if x.name == "v13-plugin-root-expanded"}))
    cmd = [c.pre.command for lab, c in cs if lab.startswith("probe-v13-command-root")]
    r.fact("command_root_rendered", cmd)
    r.reading("plugin hook fired inside agents of type: %s. A plugin agent AND a project agent in that list = hooks.json fires for both. v13-plugin-root-expanded with a path = ${CLAUDE_PLUGIN_ROOT} works in hook commands; the command's echo shows whether it expands in command text (a literal ${CLAUDE_PLUGIN_ROOT} means it does not)." % (", ".join(fired_for) or "none"))
    return r


def a_v14(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V14", "do hook denials count toward the auto-mode pause")
    denies = [x for x in log.markers if x.name == "v14-deny"]
    cs = labelled(log, 14)
    r.add("deny-hook invocations: %d" % len(denies))
    r.add(*table(CALL_HEADER, call_rows(cs)))
    r.fact("deny_hook_invocations", len(denies))
    r.fact("calls_attempted", len(cs))
    r.reading("the log shows how many denied calls the model made before you saw (or did not see) the auto-mode pause. Record from the terminal: did auto mode pause or fall back to prompting, and after how many denials.")
    return r


def a_v16(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V16", "named spawn with teams off: teammate or subagent")
    names = {}  # type: Dict[str, int]
    for x in log.events:
        if x.name in ("SubagentStart", "SubagentStop", "TeammateIdle", "TaskCompleted"):
            names[x.name] = names.get(x.name, 0) + 1
    r.add("events: " + (", ".join("%s x%d" % kv for kv in sorted(names.items())) or "none"))
    cs = labelled(log, 16)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    r.fact("events", names)
    r.fact("ran", len(cs))
    r.reading("SubagentStart/Stop without TeammateIdle = a subagent; TeammateIdle = a teammate. Two probe-v16-ran calls = the SendMessage reached it. Quote the tool result's wording in the results table.")
    return r


def a_v17(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V17", "--bg push behavior")
    r.add("The hook log cannot show a push. Check the bare remote: git --git-dir %s branch -a" % (mf.get("remote") or "<scratch>.remote.git"))
    cs = [c for c in calls(log, "Bash") if "push" in c.pre.command]
    r.add(*table(["command", "result", "output"], [[short(c.pre.command, 60), c.outcome, short(c.output, 60)] for c in cs]))
    r.reading("record: did the background session push (to which refs), open a PR, or stop at a prompt.")
    return r


def a_v18(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V18", "fork-mode wait semantics (does the parent block on a slow child)")
    r.add("variant: %s" % (mf.get("variant") or "?"))
    cs = calls(log, "Bash")
    sessions = []  # type: List[str]
    for c in cs:
        if c.pre.session not in sessions:
            sessions.append(c.pre.session)
    out = []
    n = 0
    for sid in sessions:
        slow = [c for c in cs if c.pre.session == sid and "probe-v18-slow-done" in c.pre.command]
        after = [c for c in cs if c.pre.session == sid and re.search(r"probe-v18-(parent|spawner)-after", c.pre.command)]
        starts = [x for x in log.events if x.session == sid and x.name == "SessionStart"]
        if not slow and not after:
            continue
        n += 1
        shape = "nested (spawner)" if any("spawner-after" in c.pre.command for c in after) else "direct"
        if starts and starts[0].agent_type:
            shape += ", driver child --agent " + starts[0].agent_type
        row = {"session": sid[:8], "shape": shape}
        slow_start = slow[0].pre.t if slow else None
        slow_end = slow[0].post.t if slow and slow[0].post else None
        after_t = after[0].pre.t if after else None
        row["slow_started_at"] = slow[0].pre.at if slow else None
        row["slow_finished_at"] = slow[0].post.at if slow and slow[0].post else None
        row["after_started_at"] = after[0].pre.at if after else None
        if after_t is not None and slow_end is not None:
            row["parent_waited"] = after_t >= slow_end
            row["after_minus_slow_end_s"] = round(after_t - slow_end, 3)
        elif after_t is not None and slow_start is not None:
            row["parent_waited"] = None
            row["note"] = "slow child never finished in the log (killed or still running)"
        else:
            row["parent_waited"] = None
            row["note"] = "missing slow or after marker"
        out.append(row)
        r.add("session %d (%s) %s:" % (n, sid[:8], shape))
        r.add("  slow child  start %s  end %s" % (row["slow_started_at"], row["slow_finished_at"]))
        r.add("  parent next action (after-marker) %s" % row["after_started_at"])
        if row["parent_waited"] is None:
            r.add("  parent_waited: unknown (%s)" % row.get("note"))
        else:
            gap = abs(row["after_minus_slow_end_s"])
            r.add("  parent_waited: %s (the parent's next action came %.1f s %s the slow child finished)" % (
                row["parent_waited"], gap, "after" if row["parent_waited"] else "BEFORE"))
    r.fact("sessions", out)
    if not out:
        r.reading("no V18 session found (no probe-v18 Bash calls).")
    else:
        r.reading("parent_waited false = the parent kept going while the child ran (non-blocking dispatch); true = the Agent call blocked. Map each numbered session to its matrix cell by time order; sessions with a driver child agent_type are the `-p --agent` cells. Run each variant in its own scratch repo.")
    return r


def a_v19(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V19", "sandbox failIfUnavailable")
    r.add("variant: %s" % ("control (failIfUnavailable removed)" if mf.get("control") else "failIfUnavailable: true"))
    starts = [x for x in log.events if x.name == "SessionStart"]
    cs = labelled(log, 19)
    r.add("SessionStart events: %d" % len(starts))
    r.add(*table(CALL_HEADER, call_rows(cs)))
    r.fact("session_starts", len(starts))
    r.fact("calls", {lab: c.outcome for lab, c in cs})
    outside = [c for lab, c in cs if lab.endswith("outside")]
    if not starts:
        r.reading("no SessionStart event: either claude REFUSED to start (the fail-closed answer; copy the terminal message) or the hooks did not load. Say which in the results table.")
    elif outside and any(c.outcome == "ok" and "touch" in c.pre.command for c in outside):
        r.reading("a write outside the project SUCCEEDED: in this environment the sandbox was not enforcing. If the sandbox was made unavailable on purpose, that is FAIL-OPEN for failIfUnavailable; if it was healthy, the filesystem policy is not as assumed.")
    elif outside:
        r.reading("the write outside the project was blocked/failed: the sandbox enforced (or the command was denied by permissions; read the output text).")
    else:
        r.reading("the outside-write command never ran; read the transcript.")
    return r


def a_v20(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V20", "SessionStart(compact) additionalContext")
    nonce = "V20-7f3a91"
    starts = [x for x in log.events if x.name == "SessionStart"]
    r.add("SessionStart events (time, source):")
    r.add(*table(["time", "source", "session"], [[x.at, str(x.data.get("source")), x.session[:8]] for x in starts]))
    inj = [x for x in log.markers if x.name == "v20-compact-inject"]
    r.add("", "inject hook runs: %d" % len(inj))
    for x in inj:
        out = x.data.get("output")
        r.add("  at %s hook_event_name=%s source=%s output=%s" % (x.at, x.data.get("hook_event_name"), x.data.get("source"), short(out, 100)))
    compact_starts = [x for x in starts if x.data.get("source") == "compact"]
    cs = labelled(log, 20)
    t0 = compact_starts[0].t if compact_starts else None
    before = [(lab, c) for lab, c in cs if t0 is None or c.pre.t < t0]
    after = [(lab, c) for lab, c in cs if t0 is not None and c.pre.t >= t0]
    r.add("", "model's answer BEFORE the compact (should be probe-v20-none):")
    r.add(*table(CALL_HEADER, call_rows(before)))
    r.add("model's answer AFTER the compact (the nonce means it saw the injected context):")
    r.add(*table(CALL_HEADER, call_rows(after)))
    saw = any(nonce in c.pre.command for _, c in after)
    r.fact("sources_seen", sorted({str(x.data.get("source")) for x in starts}))
    r.fact("compact_session_start_fired", bool(compact_starts))
    r.fact("inject_hook_ran_on_compact", any(x.data.get("source") == "compact" for x in inj))
    r.fact("nonce_seen_by_model_after_compact", saw)
    r.fact("nonce_seen_before_compact", any(nonce in c.pre.command for _, c in before))
    if not compact_starts:
        r.reading("no SessionStart with source=compact: it does not fire on /compact in this version (the fallback, re-reading loopctl status at each stage, is then the only mechanism).")
    elif saw:
        r.reading("SessionStart(compact) fired and the model quoted the injected nonce: additionalContext works on compact.")
    else:
        r.reading("SessionStart(compact) fired%s but the model did not quote the nonce: injection did not reach the model (or the model missed it; ask once in the transcript before concluding)." % (
            " and the inject hook ran" if r.facts["inject_hook_ran_on_compact"] else ", but the inject hook did not run"))
    return r


def a_v21(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V21", "managed settings overridden by the same OS user")
    mk = [x for x in log.markers if x.name == "v21-managed-hook"]
    r.add("managed hook markers: %d" % len(mk))
    cs = labelled(log, 21)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    obs = [x for x in log.events if x.name == "SessionStart"]
    r.add("SessionStart events logged by the project observe hook: %d (0 with allowManagedHooksOnly = project hooks were suppressed)" % len(obs))
    r.fact("managed_hook_runs", len(mk))
    r.fact("project_observe_session_starts", len(obs))
    r.fact("denied_command_outcomes", [c.outcome for _, c in cs])
    r.reading("probe-v21-denied should be 'no-post' in every attempt if the managed deny held. Any 'ok' under an override attempt (settings.local, --settings, env) means that route overrides managed settings. Record each attempt and its result.")
    return r


def a_v22(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V22", "--agents + --agent in -p, with plugin-namespaced agents")
    sess = []  # type: List[str]
    for x in log.events:
        if x.session and x.session not in sess:
            sess.append(x.session)
    rows = []
    out = []
    allc = calls(log, "Bash")
    for i, sid in enumerate(sess, 1):
        starts = [x for x in log.events if x.session == sid and x.name == "SessionStart"]
        labs = [label_in(c.pre.command, 22) for c in allc if c.pre.session == sid]
        labs = [x for x in labs if x]
        ran = [c.outcome for c in allc if c.pre.session == sid and label_in(c.pre.command, 22)]
        at = (starts[0].agent_type if starts else None) or "-"
        rows.append([str(i), sid[:8], at, ", ".join(labs) or "none", ", ".join(ran) or "-"])
        out.append({"session": sid[:8], "agent_type": at, "labels": labs, "results": ran})
    r.add("one row per session, in time order (map to the numbered command in the README):")
    r.add(*table(["#", "session", "agent_type", "probe-v22 labels run", "result"], rows))
    r.fact("sessions", out)
    r.reading("a row with agent_type = the role you asked for AND its probe-v22-<role> label means the launch line works for that combination; agent_type '-' or no label means the role was not applied (record the CLI's error text, which the log does not hold). For the override command, compare the label with probe-v22-project-role-INLINE.")
    return r


def a_v23(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V23", "Workflow from a subagent or via an allow rule in -p")
    r.add("variant: %s" % (mf.get("variant") or "?"))
    wf = calls(log, "Workflow")
    r.add("Workflow tool calls:")
    r.add(*table(["agent_id/agent_type", "session", "result", "output"],
                 [[c.who(), c.pre.session[:8], c.outcome, short(c.output, 70)] for c in wf]))
    cs = labelled(log, 23)
    r.add("", "Bash calls tagged probe-v23-*:")
    r.add(*table(CALL_HEADER, call_rows(cs)))
    from_sub = [c for c in wf if c.pre.agent_id]
    r.fact("workflow_calls", [{"from_subagent": bool(c.pre.agent_id), "result": c.outcome} for c in wf])
    r.fact("workflow_ran_marker", any(lab == "probe-v23-workflow-ran" for lab, _ in cs))
    if not wf:
        r.reading("no Workflow tool call reached PreToolUse: the tool was not offered (or the model did not call it). Read the transcript for the reason.")
    else:
        r.reading("%d Workflow call(s), %d from a subagent. 'ok' = the launch was permitted; 'no-post' = denied/blocked. The saved workflow's own agent should echo probe-v23-workflow-ran if it really started." % (len(wf), len(from_sub)))
    return r


def a_v24(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V24", "does a subagent's Bash honor a different sandbox from its parent")
    secrets = ("HELDOUT-SECRET-V24", "PROBE-SCRATCH-README")
    res = {}  # type: Dict[str, str]
    rows = []
    for lab, c in labelled(log, 24):
        who = lab[len("probe-v24-"):]
        read = any(s in c.output for s in secrets)
        verdict = "READ" if read else ("DENIED/ERROR" if c.outcome != "no-post" else "NOT RUN")
        res[who] = verdict
        rows.append([who, c.who(), c.outcome, verdict, short(c.output, 60)])
    r.add(*table(["who", "agent_id/agent_type", "result", "file content", "output"], rows))
    r.fact("results", res)
    main, plain, loose, strict = (res.get(k) for k in ("main", "plain", "loose", "strict"))
    r.add("")
    if main == "READ":
        r.reading("the MAIN session read the held-out file: the sandbox is not enforcing denyRead here; fix the setup before judging subagents.")
    elif main is None:
        r.reading("the main-session control (probe-v24-main) is missing; run it first.")
    elif plain == "READ":
        r.reading("a plain subagent READ what the parent cannot: subagent Bash is NOT under the parent's sandbox. Held-out isolation in-session is impossible; driver-only or CI-only (the build assumption) is confirmed in the strong form.")
    elif plain == "DENIED/ERROR":
        r.reading("a plain subagent was denied like the parent: it shares the parent's sandbox (no per-subagent sandbox by default).")
    if loose == "READ":
        r.add("reading: probe-sb-loose READ the held-out file: agent FRONTMATTER can turn the sandbox off or relax it. This is a boundary hole; record it as a blocker.")
    if strict == "DENIED/ERROR":
        r.add("reading: probe-sb-strict was denied README.md: frontmatter sandbox keys ARE honoured for a subagent (a per-subagent sandbox exists). Record the exact keys.")
    elif strict == "READ":
        r.add("reading: probe-sb-strict read README.md: frontmatter sandbox keys are ignored (the expected, assumed answer).")
    return r


def a_v25(log: Log, mf: Dict[str, Any]) -> Report:
    r = a_v6(log, mf)
    r.probe = "V25"
    r.title = "worktree.baseRef: head"
    return r


def a_v26(log: Log, mf: Dict[str, Any]) -> Report:
    r = Report("V26", "does a project code-review skill override the bundled one")
    cs = labelled(log, 26)
    r.add(*table(CALL_HEADER, call_rows(cs)))
    r.fact("project_skill_ran", bool(cs))
    if cs:
        r.reading("the project skill ran: a project skill named code-review overrides the bundled one. Do not ship a skill with that name.")
    else:
        r.reading("no probe-v26 marker: the bundled skill ran (or the skill did not load). The kit may still avoid the name; record which one ran from the transcript.")
    return r


def a_none(probe: str, title: str, hint: str) -> Callable[[Log, Dict[str, Any]], Report]:
    def f(log: Log, mf: Dict[str, Any]) -> Report:
        r = Report(probe, title)
        r.add("This probe is not settled by the hook log. %s" % hint)
        r.add("events in the log: %d, markers: %d" % (len(log.events), len(log.markers)))
        return r
    return f


ANALYZERS = {
    "V1": a_v1, "V3": a_v3, "V4": a_v4, "V5": a_v5, "V6": a_v6, "V7": a_v7, "V8": a_v8, "V9": a_v9,
    "V12": a_v12, "V13": a_v13, "V14": a_v14, "V16": a_v16, "V17": a_v17, "V18": a_v18, "V19": a_v19,
    "V20": a_v20, "V21": a_v21, "V22": a_v22, "V23": a_v23, "V24": a_v24, "V25": a_v25, "V26": a_v26,
    "V15": a_none("V15", "claude plugin eval --model pin", "Record the `claude plugin eval --help` text and the result of a pinned run by hand."),
    "V27": a_none("V27", "GitHub plan features on private repos", "Record the `gh api` responses per plan by hand (README V27)."),
}


def summary(log: Log) -> Report:
    r = Report("summary", "what the log contains")
    counts = {}  # type: Dict[str, int]
    for x in log.events:
        counts[x.name] = counts.get(x.name, 0) + 1
    r.add("lines: %d events, %d markers, %d unreadable, %d exact duplicates dropped" % (
        len(log.events), len(log.markers), log.bad_lines, log.duplicates))
    r.add("events by name:")
    r.add(*table(["event", "count"], [[k, str(v)] for k, v in sorted(counts.items())]))
    ident = {}  # type: Dict[Tuple[str, str, str], int]
    for x in log.events:
        k = (x.session[:8], "yes" if x.agent_id else "no", x.agent_type or "-")
        ident[k] = ident.get(k, 0) + 1
    r.add("", "identity fields seen (session, agent_id present, agent_type):")
    r.add(*table(["session", "agent_id", "agent_type", "events"], [[a, b, c, str(n)] for (a, b, c), n in sorted(ident.items())]))
    r.fact("event_counts", counts)
    return r


def load_manifest(path: Optional[str], log_path: str) -> Dict[str, Any]:
    cand = path
    if not cand and log_path.endswith(".observe.jsonl"):
        guess = log_path[:-len(".observe.jsonl")] + ".probe.json"
        if os.path.isfile(guess):
            cand = guess
    if not cand:
        return {}
    try:
        with open(cand, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Print the fields of a probe's hook log that answer the probe.")
    ap.add_argument("probe", help="V-item (V1, V9, ...) or 'summary'")
    ap.add_argument("log", help="the JSONL log (<scratch>.observe.jsonl)")
    ap.add_argument("--json", action="store_true", help="print the facts as JSON instead of text")
    ap.add_argument("--manifest", help="<scratch>.probe.json (default: found next to the log)")
    a = ap.parse_args(argv)
    probe = a.probe.upper() if a.probe.lower() != "summary" else "summary"
    if probe != "summary" and probe not in ANALYZERS:
        print("analyze: no analyzer for %r; known: %s" % (a.probe, ", ".join(sorted(ANALYZERS, key=lambda p: int(p[1:])))), file=sys.stderr)
        return 2
    try:
        log = load(a.log)
    except OSError as exc:
        print("analyze: cannot read %s: %s" % (a.log, exc), file=sys.stderr)
        return 2
    mf = load_manifest(a.manifest, a.log)
    if probe != "summary" and mf.get("probe") and mf["probe"] != probe and not (probe == "V25" and mf["probe"] == "V25"):
        print("analyze: note: the manifest says this log is for %s, not %s" % (mf["probe"], probe), file=sys.stderr)
    rep = summary(log) if probe == "summary" else ANALYZERS[probe](log, mf)
    if a.json:
        print(json.dumps({"probe": rep.probe, "title": rep.title, "facts": rep.facts,
                          "log": {"events": len(log.events), "markers": len(log.markers),
                                  "unreadable_lines": log.bad_lines, "duplicates_dropped": log.duplicates}},
                         indent=2, sort_keys=True))
        return 0
    print("%s: %s" % (rep.probe, rep.title))
    print("log: %s (%d events, %d markers%s%s)" % (
        a.log, len(log.events), len(log.markers),
        ", %d unreadable lines" % log.bad_lines if log.bad_lines else "",
        ", %d duplicate events dropped" % log.duplicates if log.duplicates else ""))
    if not log.recs:
        print("\nThe log is EMPTY. Either the session never started a hook-bearing run (a refusal at startup is a finding) or the observe hook path is wrong (is the kit checkout where make-scratch saw it?).")
    print("")
    for line in rep.lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
