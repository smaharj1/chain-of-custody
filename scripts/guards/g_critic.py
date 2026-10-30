"""guard-critic-write (report §7.1; D-15, A32): a critic is read-only.

For callers classified as critics (`code-reviewer`, `design-critic`, `security-reviewer`, with or without a
`<plugin>:` prefix, plus any role the project puts in the `roster` as a critic):
  * Edit, Write, MultiEdit, NotebookEdit are denied, except for the critic's own report: a new file under
    `docs/features/<slug>/reports/` or `/reviews/`, or an existing one whose name says it is a review
  * Bash stays available for read-only commands and scanners. Denied: redirections to files (except `/dev/null`
    and the report path), `tee`, `sed -i`, `perl -i`, `cp`, `mv`, `rm`, `touch`, `mkdir`, `ln`, `chmod`, `dd of=`,
    `curl -o`, `patch`, `git apply`, and package installs
"""
from __future__ import annotations

import re
from typing import Optional

from . import g_file, paths
from .lib import BASH_TOOLS, EDIT_TOOLS, Ctx, Decision, deny

G = "guard-critic-write"

DEVICES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2")
INSTALL_SUBS = {"install", "i", "add", "ci", "update", "up", "upgrade", "rebuild", "remove", "uninstall", "rm", "link", "dedupe"}
INSTALLERS = {"npm", "pnpm", "yarn", "bun", "pip", "pip3", "pipx", "uv", "poetry", "gem", "cargo", "brew", "apt", "apt-get", "yum", "dnf", "apk", "go", "composer", "bundle", "mix", "conda"}
WRITERS = {"cp", "mv", "rm", "touch", "mkdir", "ln", "chmod", "chown", "dd", "tee", "install", "rsync", "truncate", "shred", "unlink", "rmdir", "patch", "sponge"}


def check(ctx: Ctx) -> Optional[Decision]:
    if not ctx.is_critic:
        return None
    tool = ctx.tool_name
    if tool in EDIT_TOOLS:
        raw = g_file._target(ctx.tool_input)
        if raw and paths.is_report_path(paths.physical(raw, ctx.cwd, ctx.home)):
            return None
        return deny(G, "edit", "%s is read-only: it may write its own report under docs/features/<slug>/reports/ and nothing else (found %s)"
                    % (ctx.caller.role or "a critic", (raw or "no path")[:60]))
    if tool in BASH_TOOLS:
        ana = ctx.analysis
        for inv in ana.invs:
            if inv.name in INSTALLERS:
                pos = [a for a in inv.args if not a.startswith("-")]
                if pos and pos[0].lower() in INSTALL_SUBS and not (inv.name == "go" and pos[0] not in ("install", "get")):
                    return deny(G, "install", "%s %s installs packages; a critic is read-only (D-15)" % (inv.name, pos[0]))
            for t in g_file.write_targets(ctx, inv):
                if t.text in DEVICES or t.text.startswith("&"):
                    continue
                if t.kind == "redirect" or inv.name in WRITERS or t.kind in ("write", "patch", "move-source", "link"):
                    p = paths.physical(t.text, inv.cwd, ctx.home) if "\x00" not in t.text else ""
                    if p and t.kind in ("redirect", "write") and paths.is_report_path(p):
                        continue
                    return deny(G, "bash-write", "%s would write %s; a critic is read-only and may write only its report under docs/features/<slug>/reports/"
                                % ("a redirection" if t.kind == "redirect" else inv.name, t.text[:50] or "a file"))
            if inv.name in WRITERS and not g_file.write_targets(ctx, inv):
                return deny(G, "bash-write", "%s is a write command; a critic is read-only" % inv.name)
        for ic in ana.inline:
            if not _readonly_code(ic.code):
                return deny(G, "bash-write", "inline %s code can write files; a critic is read-only" % ic.lang)
    return None


def _readonly_code(code: str) -> bool:
    return not re.search(r"(open\s*\(|write|system|popen|subprocess|remove|unlink|rename|mkdir|rmtree|fs\.|child_process)", code)
