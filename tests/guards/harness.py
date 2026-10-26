"""Fixtures and a row runner shared by the guard tests.

One set of temp repositories is built per test class (building them is the slow part) and every corpus row runs
in-process through `guards.engine.evaluate`, which is the same code path as the hook entry point. A handful of rows also
go through the real `run.py` subprocess to prove the exit-code protocol (test_hooks.py).

Row format (tests/guards/corpus/*.json):

    {"id": "git-001", "tool": "Bash", "input": {"command": "..."}, "caller": "engineer", "repo": "main",
     "expect": "deny" | "allow" | "ask", "rule": "guard-git:no-verify", "why": "...",
     "config": {"git_autonomy": "branch"}, "tainted": true, "cwd": "{MAIN}/src", "hook": {...extra hook keys...}}

`{MAIN}`, `{FEAT}`, `{WT}`, `{PLAIN}`, `{HOME}`, `{DETACHED}` expand to fixture paths.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional

from guards import engine, lib
from guards.lib import Ctx
from kitlib import config as config_mod
from kitlib import runstate

from tests import REPO_ROOT
from tests.helpers.gitrepo import TempRepo

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")

SESSIONS = {
    "conductor": "sess-conductor",
    "unknown": "sess-unknown",
}

CONFIG_TEXT = "# Loop Config\n\n```yaml\nautonomy_level: L1\n```\n"


class Fixtures(object):
    """main repo (on main), feat repo (on feat/x), a linked worktree of main on feat/wt, a detached worktree, a plain
    directory and a fake HOME."""

    def __init__(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="kit-guards-fx-"))
        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, ".ssh"))
        self.plain = os.path.join(self.root, "plain")
        os.makedirs(self.plain)
        self.main = self._repo("main-repo", "main")
        self.feat = self._repo("feat-repo", "main")
        self.feat.git("checkout", "-q", "-b", "feat/x")
        self.wt = self.main.add_worktree("main-wt", "feat/wt")
        self.detached = self.main.add_worktree("main-detached")
        for repo in (self.main, self.feat):
            os.symlink("../.claude/settings.json", os.path.join(repo.path, "src", "settings-link.json"))
            os.symlink(".claude", os.path.join(repo.path, "dotclaude"))
            repo.git("config", "alias.shove", "push origin")
            repo.git("config", "alias.pp", "!git push")
            repo.git("config", "alias.st", "status")
        self.state = runstate.RunState(self.main.common_dir()).ensure()
        self.state.register_session(SESSIONS["conductor"], "conductor")
        self.state.register_session("sess-driver-eng", "backend-engineer")
        with open(os.path.join(self.state.root, "locked_paths.json"), "w") as fh:
            json.dump({"globs": ["tests/locked/**"]}, fh)
        self.feat_state = runstate.RunState(self.feat.common_dir()).ensure()
        with open(os.path.join(self.feat_state.root, "locked_paths.json"), "w") as fh:
            json.dump({"globs": ["tests/locked/**"]}, fh)
        self.feat_state.register_session(SESSIONS["conductor"], "conductor")
        self.subs = {
            "{MAIN}": self.main.path, "{FEAT}": self.feat.path, "{WT}": self.wt, "{PLAIN}": self.plain, "{HOME}": self.home,
            "{DETACHED}": self.detached, "{ROOT}": self.root,
        }

    def _repo(self, name: str, branch: str) -> TempRepo:
        parent = os.path.join(self.root, name + "-p")
        os.makedirs(parent)
        r = TempRepo(parent=parent, name=name, branch=branch)
        r.write(".claude/loop.config.md", CONFIG_TEXT)
        r.write(".claude/settings.json", "{}\n")
        r.write(".claude/hooks/guard.sh", "#!/bin/sh\n")
        r.write(".claude/agents/backend-engineer.md", "---\nname: backend-engineer\n---\n")
        r.write(".claude/context/backend.md", "# backend primer\n")
        r.write(".claude/scripts/guards/run.py", "# guards\n")
        r.write(".github/workflows/ci.yml", "name: ci\n")
        r.write(".github/CODEOWNERS", "* @owner\n")
        r.write(".mcp.json", "{}\n")
        r.write("src/app.py", "print('hi')\n")
        r.write("tests/test_app.py", "def test_x(): pass\n")
        r.write("docs/readme.md", "# docs\n")
        r.write("README.md", "# readme\n")
        r.write("package.json", json.dumps({"name": "acme", "scripts": {"test": "jest", "lint": "eslint .", "deploy": "cdk deploy --all",
                                                                       "build": "tsc", "predeploy": "echo pre", "release": "npm run deploy"}}, indent=2))
        r.write("Makefile", "TF = terraform\n\n.PHONY: deploy test\ntest:\n\tpytest -q\n\ndeploy:\n\t@$(TF) apply -auto-approve\n\nplan-all: test\n\techo ok\n")
        r.write("justfile", "test:\n    pytest -q\n\nship:\n    terraform apply\n")
        r.write("scripts/evil.sh", "#!/bin/sh\nterraform apply\n")
        r.write("scripts/ok.sh", "#!/bin/sh\necho fine\nnpm test\n")
        r.write("scripts/tracked_deploy.py", "import subprocess\nsubprocess.run(['terraform', 'apply'])\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "fixtures")
        # agent-authored (untracked) scripts
        r.write("scripts/untracked_deploy.py", "import subprocess\nsubprocess.run(['terraform', 'apply'])\n")
        r.write("scripts/untracked_ok.py", "print('hello')\n")
        r.write("scripts/untracked_push.js", "require('child_process').execSync('git push origin main')\n")
        r.write("scripts/untracked_writes_settings.py", "open('.claude/settings.json', 'w').write('{}')\n")
        r.write("scripts/untracked_sdk.py", "import claude_agent_sdk\n")
        return r

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def sub(self, v: Any) -> Any:
        if isinstance(v, str):
            for k, p in self.subs.items():
                v = v.replace(k, p)
            return v
        if isinstance(v, dict):
            return {k: self.sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.sub(x) for x in v]
        return v

    def repo_for(self, name: str) -> Optional[str]:
        return {"main": self.main.path, "feat": self.feat.path, "wt": self.wt, "plain": self.plain, "detached": self.detached}.get(name)

    def state_for(self, name: str):
        if name == "feat":
            return self.feat_state
        if name == "plain":
            return None
        return self.state


CALLERS = {
    "conductor": dict(session="conductor"),
    "unknown": dict(session="unknown"),
    "engineer": dict(session="unknown", agent_id="agent-eng-1", agent_type="backend-engineer"),
    "engineer-plugin": dict(session="unknown", agent_id="agent-eng-2", agent_type="chain-of-custody:backend-engineer"),
    "infra-engineer": dict(session="unknown", agent_id="agent-eng-3", agent_type="infra-engineer"),
    "critic": dict(session="unknown", agent_id="agent-crit-1", agent_type="code-reviewer"),
    "critic-plugin": dict(session="unknown", agent_id="agent-crit-2", agent_type="kit:design-critic"),
    "security-reviewer": dict(session="unknown", agent_id="agent-crit-3", agent_type="security-reviewer"),
    "unknown-subagent": dict(session="unknown", agent_id="agent-x"),
    "offroster-subagent": dict(session="unknown", agent_id="agent-y", agent_type="my-custom-helper"),
    "conductor-subagent": dict(session="conductor", agent_id="agent-eng-9", agent_type="backend-engineer"),
    "driver-child-engineer": dict(session="driver-eng"),
    "main-agent-session": dict(session="unknown", agent_type="backend-engineer"),
    "main-offroster-agent-session": dict(session="unknown", agent_type="made-up-role"),
}


def build_hook(fx: Fixtures, row: Mapping[str, Any]) -> Dict[str, Any]:
    c = dict(CALLERS[row.get("caller", "unknown")])
    sess = c.pop("session")
    sid = {"conductor": SESSIONS["conductor"], "unknown": SESSIONS["unknown"], "driver-eng": "sess-driver-eng"}[sess]
    repo = fx.repo_for(row.get("repo", "main")) or fx.main.path
    cwd = fx.sub(row.get("cwd", repo))
    tool, inp = row.get("tool", "Bash"), row.get("input")
    if inp is None:
        if "cmd" in row:
            tool, inp = "Bash", {"command": row["cmd"]}
        elif "path" in row:
            tool = row.get("tool", "Write")
            inp = {"file_path": row["path"], "content": row.get("content", "x")}
        else:
            inp = {}
    hook = {"session_id": sid, "cwd": cwd, "hook_event_name": row.get("event", "PreToolUse"), "tool_name": tool,
            "tool_input": fx.sub(inp)}
    hook.update(c)
    for k, v in (row.get("hook") or {}).items():
        hook[k] = fx.sub(v)
    return hook


def config_for(row: Mapping[str, Any]):
    cfg = row.get("config")
    if cfg is None:
        return None
    lines = ["# Loop Config", "", "```yaml"]
    for k, v in cfg.items():
        if isinstance(v, dict):
            lines.append("%s:" % k)
            for kk, vv in v.items():
                lines.append("  %s: %s" % (kk, json.dumps(vv)))
        else:
            lines.append("%s: %s" % (k, json.dumps(v)))
    lines.append("```")
    return config_mod.load_text("\n".join(lines) + "\n")


def evaluate_row(fx: Fixtures, row: Mapping[str, Any]):
    hook = build_hook(fx, row)
    state = fx.state_for(row.get("repo", "main"))
    ctx = Ctx(hook, env={"HOME": fx.home, "PATH": "/usr/bin:/bin"}, state=state, config=config_for(row))
    undo = []   # type: List[str]
    if row.get("tainted") and state is not None:
        lib.mark_tainted(state, hook, "test")
        undo.append(lib._taint_path(state, lib._taint_key(hook)))
    if row.get("halt") and state is not None:
        state.set_halt("test halt", "test")
        undo.append(state.halt_path)
    if row.get("lock") and state is not None:
        state.acquire_lock({"runId": "r1", "sessionId": SESSIONS["conductor"]})
        undo.append(state.lock_path)
    try:
        return engine.evaluate(hook, ctx=ctx)
    finally:
        for p in undo:
            try:
                os.unlink(p)
            except OSError:
                pass


def load_corpus(name: str) -> List[Dict[str, Any]]:
    with open(os.path.join(CORPUS_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)
