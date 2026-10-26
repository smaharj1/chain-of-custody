"""guard-main-edit v2 and self-modification protection (report §6.1 H6, §7.1, §5.9, CS-14, CO-11).

Edit, Write, MultiEdit and NotebookEdit
  1. H6 paths: no agent writes `.claude/settings*.json`, `.claude/hooks/**`, `.claude/scripts/**` (the guards,
     doctor and loopctl), `.claude/agents/**`, `.claude/skills/**`, `.claude/commands/**`, `.claude/rules/**`,
     `.claude/workflows/**`, `.mcp.json`, `.claude-plugin/**`, `plugin/**`, `.github/**` (CI), CODEOWNERS, `.git/**`,
     and the shell-startup and credential files in the home directory. `.claude/loop.config.md` may be edited only
     if no H6-protected key changes value.
  2. engineers (and unknown subagents) also cannot edit `.gitattributes`, `.husky/**`, `.envrc`, test/lint/CI
     config, the `scripts` of package.json, or paths listed in `<git-common-dir>/loopctl/locked_paths.json`.
  3. main-edit: on `main`, `master` or the integration branch, only `docs/**`, plan definition files and primers
     (and the project's `main_edit_allow` globs) may be edited. There is no allow-list for `.claude/**` (v1 had one).

Bash
  The same H6 and engineer rules apply to the targets of redirections, `tee`, `cp`, `mv`, `ln`, `rm`, `sed -i`,
  `perl -i`, `dd of=`, `curl -o`, `git restore|checkout --|rm|mv|apply`, `patch`, and to inline interpreter code
  that opens a protected path for writing. This is a heuristic list: an unlisted program that writes a file is
  not seen. The boundary is the sandbox `denyWrite` and CODEOWNERS (B3, B4); this guard makes the common attempts
  fail fast with a reason.

Paths are compared lexically and physically (symlinks resolved), case-folded, so `./.claude/../.claude/settings.json`,
a symlink to it, and `.CLAUDE/Settings.JSON` on a case-insensitive volume are all caught. The repository root is
resolved from the target path, never from `$CLAUDE_PROJECT_DIR`.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kitlib import config as config_mod

from . import paths
from .invoke import Inv
from .lib import Ctx, Decision, deny, locked_paths

G = "guard-file"
GM = "guard-main-edit"

MAIN_ALLOW = ["docs/**", "plan/**", "build-plan.json", "BUILD_PLAN.md", "CHANGELOG.md", "README.md", "GLOSSARY.md", "LICENSE",
              ".gitignore", "CLAUDE.md", ".claude/CLAUDE.md", ".claude/context/**"]
PKG_KEYS = ("scripts", "husky", "lint-staged", "jest", "vitest", "eslintConfig", "prettier", "nyc", "c8", "ava", "mocha", "workspaces",
            "simple-git-hooks", "pre-commit")
DIR_PROBES = {".claude": "settings", ".git": "git", ".github": "ci", ".mcp.json": "mcp", "plugin": "plugin", ".claude-plugin": "plugin"}
WRITER_CMDS = {"rm", "mv", "cp", "chmod", "chown", "chgrp", "tee", "sed", "perl", "truncate", "shred", "install", "ln", "dd", "touch", "unlink", "rmdir",
               "sponge", "rsync", "mkdir", "patch", "gsed", "ruby", "python", "python3", "sh", "bash", "zsh"}
CHILD_PROBES = ["settings.json", "hooks/x", "scripts/guards/x", "agents/x", "skills/x", "commands/x", "rules/x", "workflows/x",
                "loop.config.md", "x"]
SUSPICIOUS_STATIC = (".claude", ".git", "settings", ".mcp", "codeowners", ".github", ".husky", ".envrc", "loopctl", "hooks/", "/hooks")


def _is_engineer_like(ctx: Ctx) -> bool:
    return ctx.caller.kind in ("engineer", "unknown-subagent")


def _desc(cat: str) -> str:
    return paths.PROTECTED_DESCRIPTIONS.get(cat, cat)


def _h6(cat: str, why: str, via: str) -> Decision:
    return deny(G, cat, "%s %s is %s. Agents do not edit it (H6); emit the proposed change for a human to apply, and do not work around this "
                "guard (report §6.1, CO-16)" % (via, why, _desc(cat)))


# ---------------------------------------------------------------------------------------------------------------------
# one path

def _physical_aliases(path: str, aliases: Dict[str, str]) -> str:
    cur = path
    for _ in range(4):
        hit = None
        for link, target in aliases.items():
            if cur == link or cur.startswith(link + os.sep):
                hit = target + cur[len(link):]
                break
        if hit is None:
            break
        cur = hit
    return cur


GUARD_BASENAMES = ("settings.json", "settings.local.json", "loop.config.md", ".mcp.json", "codeowners", "hooks.json", "plugin.json")


def _judge(ctx: Ctx, text: str, dynamic: bool, cwd: str, via: str, tree: Any = False,
           aliases: Optional[Dict[str, str]] = None, unknown_cwd: bool = False) -> Optional[Decision]:
    if unknown_cwd and text and not text.startswith(("/", "~")) and "\x00" not in text:
        parts = [x.lower() for x in text.replace("\\", "/").split("/") if x not in ("", ".")]
        if parts and (parts[0] in (".claude", ".git", ".github", "..") or parts[-1] in GUARD_BASENAMES):
            return deny(G, "dynamic-target", "%s %s after a `cd` to a directory computed at run time: the guard cannot tell which file that is, and it names a "
                        "guardrail location" % (via, text[:60]))
    if dynamic or "\x00" in text:
        static = text.replace("\x00", "").lower()
        if any(s in static for s in SUSPICIOUS_STATIC):
            return deny(G, "dynamic-target", "%s a path computed at run time that mentions %s; the guard cannot prove it is not a protected "
                        "file" % (via, "a guardrail location"))
        if "\x00" in text:
            return None
    cands = [text]
    if any(c in text for c in "*?[") and "\x00" not in text:
        cands = paths.expand_glob(text, cwd, ctx.home)
    for c in cands:
        d = _judge_one(ctx, c, cwd, via, tree, aliases or {})
        if d:
            return d
    return None


def _judge_one(ctx: Ctx, text: str, cwd: str, via: str, tree: Any, aliases: Dict[str, str]) -> Optional[Decision]:
    forms = paths.forms(text, cwd, ctx.home)
    extra = []
    for f in forms:
        m = _physical_aliases(f, aliases)
        if m != f:
            extra.append(m)
            extra.extend(x for x in paths.forms(m, cwd, ctx.home) if x not in extra)
    forms = list(dict.fromkeys(forms + extra))
    for f in forms:
        hit = paths.classify(f, ctx.home)
        if hit:
            return _h6(hit[0], hit[1], via)
        if tree and os.path.isdir(f):
            for child, cat in (DIR_PROBES.items() if tree is True else ()):
                if os.path.lexists(os.path.join(f, child)):
                    return deny(G, cat, "%s %s acts on a directory that contains %s (%s); agents do not remove, move or re-permission "
                                "guardrail paths (H6)" % (via, os.path.basename(f.rstrip("/")) or "/", child, _desc(cat)))
            for probe in CHILD_PROBES:
                hit = paths.classify(os.path.join(f, probe), ctx.home)
                if hit and hit[0] != "ci":
                    return _h6(hit[0], hit[1], via)
    if _is_engineer_like(ctx):
        for f in forms:
            cat = paths.engineer_only(f)
            if cat:
                return deny(G, "engineer-" + cat, "%s %s: engineers do not edit %s (test, lint and CI configuration, git attributes, husky hooks and "
                            ".envrc are changed by a human or the conductor)" % (via, os.path.basename(f), cat))
            locked = locked_paths(ctx.state)
            if locked:
                root = paths.find_root(f)
                rel = paths.rel_to_root(f, root)
                if rel and paths.match_rel(rel, locked):
                    return deny(G, "engineer-locked", "%s %s is a locked acceptance test or another slice's footprint (loopctl locked_paths)" % (via, rel))
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Edit / Write

def _target(ti) -> str:
    for k in ("file_path", "notebook_path", "path"):
        v = ti.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _targets(ti) -> List[str]:
    """Every path-like field of the tool input: judging only the first would let a second, unused one carry the guarded path."""
    out = []   # type: List[str]
    for k in ("file_path", "notebook_path", "path"):
        v = ti.get(k)
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def _new_text(ti, tool: str, current: Optional[str]) -> Optional[str]:
    """What the file will contain after the tool runs, or None if it cannot be worked out."""
    if tool == "Write":
        c = ti.get("content")
        return c if isinstance(c, str) else None
    if current is None:
        current = ""
    if tool == "Edit":
        old, new = ti.get("old_string"), ti.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if old and old not in current:
            return current
        return current.replace(old, new) if ti.get("replace_all") else current.replace(old, new, 1)
    if tool == "MultiEdit":
        edits = ti.get("edits")
        if not isinstance(edits, list):
            return None
        cur = current
        for e in edits:
            if not isinstance(e, dict) or not isinstance(e.get("old_string"), str) or not isinstance(e.get("new_string"), str):
                return None
            if e["old_string"] and e["old_string"] not in cur:
                continue
            cur = cur.replace(e["old_string"], e["new_string"]) if e.get("replace_all") else cur.replace(e["old_string"], e["new_string"], 1)
        return cur
    if tool == "NotebookEdit":
        return None
    return None


def _loop_config_check(ctx: Ctx, path: str, tool: str) -> Optional[Decision]:
    try:
        cur = paths.safe_read(paths.physical(path, ctx.cwd, ctx.home), 1 << 20)
        new = _new_text(ctx.tool_input, tool, cur)
        if new is None:
            return deny(G, "h6-config-key", "loop.config.md edits that cannot be simulated are refused (H6 keys must not change)")
        base_cfg = config_mod.load_text(cur if cur is not None else "```yaml\n```\n")
        new_cfg = config_mod.load_text(new)
        changes = config_mod.h6_changes(base_cfg, new_cfg)
    except Exception as exc:
        return deny(G, "h6-config-key", "loop.config.md cannot be parsed after this edit (%s): refused, because an H6 key could have changed"
                    % str(exc).split("\n")[0][:80])
    if changes:
        return deny(G, "h6-config-key", "this edit changes H6-protected loop.config keys (%s): caps, gates, delivery and attestations are changed by a "
                    "human under H6" % ", ".join(c[0] for c in changes[:6]))
    return None


def _package_json_check(ctx: Ctx, path: str, tool: str) -> Optional[Decision]:
    cur = paths.safe_read(paths.physical(path, ctx.cwd, ctx.home), 1 << 20)
    new = _new_text(ctx.tool_input, tool, cur)
    if new is None:
        return None
    if cur is None:           # a new package.json (a new workspace package): lifecycle scripts are what run on install
        try:
            doc = json.loads(new)
        except ValueError:
            return None
        sc = doc.get("scripts") if isinstance(doc, dict) else None
        hooks = [k for k in (sc or {}) if k in ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack")] if isinstance(sc, dict) else []
        if hooks or (isinstance(doc, dict) and any(k in doc for k in ("husky", "simple-git-hooks"))):
            return deny(G, "package-scripts", "a new package.json with lifecycle scripts (%s) runs code on install; engineers do not add them (report §5.9)" % ", ".join(hooks[:3]))
        return None
    try:
        a, b = json.loads(cur), json.loads(new)
    except ValueError:
        return None
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    for k in PKG_KEYS:
        if a.get(k) != b.get(k):
            return deny(G, "package-scripts", "package.json#%s changes what runs on install and test (postinstall, prepare, pretest); engineers do not edit "
                        "it (report §5.9, G5 integrity set)" % k)
    return None


def check_edit(ctx: Ctx) -> Optional[Decision]:
    """Self-modification protection for Edit, Write, MultiEdit and NotebookEdit."""
    for raw in _targets(ctx.tool_input):
        d = _judge_edit_path(ctx, raw, ctx.cwd, "%s to" % ctx.tool_name, ctx.tool_name)
        if d:
            return d
    return None


def check_main_edit(ctx: Ctx) -> Optional[Decision]:
    for raw in _targets(ctx.tool_input):
        d = _main_edit(ctx, raw)
        if d:
            return d
    return None


def _judge_edit_path(ctx: Ctx, raw: str, cwd: str, via: str, tool: str) -> Optional[Decision]:
    forms = paths.forms(raw, cwd, ctx.home)
    for f in forms:
        hit = paths.classify(f, ctx.home)
        if hit and hit[0] == "loop-config":
            return _loop_config_check(ctx, raw, tool)
    d = _judge(ctx, raw, False, cwd, via)
    if d:
        return d
    if _is_engineer_like(ctx) and os.path.basename(raw).lower() == "package.json":
        return _package_json_check(ctx, raw, tool)
    return None


def _main_edit(ctx: Ctx, raw: str) -> Optional[Decision]:
    target = paths.physical(raw, ctx.cwd, ctx.home)
    top = ctx.toplevel_of(target)
    if top is None:
        return None
    branch = ctx.branch_at(top)
    if not ctx.is_protected_branch(branch):
        return None
    rel = paths.rel_to_root(target, os.path.realpath(top)) or paths.rel_to_root(paths.lexical(raw, ctx.cwd, ctx.home), top)
    if rel is None:
        return None
    allow = list(MAIN_ALLOW)
    extra = ctx.cfg("main_edit_allow", [])
    if isinstance(extra, list):
        allow.extend(x for x in extra if isinstance(x, str))
    if paths.match_rel(rel, allow):
        return None
    return deny(GM, "shared-branch", "editing %s while on the shared branch %s. Create a feature branch first (ask the user or the Tech Lead); do not run "
                "`git checkout -b` yourself. Only docs/**, plan files and primers may be edited here, and nothing under .claude/ other than primers"
                % (rel, branch))


# ---------------------------------------------------------------------------------------------------------------------
# Bash

class Target(object):
    __slots__ = ("text", "dynamic", "tree", "kind")

    def __init__(self, text: str, dynamic: bool, tree: bool = False, kind: str = "write") -> None:
        self.text = text
        self.dynamic = dynamic
        self.tree = tree
        self.kind = kind


def _pos(args: Sequence[str], dyn: Sequence[bool], with_arg: Sequence[str] = ()) -> List[Tuple[str, bool]]:
    out, skip, dd = [], False, False   # type: List[Tuple[str, bool]], bool, bool
    for a, d in zip(args, dyn):
        if skip:
            skip = False
            continue
        if dd:
            out.append((a, d))
            continue
        if a == "--":
            dd = True
            continue
        if a.startswith("-") and len(a) > 1:
            if a in with_arg:
                skip = True
            continue
        out.append((a, d))
    return out


def _flag_has(args: Sequence[str], short: str = "", long: Sequence[str] = ()) -> bool:
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if any(a == l or a.startswith(l + "=") for l in long):
                return True
        elif a.startswith("-") and short and any(ch in short for ch in a[1:]):
            return True
    return False


def _patch_targets(text: str) -> List[str]:
    out = []   # type: List[str]
    for m in re.finditer(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)", text, re.M):
        if m.group(1) != "/dev/null":
            out.append(m.group(1))
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)", text, re.M):
        out.extend([m.group(1), m.group(2)])
    return out


def write_targets(ctx: Ctx, inv: Inv) -> List[Target]:
    out = []   # type: List[Target]
    for r in inv.redirs:
        if r.writes and r.target is not None:
            out.append(Target(r.target.text, r.target.dynamic, False, "redirect"))
    name, args, dyn = inv.name, inv.args, inv.dyn
    P = lambda wa=(): _pos(args, dyn, wa)   # noqa: E731
    if name == "tee":
        out.extend(Target(a, d) for a, d in P())
    elif name in ("cp", "install", "rsync", "ditto", "scp", "mv", "gcp", "cpio"):
        if name == "rsync":
            arg_opts = ("-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--exclude-from", "--include-from", "--files-from", "-T", "--temp-dir")
        else:      # `cp -f` and `mv -f` are plain flags; treating them as taking a value would hide a source (mv -f .claude/settings.json /tmp)
            arg_opts = ("-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group")
        pos = P(arg_opts)
        tdir = None
        for k, a in enumerate(args):
            if a in ("-t", "--target-directory") and k + 1 < len(args):
                tdir = (args[k + 1], dyn[k + 1])
            elif a.startswith("--target-directory="):
                tdir = (a.split("=", 1)[1], dyn[k])
        if tdir:
            out.append(Target(tdir[0], tdir[1], "children"))
            srcs = pos
        else:
            srcs = pos[:-1]
            if pos:
                out.append(Target(pos[-1][0], pos[-1][1], "children"))
        if name == "mv":
            out.extend(Target(a, d, True, "move-source") for a, d in srcs)
        if name == "rsync" and _flag_has(args, "", ("--delete", "--delete-after", "--delete-before", "--remove-source-files")) and pos:
            out.append(Target(pos[-1][0], pos[-1][1], True))
    elif name == "ln":
        for a, d in P(("-t", "--target-directory", "-S", "--suffix")):
            out.append(Target(a, d, False, "link"))
    elif name in ("rm", "unlink", "rmdir", "shred", "srm", "trash", "gio"):
        pos = P()
        if name == "gio":
            pos = pos[1:] if pos and pos[0][0] in ("remove", "trash", "move", "copy", "rename") else []
        out.extend(Target(a, d, True) for a, d in pos)
    elif name in ("truncate", "touch", "mkdir", "mkfifo", "mknod", "setfacl", "chattr", "xattr", "dd", "fallocate", "chflags", "install-d"):
        if name == "dd":
            for a, d in zip(args, dyn):
                if a.startswith("of="):
                    out.append(Target(a[3:], d))
        else:
            out.extend(Target(a, d) for a, d in P(("-s", "-r", "--size", "--reference", "-m", "-d", "-t", "-c")))
    elif name in ("chmod", "chown", "chgrp"):
        # a symbolic mode may start with '-' (chmod -x file), so every word that is not a known option is a candidate target
        known = {"-R", "-v", "-f", "-c", "-h", "-H", "-L", "-P", "--recursive", "--verbose", "--silent", "--quiet", "--changes", "--no-dereference", "--preserve-root"}
        out.extend(Target(a, d, True) for a, d in zip(args, dyn) if a not in known and not a.startswith("--reference"))
    elif name in ("sed", "gsed"):
        for a in args:
            m = re.search(r"(?:^|[;{}/\s])w\s+([^\s;}]+)\s*$|/w\s*([^\s;}]+)\s*$", a)
            if m and not a.startswith("-"):
                out.append(Target(m.group(1) or m.group(2), False))
        inplace = _flag_has(args, "i", ("--in-place",)) or any(a.startswith("-i") for a in args)
        if inplace:
            has_script_flag = any(a in ("-e", "-f", "--expression", "--file") or a.startswith(("--expression=", "--file=")) for a in args)
            pos = P(("-e", "-f", "--expression", "--file", "-l", "--line-length"))
            out.extend(Target(a, d) for a, d in (pos if has_script_flag else pos[1:]))
    elif name in ("perl", "ruby"):
        if any(re.match(r"^-[A-Za-z]*i", a) for a in args if a.startswith("-") and not a.startswith("--")):
            has_e = any(re.match(r"^-[A-Za-z]*e$", a) for a in args)
            pos = _pos(args, dyn, ("-e", "-E", "-I", "-M", "-m", "-r"))
            out.extend(Target(a, d) for a, d in (pos if has_e else pos[1:]))
    elif name in ("awk", "gawk"):
        if "inplace" in " ".join(args):
            out.extend(Target(a, d) for a, d in P(("-i", "-f", "-v", "-F"))[1:])
    elif name in ("sponge", "ed", "ex", "vi", "vim", "nvim", "view", "nano", "emacs", "code", "subl", "mate", "pico", "joe", "micro"):
        out.extend(Target(a, d) for a, d in P(("-c", "-S", "-s", "-u", "--cmd", "-w")))
    elif name in ("yq", "gojq", "jq"):
        if name == "yq" and (_flag_has(args, "i", ("--inplace",))):
            out.extend(Target(a, d) for a, d in P()[1:])
    elif name == "curl":
        for k, a in enumerate(args):
            if a in ("-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar", "--trace", "--trace-ascii", "--stderr", "--output-dir",
                     "--libcurl") and k + 1 < len(args):
                out.append(Target(args[k + 1], dyn[k + 1]))
            elif a.startswith(("--output=", "--output-dir=", "--dump-header=")):
                out.append(Target(a.split("=", 1)[1], dyn[k]))
            elif a.startswith("-o") and len(a) > 2 and not a.startswith("--"):
                out.append(Target(a[2:], dyn[k]))
        if any(a in ("-O", "--remote-name", "--remote-name-all", "-J") or (a.startswith("-") and not a.startswith("--") and "O" in a[1:] and a != "-o") for a in args):
            out.append(Target(".", False, "children"))       # the file lands in the working directory
    elif name == "wget":
        for k, a in enumerate(args):
            if a in ("-O", "-o", "-P", "--output-document", "--output-file", "--directory-prefix", "-a") and k + 1 < len(args):
                out.append(Target(args[k + 1], dyn[k + 1]))
            elif a.startswith(("--output-document=", "--directory-prefix=", "--output-file=")):
                out.append(Target(a.split("=", 1)[1], dyn[k]))
        if not any(a in ("-O", "-P", "--output-document", "--directory-prefix") or a.startswith(("--output-document=", "--directory-prefix=", "-O")) for a in args):
            out.append(Target(".", False, "children"))
    elif name in ("tar", "bsdtar", "gtar", "unzip", "7z", "7za", "unar", "ar"):
        for k, a in enumerate(args):
            if a in ("-C", "--directory", "-d", "-o") and k + 1 < len(args):
                out.append(Target(args[k + 1], dyn[k + 1], "children"))
            elif a.startswith(("--directory=", "-C")) and len(a) > 2:
                out.append(Target(a.split("=", 1)[-1] if "=" in a else a[2:], dyn[k], "children"))
            elif a.startswith("-o") and name in ("7z", "7za") and len(a) > 2:
                out.append(Target(a[2:], dyn[k], "children"))
        extracting = (name in ("unzip", "7z", "7za", "unar") or (name in ("tar", "bsdtar", "gtar") and any(
            a == "--extract" or (a.startswith("-") and not a.startswith("--") and "x" in a[1:]) or (not a.startswith("-") and "x" in a and args.index(a) == 0)
            for a in args[:2])))
        if extracting and not any(a in ("-C", "--directory", "-d") or a.startswith(("--directory=", "-o")) for a in args):
            out.append(Target(".", False, "children"))
        if name in ("tar", "bsdtar", "gtar"):
            create = any(re.match(r"^-?[A-Za-z]*c[A-Za-z]*f?$", a) for a in args[:1] if a and not a.startswith("--")) or "--create" in args
            if create:
                for k, a in enumerate(args):
                    if a in ("-f", "--file") and k + 1 < len(args):
                        out.append(Target(args[k + 1], dyn[k + 1]))
    elif name in ("patch", "gpatch"):
        pos = P(("-p", "-i", "-d", "-r", "-o", "-F"))
        text = inv.stdin_text
        infile = None
        for k, a in enumerate(args):
            if a in ("-i", "--input") and k + 1 < len(args):
                infile = args[k + 1]
            elif a.startswith("--input="):
                infile = a.split("=", 1)[1]
        for r in inv.redirs:
            if r.op == "<" and r.target is not None and not r.target.dynamic:
                infile = r.target.text
        if infile:
            text = paths.safe_read(paths.physical(infile, inv.cwd, ctx.home), 512 * 1024) or text
        if text:
            out.extend(Target(t, False, False, "patch") for t in _patch_targets(text))
        elif pos and not infile:
            out.append(Target(pos[0][0], pos[0][1]))
    elif name == "find":
        exec_writes = any(a in ("-exec", "-execdir", "-ok", "-okdir") and k + 1 < len(args) and args[k + 1].rsplit("/", 1)[-1].lower() in WRITER_CMDS
                          for k, a in enumerate(args))
        if exec_writes or any(a in ("-delete", "-fprint", "-fprint0", "-fls", "-fprintf") for a in args):
            for k, a in enumerate(args):
                if a.startswith("-") or a in ("(", ")", "!"):
                    break
                out.append(Target(a, dyn[k], True))
            for k, a in enumerate(args):
                if a in ("-fprint", "-fprint0", "-fls", "-fprintf") and k + 1 < len(args):
                    out.append(Target(args[k + 1], dyn[k + 1]))
    elif name == "git":
        out.extend(_git_targets(inv))
    return out


def _git_targets(inv: Inv) -> List[Target]:
    args, dyn = inv.args, inv.dyn
    i, n = 0, len(args)
    cwd = inv.cwd
    while i < n and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= n:
        return []
    sub = args[i].lower()
    rest, rdyn = args[i + 1:], dyn[i + 1:]
    out = []   # type: List[Target]
    if sub in ("restore", "rm", "mv"):
        pos = _pos(rest, rdyn, ("--source", "-s", "--pathspec-from-file"))
        out.extend(Target(a, d, True) for a, d in pos)
    elif sub == "checkout" and "--" in rest:
        k = rest.index("--")
        out.extend(Target(a, rdyn[k + 1 + j], True) for j, a in enumerate(rest[k + 1:]))
    elif sub == "clean":
        out.extend(Target(a, d, True) for a, d in _pos(rest, rdyn, ("-e", "--exclude")))
    elif sub in ("apply", "am"):
        text = inv.stdin_text
        files = [a for a, d in _pos(rest, rdyn, ("-p", "-C", "--directory", "--include", "--exclude"))]
        for f in files:
            t = paths.safe_read(paths.physical(f, cwd), 512 * 1024)
            if t:
                out.extend(Target(x, False, False, "patch") for x in _patch_targets(t))
        if text:
            out.extend(Target(x, False, False, "patch") for x in _patch_targets(text))
    elif sub in ("diff", "log", "show", "format-patch", "archive"):
        for k, a in enumerate(rest):
            if a.startswith("--output="):
                out.append(Target(a.split("=", 1)[1], rdyn[k]))
            elif a in ("--output", "-o") and k + 1 < len(rest) and sub in ("archive", "diff", "log", "show", "format-patch"):
                out.append(Target(rest[k + 1], rdyn[k + 1]))
            elif sub == "format-patch" and a in ("--output-directory",) and k + 1 < len(rest):
                out.append(Target(rest[k + 1], rdyn[k + 1]))
    elif sub in ("clone", "init", "bundle", "checkout-index", "worktree", "submodule", "sparse-checkout"):
        pos = _pos(rest, rdyn, ("-b", "--branch", "-c", "--config", "--template", "--prefix", "--reference", "-o", "--origin", "--depth", "--separate-git-dir"))
        if sub == "worktree":
            pos = pos[1:] if pos and pos[0][0] in ("add", "move", "remove", "repair") else []
        if sub == "bundle":
            pos = pos[1:2] if pos and pos[0][0] == "create" else []
        if sub == "submodule":
            pos = pos[2:3] if pos and pos[0][0] == "add" else []
        if sub in ("clone",):
            pos = pos[1:2]
        out.extend(Target(a, d, "children") for a, d in pos[:1])
        for k, a in enumerate(rest):
            if a.startswith("--prefix=") and sub == "checkout-index":
                out.append(Target(a.split("=", 1)[1], rdyn[k], "children"))
    return out


def _link_aliases(ctx: Ctx, invs: Sequence[Inv]) -> Dict[str, str]:
    """`ln -s TARGET LINK` earlier in the same command: a later write to LINK is a write to TARGET."""
    out = {}   # type: Dict[str, str]
    for inv in invs:
        if inv.name != "ln":
            continue
        pos = _pos(inv.args, inv.dyn, ("-t", "--target-directory", "-S", "--suffix"))
        if len(pos) >= 2 and _flag_has(inv.args, "s", ("--symbolic",)):
            tgt, link = pos[0][0], pos[-1][0]
            lp = paths.lexical(link, inv.cwd, ctx.home)
            if os.path.isdir(lp):
                lp = os.path.join(lp, os.path.basename(tgt))
            out[lp] = paths.lexical(tgt, os.path.dirname(lp), ctx.home) if not os.path.isabs(tgt) else tgt
    return out


# -- inline interpreter code ---------------------------------------------------------------------------------------------

PROT_RE = re.compile(r"(\.claude/(settings|hooks|scripts|agents|skills|commands|rules|workflows|loop\.config)|settings(\.local)?\.json|"
                     r"\.mcp\.json|\.git/(hooks|config|info|loopctl)|\.github/(workflows|codeowners)|codeowners|\.claude-plugin|\.husky|"
                     r"\.bashrc|\.zshrc|\.zprofile|\.profile|\.gitconfig|\.ssh/|\.aws/|\.netrc)", re.I)
WRITE_RE = re.compile(r"(open\s*\([^\n]{0,400}?['\"][bt]?[wax+][bt+]?['\"]|write_text|write_bytes|\.write\s*\(|writefile|appendfile|createwritestream|copyfile|"
                      r"rename|unlink|rmsync|rmtree|rm -|shutil\.|os\.system|os\.popen|subprocess|child_process|execsync|spawn|\bexec\s*\(|"
                      r"sed -i|\btee\b|>>?\s*['\"]?[\w./~$-]|fs\.|file_put_contents|fopen|system\s*\()", re.I)


def normalise_code(code: str, join: bool = True) -> str:
    """Undo the cheap ways to hide a path inside a string: escapes, concatenation and (with `join`) path joins."""
    c = code
    try:
        c = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), c)
        c = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), c)
        c = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), c)
    except ValueError:
        pass
    for _ in range(4):
        c2 = re.sub(r"""['"]\s*\+\s*['"]""", "", c)
        if join:
            c2 = re.sub(r"""['"]\s*\)?\s*[,/]\s*(?:r|b|f)?['"]""", "/", c2)
        c2 = re.sub(r"""['"]\s*['"]""", "", c2)
        if c2 == c:
            break
        c = c2
    return c


def scan_code(code: str) -> Optional[str]:
    """Reason string if inline code looks like it writes a protected path, else None. The path is looked for in the joined form
    (os.path.join('.claude','settings.json')) and the write call in both, since joining also fuses `'path','w'`."""
    n = normalise_code(code)
    m = PROT_RE.search(n)
    if m and (WRITE_RE.search(n) or WRITE_RE.search(normalise_code(code, join=False))):
        return m.group(0)
    return None


def check_bash(ctx: Ctx) -> Optional[Decision]:
    ana = ctx.analysis
    aliases = _link_aliases(ctx, ana.invs)
    for inv in ana.invs:
        if inv.name == "ln":
            pos = _pos(inv.args, inv.dyn, ("-t", "--target-directory", "-S", "--suffix"))
            for a, d in pos[:-1] if len(pos) > 1 else pos:
                hit = paths.classify_forms(a, inv.cwd, ctx.home)
                if hit:
                    return deny(G, "link-protected", "ln links %s (%s): a link to a guardrail file is a way around the write guard" % (hit[1], _desc(hit[0])))
        for t in write_targets(ctx, inv):
            via = "%s to" % ("a redirection" if t.kind == "redirect" else inv.name)
            if t.kind == "patch":
                via = "a patch (%s) that edits" % inv.name
            tree = t.tree and inv.name in ("rm", "rmdir", "mv", "chmod", "chown", "shred", "git", "rsync", "chgrp", "trash")
            if t.tree == "children" or (t.tree and inv.name == "find"):
                tree = "children"      # `find . -name x -delete` is not `rm -rf .`: only a start path that IS a guardrail directory counts
            d = _judge(ctx, t.text, t.dynamic, inv.cwd, via, tree=tree, aliases=aliases, unknown_cwd=inv.cwd_unknown)
            if d:
                return d
    for ic in ana.inline:
        why = scan_code(ic.code)
        if why:
            return deny(G, "script-writes-protected", "inline %s code names %s and writes files; agents do not modify guardrail paths (H6)" % (ic.lang, why))
        if (WRITE_RE.search(normalise_code(ic.code)) or WRITE_RE.search(normalise_code(ic.code, join=False))) and not ic.from_file and (paths.classify(os.path.join(ic.inv.cwd, "x"), ctx.home)
                                                                          or paths.classify(os.path.join(ic.inv.cwd, "settings.json"), ctx.home)
                                                                          or ic.inv.cwd_unknown):
            return deny(G, "script-writes-protected", "inline %s code writes files from %s, which is (or may be) a guardrail directory (H6)" %
                        (ic.lang, "a directory computed at run time" if ic.inv.cwd_unknown else os.path.basename(ic.inv.cwd)))
    for sf in ana.scripts:
        content = ana.vfs.get(sf.path)
        authored = content is not None
        if content is None and paths.agent_authored(sf.path):
            content = paths.safe_read(sf.path, 256 * 1024)
            authored = content is not None
        if authored:       # a script this command wrote, or one that is untracked or modified: agent-authored, so it is scanned like inline code
            why = scan_code(content)
            if why:
                return deny(G, "script-writes-protected", "%s (written or changed by the agent) names %s and writes files (H6)" % (os.path.basename(sf.path), why))
    return None


PATHISH_KEY = re.compile(r"(path|file|dir|folder|destination|target|source|dest|output|filename)", re.I)


def check_mcp_paths(ctx: Ctx) -> Optional[Decision]:
    """An MCP filesystem server is another way to write a file. Any path-like string argument of an MCP tool is judged like an Edit target."""
    ti = ctx.tool_input
    for k, v in list(ti.items())[:40]:
        vals = [v] if isinstance(v, str) else (v[:20] if isinstance(v, list) else [])
        if not PATHISH_KEY.search(str(k)):
            continue
        for item in vals:
            if isinstance(item, str) and item and len(item) < 1000 and "\n" not in item:
                d = _judge(ctx, item, False, ctx.cwd, "%s (%s) to" % (ctx.tool_name, k))
                if d:
                    return d
    return None
