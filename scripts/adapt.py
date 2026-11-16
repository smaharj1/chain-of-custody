#!/usr/bin/env python3
"""adapt: give a Codex or Cursor project a guidance-only copy of the kit.

Claude Code users install the kit as a plugin. Codex and Cursor cannot load a plugin, so this script copies the parts that
work as plain instructions into the project and writes the file each tool reads at startup:

    python3 scripts/adapt.py --target DIR [--tool codex|cursor|both] [--force]

Run it from a clone of the kit checked out at a release tag. What it writes into DIR:

  .chain-of-custody/            generated, REPLACED WHOLESALE on every run (except project/): the modes (skills/, the
                                Claude-Code-only setup modes left out), agents/, context/, templates/ (the Claude Code
                                settings and hook snippets left out), spec/, GUIDE.md, README.md, project-seed/ (the
                                starter project files of this release, to compare against) and VERSION (plugin version,
                                source commit, and the hashes of the seeds and of the generated AGENTS.md / .mdc)
  .chain-of-custody/project/    the project's own files (PROJECT.md, loop.config.md, the primers context/<domain>.md),
                                copied from the seed only when missing; never overwritten
  AGENTS.md                     --tool codex or both
  .cursor/rules/chain-of-custody.mdc
                                --tool cursor or both

Markdown is rewritten for the new home; every other file is copied byte for byte. `${CLAUDE_PLUGIN_ROOT}/<path>` (and
`$KIT/<path>`) becomes `.chain-of-custody/<path>` when the file was copied. A file that was not copied (scripts/, hooks/,
docs/: they run only under the Claude Code plugin) becomes a link to it at this release on GitHub; a script, or any such
path inside a command, becomes `scripts/<x> (Claude Code plugin only; <link>)` so that no command in the copy runs a URL.
The project files `.claude/CLAUDE.md`, `.claude/loop.config.md` and `.claude/context/<x>` become their
`.chain-of-custody/project/` copies (`CLAUDE.md` is `PROJECT.md` here; a kit context file named under `.claude/context/`
goes to `.chain-of-custody/context/`). Relative Markdown links are resolved against the source file and re-pointed.

The seeded `project/` differs from a Claude Code install on purpose: `PROJECT.md` comes from `adapters/seed/` (no slash
commands, no `$KIT`), and `loop.config.md` starts at `git_autonomy: none` and `delivery: local`, because nothing in Codex
or Cursor enforces the push rule.

AGENTS.md and the .mdc carry a marker line. A file without the marker is the user's own: it is left alone and the line to add
by hand is printed; --force overwrites it (keeping a `.pre-adapt` backup). A marked file whose text differs from what the
last run wrote is kept as `<file>.prev` before it is rewritten. adapt.py never writes through a symlink, even with --force.
Nothing here installs guards, hooks or the loop driver. Standard library only, Python 3.9.

Exit codes: 0 done, 1 refused (target is the kit, a .chain-of-custody/ this script did not write, or a symlink where it would
write), 2 usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitlib import kitpaths  # noqa: E402

OUT_DIR = ".chain-of-custody"
PROJECT_DIR = "project"
SEED_COPY_DIR = "project-seed"
PROJECT_FILE = "PROJECT.md"
MARKER = "chain-of-custody:adapt"
COPY_AREAS = ("skills", "agents", "context", "templates", "spec")
# modes that set up, check or upgrade a Claude Code install: nothing to follow in Codex or Cursor
CLAUDE_ONLY_SKILLS = ("bootstrap", "init", "migrate")
SEED = os.path.join("seed", ".claude")
ADAPTER_SEED = os.path.join("adapters", "seed")
AGENTS_MD = "AGENTS.md"
MDC = ".cursor/rules/chain-of-custody.mdc"
TOOL_FILES = ((AGENTS_MD, "codex"), (MDC, "cursor"))
# what the adapter copy of loop.config.md starts at: nothing in Codex or Cursor enforces a push tier
POSTURE = (("git_autonomy", "none"), ("delivery", "local"))
PLUGIN_ONLY = "Claude Code plugin only"
PLUGIN_ROOT_PROSE = "Claude Code's plugin-root variable"

# (mode, what it is for), in the order the guide lists them
ROUTES = (
    ("pm", "product requirements, by interview, into `requirements.md`"),
    ("architect", "architecture and API design, into `technical-design.md` and `api-contract.md`"),
    ("tech-lead", "engineer briefs, implementation and review, and the quick lane for small fixes"),
    ("planner", "whole-project planning, into an approved build plan"),
    ("orchestrate", "the autonomous item loop; reference only, it needs the Claude Code `loopctl` driver"),
    ("retro", "a retrospective over a milestone; mostly reference, it reads run data the Claude Code loop writes"),
)

_PATH = r"[A-Za-z0-9_.<>*/-]"
# an optional interpreter and opening quote in front of a kit path, so a command that would run it can be recognised
_CMD = r"(?P<interp>(?<![\w/.-])(?:python3|sh|bash) +)?(?P<oq>\")?"
_CQ = r"(?(oq)(?P<cq>\")?)"
BARE_PLUGIN_REF = re.compile(r"`" + re.escape(kitpaths.PLUGIN_ROOT_VAR) + r"`")
PLUGIN_REF = re.compile(_CMD + re.escape(kitpaths.PLUGIN_ROOT_VAR) + r"(?P<path>/" + _PATH + r"*)?" + _CQ)
KIT_REF = re.compile(_CMD + r"\$KIT(?P<path>/" + _PATH + r"*)" + _CQ)
PROJECT_REF = re.compile(_CMD + r"(?<![\w/.~-])\.claude/(?P<what>CLAUDE\.md|loop\.config\.md|context/(?P<ctx>[A-Za-z0-9_.<>*-]*)"
                         r"|(?:scripts|hooks|spec|templates)(?:/" + _PATH + r"*)?)" + _CQ)
LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
COMMAND_START = re.compile(r"\s*(?:\$ )?(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:python3|sh|bash)\s")


class AdaptError(Exception):
    pass


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def plugin_meta(kit_root: str) -> Dict[str, str]:
    with open(os.path.join(kit_root, ".claude-plugin", "plugin.json"), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    repo = str(data.get("repository") or data.get("homepage") or "https://github.com/smaharj1/chain-of-custody").rstrip("/")
    return {"version": str(data["version"]), "repo": repo}


def release_url(meta: Dict[str, str]) -> str:
    return "%s/blob/v%s/" % (meta["repo"], meta["version"])


def git_out(kit_root: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", kit_root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def claude_only_template(rel: str) -> bool:
    """Claude Code settings and hook snippets: they configure Claude Code itself, so they mean nothing in Codex or Cursor."""
    parts = rel.split("/")
    if parts[0] != "templates":
        return False
    name = parts[-1]
    return (len(parts) > 2 and parts[1] == "settings") or name.endswith(".settings.json") or name.startswith("managed-settings")


def copied_files(kit_root: str) -> List[str]:
    """Kit-relative paths of every file that goes into .chain-of-custody/."""
    out = []
    for rel in kitpaths.kit_files(kit_root, COPY_AREAS):
        parts = rel.split("/")
        if parts[0] == "skills" and len(parts) > 1 and parts[1] in CLAUDE_ONLY_SKILLS:
            continue
        if claude_only_template(rel):
            continue
        out.append(rel)
    return out


def _walk(base: str) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in filenames:
            if fn != ".DS_Store":
                out.append(os.path.relpath(os.path.join(dirpath, fn), base).replace(os.sep, "/"))
    return out


def seed_files(kit_root: str) -> List[str]:
    """Project-relative paths of the seeded files (PROJECT.md, loop.config.md, context/<domain>.md)."""
    adapter = set(_walk(os.path.join(kit_root, ADAPTER_SEED)))
    kit = set(rel for rel in _walk(os.path.join(kit_root, SEED)) if rel != "CLAUDE.md")
    return sorted(adapter | kit)


def seed_source(kit_root: str, rel: str) -> str:
    """Kit-relative source of a seeded project file: the adapter's own seed wins over the Claude Code seed."""
    if os.path.isfile(os.path.join(kit_root, ADAPTER_SEED, rel)):
        return "adapters/seed/" + rel
    return "seed/.claude/" + rel


class Rewriter(object):
    """Rewrites kit Markdown for a project where the kit lives at .chain-of-custody/ and the Claude Code plugin is absent."""

    def __init__(self, files: Sequence[str], seeds: Sequence[str], kit_context: Sequence[str], version: str, repo: str):
        self.files = set(files)                                       # type: Set[str]
        self.dirs = set()                                             # type: Set[str]
        for f in files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                self.dirs.add("/".join(parts[:i]))
        self.primers = set(s[len("context/"):] for s in seeds if s.startswith("context/"))
        self.kit_context = set(kit_context)
        self.base_url = "%s/blob/v%s/" % (repo, version)

    def local(self, path: str) -> bool:
        p = path.rstrip("/")
        if p in ("", "...") or p in self.files or p in self.dirs:
            return True
        if "<" in p or "*" in p:                                      # a placeholder: decide by the directory above it
            head = []
            for seg in p.split("/"):
                if "<" in seg or "*" in seg:
                    break
                head.append(seg)
            return "/".join(head) in self.dirs or not head
        return False

    def location(self, path: str) -> Optional[str]:
        """Where a kit path lands inside .chain-of-custody/, or None when it was not copied."""
        if path.startswith("seed/.claude/"):
            rest = path[len("seed/.claude/"):]
            return "%s/%s" % (PROJECT_DIR, PROJECT_FILE if rest == "CLAUDE.md" else rest)
        if path.startswith("adapters/seed/"):
            return "%s/%s" % (PROJECT_DIR, path[len("adapters/seed/"):])
        if path == "KIT_VERSION":
            return "VERSION"
        if self.local(path):
            return path
        return None

    def plugin_only(self, path: str) -> str:
        return "%s (%s; %s%s)" % (path, PLUGIN_ONLY, self.base_url, path)

    def kit_path(self, path: str) -> str:
        """A kit path as prose names it in the copy: the copied file, or what the plugin has and this copy does not."""
        loc = self.location(path)
        if loc is not None:
            return OUT_DIR + "/" + loc
        if path.split("/")[0] in ("scripts", "hooks"):
            return self.plugin_only(path)
        return self.base_url + path

    @staticmethod
    def _in_command(m: "re.Match") -> bool:
        text, start = m.string, m.start()
        before = text[text.rfind("\n", 0, start) + 1:start]
        if before.count("`") % 2 == 1:                                # inside a code span: does the span start a command?
            return bool(COMMAND_START.match(before[before.rindex("`") + 1:]))
        return bool(COMMAND_START.match(before))

    def _emit(self, m: "re.Match", path: str) -> str:
        tail = ""
        while path.endswith(".") and path != "...":                    # a sentence's full stop is not part of the path
            path, tail = path[:-1], "." + tail
        interp, oq, cq = m.group("interp") or "", m.group("oq") or "", m.group("cq") or ""
        loc = self.location(path)
        if loc is not None:
            return interp + oq + OUT_DIR + "/" + loc + cq + tail
        if interp or oq or self._in_command(m) or path.split("/")[0] in ("scripts", "hooks"):
            return self.plugin_only(path) + tail                       # never a command that runs a URL
        return self.base_url + path + tail

    def _plugin(self, m: "re.Match") -> str:
        raw = m.group("path")
        if raw is None:
            return (m.group("interp") or "") + (m.group("oq") or "") + PLUGIN_ROOT_PROSE + (m.group("cq") or "")
        return self._emit(m, raw[1:])

    def _kit(self, m: "re.Match") -> str:
        return self._emit(m, m.group("path")[1:])

    def _project(self, m: "re.Match") -> str:
        what = m.group("what")
        if what in ("CLAUDE.md", "loop.config.md"):
            return self._emit(m, "seed/.claude/" + what)
        if what.startswith("context/"):
            name = m.group("ctx")
            core = name.rstrip(".")
            if core and core not in self.primers and core in self.kit_context:
                return self._emit(m, "context/" + name)
            return self._emit(m, "seed/.claude/context/" + name)
        return self._emit(m, what)

    def _links(self, text: str, src: str, dst: str) -> str:
        def sub(m: "re.Match") -> str:
            target = m.group("target")
            path, hash_, frag = target.partition("#")
            if not path or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", path) or path.startswith(("/", "<")):
                return m.group(0)
            if path.startswith(kitpaths.PLUGIN_ROOT_VAR + "/"):
                kit = path[len(kitpaths.PLUGIN_ROOT_VAR) + 1:]
            elif "$" in path or "<" in path:
                return m.group(0)
            else:
                kit = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
                if kit == ".." or kit.startswith("../"):
                    return m.group(0)
            loc = self.location(kit)
            new = self.base_url + kit if loc is None else posixpath.relpath(loc, posixpath.dirname(dst) or ".")
            return "](%s%s%s)" % (new, hash_, frag)
        return LINK.sub(sub, text)

    def __call__(self, text: str, src: Optional[str] = None, dst: Optional[str] = None) -> str:
        """Rewrite `text`. With `src` (its kit-relative path) and `dst` (where it lands in .chain-of-custody/, default
        `src`), relative Markdown links are re-pointed too."""
        if src:
            text = self._links(text, src, dst or src)
        text = BARE_PLUGIN_REF.sub(PLUGIN_ROOT_PROSE, text)
        text = PLUGIN_REF.sub(self._plugin, text)
        text = KIT_REF.sub(self._kit, text)
        return PROJECT_REF.sub(self._project, text)


def loop_config_note() -> str:
    return "\n".join([
        "> **Codex / Cursor copy**, seeded by the kit's `scripts/adapt.py`. Most keys below are read by the Claude Code loop",
        "> driver, the doctor and the guards, none of which run here: read them as documentation. What applies here is the",
        "> `verify_*` commands (run them before calling work done) and `git_autonomy` with `delivery`. This copy starts those",
        "> two at `none` and `local`, because nothing in Codex or Cursor enforces the push rule in `%s/GUIDE.md`;" % OUT_DIR,
        "> raising them is a person's decision, committed on the base branch. A script shown as",
        "> `scripts/<x> (%s; <link>)` does not run here." % PLUGIN_ONLY,
    ])


def adapter_loop_config(text: str) -> str:
    """The seed loop.config.md as the adapter seeds it: the push tier off, and a note on what applies here."""
    for key, value in POSTURE:
        pat = re.compile(r"^(%s:[ \t]*)(\S+)([ \t]*)(#.*)?$" % re.escape(key), re.M)

        def sub(m: "re.Match", value: str = value) -> str:
            if not m.group(4):
                return m.group(1) + value
            pad = max(1, len(m.group(2)) + len(m.group(3)) - len(value))
            return m.group(1) + value + " " * pad + m.group(4)
        text, n = pat.subn(sub, text, count=1)
        if n != 1:
            raise AdaptError("the seed loop.config.md has no `%s:` line to set to %s; adapt.py needs updating" % (key, value))
    lines = text.split("\n")
    at = 1 if lines and lines[0].startswith("# ") else 0
    return "\n".join(lines[:at] + ([""] if at else []) + [loop_config_note()] + lines[at:])


def guide_body(meta: Dict[str, str], skills: Set[str]) -> str:
    url = release_url(meta)
    proj = "%s/%s" % (OUT_DIR, PROJECT_DIR)
    lines = [
        "# Chain of Custody (guidance-only copy, v%s)" % meta["version"],
        "",
        "> **Advisory, not enforced.** This is the Chain of Custody workflow as plain instructions. In Claude Code the kit runs as a",
        "> plugin with guards, hooks, subagents in a clean context and a loop driver. None of that is installed here. Read the parity",
        "> table below before relying on it.",
        "",
        "The kit's files live in `%s/` (generated by the kit's `scripts/adapt.py`; do not edit them, re-run it)." % OUT_DIR,
        "The project's own files live in `%s/`: `%s` (stack, layout, commands, posture), `loop.config.md`," % (proj, PROJECT_FILE),
        "and the domain primers `context/<domain>.md`. Fill those in; they are never overwritten. Where the kit's files say",
        "`CLAUDE.md`, they mean `%s/%s`." % (proj, PROJECT_FILE),
        "",
        "## Session start",
        "",
        "At the start of every session, read `%s/%s`. When the prompt names no mode, act as the PM" % (proj, PROJECT_FILE),
        "(`%s/skills/pm/SKILL.md`): gather requirements through questions, and do not design or write code until the person" % OUT_DIR,
        "names another mode.",
        "",
        "## Workflow routing",
        "",
        "Name the mode in your prompt and read its file first:",
        "",
    ]
    for mode, purpose in ROUTES:
        if mode in skills:
            lines.append("- **%s**: %s. Read `%s/skills/%s/SKILL.md`." % (mode, purpose, OUT_DIR, mode))
    extra = sorted(skills - set(r[0] for r in ROUTES))
    for mode in extra:
        lines.append("- **%s**: read `%s/skills/%s/SKILL.md`." % (mode, OUT_DIR, mode))
    lines += [
        "",
        "## Roles",
        "",
        "The role definitions are in `%s/agents/`: the engineers (`backend-engineer`, `database-engineer`, `app-engineer`," % OUT_DIR,
        "`admin-app-engineer`, `infra-engineer`), `acceptance-author` (writes black-box tests, never the implementation), the",
        "critics `design-critic`, `code-reviewer` and `security-reviewer`, and `intake-triage`. In Claude Code the critics are",
        "read-only and `intake-triage` reads one piece of untrusted text with every tool denied. Here nothing limits a role's",
        "tools: \"read-only\" is a request (do not edit files while you play a critic), and `intake-triage` must not run on",
        "untrusted text in a session that can write files, run commands or reach the network.",
        "When you take on a role, read its file and follow it. Before implementing, read",
        "`%s/context/engineer-protocol.md` and the domain primer in `%s/context/`." % (OUT_DIR, proj),
        "",
        "The written artifacts in `docs/features/<slug>/` are the hand-off contract between roles:",
        "",
        "`requirements.md` -> `technical-design.md` + `api-contract.md` -> `briefs/<domain>.md` -> `reports/<domain>.md`",
        "",
        "Templates for each are in `%s/templates/`. The loop and its gates are specified in `%s/spec/`." % (OUT_DIR, OUT_DIR),
        "",
        "## Verify and deliver here",
        "",
        "The kit's own steps for these run scripts that only the Claude Code plugin has. Here:",
        "",
        "- **Verify**: run the project's own verify commands, the `verify_build`, `verify_lint`, `verify_typecheck` and",
        "  `verify_tests` values in `%s/loop.config.md` (the primers in `%s/context/` say how to run the app" % (proj, proj),
        "  and its database). Report each command and its exit code. A test run that collects zero tests is a failure.",
        "- **Deliver**: work on `feat/<slug>`. Create the branch and commit only as `git_autonomy` allows; at `none`, ask the",
        "  person to do it and name the paths. Push only as the push rule below allows. If the person wants a pull request, fill",
        "  `%s/templates/pr-body.md` by hand." % OUT_DIR,
        "- **Skip** `pr_prepare.py`, `verify.py`, `loopctl`, `doctor.py` and every other script the copy shows as",
        "  `scripts/<x> (%s; <link>)`. Do not run them or look for them; the two steps above replace them." % PLUGIN_ONLY,
        "  `$KIT` and the session's `kit:` line name the Claude Code plugin's folder, which does not exist here.",
        "",
        "## Rules",
        "",
        "- Do not skip the design or review steps for feature work. Keep changes to the active brief; report missing or",
        "  conflicting requirements instead of inventing them.",
        "- Run the verify commands above before calling work done.",
        "- **Push rule.** You may push only your own feature branch (`git push <remote> feat/<slug>`, never a protected",
        "  branch), and only when `%s/loop.config.md` as committed on the base branch says" % proj,
        "  `git_autonomy: branch+commit+push` together with `delivery: push-branch` or `delivery: draft-pr`. Otherwise print the",
        "  push for the person. Never push main or any other protected branch, never force-push, never merge a pull request,",
        "  and never approve your own work. Nothing here checks this rule or knows which branches are protected.",
        "- Where the kit says `HUMAN STEP: <command>`, print the command and wait for the person.",
        "- A person, not an agent, changes guardrail files (CI workflows, branch rules, `loop.config.md` guard keys,",
        "  `git_autonomy`, `delivery`).",
        "- Links into github.com name kit files that were not copied (scripts, hooks, docs). They run only under the",
        "  Claude Code plugin; read them for what they check, do not expect them to run here.",
        "- Templates other than Markdown are copied byte for byte. The CI workflows in `%s/templates/github/` and" % OUT_DIR,
        "  `templates/infra/` read the config at `.claude/loop.config.md` and fetch the kit's scripts in CI: to use one, put",
        "  the config at that path or edit the workflow. The Claude Code settings and hook snippets are left out; they are in",
        "  the kit release under %stemplates/." % url,
        "",
        "## What works here and what does not",
        "",
        "| Capability | Claude Code plugin | Codex / Cursor (this copy) |",
        "|---|---|---|",
        "| Modes (`skills/*/SKILL.md`) | Slash commands: `/pm`, `/architect`, `/tech-lead`, `/planner`, `/orchestrate`, `/retro` | Works as instructions; you name the mode in your prompt |",
        "| Roles (`agents/*.md`) | Subagents, each in a clean context | Works as instructions. No isolation: one context playing reviewer of its own work is not an independent review |",
        "| Per-role tool limits (`tools:` / `disallowedTools:` in `agents/*.md`) | Enforced for each subagent | **No.** \"Read-only\" is a request. Do not run `intake-triage` on untrusted text in a session that can write, run commands or reach the network |",
        "| Templates and docs-as-contract (`docs/features/<slug>/`) | Yes | Yes |",
        "| Guards (blocks pushes to main, edits on protected branches, guardrail-file writes) | On, from the plugin's hooks | **No.** Nothing blocks these |",
        "| Push tier (`git_autonomy: branch+commit+push`: the agent pushes its own feature branch) | Allowed only by the config at the base, and the guards refuse main, protected branches and force | **No.** Nothing checks the config or which branches are protected. This copy seeds `git_autonomy: none` and `delivery: local` |",
        "| Hooks (session start, worktree setup, telemetry) | Yes | **No** |",
        "| `loopctl` driver and the unattended loop | Yes | **No.** Do not run the loop unattended here |",
        "| Gates (verify, design-critic, review, security verdicts enforced before done) | Enforced by `loopctl` | **No.** Followed only if you follow them |",
        "| Human-only steps (merge approvals, infra apply) | Enforced | **No.** Only the instruction to stop and ask |",
        "",
        "Enforcement for these tools comes from your CI (required checks and code review on every pull request, branch protection",
        "on main), not from this copy.",
        "",
    ]
    return "\n".join(lines)


def marker_line(meta: Dict[str, str]) -> str:
    return "<!-- %s: generated by chain-of-custody v%s scripts/adapt.py. Re-run it to update; delete this line to keep your own edits. -->" % (
        MARKER, meta["version"])


def tool_texts(meta: Dict[str, str], body: str) -> Dict[str, str]:
    front = "---\ndescription: Apply the Chain of Custody role-based engineering workflow and hand-off rules.\nalwaysApply: true\n---\n\n"
    return {AGENTS_MD: marker_line(meta) + "\n\n" + body, MDC: front + marker_line(meta) + "\n\n" + body}


def readme_text(meta: Dict[str, str]) -> str:
    return "\n".join([
        "# .chain-of-custody (generated)",
        "",
        "Generated by `scripts/adapt.py` from chain-of-custody v%s (%s)." % (meta["version"], meta["repo"]),
        "",
        "Everything in this directory except `project/` is replaced wholesale each time adapt.py runs. Do not edit it; your",
        "changes would be lost. To update, check the kit out at a newer release tag and run adapt.py again.",
        "",
        "`project/` holds the project's own files (%s, loop.config.md, the primers in context/). adapt.py copies them" % PROJECT_FILE,
        "from the kit's seed only when they are missing and never overwrites them. Edit them. `%s/` holds the same" % SEED_COPY_DIR,
        "files as this release seeds them; when adapt.py says a seed changed, diff the two.",
        "",
        "`VERSION` names the kit version and commit this copy came from.",
        "",
        "Start with GUIDE.md. Links into github.com name kit files that are not copied here (scripts, hooks, docs); they run only",
        "under the Claude Code plugin.",
        "",
    ])


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def read_record(out: str) -> Dict[str, object]:
    """What the last run recorded in .chain-of-custody/VERSION: its version, the seed hashes and the generated-file hashes."""
    rec = {"version": "", "seed": {}, "generated": {}}  # type: Dict[str, object]
    for line in (read_text(os.path.join(out, "VERSION")) or "").splitlines():
        key, _sep, val = line.partition(": ")
        if key == "version":
            rec["version"] = val.strip()
        elif key in ("seed", "generated"):
            name, _sp, value = val.strip().rpartition(" ")
            if name:
                rec[key][name] = value                                  # type: ignore
    return rec


def copy_file(src: str, dst: str, rel: str, rewrite: Rewriter) -> None:
    """Markdown is rewritten; every other file is copied byte for byte (a template's paths are its own business)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not rel.endswith(".md"):
        shutil.copyfile(src, dst)
        return
    text = read_text(src)
    if text is None:
        shutil.copyfile(src, dst)
        return
    write_text(dst, rewrite(text, rel))


def seed_text(kit_root: str, rel: str, rewrite: Rewriter) -> str:
    src = seed_source(kit_root, rel)
    with open(os.path.join(kit_root, *src.split("/")), "r", encoding="utf-8") as fh:
        text = fh.read()
    if not rel.endswith(".md"):
        return text
    text = rewrite(text, src, "%s/%s" % (PROJECT_DIR, rel))
    if rel == "loop.config.md":
        text = adapter_loop_config(text)
    return text


def symlink_on(target: str, rel: str) -> Optional[str]:
    """The first path from `target` down to `rel` that is a symlink, or None."""
    cur = target
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return cur
    return None


def check_target(target: str, kit_root: str) -> str:
    if not os.path.isdir(target):
        raise AdaptError("%s is not a directory" % target)
    real, kit = os.path.realpath(target), os.path.realpath(kit_root)
    if real == kit or real.startswith(kit + os.sep) or kitpaths.is_kit_dev_checkout(real):
        raise AdaptError("%s is the kit itself (or inside it); point --target at your project" % real)
    out = os.path.join(real, OUT_DIR)
    for path in (out, out + ".adapt-tmp", os.path.join(out, PROJECT_DIR)):
        if os.path.islink(path):
            raise AdaptError("%s is a symlink to %s; adapt.py never writes through a symlink. Replace it with a directory"
                             % (path, os.readlink(path)))
    if os.path.lexists(out) and not os.path.isdir(out):
        raise AdaptError("%s exists and is not a directory" % out)
    if os.path.isdir(out) and os.listdir(out):
        version = read_text(os.path.join(out, "VERSION")) or ""
        if MARKER not in version:
            raise AdaptError("%s exists but adapt.py did not write it (no %s line in its VERSION); move it away first" % (out, MARKER))
    return real


def build(target: str, kit_root: str, meta: Dict[str, str], commit: str, files: Sequence[str],
          generated: Dict[str, str], old: Dict[str, object]) -> Dict[str, object]:
    """Regenerate .chain-of-custody/ (keeping project/) and seed the missing project files. Returns what happened."""
    seeds = seed_files(kit_root)
    kit_context = [f[len("context/"):] for f in files if f.startswith("context/")]
    rewrite = Rewriter(files, seeds, kit_context, meta["version"], meta["repo"])
    out = os.path.join(target, OUT_DIR)
    stage = out + ".adapt-tmp"
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    for rel in files:
        copy_file(os.path.join(kit_root, rel), os.path.join(stage, rel), rel, rewrite)
    seed_texts = dict((rel, seed_text(kit_root, rel, rewrite)) for rel in seeds)
    for rel, text in seed_texts.items():
        write_text(os.path.join(stage, SEED_COPY_DIR, rel), text)
    skills = skills_of(files)
    write_text(os.path.join(stage, "GUIDE.md"), guide_body(meta, skills))
    write_text(os.path.join(stage, "README.md"), readme_text(meta))
    record = [
        "# %s: generated, replaced on every run" % MARKER,
        "version: %s" % meta["version"],
        "commit: %s" % (commit or "unknown"),
        "source: %s" % meta["repo"],
    ]
    record += ["seed: %s %s" % (rel, digest(seed_texts[rel])) for rel in seeds]
    record += ["generated: %s %s" % (rel, generated[rel]) for rel in sorted(generated)]
    write_text(os.path.join(stage, "VERSION"), "\n".join(record) + "\n")
    if os.path.isdir(os.path.join(out, PROJECT_DIR)):
        os.rename(os.path.join(out, PROJECT_DIR), os.path.join(stage, PROJECT_DIR))
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.rename(stage, out)
    old_seeds = old.get("seed") or {}                               # type: Dict[str, str]
    seeded, kept, changed = [], [], []  # type: List[str], List[str], List[Tuple[str, str, bool]]
    for rel in seeds:
        dst = os.path.join(out, PROJECT_DIR, rel)
        shown = "%s/%s/%s" % (OUT_DIR, PROJECT_DIR, rel)
        if os.path.lexists(dst):
            kept.append(shown)
            was = old_seeds.get(rel)
            if was and was != digest(seed_texts[rel]):
                mine = read_text(dst)
                changed.append((shown, "%s/%s/%s" % (OUT_DIR, SEED_COPY_DIR, rel), mine is not None and digest(mine) == was))
            continue
        write_text(dst, seed_texts[rel])
        seeded.append(shown)
    return {"copied": list(files), "seeded": seeded, "kept": kept, "changed": changed, "skills": sorted(skills)}


def skills_of(files: Sequence[str]) -> Set[str]:
    return set(f.split("/")[1] for f in files if f.startswith("skills/") and f.count("/") >= 2)


def place(target: str, rel: str, text: str, force: bool, hand_note: str, recorded: Optional[str]) -> str:
    """Write a tool file that carries the marker. Returns a one-line outcome for the summary."""
    path = os.path.join(target, *rel.split("/"))
    for check in (rel, rel + ".pre-adapt", rel + ".prev"):
        link = symlink_on(target, check)
        if link:
            return "REFUSED %s: %s is a symlink to %s; adapt.py never writes through a symlink, not even with --force. " \
                   "Replace it with a plain file, or add by hand: %s" % (rel, os.path.relpath(link, target), os.readlink(link), hand_note)
    old = read_text(path) if os.path.lexists(path) else None
    if old is not None and MARKER not in old and not force:
        return "SKIPPED %s: it is yours (no %s marker). Add by hand: %s" % (rel, MARKER, hand_note)
    if os.path.lexists(path) and old is None and not force:
        return "SKIPPED %s: unreadable; fix or remove it, or re-run with --force" % rel
    if old == text:
        return "unchanged %s" % rel
    if old is not None and MARKER not in old:
        shutil.copyfile(path, path + ".pre-adapt")
        write_text(path, text)
        return "OVERWROTE %s (--force; your copy is %s.pre-adapt)" % (rel, rel)
    note = ""
    if old is not None and recorded != digest(old):
        shutil.copyfile(path, path + ".prev")
        note = " (it differed from what adapt.py last wrote; the old text is %s.prev)" % rel
    write_text(path, text)
    return "wrote %s%s" % (rel, note)


def main(argv: Optional[Sequence[str]] = None, kit_root: str = kitpaths.KIT_ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--target", required=True, help="the Codex or Cursor project to write into")
    ap.add_argument("--tool", choices=("codex", "cursor", "both"), default="both")
    ap.add_argument("--force", action="store_true", help="overwrite an AGENTS.md or .mdc that lacks the adapt marker (a .pre-adapt backup is kept)")
    args = ap.parse_args(argv)
    selected = [rel for rel, tool in TOOL_FILES if args.tool in (tool, "both")]
    try:
        target = check_target(os.path.abspath(args.target), kit_root)
        old = read_record(os.path.join(target, OUT_DIR))
        meta = plugin_meta(kit_root)
        commit = git_out(kit_root, "rev-parse", "HEAD")
        tags = git_out(kit_root, "tag", "--points-at", "HEAD").split()
        files = copied_files(kit_root)
        texts = tool_texts(meta, guide_body(meta, skills_of(files)))
        old_generated = old.get("generated") or {}                  # type: Dict[str, str]
        generated = {}
        for rel, _tool in TOOL_FILES:
            value = digest(texts[rel]) if rel in selected else old_generated.get(rel)
            if value:
                generated[rel] = value
        done = build(target, kit_root, meta, commit, files, generated, old)
    except (AdaptError, OSError, ValueError, KeyError) as exc:
        sys.stderr.write("adapt: %s\n" % exc)
        return 1
    pointer = "Read `%s/GUIDE.md` for the Chain of Custody workflow, and `%s/%s/%s` at session start." % (
        OUT_DIR, OUT_DIR, PROJECT_DIR, PROJECT_FILE)
    hand = {AGENTS_MD: "the line \"%s\" in AGENTS.md" % pointer,
            MDC: "a rule with `alwaysApply: true` that says \"%s\"" % pointer}
    outcomes = []
    for rel in selected:
        outcomes.append(place(target, rel, texts[rel], args.force, hand[rel], old_generated.get(rel)))
    for rel, tool in TOOL_FILES:
        path = os.path.join(target, *rel.split("/"))
        if rel in selected or not os.path.lexists(path) or symlink_on(target, rel):
            continue
        if MARKER in (read_text(path) or ""):
            outcomes.append("stale %s: an earlier run wrote it for %s and this run did not update it; delete it or re-run with --tool both"
                            % (rel, tool))
    print("adapt: chain-of-custody v%s (%s) -> %s" % (meta["version"], (commit[:12] or "commit unknown"), target))
    if "v" + meta["version"] not in tags:
        print("  note: the kit checkout is not at tag v%s; the github.com links in the copy point at that tag" % meta["version"])
    print("  %s/: %d kit files (modes: %s), replaced wholesale" % (OUT_DIR, len(done["copied"]), ", ".join(done["skills"])))
    if done["seeded"]:
        print("  seeded %d project files into %s/%s/ (yours to edit)" % (len(done["seeded"]), OUT_DIR, PROJECT_DIR))
    if done["kept"]:
        print("  kept %d existing project files in %s/%s/" % (len(done["kept"]), OUT_DIR, PROJECT_DIR))
    for shown, fresh, unedited in done["changed"]:
        print("  seed for %s changed since v%s; compare with %s%s" % (
            shown, old.get("version") or "?", fresh, " (you have not edited yours, so copying it over is safe)" if unedited else ""))
    for line in outcomes:
        print("  " + line)
    print("  Not installed (Claude Code plugin only): guards, hooks, subagent isolation, per-role tool limits, the loopctl driver,"
          " gates, push-tier and human-step enforcement.")
    return 1 if any(line.startswith("REFUSED ") for line in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
