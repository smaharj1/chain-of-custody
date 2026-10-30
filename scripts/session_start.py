#!/usr/bin/env python3
"""SessionStart hook: where the kit is, the version check and the hook-liveness canary (Phase 1; report §5.8, §14.2; IMP-21).

Reads the SessionStart hook input from stdin and does three things, all advisory:

  0. Kit line. One short line with the kit's absolute folder and version (`kit: /abs/plugin/root (2.0.0).`), so the session can name
     kit files and print commands with full paths (`${CLAUDE_PLUGIN_ROOT}` is not set in the Bash tool). Until the project has
     `.claude/loop.config.md` it adds a reminder to run /chain-of-custody:init (or /chain-of-custody:migrate when an old copy install
     is still there). In the kit's own checkout it says neither.
  1. Version check. Compares `claude --version` with the range in the kit's `KIT_VERSION`
     (`claude_code_min`, `claude_code_tested_max`). Below the minimum, above the tested maximum,
     untested (`tested_max: none`) or unreadable: a loud banner at L0-L2; at configured L3 and
     above, a REFUSAL, which means no nonce is written.
  2. Liveness canary. Writes a per-session nonce with `kitlib.runstate` into
     `<git-common-dir>/loopctl/nonces/<session_id>` and prints it in the context it returns. If this
     hook did not load, there is no file and no line in the context. In Phase 1 the conductor's
     prose checks both; loopctl enforces it from Phase 4 (RC-7). A session without a nonce does
     not dispatch.

It never blocks: every path exits 0. A failure is reported in the returned context, and the missing
nonce is what the conductor notices. The returned `additionalContext` stays under 1 KiB (report §19.7,
SessionStart pattern), and the hook does no network I/O and caches the version lookup.

Wiring: the plugin's `hooks/hooks.json` runs it on every SessionStart source:

    "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command", "timeout": 10,
        "command": "... exec python3 -B \"${CLAUDE_PLUGIN_ROOT}/scripts/session_start.py\" ..."}]}]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio  # noqa: E402
from kitlib import config as config_mod  # noqa: E402
from kitlib import install, jsonio, kitpaths, levels, runstate, semver  # noqa: E402

MAX_CONTEXT = 1000
DEFAULT_MIN = "2.1.302"


def _read_version_file(kit_root: str) -> Dict[str, Optional[str]]:
    empty = {"version": None, "date": None, "claude_code_min": None, "claude_code_tested_max": None}  # type: Dict[str, Optional[str]]
    text = safeio.read_text(os.path.join(kit_root, "KIT_VERSION"), 8192)
    if text is None:
        return empty
    try:
        return semver.parse_kit_version_file(text)
    except ValueError:
        return empty


def claude_version(env: Mapping[str, str], state: Optional[runstate.RunState], run=subprocess.run) -> Tuple[Optional[str], str]:
    """The installed Claude Code version, from env, then a cached `--version` call. (None, why) if unreadable."""
    for name in ("KIT_CLAUDE_VERSION", "CLAUDE_CODE_VERSION"):
        if env.get(name):
            return env[name], name
    binary = env.get("KIT_CLAUDE_BIN")
    if not binary:
        for d in (env.get("PATH") or "").split(os.pathsep):
            cand = os.path.join(d, "claude")
            if d and os.path.isfile(cand) and os.access(cand, os.X_OK):
                binary = cand
                break
    if not binary:
        return None, "claude not found on PATH"
    try:
        st = os.stat(binary)
        key = "%s:%d:%d" % (os.path.realpath(binary), int(st.st_mtime), st.st_size)
    except OSError:
        return None, "cannot stat claude"
    cache = os.path.join(state.root, "claude-version.json") if state is not None else None
    if cache:
        try:
            cached = jsonio.read_json_opt(cache)
            if isinstance(cached, dict) and cached.get("key") == key and cached.get("version"):
                return cached["version"], "cached"
        except (ValueError, OSError):
            pass
    try:
        proc = run([binary, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, "claude --version did not answer"
    text = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not text:
        return None, "claude --version failed"
    if cache:
        try:
            state.ensure()
            jsonio.atomic_write_json(cache, {"key": key, "version": text}, 0o600)
        except OSError:
            pass
    return text, "claude --version"


def configured_level(repo: str) -> Tuple[int, str]:
    try:
        text, problem = safeio.read_text_status(os.path.join(repo, config_mod.CONFIG_PATH), 1 << 20)
        if problem == safeio.MISSING:
            return levels.L1, "loop.config not found"
        if problem:
            return levels.L1, "loop.config unreadable (%s)" % problem
        cfg = config_mod.load_text(text)
        return levels.parse_level(str(cfg.get("autonomy_level", "L1"))), ""
    except Exception as exc:  # unreadable config: assume the default, say so
        return levels.L1, "loop.config unreadable (%s)" % str(exc).split("\n")[0][:60]


def kit_line(repo: str, kit_root: str, version: Optional[str]) -> List[str]:
    """The kit-root line and the /init (or /migrate) reminder. Nothing in the kit's own checkout."""
    if kitpaths.is_kit_dev_checkout(repo):
        return []
    out = ["kit: %s (%s)." % (kit_root, version or "version unreadable")]
    if install.copy_install_markers(repo):
        out.append("This project still has an old copy install of the kit: ask the user to run /chain-of-custody:migrate.")
    elif not os.path.isfile(os.path.join(repo, config_mod.CONFIG_PATH)):
        out.append("This project is not set up for the kit yet (no %s): suggest /chain-of-custody:init." % config_mod.CONFIG_PATH)
    return out


def run(hook_input: Any, env: Mapping[str, str], kit_root: str = kitpaths.KIT_ROOT) -> Dict[str, Any]:
    """Pure-ish core: returns {context, nonce_written, refused, level, status}."""
    hi = hook_input if isinstance(hook_input, dict) else {}
    sid = hi.get("session_id")
    source = str(hi.get("source") or "startup")
    cwd = hi.get("cwd") if isinstance(hi.get("cwd"), str) and hi.get("cwd") else os.getcwd()
    notes = []  # type: List[str]
    state = runstate.RunState.for_path(cwd)
    from kitlib import gitutil
    repo = (gitutil.toplevel(cwd) if state is not None else None) or cwd
    kv = _read_version_file(kit_root)
    kl = kit_line(repo, kit_root, kv.get("version"))
    notes += kl
    level, lnote = configured_level(repo)
    if lnote and len(kl) < 2:          # the /init reminder already says the config is missing
        notes.append(lnote)
    minimum = kv.get("claude_code_min") or DEFAULT_MIN
    tested = kv.get("claude_code_tested_max")
    text, how = claude_version(env, state)
    status = semver.range_status(text, minimum, tested)
    refused = False
    if status != semver.OK:
        msg = {
            semver.UNKNOWN: "Claude Code version unreadable (%s)" % how,
            semver.BELOW_MIN: "Claude Code %s is BELOW the kit floor %s" % (text, minimum),
            semver.UNTESTED: "Claude Code %s: no tested maximum recorded, the canary has never passed" % text,
            semver.ABOVE_TESTED: "Claude Code %s is ABOVE the tested maximum %s" % (text, tested),
        }[status]
        if level >= levels.L3:
            refused = True
            notes.append("REFUSED at L%d: %s. No canary nonce was written; do not dispatch." % (level, msg))
        elif status in (semver.BELOW_MIN, semver.ABOVE_TESTED):
            notes.append("WARNING: %s (report section 14.2)." % msg)
        else:
            notes.append("note: %s." % msg)
    nonce = None  # type: Optional[str]
    if state is None:
        notes.append("canary NOT recorded: not inside a git repository.")
    elif not runstate.valid_id(sid):
        notes.append("canary NOT recorded: hook input has no usable session_id.")
    elif refused:
        pass
    else:
        try:
            if source in ("resume", "compact"):
                nonce = state.read_nonce(sid)
            if not nonce:
                nonce = state.write_nonce(sid)
        except (OSError, ValueError) as exc:
            notes.append("canary NOT recorded: %s." % str(exc)[:80])
    if state is not None:
        try:
            if state.is_halted():
                notes.append("HALT is set (%s): the loop must not dispatch." % (state.read_halt() or {}).get("reason", "no reason recorded"))
        except (OSError, ValueError):
            pass
    head = "kit session_start:"
    if nonce:
        head += " canary session=%s nonce=%s." % (sid, nonce)
    ctx = (head + " " + " ".join(notes)).strip()
    if len(ctx) > MAX_CONTEXT:
        ctx = ctx[:MAX_CONTEXT - 3] + "..."
    return {"context": ctx, "nonce": nonce, "refused": refused, "level": level, "status": status}


def main(stdin=None, stdout=None, env: Optional[Mapping[str, str]] = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    env = os.environ if env is None else env
    try:
        raw = stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
        result = run(hook_input, env)
        context = result["context"]
    except Exception as exc:  # never block: report, exit 0
        context = "kit session_start FAILED (%s: %s). The liveness canary was not recorded." % (type(exc).__name__, str(exc)[:120])
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # even a broken stdout must not turn into a block
        sys.exit(0)
