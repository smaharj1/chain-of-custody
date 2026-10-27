"""Shared guard library: hook input, caller, repo and state resolution, decisions (report §7.1, §5.7).

Identity and repo resolution (§7.1):
  * `agent_id`, `agent_type`, `session_id` and `cwd` come from the hook input. The caller is classified
    by `kitlib.caller.classify` (registered `session_id` + `agent_id`, never `agent_type` alone), after the
    `<plugin>:` prefix is stripped and the name is matched against the roster (built-in roster plus the
    `roster` key of loop.config, CS-39).
  * the branch and the top level are resolved from the TARGET PATH for Edit/Write and from `cwd` for Bash,
    through git. `$CLAUDE_PROJECT_DIR` is never read, and `.claude/worktrees/` is not exempt (D-2, A11).
  * state (HALT, session registry, taint, locked paths) is read from `<git-common-dir>/loopctl/` so every
    worktree of a repo sees the same one (CS-13, CS-25).

A guard is a function `check(ctx) -> Optional[Decision]`. `None` means "no opinion".

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from kitlib import caller as caller_mod  # noqa: E402
from kitlib import config as config_mod  # noqa: E402
from kitlib import gitutil, jsonio, levels, runstate  # noqa: E402

from . import invoke, paths  # noqa: E402

__all__ = ["Decision", "deny", "ask", "Ctx", "UNSET", "BASH_TOOLS", "EDIT_TOOLS", "WEB_TOOLS", "mark_tainted", "is_tainted",
           "locked_paths", "rule_id"]

BASH_TOOLS = ("Bash",)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WEB_TOOLS = ("WebFetch", "WebSearch")
UNSET = object()
DEFAULT_PROTECTED = ("main", "master")


class Decision(object):
    __slots__ = ("kind", "rule", "message")

    def __init__(self, kind: str, rule: str, message: str) -> None:
        self.kind = kind          # "deny" | "ask"
        self.rule = rule          # stable id such as guard-git:no-verify (IMP-22)
        self.message = message

    @property
    def denied(self) -> bool:
        return self.kind == "deny"

    def line(self) -> str:
        one = " ".join(self.message.split())
        return "BLOCKED by %s: %s" % (self.rule, one)

    def __repr__(self) -> str:
        return "Decision(%s %s: %s)" % (self.kind, self.rule, self.message)


def rule_id(guard: str, name: str) -> str:
    return "%s:%s" % (guard, name)


def deny(guard: str, name: str, message: str) -> Decision:
    return Decision("deny", rule_id(guard, name), message)


def ask(guard: str, name: str, message: str) -> Decision:
    return Decision("ask", rule_id(guard, name), message)


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


class Ctx(object):
    """Everything a guard may look at, resolved lazily. Tests pass `state=`, `config=` and `env=` directly."""

    def __init__(self, hook: Mapping[str, Any], env: Optional[Mapping[str, str]] = None, state: Any = UNSET,
                 config: Any = UNSET, home: Optional[str] = None) -> None:
        self.hook = hook if isinstance(hook, Mapping) else {}
        self.env = dict(os.environ if env is None else env)
        self.home = home or paths.home_dir(self.env)
        self.event = _s(self.hook.get("hook_event_name"))
        self.tool_name = _s(self.hook.get("tool_name"))
        ti = self.hook.get("tool_input")
        self.tool_input = ti if isinstance(ti, Mapping) else {}
        self.session_id = self.hook.get("session_id")
        self.agent_id = self.hook.get("agent_id") if isinstance(self.hook.get("agent_id"), str) else None
        self.agent_type = self.hook.get("agent_type")
        cwd = _s(self.hook.get("cwd"))
        self.cwd = os.path.abspath(cwd) if cwd else os.getcwd()
        self._state = state
        self._config = config
        self._caller = None   # type: Any
        self._root = UNSET    # type: Any
        self._analysis = None  # type: Any
        self._tainted = None  # type: Optional[bool]
        self.force_tier = None  # type: Optional[int]   # set by guard-halt to judge git as read-only
        self._branch_cache = {}  # type: Dict[str, Optional[str]]
        self._top_cache = {}   # type: Dict[str, Optional[str]]

    # -- repo and state ------------------------------------------------------------------------------------
    @property
    def root(self) -> Optional[str]:
        if self._root is UNSET:
            self._root = gitutil.toplevel(self.cwd) if os.path.isdir(self.cwd) else None
        return self._root

    @property
    def state(self) -> Optional[runstate.RunState]:
        if self._state is UNSET:
            self._state = runstate.RunState.for_path(self.cwd) if os.path.isdir(self.cwd) else None
        return self._state

    def state_for(self, path: str) -> Optional[runstate.RunState]:
        """The state of the repository that contains `path` (a worktree path resolves to the shared state)."""
        st = self.state
        top = self.toplevel_of(path)
        if st is not None and top is not None and self.root is not None and os.path.realpath(top) == os.path.realpath(self.root):
            return st
        if st is not None and top is None:
            return st
        return runstate.RunState.for_path(path) if top is not None else st

    def toplevel_of(self, path: str) -> Optional[str]:
        key = os.path.normpath(path)
        if key not in self._top_cache:
            self._top_cache[key] = gitutil.toplevel(path)
        return self._top_cache[key]

    def branch_at(self, directory: str) -> Optional[str]:
        key = os.path.normpath(directory)
        if key not in self._branch_cache:
            self._branch_cache[key] = gitutil.current_branch(directory)
        return self._branch_cache[key]

    # -- config --------------------------------------------------------------------------------------------
    @property
    def config(self) -> Any:
        if self._config is UNSET:
            self._config = None
            if self.root:
                text = paths.safe_read(os.path.join(self.root, config_mod.CONFIG_PATH), 1 << 20)
                if text is not None:
                    try:
                        self._config = config_mod.load_text(text, source="loop.config.md")
                    except Exception:
                        self._config = None
        return self._config

    def cfg(self, name: str, default: Any = None) -> Any:
        c = self.config
        if c is None:
            return default
        try:
            return c.get(name, default)
        except Exception:
            return default

    @property
    def level(self) -> int:
        try:
            return levels.parse_level(str(self.cfg("autonomy_level", "L1")))
        except ValueError:
            return levels.L1

    @property
    def roster(self) -> Dict[str, str]:
        ros = dict(caller_mod.DEFAULT_ROSTER)
        extra = self.cfg("roster", None)
        if isinstance(extra, Mapping):
            for k, v in extra.items():
                if isinstance(k, str) and v in (caller_mod.ENGINEER, caller_mod.CRITIC):
                    ros[k] = v
        return ros

    @property
    def protected_branches(self) -> List[str]:
        v = self.cfg("protected_branches", None)
        out = [x for x in v if isinstance(x, str)] if isinstance(v, list) else list(DEFAULT_PROTECTED)
        ib = self.cfg("integration_branch", "")
        if isinstance(ib, str) and ib:
            out.append(ib)
        for d in DEFAULT_PROTECTED:
            if d not in out:
                out.append(d)
        return out

    def is_protected_branch(self, name: Optional[str]) -> bool:
        if not name:
            return False
        low = name.lower()
        for b in self.protected_branches:
            if low == b.lower():
                return True
        return False

    # -- caller --------------------------------------------------------------------------------------------
    @property
    def caller(self) -> caller_mod.Caller:
        if self._caller is None:
            registry = {}  # type: Dict[str, Mapping[str, Any]]
            st = self.state
            sid = self.session_id
            if st is not None and runstate.valid_id(sid):
                try:
                    rec = st.get_session(sid)
                except Exception:
                    rec = None
                if rec is not None:
                    registry[sid] = rec
            self._caller = caller_mod.classify(self.hook, registry, self.roster)
        return self._caller

    @property
    def kind(self) -> str:
        """The caller kind after taint demotion (a tainted conductor loses conductor rights at L2+, §5.3)."""
        k = self.caller.kind
        if k == caller_mod.CONDUCTOR and self.level >= levels.L2 and self.tainted:
            return caller_mod.UNKNOWN
        return k

    @property
    def is_subagent(self) -> bool:
        return self.caller.kind in (caller_mod.ENGINEER, caller_mod.CRITIC, caller_mod.UNKNOWN_SUBAGENT)

    @property
    def is_critic(self) -> bool:
        return self.caller.kind == caller_mod.CRITIC

    @property
    def tainted(self) -> bool:
        if self._tainted is None:
            self._tainted = is_tainted(self.state, self.hook)
        return self._tainted

    @property
    def git_autonomy(self) -> str:
        v = str(self.cfg("git_autonomy", "none") or "none").strip().lower()
        return v if v in ("none", "branch", "branch+commit") else "none"

    # -- bash --------------------------------------------------------------------------------------------------
    @property
    def command(self) -> str:
        return _s(self.tool_input.get("command"))

    @property
    def analysis(self) -> invoke.Analysis:
        if self._analysis is None:
            env = dict(self.env)
            env["PWD"] = self.cwd          # the shell's cwd is the hook's cwd, whatever directory the hook process itself was started in
            env.pop("OLDPWD", None)
            self._analysis = invoke.analyze(self.command, self.cwd, env, self.home)
        return self._analysis


# -------------------------------------------------------------------------------------------------------------------
# taint (§5.3 rule 1)

def _taint_key(hook: Mapping[str, Any]) -> Optional[str]:
    aid = hook.get("agent_id")
    if isinstance(aid, str) and runstate.valid_id(aid):
        return "a-" + aid
    sid = hook.get("session_id")
    if runstate.valid_id(sid):
        return "s-" + sid
    return None


def _taint_path(state: runstate.RunState, key: str) -> str:
    return os.path.join(state.root, "taint", key)


def is_tainted(state: Optional[runstate.RunState], hook: Mapping[str, Any]) -> bool:
    key = _taint_key(hook)
    if state is None or key is None:
        return False
    if os.path.lexists(_taint_path(state, key)):
        return True
    # a subagent inherits its parent session's taint
    sid = hook.get("session_id")
    if isinstance(hook.get("agent_id"), str) and runstate.valid_id(sid):
        return os.path.lexists(_taint_path(state, "s-" + sid))
    return False


def mark_tainted(state: Optional[runstate.RunState], hook: Mapping[str, Any], why: str) -> bool:
    key = _taint_key(hook)
    if state is None or key is None:
        return False
    d = os.path.join(state.root, "taint")
    jsonio.ensure_dir(state.root)
    jsonio.ensure_dir(d)
    jsonio.atomic_write_json(_taint_path(state, key), {"why": why[:200], "at": runstate.now_iso(),
                                                       "tool": str(hook.get("tool_name", ""))[:80]}, 0o600)
    return True


# -------------------------------------------------------------------------------------------------------------------
# state the guards read but loopctl (Phase 4) writes

def locked_paths(state: Optional[runstate.RunState]) -> List[str]:
    """`<git-common-dir>/loopctl/locked_paths.json`: {"globs": [...]} of paths engineers may not edit (locked AC
    tests and other slices' footprints). Absent means none. Contract for Phase 4a."""
    if state is None:
        return []
    try:
        data = jsonio.read_json_opt(os.path.join(state.root, "locked_paths.json"))
    except (ValueError, OSError):
        return []
    if isinstance(data, dict) and isinstance(data.get("globs"), list):
        return [g for g in data["globs"] if isinstance(g, str)]
    return []
