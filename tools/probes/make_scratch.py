#!/usr/bin/env python3
"""Prepare a scratch repo for one platform probe (IMP-7, report §17). Human-run only.

    tools/probes/make-scratch.sh <scratch-dir> --probe V1 [--variant NAME] [--control] [--dry-run]

What it does, in order:
  1. refuses if CLAUDECODE is set (it prepares; the human runs `claude` in a normal terminal)
  2. checks the scratch path is new or empty, outside this repo, and not inside another git repo
  3. creates a git repo (branch `main`, one commit) holding the probe's files: agents, skills,
     scripts, a plugin directory, a prompt file (`PROBE-PROMPT.txt`)
  4. installs tools/baseline/observe.py as a hook-input logger on every hook event, then merges the
     probe's settings snippet (tools/probes/settings/<probe>[-<variant>].json) into
     `.claude/settings.json`
  5. writes `<scratch>.observe.jsonl`'s path into the settings, a manifest `<scratch>.probe.json`
     and prints the exports, the pointer to the README section and the exact next step

It never runs `claude` (not even `--version`), never touches the network and never pushes to a
real remote (probes that need a remote get a local bare one next to the scratch repo).
`--dry-run` prints the plan and creates nothing. Exit codes: 3 refused, 2 usage.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(KIT_ROOT, ".claude", "scripts"))
sys.path.insert(0, os.path.join(KIT_ROOT, "tools", "baseline"))

from kitlib import gitutil, jsonio  # noqa: E402
import prepare  # noqa: E402  (tools/baseline/prepare.py: sh_quote, inside, event lists)

SETTINGS_DIR = os.path.join(HERE, "settings")
FILES_DIR = os.path.join(HERE, "files")
OBSERVE = os.path.join(KIT_ROOT, "tools", "baseline", "observe.py")

# Every hook event the logger subscribes to. The first twelve are the baseline's (0a); the last
# four are named in the report (§5.8, §7.1, §13 P30). WorktreeCreate and WorktreeRemove are left
# out on purpose: a WorktreeCreate hook REPLACES the default worktree creation, so a logger that
# prints nothing would break it. Override with --events if the first real run names a wrong one.
ALL_EVENTS = tuple(prepare.OBSERVED_EVENTS) + ("ConfigChange", "TeammateIdle", "TaskCompleted", "PostCompact")
TOOL_EVENTS = tuple(prepare.TOOL_EVENTS)

# The Bash commands every probe prompt uses are plain echoes tagged `probe-v<n>-...`.
BASE_ALLOW = ["Bash(echo probe-*)"]

PLACEHOLDERS = ("SCRATCH", "LOG", "OBSERVE", "OBSERVE_CMD", "HOOK")
TEXT_SUFFIXES = (".json", ".md", ".txt", ".py", ".sh", ".toml", ".yml", ".yaml")


class ProbeError(Exception):
    pass


# Probe table. `files` are directories under tools/probes/files/ copied into the scratch root
# (in order, later ones win). `control_key` is stripped from the settings by --control.
# `remote` gives the scratch a local bare `origin`. `scratch: False` means the probe needs no
# scratch repo at all (the README says what to do instead).
PROBES = {
    "V1": {"title": "agent_id / agent_type at depth 2-3, in Workflow agents and driver children",
           "files": ["V1"]},
    "V3": {"title": "Workflow per-agent isolation and subagent_type", "files": []},
    "V4": {"title": "context: fork + agent: skill starts clean", "files": ["V4"]},
    "V5": {"title": "driver child (-p --agent): inherited hooks, settings, sandbox, branch point",
           "files": ["V5"], "git_branch": "feature/probe-v5"},
    "V6": {"title": "isolation: worktree base-branch resolution", "files": ["V6"], "remote": True,
           "feature_branch": True},
    "V7": {"title": "SubagentStart sees the dispatch prompt", "files": ["V7"]},
    "V8": {"title": "SubagentStop / statusLine expose model, cost, tokens", "files": ["V7", "V8"]},
    "V9": {"title": "onFailure: block with a missing interpreter, a crash, a timeout",
           "files": ["V9"], "variants_required": True, "control_key": "onFailure", "local_settings": {}},
    "V12": {"title": "omitClaudeMd effect", "files": ["V12"]},
    "V13": {"title": "plugin hooks.json for plugin and project agents; ${CLAUDE_PLUGIN_ROOT}",
            "files": ["V13"]},
    "V14": {"title": "do hook denials count toward the auto-mode pause", "files": ["V14"]},
    "V15": {"title": "claude plugin eval --model pin", "scratch": False},
    "V16": {"title": "named spawn with teams off: teammate or subagent", "files": ["V16"]},
    "V17": {"title": "--bg push behavior", "files": [], "remote": True},
    "V18": {"title": "fork-mode env names and wait semantics", "files": ["V18"], "variants_required": True},
    "V19": {"title": "sandbox failIfUnavailable", "files": ["V19"], "control_key": "failIfUnavailable"},
    "V20": {"title": "SessionStart(compact) additionalContext", "files": ["V20"]},
    "V21": {"title": "managed settings overridden by the same OS user", "files": [], "managed": "V21-managed"},
    "V22": {"title": "--agents + --agent in -p with plugin-namespaced agents", "files": ["V22"]},
    "V23": {"title": "Workflow from a subagent / via an allow rule in -p", "files": ["V23"],
            "variants_required": True},
    "V24": {"title": "per-subagent sandbox", "files": ["V24"]},
    "V25": {"title": "worktree.baseRef: head", "files": ["V6", "V25"], "remote": True, "feature_branch": True},
    "V26": {"title": "project code-review skill vs the bundled one", "files": ["V26"]},
    "V27": {"title": "GitHub plan features on private repos", "scratch": False},
}
# Probes the build treats as closed or moot: no harness.
CLOSED = {"V2": "closed: .claude/rules load in subagents [docs] (P21)",
          "V10": "moot: cloud MCP is deny-only (P39)",
          "V11": "closed: --max-budget-usd covers subagents, as an estimate (P26)"}


def sort_key(probe: str) -> int:
    return int(probe[1:])


def snippet_files(probe: str) -> Dict[str, str]:
    """variant name ('' for the plain snippet) -> path, for tools/probes/settings/<probe>[-variant].json."""
    out = {}  # type: Dict[str, str]
    if not os.path.isdir(SETTINGS_DIR):
        return out
    for fn in sorted(os.listdir(SETTINGS_DIR)):
        m = re.match(r"^(V\d+)(?:-([a-z0-9][a-z0-9-]*))?\.json$", fn)
        if m and m.group(1) == probe:
            out[m.group(2) or ""] = os.path.join(SETTINGS_DIR, fn)
    return out


def variants_of(probe: str) -> List[str]:
    return [v for v in snippet_files(probe) if v]


# ---------------------------------------------------------------- settings

def observe_command(log: str) -> str:
    return "python3 %s --log %s" % (prepare.sh_quote(OBSERVE), prepare.sh_quote(log))


def base_settings(log: str, events: Tuple[str, ...]) -> Dict[str, Any]:
    """The logger on every event, plus the one allow rule every probe prompt relies on."""
    hooks = {}  # type: Dict[str, Any]
    for event in events:
        entry = {"hooks": [{"type": "command", "command": observe_command(log), "timeout": 10}]}  # type: Dict[str, Any]
        if event in TOOL_EVENTS:
            entry["matcher"] = "*"
        hooks[event] = [entry]
    return {"hooks": hooks, "permissions": {"allow": list(BASE_ALLOW)}}


def merge_settings(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Hook lists and permission lists are concatenated; every other key is replaced or deep-merged."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if key == "hooks" and isinstance(value, dict):
            hooks = out.setdefault("hooks", {})
            for event, entries in value.items():
                hooks.setdefault(event, []).extend(copy.deepcopy(entries))
        elif key == "permissions" and isinstance(value, dict):
            perms = out.setdefault("permissions", {})
            for pk, pv in value.items():
                if isinstance(pv, list):
                    cur = perms.setdefault(pk, [])
                    for item in pv:
                        if item not in cur:
                            cur.append(item)
                else:
                    perms[pk] = pv
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_settings(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def strip_key(obj: Any, key: str) -> Any:
    """Remove `key` at any depth (the --control form of a snippet)."""
    if isinstance(obj, dict):
        return {k: strip_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [strip_key(v, key) for v in obj]
    return obj


def substitute(text: str, values: Dict[str, str]) -> str:
    for name in PLACEHOLDERS:
        text = text.replace("{{%s}}" % name, values[name])
    return text


def placeholder_values(scratch: str, log: str) -> Dict[str, str]:
    return {"SCRATCH": scratch, "LOG": log, "OBSERVE": OBSERVE,
            "OBSERVE_CMD": observe_command(log),
            "HOOK": os.path.join(scratch, ".claude", "probe", "probe_hook.py")}


def load_snippet(path: str, values: Dict[str, str]) -> Dict[str, Any]:
    """Read a snippet, resolve the placeholders inside JSON strings, return the parsed object."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return substitute(o, values)
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o
    out = walk(raw)
    if not isinstance(out, dict):
        raise ProbeError("%s is not a JSON object" % path)
    return out


# ---------------------------------------------------------------- files

def probe_file_pairs(dirs: List[str]) -> List[Tuple[str, str]]:
    """(source path, relative destination) for every file of the given files/<dir> trees."""
    pairs = {}  # type: Dict[str, str]
    for d in dirs:
        base = os.path.join(FILES_DIR, d)
        if not os.path.isdir(base):
            raise ProbeError("missing probe files directory %s" % base)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames if x != "__pycache__")
            for fn in sorted(filenames):
                if fn.endswith(".pyc"):
                    continue
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, base)
                if rel == "PROMPT.txt":
                    rel = "PROBE-PROMPT.txt"
                elif d == "common":
                    rel = os.path.join(".claude", "probe", rel)
                pairs[rel] = src
    return sorted((src, rel) for rel, src in pairs.items())


def copy_probe_file(src: str, dst: str, values: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src.endswith(TEXT_SUFFIXES):
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(dst, "w", encoding="utf-8") as out:
            out.write(substitute(text, values))
    else:
        shutil.copyfile(src, dst)
    shutil.copymode(src, dst)


# ---------------------------------------------------------------- the build

def plan_lines(probe: str, variant: str, scratch: str, log: str, manifest: str, files: List[str],
               settings_src: Optional[str], control: bool, events: Tuple[str, ...]) -> List[str]:
    return [
        "probe              : %s  %s" % (probe, PROBES[probe]["title"]),
        "variant            : %s%s" % (variant or "(none)", "  [--control: %s stripped]" % PROBES[probe]["control_key"]
                                      if control else ""),
        "scratch repo       : %s" % scratch,
        "hook-input log     : %s  (observe.py on %d events)" % (log, len(events)),
        "manifest           : %s" % manifest,
        "settings snippet   : %s" % (os.path.relpath(settings_src, KIT_ROOT) if settings_src else "(none)"),
        "files              : %s" % (", ".join(files) if files else "(none)"),
    ]


def build(a: argparse.Namespace) -> int:
    if os.environ.get("CLAUDECODE"):
        raise ProbeError("refusing to run inside a Claude Code session (CLAUDECODE is set); "
                         "run this from a normal terminal")
    probe = a.probe.upper()
    if probe in CLOSED:
        raise ProbeError("%s is %s" % (probe, CLOSED[probe]))
    if probe not in PROBES:
        raise ProbeError("unknown probe %r; known: %s" % (a.probe, ", ".join(sorted(PROBES, key=sort_key))))
    spec = PROBES[probe]
    if spec.get("scratch") is False:
        print("%s needs no scratch repo. See the %s section of tools/probes/README.md." % (probe, probe))
        return 0
    variants = variants_of(probe)
    variant = a.variant or ""
    if spec.get("variants_required") and not variant:
        raise ProbeError("%s needs --variant, one of: %s" % (probe, ", ".join(variants)))
    snippets = snippet_files(probe)
    if variant and variant not in snippets:
        raise ProbeError("%s has no variant %r; known: %s" % (probe, variant, ", ".join(variants) or "(none)"))
    if a.control and not spec.get("control_key"):
        raise ProbeError("%s has no --control form" % probe)
    managed = spec.get("managed")
    settings_src = None  # type: Optional[str]
    if managed:
        settings_src = os.path.join(SETTINGS_DIR, managed + ".json")
    elif variant in snippets:
        settings_src = snippets[variant]
    elif "" in snippets:
        settings_src = snippets[""]
    events = tuple(e.strip() for e in a.events.split(",") if e.strip()) if a.events else ALL_EVENTS
    for e in events:
        if not re.match(r"^[A-Za-z]+$", e):
            raise ProbeError("bad event name %r" % e)

    scratch = os.path.realpath(os.path.expanduser(a.scratch))
    if prepare.inside(scratch, KIT_ROOT):
        raise ProbeError("scratch directory %s is inside the kit repo; choose a directory outside it" % scratch)
    nearest = gitutil.nearest_existing_dir(scratch)
    outer = gitutil.toplevel(nearest) if nearest else None
    if outer is not None and not os.path.exists(scratch):
        raise ProbeError("scratch directory %s would be nested inside the git repo %s" % (scratch, outer))
    if os.path.exists(scratch) and (not os.path.isdir(scratch) or os.listdir(scratch)):
        raise ProbeError("scratch directory %s already exists and is not empty" % scratch)
    log = scratch + ".observe.jsonl"
    manifest = scratch + ".probe.json"
    remote = scratch + ".remote.git"
    extras = [log, manifest] + ([remote] if spec.get("remote") else [])
    for p in extras:
        if os.path.exists(p):
            raise ProbeError("%s already exists; use a new scratch path" % p)

    pairs = probe_file_pairs(["common"] + list(spec.get("files", [])))
    rels = [rel for _, rel in pairs]
    for line in plan_lines(probe, variant, scratch, log, manifest, rels, settings_src, a.control, events):
        print(line)
    if a.dry_run:
        print("\n[dry-run] nothing was created. It would write %d files, a git repo with one commit%s, "
              "and print the next step." % (len(pairs), " and a local bare remote" if spec.get("remote") else ""))
        return 0

    os.makedirs(os.path.dirname(scratch), exist_ok=True)
    try:
        return _build(a, probe, variant, spec, scratch, log, manifest, remote, pairs, settings_src, events)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)  # never leave a half-built scratch repo
        if spec.get("remote"):
            shutil.rmtree(remote, ignore_errors=True)
        for p in (log, manifest):
            if os.path.exists(p):
                os.unlink(p)
        raise


def _git(args: List[str], cwd: str) -> str:
    ident = ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull]
    return gitutil.run_git(ident + args, cwd=cwd, check=True, isolated=True).stdout.strip()


def _build(a: argparse.Namespace, probe: str, variant: str, spec: Dict[str, Any], scratch: str, log: str,
           manifest: str, remote: str, pairs: List[Tuple[str, str]], settings_src: Optional[str],
           events: Tuple[str, ...]) -> int:
    values = placeholder_values(scratch, log)
    os.makedirs(scratch)
    gitutil.run_git(["init", "-q"], cwd=scratch, check=True, isolated=True)
    gitutil.run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=scratch, check=True, isolated=True)

    for src, rel in pairs:
        copy_probe_file(src, os.path.join(scratch, rel), values)
    with open(os.path.join(scratch, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("PROBE-SCRATCH-README: scratch repo for kit probe %s. Safe to delete.\n" % probe)
    with open(os.path.join(scratch, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write(".claude/settings.local.json\n")
    if "local_settings" in spec:  # the file a probe's second terminal edits; ignored by git, so created here
        jsonio.atomic_write_json(os.path.join(scratch, ".claude", "settings.local.json"), spec["local_settings"])

    settings = base_settings(log, events)
    managed_note = None  # type: Optional[str]
    if settings_src and spec.get("managed"):
        # Managed settings are installed by the human with sudo, never into the project.
        target = os.path.join(scratch, ".claude", "probe", "managed-settings.json")
        jsonio.atomic_write_json(target, load_snippet(settings_src, values))
        managed_note = target
    elif settings_src:
        snippet = load_snippet(settings_src, values)
        if a.control:
            snippet = strip_key(snippet, spec["control_key"])
        settings = merge_settings(settings, snippet)
    jsonio.atomic_write_json(os.path.join(scratch, ".claude", "settings.json"), settings)

    instructions = ["Probe %s%s%s. Steps: tools/probes/README.md, section %s." % (
        probe, " variant " + variant if variant else "", " (control)" if a.control else "", probe),
        "Hook-input log: %s" % log, "Analyze: python3 %s %s %s" % (
            os.path.join(HERE, "analyze.py"), probe, log)]
    with open(os.path.join(scratch, "PROBE-INFO.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(instructions) + "\n")

    facts = {}  # type: Dict[str, str]
    _git(["add", "-A"], scratch)
    _git(["commit", "-q", "-m", "probe %s: scratch repo" % probe], scratch)
    if spec.get("remote"):
        gitutil.run_git(["init", "-q", "--bare", remote], check=True, isolated=True)
        _git(["remote", "add", "origin", remote], scratch)
        _git(["push", "-q", "origin", "main"], scratch)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], scratch)
        gitutil.run_git(["--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main"], check=True, isolated=True)
        facts["main"] = _git(["rev-parse", "--short", "main"], scratch)
        facts["origin/main"] = _git(["rev-parse", "--short", "origin/main"], scratch)
    if spec.get("feature_branch"):
        _git(["checkout", "-q", "-b", "feature/probe"], scratch)
        with open(os.path.join(scratch, "FEATURE.txt"), "w", encoding="utf-8") as fh:
            fh.write("a commit that exists only on feature/probe\n")
        _git(["add", "FEATURE.txt"], scratch)
        _git(["commit", "-q", "-m", "feature-only commit"], scratch)
        facts["feature/probe"] = _git(["rev-parse", "--short", "HEAD"], scratch)
    elif spec.get("git_branch"):
        _git(["checkout", "-q", "-b", spec["git_branch"]], scratch)
        facts["main"] = _git(["rev-parse", "--short", "main"], scratch)
        with open(os.path.join(scratch, "FEATURE.txt"), "w", encoding="utf-8") as fh:
            fh.write("a commit that exists only on %s\n" % spec["git_branch"])
        _git(["add", "FEATURE.txt"], scratch)
        _git(["commit", "-q", "-m", "feature-only commit"], scratch)
        facts[spec["git_branch"]] = _git(["rev-parse", "--short", "HEAD"], scratch)

    data = {"schema": "probe-manifest.v1", "probe": probe, "variant": variant, "control": bool(a.control),
            "prepared_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scratch": scratch, "observe_log": log, "remote": remote if spec.get("remote") else None,
            "facts": facts, "events": list(events), "python3": sys.version.split()[0]}
    jsonio.atomic_write_json(manifest, data)

    q = prepare.sh_quote
    print("\nprepared probe %s at %s (clean tree, branch %s)" % (probe, scratch, _git(["branch", "--show-current"], scratch)))
    print("\nIn a NORMAL terminal (not a Claude Code session), set these first:\n")
    print("  export SCRATCH=%s" % q(scratch))
    print("  export LOG=%s" % q(log))
    print("  claude --version        # record this in the results table")
    if facts:
        print("\ncommits to compare against: " + ", ".join("%s=%s" % kv for kv in sorted(facts.items())))
    if managed_note:
        print("\nThe managed-settings file to install (needs sudo, see README V21): %s" % managed_note)
    if os.path.exists(os.path.join(scratch, "PROBE-PROMPT.txt")):
        print("\nThe prompt(s) to paste are in %s" % os.path.join(scratch, "PROBE-PROMPT.txt"))
    print("\nThen follow tools/probes/README.md, section %s. Nothing has been started; this script does not run claude." % probe)
    print("After the run:\n  python3 %s %s %s" % (q(os.path.join(HERE, "analyze.py")), probe, q(log)))
    return 0


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Prepare a scratch repo for one probe (human-run only).")
    ap.add_argument("scratch", help="directory to create; must be outside this repo and empty or absent")
    ap.add_argument("--probe", required=True, help="V-item, for example V1 (see tools/probes/README.md)")
    ap.add_argument("--variant", help="snippet variant (V9, V18 and V23 need one)")
    ap.add_argument("--control", action="store_true",
                    help="strip the probe's control key from the snippet (V9: onFailure, V19: failIfUnavailable)")
    ap.add_argument("--events", help="comma-separated hook events to log (default: %s)" % ",".join(ALL_EVENTS))
    ap.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    a = ap.parse_args(argv)
    try:
        return build(a)
    except (ProbeError, gitutil.GitError, ValueError, OSError) as exc:
        print("make-scratch: %s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
