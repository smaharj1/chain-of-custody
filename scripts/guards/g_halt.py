"""guard-halt (report §7.1): checked first. While `<git-common-dir>/loopctl/HALT` exists, every mutating tool is
denied; read-only tools and read-only shell commands stay available so a human can still look around.

The `factory:halt` GitHub label is checked by the driver (it needs the network); this guard reads only the file.
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import g_exec, g_file, g_git
from .invoke import Inv
from .lib import BASH_TOOLS, EDIT_TOOLS, Ctx, Decision, deny

G = "guard-halt"

SAFE_CMDS = {
    "cat", "head", "tail", "less", "more", "ls", "pwd", "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "wc", "diff", "cmp", "stat", "file",
    "tree", "which", "whoami", "id", "date", "printenv", "uname", "hostname", "basename", "dirname", "realpath", "readlink", "sort", "uniq", "cut", "tr",
    "jq", "true", "false", "test", "[", "cd", "pushd", "popd", "sleep", "md5sum", "shasum", "sha256sum", "xxd", "od", "hexdump", "column", "nl", "tac",
    "rev", "fold", "expand", "paste", "join", "comm", "seq", ":", "export", "set", "unset", "local", "declare", "type", "command", "wait", "read",
    "awk", "gawk", "sed", "gsed", "yq", "find", "env", "xargs", "tee", ":assign", ":redirect", "du", "df", "ps", "lsof", "top", "uptime", "free", "vm_stat",
}
# `resume` is not here: HALT is released by a human, not by a session that is halted (CS-13). The read commands are g_exec's.


def _halted(ctx: Ctx) -> Optional[dict]:
    seen = []
    states = [ctx.state]
    raw = ctx.tool_input.get("file_path") or ctx.tool_input.get("notebook_path") or ""
    if isinstance(raw, str) and raw and ctx.tool_name in EDIT_TOOLS:
        try:
            states.append(ctx.state_for(raw if raw.startswith("/") else ctx.cwd + "/" + raw))
        except Exception:
            pass
    for st in states:
        if st is None or st.root in seen:
            continue
        seen.append(st.root)
        if st.is_halted():
            h = st.read_halt() or {}
            return {"reason": str(h.get("reason", "no reason recorded")), "by": str(h.get("by", "?")), "root": st.root}
    return None


def check(ctx: Ctx) -> Optional[Decision]:
    h = _halted(ctx)
    if h is None:
        return None
    tool = ctx.tool_name
    msg = "HALT is set (%s, by %s): the run is stopped. Read-only work is still allowed; a human clears it with `! loopctl resume` or by deleting %s/HALT" % (
        h["reason"][:80], h["by"][:30], h["root"])
    if tool in EDIT_TOOLS:
        return deny(G, "halted", "%s is refused. %s" % (tool, msg))
    if tool in BASH_TOOLS:
        ana = ctx.analysis
        for p in ana.problems:
            return deny(G, "halted", "the command cannot be analysed (%s) and HALT is set. %s" % (p.message, msg))
        ctx.force_tier = 0
        try:
            for inv in ana.invs:
                if not _read_only(ctx, inv):
                    return deny(G, "halted", "`%s` is not a read-only command. %s" % (inv.name[:30], msg))
        finally:
            ctx.force_tier = None
        for ic in ana.inline:
            return deny(G, "halted", "inline %s code is not a read-only command. %s" % (ic.lang, msg))
        for t in [x for inv in ana.invs for x in g_file.write_targets(ctx, inv)]:
            if t.kind == "redirect" and t.text in ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"):
                continue
            return deny(G, "halted", "writing %s is refused. %s" % (t.text[:40], msg))
        return None
    if tool.startswith("mcp__") or tool in ("Agent", "Task", "Workflow", "Skill"):
        return deny(G, "halted", "%s starts new work. %s" % (tool, msg))
    return None


def _read_only(ctx: Ctx, inv: Inv) -> bool:
    n = inv.name
    if n == "git":
        return g_git._check_git(ctx, inv, 0) is None and _git_sub_ok(inv)
    if n == "gh":
        return g_git._check_gh(ctx, inv) is None
    lc = g_exec.loopctl_is_read(inv)
    if lc is not None:
        return lc
    if re.match(r"^(python[0-9.]*)$", n):
        return any(re.search(r"(doctor|session_start|ci_lint)\.py$", a) for a in inv.args[:2]) or "json.tool" in inv.args[:2]
    if n in ("sed", "gsed") and any(a.startswith("-i") or a == "--in-place" for a in inv.args):
        return False
    if n in ("find",) and any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for a in inv.args):
        return False
    if n in ("tee", "xargs", "env") and (inv.args or n == "tee"):
        return n == "env" and not inv.args
    return n in SAFE_CMDS


def _git_sub_ok(inv: Inv) -> bool:
    return True
