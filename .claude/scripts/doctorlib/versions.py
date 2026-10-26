"""Claude Code version range and the kit's own version (report §14, IMP-8, IMP-21).

The doctor never runs the real `claude` from inside a Claude Code session (an agent must not
launch it, CS-2). The order of precedence is: an explicit version (`--claude-version`,
`KIT_CLAUDE_VERSION`); `KIT_CLAUDE_BIN` (tests point it at the fake); the `claude` on PATH, but
only when `ctx.allow_path_claude` is set (the CLI sets it) and `CLAUDECODE` is not in the
environment. Anything else is "unknown", which caps the level at L1.
"""
from __future__ import annotations

import shutil
from typing import Any, Dict, Optional, Tuple

from kitlib import semver

from . import safeio
from .model import Context, Result, check, failed, gatherer, passed, WARN

DEFAULT_MIN = "2.1.302"


def read_claude_version(ctx: Context, allow_path_claude: bool = False) -> Tuple[Optional[str], str]:
    """(version text or None, how it was found or why not)."""
    if ctx.claude_version:
        return ctx.claude_version, "given explicitly"
    env_ver = ctx.getenv("KIT_CLAUDE_VERSION")
    if env_ver:
        return env_ver, "KIT_CLAUDE_VERSION"
    binary = ctx.getenv("KIT_CLAUDE_BIN")
    if binary:
        rc, out, err = ctx.run([binary, "--version"], 10.0, ctx.env, None)
        return (out.strip() or None, "KIT_CLAUDE_BIN --version") if rc == 0 else (None, "KIT_CLAUDE_BIN failed: %s" % (err.strip() or rc))
    if "CLAUDECODE" in ctx.env:
        return None, "running inside a Claude Code session: the doctor does not execute claude here"
    if allow_path_claude:
        found = shutil.which("claude", path=ctx.getenv("PATH") or None)
        if found:
            rc, out, err = ctx.run([found, "--version"], 10.0, ctx.env, None)
            if rc == 0:
                return out.strip() or None, "claude --version"
            return None, "claude --version failed"
        return None, "claude is not on PATH"
    return None, "not read (pass --claude-version or run from a plain terminal)"


@gatherer("kit_version")
def gather_kit_version(ctx: Context) -> Dict[str, Any]:
    p = ctx.repo_path(".claude", "KIT_VERSION")
    text, problem = safeio.read_text_status(p, 8192)
    if problem:
        return {"ok": False, "error": ".claude/KIT_VERSION %s" % ("not found" if problem == safeio.MISSING else problem), "version": None, "date": None,
                "claude_code_min": None, "claude_code_tested_max": None}
    try:
        parsed = semver.parse_kit_version_file(text)
        return {"ok": True, "error": "", **parsed}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "version": None, "date": None, "claude_code_min": None, "claude_code_tested_max": None}


@gatherer("claude_version")
def gather_claude_version(ctx: Context) -> Dict[str, Any]:
    kv = ctx.fact("kit_version")
    minimum = kv.get("claude_code_min") or DEFAULT_MIN
    tested = kv.get("claude_code_tested_max")
    text, how = read_claude_version(ctx, getattr(ctx, "allow_path_claude", False))
    status = semver.range_status(text, minimum, tested)
    return {"text": text, "how": how, "status": status, "min": minimum, "tested_max": tested or "none"}


@check("version.kit", "version")
def check_kit_version(ctx: Context) -> Result:
    kv = ctx.fact("kit_version")
    if not kv["ok"]:
        return failed("version.kit", kv["error"], "restore .claude/KIT_VERSION from the kit release", severity=WARN)
    extra = ""
    if kv.get("claude_code_min") is None:
        extra = "; no claude_code_min recorded (using the floor %s)" % DEFAULT_MIN
    return passed("version.kit", "kit version %s%s" % (kv["version"], extra))


@check("version.claude", "version")
def check_claude_version(ctx: Context) -> Result:
    cv = ctx.fact("claude_version")
    st = cv["status"]
    detail = {"status": st, "min": cv["min"], "tested_max": cv["tested_max"], "version": cv["text"]}
    if st == semver.OK:
        return passed("version.claude", "Claude Code %s is within %s to %s" % (cv["text"], cv["min"], cv["tested_max"]), detail=detail)
    rem = {
        semver.UNKNOWN: "run the doctor from a plain terminal (not inside a Claude Code session), or pass --claude-version; unknown caps the level at L1",
        semver.BELOW_MIN: "upgrade Claude Code to at least %s (the floor changes how dispatch waits and how headless permissions start, CS-41)" % cv["min"],
        semver.UNTESTED: "run the canary on your pinned CLI, then set claude_code_tested_max in .claude/KIT_VERSION (human, H6)",
        semver.ABOVE_TESTED: "run the canary on this CLI version, or pin the CLI to %s (report §14.3)" % cv["tested_max"],
    }[st]
    msg = {
        semver.UNKNOWN: "Claude Code version could not be read (%s)" % cv["how"],
        semver.BELOW_MIN: "Claude Code %s is below the floor %s" % (cv["text"], cv["min"]),
        semver.UNTESTED: "Claude Code %s: claude_code_tested_max is none, the canary has never passed" % cv["text"],
        semver.ABOVE_TESTED: "Claude Code %s is newer than the tested maximum %s" % (cv["text"], cv["tested_max"]),
    }[st]
    return failed("version.claude", msg, rem, severity=WARN, detail=detail)


@check("version.python", "version")
def check_python(ctx: Context) -> Result:
    import sys
    if sys.version_info < (3, 9):
        return failed("version.python", "python %d.%d is below the 3.9 floor" % sys.version_info[:2], "install python 3.9 or newer")
    return passed("version.python", "python %d.%d" % sys.version_info[:2])
