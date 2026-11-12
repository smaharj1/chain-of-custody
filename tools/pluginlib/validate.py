"""Offline structural checks for the plugin and the marketplace file: the stdlib stand-in for `claude plugin validate --strict` (IMP-3).

It cannot know what the real validator knows (the manifest schema may have changed, `${CLAUDE_PLUGIN_ROOT}` expansion is probe V13),
so passing here is necessary, not sufficient. The real command is a pending-human step: `tools/validate-plugin-real.sh`.

The repository root is the plugin root. `validate(files, marketplace)` takes {path: bytes} for the repository's files (paths relative
to the root) and the bytes of `.claude-plugin/marketplace.json`, and returns a list of problems. Empty means the plugin is sound.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Dict, List, Optional, Set

KNOWN_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied", "Notification", "UserPromptSubmit",
                "Stop", "StopFailure", "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "TeammateIdle", "ConfigChange",
                "CwdChanged", "FileChanged", "WorktreeCreate", "WorktreeRemove", "PreCompact", "PostCompact", "SessionStart", "SessionEnd",
                "Elicitation", "ElicitationResult", "InstructionsLoaded"}
# opt-in snippets, never part of the plugin's hooks.json (Phase 8: the worktree hooks are a human's choice)
HOOKS_NOT_IN_PLUGIN = {"WorktreeCreate", "WorktreeRemove"}
USER_CONFIG_TYPES = {"string", "number", "boolean", "directory", "file"}
RESERVED_SKILL_NAMES = {"loop", "code-review"}
SKILL_KEYS = {"name", "description", "argument-hint", "disable-model-invocation", "user-invocable", "allowed-tools", "model", "effort", "context",
              "agent", "hooks", "when_to_use", "paths", "shell"}
AGENT_FORBIDDEN_KEYS = {"hooks", "mcpServers", "permissionMode"}     # plugin agents ignore them (P20): enforcement lives in hooks.json
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
MD_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
# the parts of the repository a user's session reads as kit prose (CLAUDE.md "Paths in prose")
PROSE_AREAS = ("agents/", "skills/", "context/", "spec/", "templates/")
# human docs: their ${CLAUDE_PLUGIN_ROOT}/ paths must exist too, but they may name an old install's `.claude/scripts/` on purpose
# (docs/migrating-to-v2.md describes the copy layout), so the leftover check is for kit prose only. docs/design and docs/baseline are history.
DOC_AREAS = ("docs/",)
PROSE_SKIP = ("docs/design/", "docs/baseline/")
# a kit-owned `.claude/...` path left in kit prose: the kit is not in the project's .claude/ any more. `<role>`, `*` and `{` forms name
# project overrides and patterns; the project skill launch-app is a project file.
LEFTOVER = re.compile(r"(?<![A-Za-z0-9_./${}~-])\.claude/(?:scripts|spec|templates|skills)/(?![*<{]|launch-app)")
# Skills that detect or take apart an old copy install name its `.claude/scripts/...` on purpose: only their ${CLAUDE_PLUGIN_ROOT} paths are checked.
OLD_INSTALL_PROSE = ("skills/init/SKILL.md", "skills/migrate/SKILL.md", "skills/bootstrap/SKILL.md")
RUNTIME_FILES = {"loop.jsonl", "metrics.jsonl", "orchestrate.lock", "settings.local.json", "bootstrap-receipt.json", "launch.json"}


def frontmatter(text: str):
    """(dict of top-level `key: value`, error or None). Values keep their text with one pair of surrounding quotes removed."""
    if not text.startswith("---\n"):
        return {}, "no --- fence at the top"
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, "frontmatter is not closed"
    data = {}   # type: Dict[str, str]
    for n, line in enumerate(text[4:end].split("\n"), 2):
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not m:
            return data, "line %d is not `key: value`: %r" % (n, line[:40])
        if m.group(1) in data:
            return data, "duplicate key %s" % m.group(1)
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif v[:1] not in ("", "|", ">", "[", "{", "\"", "'") and (": " in v or " #" in v):
            # YAML reads `a: b` inside a plain scalar as a nested mapping (an error) and ` #` as the start of a comment (truncation)
            return data, "line %d: the unquoted value of %s contains %r; quote it or reword it" % (n, m.group(1), ": " if ": " in v else " #")
        data[m.group(1)] = v
    return data, None


def _json(files: Dict[str, bytes], rel: str, problems: List[str]):
    if rel not in files:
        problems.append("%s is missing" % rel)
        return None
    try:
        return json.loads(files[rel].decode("utf-8"))
    except ValueError as exc:
        problems.append("%s is not valid JSON: %s" % (rel, exc))
        return None


def check_manifest(files: Dict[str, bytes], problems: List[str]) -> Optional[dict]:
    rel = ".claude-plugin/plugin.json"
    doc = _json(files, rel, problems)
    if not isinstance(doc, dict):
        if doc is not None:
            problems.append("%s must be a JSON object" % rel)
        return None
    name = doc.get("name")
    if not isinstance(name, str) or not KEBAB.match(name):
        problems.append("plugin.json: `name` must be kebab-case, got %r" % (name,))
    version = doc.get("version")
    if not isinstance(version, str) or not SEMVER.match(version):
        problems.append("plugin.json: `version` must be a semantic version, got %r" % (version,))
    for k in ("description", "license"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            problems.append("plugin.json: `%s` must be a non-empty string" % k)
    if not isinstance(doc.get("author"), dict) or not doc["author"].get("name"):
        problems.append("plugin.json: `author.name` is required")
    uc = doc.get("userConfig")
    if uc is not None:
        if not isinstance(uc, dict) or not uc:
            problems.append("plugin.json: `userConfig` must be a non-empty object when present")
        else:
            for key, ent in uc.items():
                if not re.match(r"^[a-z][a-z0-9_]*$", key):
                    problems.append("plugin.json: userConfig key %r must be snake_case" % key)
                if not isinstance(ent, dict):
                    problems.append("plugin.json: userConfig.%s must be an object" % key)
                    continue
                if ent.get("type") not in USER_CONFIG_TYPES:
                    problems.append("plugin.json: userConfig.%s.type must be one of %s" % (key, sorted(USER_CONFIG_TYPES)))
                for k in ("title", "description"):
                    if not isinstance(ent.get(k), str) or not ent[k]:
                        problems.append("plugin.json: userConfig.%s.%s is required" % (key, k))
                if ent.get("sensitive") is True and "default" in ent:
                    problems.append("plugin.json: userConfig.%s is sensitive and has a default" % key)
    for field in ("agents", "skills", "hooks", "commands"):
        v = doc.get(field)
        for p in ([v] if isinstance(v, str) else (v if isinstance(v, list) else [])):
            if not isinstance(p, str) or not p.startswith("./") or ".." in p.split("/"):
                problems.append("plugin.json: `%s` path %r must start with ./ and stay inside the plugin" % (field, p))
            elif not any(f == p[2:].rstrip("/") or f.startswith(p[2:].rstrip("/") + "/") for f in files):
                problems.append("plugin.json: `%s` path %s does not exist" % (field, p))
    return doc


def check_hooks(files: Dict[str, bytes], problems: List[str]) -> None:
    doc = _json(files, "hooks/hooks.json", problems)
    if doc is None:
        return
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        problems.append("hooks/hooks.json: needs a non-empty `hooks` object")
        return
    for event, groups in hooks.items():
        if event not in KNOWN_EVENTS:
            problems.append("hooks/hooks.json: unknown event %s" % event)
        if event in HOOKS_NOT_IN_PLUGIN:
            problems.append("hooks/hooks.json: %s must not be in the plugin (it is an opt-in snippet, Phase 8)" % event)
        if not isinstance(groups, list) or not groups:
            problems.append("hooks/hooks.json: %s needs a list of groups" % event)
            continue
        for g in groups:
            hs = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hs, list) or not hs:
                problems.append("hooks/hooks.json: a %s group has no hooks" % event)
                continue
            for h in hs:
                if h.get("type") != "command" or not isinstance(h.get("command"), str):
                    problems.append("hooks/hooks.json: a %s handler is not a command" % event)
                    continue
                cmd = h["command"]
                if not isinstance(h.get("timeout"), int) or h["timeout"] <= 0:
                    problems.append("hooks/hooks.json: a %s handler has no timeout" % event)
                if ".claude/scripts" in cmd or "CLAUDE_PROJECT_DIR" in cmd:
                    problems.append("hooks/hooks.json: a %s handler points at the project's .claude/scripts instead of the plugin" % event)
                refs = ROOT_REF.findall(cmd)
                if not refs:
                    problems.append("hooks/hooks.json: a %s handler does not use ${CLAUDE_PLUGIN_ROOT}" % event)
                for r in refs:
                    r = r.rstrip('"').rstrip(".")
                    if r not in files:
                        problems.append("hooks/hooks.json: %s handler script %s does not exist in the plugin" % (event, r))
                if '"${CLAUDE_PLUGIN_ROOT}' not in cmd:
                    problems.append("hooks/hooks.json: a %s handler does not quote ${CLAUDE_PLUGIN_ROOT}" % event)


def check_agents(files: Dict[str, bytes], problems: List[str]) -> None:
    agents = sorted(p for p in files if re.match(r"^agents/[^/]+\.md$", p))
    if not agents:
        problems.append("agents/: no agent files")
    for p in agents:
        fm, err = frontmatter(files[p].decode("utf-8"))
        if err:
            problems.append("%s: %s" % (p, err))
            continue
        if fm.get("name") != p[len("agents/"):-3]:
            problems.append("%s: `name` %r does not match the file name" % (p, fm.get("name")))
        for k in ("description", "tools"):
            if not fm.get(k):
                problems.append("%s: `%s` is required" % (p, k))
        for k in AGENT_FORBIDDEN_KEYS & set(fm):
            problems.append("%s: `%s` is ignored in a plugin agent (P20); enforcement lives in hooks.json" % (p, k))
        for item in [x.strip() for x in fm.get("disallowedTools", "").split(",") if x.strip()]:
            if "(" in item:
                problems.append("%s: disallowedTools entry %r is a permission rule; it takes tool names only" % (p, item))
        if fm.get("effort") and fm["effort"] not in EFFORTS:
            problems.append("%s: effort %r is not one of %s" % (p, fm["effort"], sorted(EFFORTS)))


def check_skills(files: Dict[str, bytes], problems: List[str]) -> None:
    dirs = {p.split("/")[1] for p in files if p.startswith("skills/") and p.count("/") >= 2}
    if not dirs:
        problems.append("skills/: no skills")
    for d in sorted(dirs):
        rel = "skills/%s/SKILL.md" % d
        if rel not in files:
            problems.append("skills/%s: no SKILL.md" % d)
            continue
        if d in RESERVED_SKILL_NAMES:
            problems.append("skills/%s: the name collides with a bundled command (loop, code-review are reserved)" % d)
        text = files[rel].decode("utf-8")
        fm, err = frontmatter(text)
        if err:
            problems.append("%s: %s" % (rel, err))
            continue
        if fm.get("name") != d:
            problems.append("%s: `name` %r does not match the directory" % (rel, fm.get("name")))
        if fm.get("name") in RESERVED_SKILL_NAMES:
            problems.append("%s: reserved skill name %r" % (rel, fm.get("name")))
        if not fm.get("description"):
            problems.append("%s: `description` is required" % rel)
        elif len(fm["description"]) > 1024:
            problems.append("%s: `description` is longer than 1024 characters" % rel)
        for k in sorted(set(fm) - SKILL_KEYS):
            problems.append("%s: unknown frontmatter key %r" % (rel, k))
        if fm.get("disable-model-invocation") not in (None, "true", "false"):
            problems.append("%s: disable-model-invocation must be true or false" % rel)
        if fm.get("context") == "fork":
            problems.append("%s: a mode never uses `context: fork`" % rel)
        if fm.get("effort") and fm["effort"] not in EFFORTS:
            problems.append("%s: effort %r is not one of %s" % (rel, fm["effort"], sorted(EFFORTS)))
        for target in MD_LINK.findall(text):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("/"):
                continue
            parts = ("skills/%s/%s" % (d, target)).split("/")
            norm = []   # type: List[str]
            for seg in parts:
                if seg == "..":
                    if norm:
                        norm.pop()
                elif seg not in (".", ""):
                    norm.append(seg)
            if "/".join(norm) not in files:
                problems.append("%s: link to %s, which is not in the plugin" % (rel, target))


def check_paths(files: Dict[str, bytes], problems: List[str]) -> None:
    """Every ${CLAUDE_PLUGIN_ROOT}/... in kit prose exists; no kit-owned .claude/ path is left over."""
    kit_context = sorted(p[len("context/"):] for p in files if re.match(r"^context/[^/]+\.md$", p))
    for p, data in sorted(files.items()):
        if not p.endswith((".md", ".mdc")) or not p.startswith(PROSE_AREAS + DOC_AREAS) or p.startswith(PROSE_SKIP):
            continue
        text = data.decode("utf-8", "replace")
        for r in ROOT_REF.findall(text):
            r = r.rstrip(".,;:)")
            if "*" in r or "<" in r or not r.strip("./"):
                continue        # a pattern, a placeholder, or the bare `${CLAUDE_PLUGIN_ROOT}/...` that names the form itself
            if r.endswith("/"):
                if not any(f.startswith(r) for f in files):
                    problems.append("%s: ${CLAUDE_PLUGIN_ROOT}/%s is not in the plugin" % (p, r))
            elif r not in files and not any(f.startswith(r + "/") for f in files):
                problems.append("%s: ${CLAUDE_PLUGIN_ROOT}/%s is not in the plugin" % (p, r))
        for n, line in ([] if p in OLD_INSTALL_PROSE or not p.startswith(PROSE_AREAS) else leftovers(text, kit_context)):
            problems.append("%s:%d: kit path not rewritten for the plugin: %s" % (p, n, line))
            break


def leftovers(text: str, kit_context=()):
    """Kit-owned `.claude/...` paths still present in kit prose: [(line number, line)]. `kit_context` are the kit's own context files
    (engineer-protocol.md, ...), which are no longer in the project's .claude/context/; the primers there are project files."""
    ctx = re.compile(r"(?<![A-Za-z0-9_./${}~-])\.claude/context/(?:%s)\b" % "|".join(re.escape(c) for c in kit_context)) if kit_context else None
    found = []
    for n, line in enumerate(text.split("\n"), 1):
        if LEFTOVER.search(line) or (ctx and ctx.search(line)):
            found.append((n, line.strip()[:120]))
    return found


def check_layout(files: Dict[str, bytes], problems: List[str], parse_python: bool = True) -> None:
    for p in sorted(files):
        parts = p.split("/")
        if parts[0] in (".git", "plugin", "dist") or "__pycache__" in parts:
            problems.append("%s: does not belong in the plugin" % p)
        if parts[0] in ("tests", "evals"):
            continue        # fixtures may hold runtime-looking files on purpose
        if parts[-1] in RUNTIME_FILES or parts[-1] == ".DS_Store" or p.endswith((".pyc", ".lock")):
            problems.append("%s: runtime or junk file in the plugin" % p)
    sj = files.get("settings.json")
    if sj is not None:
        try:
            keys = set(json.loads(sj.decode("utf-8")))
        except ValueError:
            keys = set()
        if keys - {"agent", "subagentStatusLine"}:
            problems.append("settings.json: a plugin's settings.json applies only `agent` and `subagentStatusLine` (P19); the deny block is written by /init")
    # `.claude/settings*.json` is not checked: Claude Code reads it only when the plugin's folder is itself the project, never from an
    # installed copy, so it cannot switch the plugin off for anyone else. This repository's own .claude/settings.json sets
    # enabledPlugins false on purpose, so the kit's guards do not block edits to the kit (.claude/CLAUDE.md).
    for p, data in files.items():
        if parse_python and p.endswith(".py"):
            try:
                ast.parse(data.decode("utf-8"), filename=p)
            except SyntaxError as exc:
                problems.append("%s: does not parse: %s" % (p, exc))
    if "KIT_VERSION" not in files:
        problems.append("KIT_VERSION is missing")


def check_marketplace(plugin_doc: Optional[dict], marketplace: Optional[bytes], problems: List[str]) -> None:
    if marketplace is None:
        problems.append(".claude-plugin/marketplace.json is missing")
        return
    try:
        doc = json.loads(marketplace.decode("utf-8"))
    except ValueError as exc:
        problems.append("marketplace.json is not valid JSON: %s" % exc)
        return
    if not isinstance(doc, dict):
        problems.append("marketplace.json must be a JSON object")
        return
    if not isinstance(doc.get("name"), str) or not KEBAB.match(doc.get("name", "")):
        problems.append("marketplace.json: `name` must be kebab-case")
    if not isinstance(doc.get("owner"), dict) or not doc["owner"].get("name"):
        problems.append("marketplace.json: `owner.name` is required")
    plugins = doc.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        problems.append("marketplace.json: `plugins` must be a non-empty list")
        return
    seen = set()   # type: Set[str]
    for ent in plugins:
        if not isinstance(ent, dict) or not ent.get("name") or not ent.get("source"):
            problems.append("marketplace.json: every plugin needs `name` and `source`")
            continue
        if ent["name"] in seen:
            problems.append("marketplace.json: duplicate plugin name %s" % ent["name"])
        seen.add(ent["name"])
        src = ent["source"]
        if isinstance(src, str):
            if not src.startswith("./") or ".." in src.split("/"):
                problems.append("marketplace.json: source %r must be a relative ./ path inside the repository" % src)
            elif src != "./":
                problems.append("marketplace.json: source %s does not point at the repository root ./, where the plugin is" % src)
        if plugin_doc is not None:
            if ent["name"] != plugin_doc.get("name"):
                problems.append("marketplace.json: plugin name %r differs from plugin.json %r" % (ent["name"], plugin_doc.get("name")))
            if ent.get("version") not in (None, plugin_doc.get("version")):
                problems.append("marketplace.json: version %r differs from plugin.json %r" % (ent.get("version"), plugin_doc.get("version")))


def validate(plugin: Dict[str, bytes], marketplace: Optional[bytes], parse_python: bool = True) -> List[str]:
    problems = []   # type: List[str]
    doc = check_manifest(plugin, problems)
    check_hooks(plugin, problems)
    check_agents(plugin, problems)
    check_skills(plugin, problems)
    check_paths(plugin, problems)
    check_layout(plugin, problems, parse_python)
    if doc and "KIT_VERSION" in plugin and (doc.get("version") or "x") not in plugin["KIT_VERSION"].decode("utf-8", "replace"):
        problems.append("KIT_VERSION does not match plugin.json version %s" % doc.get("version"))
    check_marketplace(doc, marketplace, problems)
    return problems
