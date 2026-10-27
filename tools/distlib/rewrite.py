"""Path rewriting for the plugin copy of the kit's prose (IMP-3).

In a copy install the prose says `python3 .claude/scripts/doctor.py` and `.claude/spec/loopctl.md`. In a plugin the same files live
under the plugin root, so the plugin copy of every prose file (`*.md`, `*.mdc`) gets those paths rewritten to `${CLAUDE_PLUGIN_ROOT}/...`.

What is rewritten (kit-owned places):  .claude/{scripts,spec,templates,agents,skills}/<something>, .claude/context/<kit file>.md,
and docs/<file>.md for the docs the plugin ships.
What is NOT rewritten (the project's own files, or a pattern, not a path):
    .claude/CLAUDE.md, loop.config.md, settings*.json, loop.jsonl, pr/, bootstrap/, the primers .claude/context/<primer>.md,
    globs such as .claude/agents/*.md or .claude/skills/**, bare directory names, and the project skill .claude/skills/launch-app.

Whether `${CLAUDE_PLUGIN_ROOT}` expands inside skill and agent text is probe V13 (unverified). The variable is one constant, so if the
probe says otherwise the fix is one line here (the fallback in docs/design/probe-assumptions.md is a path relative to the skill directory).
"""
from __future__ import annotations

import re
from typing import Iterable, Set

PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"

_KIT_DIRS = "scripts|spec|templates|agents|skills"
# a relative link from a doc into the copy install's `.claude/` (`../.claude/spec/x.md`) is `../spec/x.md` in the plugin
_UP_CLAUDE = re.compile(r"\]\(((?:\.\./)+)\.claude/")
# `.claude/<kitdir>/<name>`: not preceded by a path character, and the next character is not a glob or a placeholder.
_KIT_PATH = re.compile(r"(?<![A-Za-z0-9_./${}~-])\.claude/(" + _KIT_DIRS + r")/(?![*<{])")
_CONTEXT_PATH = re.compile(r"(?<![A-Za-z0-9_./${}~-])\.claude/context/([A-Za-z0-9_-]+\.md)")
_DOC_PATH = re.compile(r"(?<![A-Za-z0-9_./${}~-])docs/((?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md)")

# project skills the kit tells a human to create in the project (not part of the plugin)
_PROJECT_SKILLS = ("launch-app",)

LEFTOVER = re.compile(r"(?<![A-Za-z0-9_./${}~-])\.claude/(?:" + _KIT_DIRS + r")/(?![*<{])")


def rewrite_text(text: str, primers: Iterable[str], plugin_docs: Set[str]) -> str:
    """The plugin form of `text`. `primers` are the primer names (project files); `plugin_docs` the repo paths of the docs the plugin ships."""
    primer_files = {p + ".md" for p in primers}
    docs = set(plugin_docs)

    def kit(m):
        rest = m.string[m.end():m.end() + 24]
        if m.group(1) == "skills" and any(rest.startswith(s) for s in _PROJECT_SKILLS):
            return m.group(0)
        return "%s/%s/" % (PLUGIN_ROOT_VAR, m.group(1))

    def ctx(m):
        if m.group(1) in primer_files:
            return m.group(0)
        return "%s/context/%s" % (PLUGIN_ROOT_VAR, m.group(1))

    def doc(m):
        return "%s/docs/%s" % (PLUGIN_ROOT_VAR, m.group(1)) if "docs/" + m.group(1) in docs else m.group(0)

    out = _UP_CLAUDE.sub(r"](\1", text)
    out = _KIT_PATH.sub(kit, out)
    out = _CONTEXT_PATH.sub(ctx, out)
    out = _DOC_PATH.sub(doc, out)
    return out


def leftovers(text: str):
    """Kit-owned `.claude/...` paths still present in plugin prose: [(line number, line)]. The project skill launch-app is allowed."""
    found = []
    for n, line in enumerate(text.split("\n"), 1):
        for m in LEFTOVER.finditer(line):
            if m.group(0).startswith(".claude/skills/") and any(line[m.end():].startswith(s) for s in _PROJECT_SKILLS):
                continue
            found.append((n, line.strip()[:120]))
            break
    return found
