"""Guard self-test (report §7.1): SessionStart feeds known-bad payloads through the guards and expects denies.

The report asks for three; this feeds one per guard family, plus a few payloads that must be ALLOWED so a guard that
denies everything is also caught. A failed self-test is recorded in `<git-common-dir>/loopctl/selftest.json`; the
doctor reads it and caps the effective level at L0 (IMP-21: a guard suite that is not working is no guard suite).
It runs without a repository, a registry or a config file, in a temporary directory.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Tuple

from . import engine
from .lib import Ctx

SELFTEST_VERSION = "1"


def _cases(d: str) -> List[Tuple[str, Dict[str, Any], str]]:
    """(name, hook input, expected: 'deny' | 'allow'). Callers are unregistered or engineer subagents, no state needed."""
    base = {"session_id": "selftest-session", "cwd": d, "hook_event_name": "PreToolUse"}
    eng = dict(base, agent_id="selftest-agent", agent_type="backend-engineer")
    crit = dict(base, agent_id="selftest-critic", agent_type="code-reviewer")

    def bash(cmd: str, who: Dict[str, Any] = base) -> Dict[str, Any]:
        return dict(who, tool_name="Bash", tool_input={"command": cmd})

    def edit(path: str, who: Dict[str, Any] = base) -> Dict[str, Any]:
        return dict(who, tool_name="Write", tool_input={"file_path": path, "content": "x"})
    return [
        ("git push is denied", bash("git push origin main"), "deny"),
        ("--no-verify is denied", bash("git commit --no-verify -m x"), "deny"),
        ("wrapped git push is denied", bash("sh -c 'env FOO=1 git push'"), "deny"),
        ("terraform apply is denied", bash("terraform apply -auto-approve"), "deny"),
        ("launching claude is denied", bash("claude -p hello"), "deny"),
        ("settings.json write is denied", edit(os.path.join(d, ".claude", "settings.json")), "deny"),
        ("a path trick to settings.json is denied", edit(os.path.join(d, "src", "..", ".claude", ".", "settings.json")), "deny"),
        ("a redirect into a hook is denied", bash("echo x > .claude/hooks/guard.sh"), "deny"),
        ("a critic cannot write source", edit(os.path.join(d, "src", "a.py"), crit), "deny"),
        ("an engineer cannot replay loopctl", bash("python3 .claude/scripts/loopctl advance item-1 building", eng), "deny"),
        ("an engineer's inline interpreter is denied", bash("python3 -c 'import os; os.system(\"id\")'", eng), "deny"),
        ("git status is allowed", bash("git status --short"), "allow"),
        ("a normal test run is allowed", bash("npm run test -- --watch=false"), "allow"),
        ("writing source is allowed", edit(os.path.join(d, "src", "a.py")), "allow"),
        ("a critic can read", bash("grep -rn TODO src | head -20", crit), "allow"),
    ]


def run_selftest() -> Tuple[bool, List[str], int]:
    """Returns (ok, failure lines, number of cases)."""
    d = os.path.realpath(tempfile.mkdtemp(prefix="kit-guards-selftest-"))
    failures = []   # type: List[str]
    cases = _cases(d)
    try:
        os.makedirs(os.path.join(d, "src"))
        os.makedirs(os.path.join(d, ".git"))          # a repository, so that its .claude/ is a project's
        env = {"HOME": d, "PATH": "/usr/bin:/bin"}
        for name, hook, expect in cases:
            try:
                dec = engine.evaluate(hook, env=env, state=None, config=None)
            except Exception as exc:
                failures.append("%s: raised %s: %s" % (name, type(exc).__name__, str(exc)[:80]))
                continue
            got = "deny" if (dec is not None and dec.denied) else "allow"
            if got != expect:
                failures.append("%s: expected %s, got %s%s" % (name, expect, got, " (%s)" % dec.rule if dec else ""))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return (not failures), failures, len(cases)
