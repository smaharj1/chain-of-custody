#!/usr/bin/env python3
"""Prepare one baseline run directory (IMP-2). Called by run-baseline.sh; human-run only.

What it does, in order:
  1. refuses if CLAUDECODE is set (it is never run from inside a Claude Code session)
  2. checks the source repo has the tag (default v1.2.0) and that the destination is outside
     the source repo and is empty or absent
  3. `git clone --no-hardlinks --branch <tag>` of the source into the destination, checks out
     a branch `main`, and removes the `origin` remote so nothing can be pushed
  4. overlays the fixture (examples/hello-loop) WITHOUT reference-solution/, acceptance/,
     verify.json and the fixture README, then the fixture's kit-overlay/ (pre-filled primers,
     CLAUDE.md, loop.config.md), then deletes the paths in kit-overlay/remove.txt
  5. installs the observation hook and the sandbox setting into the clone's .claude/settings.json
  6. commits the result so the working tree is clean, writes <dest>.baseline.json (facts about
     the run) next to the clone, and prints the exact `claude` command for the human

It never runs `claude` (not even `--version`), never touches the network, and never pushes.
`--dry-run` prints the plan and creates nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(KIT_ROOT, ".claude", "scripts"))

from kitlib import gitutil, jsonio  # noqa: E402

DEFAULT_TAG = "v1.2.0"
DEFAULT_FIXTURE = os.path.join(KIT_ROOT, "examples", "hello-loop")

# Never copied into a run: the answer key, the held-out checks and the maintainer docs.
FIXTURE_EXCLUDE = ("reference-solution", "acceptance", "kit-overlay", "verify.json", "README.md",
                   "__pycache__", ".data")

# Hook events the observation hook subscribes to. Event names are the documented ones for the
# version floor (report §14); the first real run confirms them (docs/baseline/README.md).
OBSERVED_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                   "PostToolUseFailure", "PermissionRequest", "Notification", "SubagentStart",
                   "SubagentStop", "Stop", "PreCompact")
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest")

# Environment variables the printed command unsets (baseline condition: no cloud credentials).
CREDENTIAL_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                  "AWS_DEFAULT_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
                  "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "GH_TOKEN", "GITHUB_TOKEN",
                  "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
                  "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "ARM_CLIENT_SECRET",
                  "KUBECONFIG", "DOCKER_HOST", "NPM_TOKEN", "NODE_AUTH_TOKEN", "SSH_AUTH_SOCK")

SANDBOX_SETTINGS = {"enabled": True, "network": {"allowLocalBinding": True}}


class PrepareError(Exception):
    pass


def sh_quote(s: str) -> str:
    if s and all(c.isalnum() or c in "@%+=:,./-_" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def inside(path: str, parent: str) -> bool:
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def fixture_files(fixture: str) -> List[str]:
    """Relative paths of every file that goes into the run, sorted."""
    out = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(fixture):
        rel_dir = os.path.relpath(dirpath, fixture)
        dirnames[:] = sorted(d for d in dirnames
                             if not (rel_dir == "." and d in FIXTURE_EXCLUDE) and d != "__pycache__")
        for fn in sorted(filenames):
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            if rel in FIXTURE_EXCLUDE or fn.endswith(".pyc"):
                continue
            out.append(rel)
    return out


def overlay_files(fixture: str) -> List[str]:
    base = os.path.join(fixture, "kit-overlay")
    out = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), base)
            if rel != "remove.txt":
                out.append(rel)
    return out


def removals(fixture: str) -> List[str]:
    path = os.path.join(fixture, "kit-overlay", "remove.txt")
    out = []  # type: List[str]
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if os.path.isabs(line) or ".." in line.split("/"):
                    raise PrepareError("kit-overlay/remove.txt: unsafe path %r" % line)
                out.append(line)
    return out


def fixture_digest(fixture: str) -> str:
    h = hashlib.sha256()
    for rel in sorted(fixture_files(fixture) + ["kit-overlay/" + r for r in overlay_files(fixture)]):
        h.update(rel.encode("utf-8") + b"\0")
        with open(os.path.join(fixture, rel), "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    shutil.copymode(src, dst)


def install_settings(settings_path: str, observe: str, log: str) -> None:
    """Add the observation hooks and the sandbox setting; keep every existing setting."""
    data = {}  # type: Dict
    if os.path.isfile(settings_path):
        data = jsonio.read_json(settings_path)
    if not isinstance(data, dict):
        raise PrepareError("%s is not a JSON object" % settings_path)
    command = "python3 %s --log %s" % (sh_quote(observe), sh_quote(log))
    hooks = data.setdefault("hooks", {})
    for event in OBSERVED_EVENTS:
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        if event in TOOL_EVENTS:
            entry["matcher"] = "*"
        hooks.setdefault(event, []).append(entry)
    sandbox = data.setdefault("sandbox", {})
    sandbox.update(SANDBOX_SETTINGS)
    jsonio.atomic_write_json(settings_path, data)


def claude_command(dest: str) -> str:
    unset = " ".join("-u %s" % name for name in CREDENTIAL_ENV)
    return "cd %s && env %s claude \"/orchestrate\"" % (sh_quote(dest), unset)


def plan_lines(a: argparse.Namespace, dest: str, log: str, commit: Optional[str]) -> List[str]:
    return [
        "source repo        : %s" % a.source,
        "tag                : %s%s" % (a.tag, " (%s)" % commit[:12] if commit else ""),
        "run directory      : %s" % dest,
        "fixture            : %s" % a.fixture,
        "observation log    : %s" % log,
        "manifest           : %s" % (dest + ".baseline.json"),
    ]


def prepare(a: argparse.Namespace) -> int:
    if os.environ.get("CLAUDECODE"):
        raise PrepareError("refusing to run inside a Claude Code session (CLAUDECODE is set); "
                           "run this from a normal terminal")
    source = os.path.realpath(a.source)
    top = gitutil.toplevel(source)
    if top is None:
        raise PrepareError("source %s is not a git repository" % source)
    a.source = top
    commit = gitutil.rev_parse("refs/tags/%s^{commit}" % a.tag, top)
    if not commit:
        raise PrepareError("tag %s does not exist in %s (create it first: it is the baseline)" % (a.tag, top))
    fixture = os.path.realpath(a.fixture)
    for needed in ("build-plan.json", "kit-overlay"):
        if not os.path.exists(os.path.join(fixture, needed)):
            raise PrepareError("fixture %s has no %s" % (fixture, needed))
    a.fixture = fixture
    dest = os.path.realpath(os.path.join(os.path.expanduser(a.dest_root), a.run_id))
    if inside(dest, top):
        raise PrepareError("run directory %s is inside the kit repo; choose a directory outside it" % dest)
    nearest = gitutil.nearest_existing_dir(dest)
    outer = gitutil.toplevel(nearest) if nearest else None
    if outer is not None and not os.path.exists(dest):
        raise PrepareError("run directory %s would be nested inside the git repo %s" % (dest, outer))
    if os.path.exists(dest) and os.listdir(dest):
        raise PrepareError("run directory %s already exists and is not empty" % dest)
    log = dest + ".observe.jsonl"
    manifest_path = dest + ".baseline.json"
    for p in (log, manifest_path):
        if os.path.exists(p):
            raise PrepareError("%s already exists; use a new run id" % p)

    for line in plan_lines(a, dest, log, commit):
        print(line)
    if a.dry_run:
        print("\n[dry-run] would clone, overlay %d fixture files and %d overlay files, remove %d paths, "
              "install hooks, commit; nothing was created." %
              (len(fixture_files(fixture)), len(overlay_files(fixture)), len(removals(fixture))))
        print("\nthe command it would print:\n  " + claude_command(dest))
        return 0

    remove_list = removals(fixture)  # validated before anything is created
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        return _build(a, top, commit, fixture, dest, log, manifest_path, remove_list)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a half-built run directory
        for p in (log, manifest_path):
            if os.path.exists(p):
                os.unlink(p)
        raise


def _build(a: argparse.Namespace, top: str, commit: str, fixture: str, dest: str, log: str,
           manifest_path: str, remove_list: List[str]) -> int:
    gitutil.run_git(["clone", "-q", "--no-hardlinks", "--branch", a.tag, top, dest], check=True,
                    timeout=300, isolated=True)
    gitutil.run_git(["checkout", "-q", "-b", "main"], cwd=dest, check=True, isolated=True)
    gitutil.run_git(["remote", "remove", "origin"], cwd=dest, check=True, isolated=True)

    for rel in fixture_files(fixture):
        src, dst = os.path.join(fixture, rel), os.path.join(dest, rel)
        if rel == ".gitignore" and os.path.isfile(dst):
            with open(dst, "a", encoding="utf-8") as out, open(src, "r", encoding="utf-8") as inp:
                out.write("\n# hello-loop fixture\n" + inp.read())
        else:
            copy_file(src, dst)
    for rel in overlay_files(fixture):
        copy_file(os.path.join(fixture, "kit-overlay", rel), os.path.join(dest, rel))
    missing = []  # type: List[str]
    for rel in remove_list:
        target = os.path.join(dest, rel)
        if os.path.isfile(target):
            os.unlink(target)
        else:
            missing.append(rel)
    install_settings(os.path.join(dest, ".claude", "settings.json"),
                     os.path.join(HERE, "observe.py"), log)

    ident = ["-c", "user.name=baseline", "-c", "user.email=baseline@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull]
    gitutil.run_git(["add", "-A"], cwd=dest, check=True, isolated=True)
    gitutil.run_git(ident + ["commit", "-q", "-m", "baseline: hello-loop fixture, kit overlay, observation hook"],
                    cwd=dest, check=True, isolated=True)
    head = gitutil.rev_parse("HEAD", dest) or ""

    manifest = {
        "schema": "baseline-manifest.v1",
        "run_id": a.run_id,
        "prepared_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kit_tag": a.tag,
        "kit_commit": commit,
        "run_commit": head,
        "run_dir": dest,
        "observe_log": log,
        "fixture_digest": fixture_digest(fixture),
        "python3": sys.version.split()[0],
        "hook_events": list(OBSERVED_EVENTS),
        "sandbox_setting": SANDBOX_SETTINGS,
        "removed_paths_missing": missing,
        "claude_bin": a.claude_bin,
    }
    jsonio.atomic_write_json(manifest_path, manifest)
    if missing:
        print("warning: kit-overlay/remove.txt paths not found in the clone: %s" % ", ".join(missing),
              file=sys.stderr)

    print("\nprepared %s at %s (clean tree, branch main, no remote)" % (a.run_id, head[:12]))
    print("\nBaseline conditions in effect (record them in the results file):")
    print("  - sandbox: enabled in .claude/settings.json (confirm with /sandbox in the session)")
    print("  - cloud credentials: none. The command below unsets the usual variables; also make sure")
    print("    ~/.aws, ~/.config/gcloud, ~/.kube and similar are not readable by this OS user.")
    print("  - observation hook: installed; events are appended to the log above")
    print("  - demo capability: record whether a browser preview MCP is connected (/mcp)")
    print("\nStart the run (nothing has been started; this script does not run claude):\n")
    print("  " + claude_command(dest))
    print("\nWhen the run ends: record `claude --version` and the model, fill a results file from")
    print("docs/baseline/results/TEMPLATE.json, and run the held-out checks:")
    print("  python3 %s --project %s --json" % (
        sh_quote(os.path.join(fixture, "acceptance", "run_checks.py")), sh_quote(dest)))
    return 0


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Prepare a baseline run directory (human-run only).")
    ap.add_argument("run_id", help="run name, for example run-1")
    ap.add_argument("--dest-root", default=os.path.join("~", "kit-baseline"),
                    help="directory that will hold the run (default ~/kit-baseline); must be outside the kit repo")
    ap.add_argument("--source", default=KIT_ROOT, help="repo to clone the tag from (default: this kit repo)")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--fixture", default=DEFAULT_FIXTURE)
    ap.add_argument("--claude-bin", default=os.environ.get("KIT_CLAUDE_BIN", "claude"),
                    help="recorded in the manifest only; never executed")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, create nothing")
    a = ap.parse_args(argv)
    if not a.run_id or not all(c.isalnum() or c in "-_." for c in a.run_id) or a.run_id.startswith("."):
        print("run-baseline: run id must be letters, digits, '-', '_' or '.'", file=sys.stderr)
        return 2
    try:
        return prepare(a)
    except (PrepareError, gitutil.GitError, ValueError, OSError) as exc:
        print("run-baseline: %s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
