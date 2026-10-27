"""Load and validate `loop.config.md` (IMP-11, report §9, §10.3).

The file is markdown with exactly one fenced `yaml` block, parsed by `miniyaml`. Keys
come from the registry (`registry.py`); this module adds typed access, validation, the
v1 compatibility behaviour, and loading the config *as of a git sha*.

Base-sha rule (report §9, CO-11): gate toggles, skip regexes and verify command
definitions are read from the base sha, never from the head, so an item cannot weaken its
own gate. Use `load_at_sha(repo, base_sha)` for anything a gate decides on, and
`h6_changes(base_cfg, head_cfg)` to see which H6-protected keys a diff touches.

v1 compatibility: a v1 file (only the 16 v1 keys) loads unchanged; every key that is
absent takes its registry default. A key marked `deprecated_by` in the registry is still
accepted: its value moves to the new key (unless that is set explicitly) and a warning is
recorded.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import gitutil, miniyaml, registry as registry_mod
from .registry import PLACEHOLDER, Key, Registry

__all__ = ["ConfigError", "Issue", "Config", "CONFIG_PATH", "extract_yaml_block", "load_text",
           "load_file", "load_at_sha", "validate", "flatten", "h6_changes", "GATE_MODES"]

CONFIG_PATH = os.path.join(".claude", "loop.config.md")
GATE_MODES = ("on", "report", "off")

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})\s*(yaml|yml)\s*$", re.I)


class ConfigError(ValueError):
    """The config file cannot be used at all (no yaml block, parse error, strict failure)."""

    def __init__(self, message: str, issues: Optional[List["Issue"]] = None) -> None:
        ValueError.__init__(self, message)
        self.issues = issues or []


class Issue(object):
    """A validation finding. `level` is 'error' or 'warning'."""

    __slots__ = ("level", "key", "message", "line")

    def __init__(self, level: str, key: str, message: str, line: Optional[int] = None) -> None:
        self.level = level
        self.key = key
        self.message = message
        self.line = line

    def __str__(self) -> str:
        where = "line %d: " % self.line if self.line else ""
        return "%s%s: %s: %s" % (where, self.level, self.key, self.message)

    def __repr__(self) -> str:
        return "Issue(%s)" % self


def _is_close(line: str, fence: str) -> bool:
    s = line.strip()
    return len(s) >= len(fence) and set(s) == {fence[0]}


def extract_yaml_block(text: str) -> Tuple[str, int]:
    """The single fenced yaml block of a config file: (yaml text, line offset of its first line).

    Exactly one block is required; zero or several is an error (an ambiguous config is
    never guessed at)."""
    lines = text.split("\n")
    blocks = []  # type: List[Tuple[int, int]]
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if m:
            fence = m.group(1)
            j = i + 1
            while j < len(lines) and not _is_close(lines[j], fence):
                j += 1
            if j >= len(lines):
                raise ConfigError("line %d: the yaml fence is never closed" % (i + 1))
            blocks.append((i + 1, j))
            i = j + 1
            continue
        i += 1
    if not blocks:
        raise ConfigError("no fenced ```yaml block found")
    if len(blocks) > 1:
        raise ConfigError("found %d fenced yaml blocks (first at line %d, second at line %d); "
                          "a config file must contain exactly one" % (len(blocks), blocks[0][0], blocks[1][0]))
    start, end = blocks[0]
    return "\n".join(lines[start:end]), start


def flatten(data: Dict[str, Any], reg: Registry, lines: Optional[Dict[Tuple[str, ...], int]] = None
            ) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Nested mapping -> {'a.b': value}. A mapping stops being expanded where the registry
    knows the key as a leaf (type map)."""
    out = {}  # type: Dict[str, Any]
    where = {}  # type: Dict[str, int]
    lines = lines or {}

    def walk(node: Dict[str, Any], prefix: Tuple[str, ...]) -> None:
        for k, v in node.items():
            path = prefix + (k,)
            name = ".".join(path)
            known = reg.keys.get(name)
            if isinstance(v, dict) and v and not (known is not None and known.type == "map"):
                walk(v, path)
                continue
            out[name] = v
            if path in lines:
                where[name] = lines[path]

    walk(data, ())
    return out, where


def validate(values: Dict[str, Any], reg: Registry, where: Optional[Dict[str, int]] = None) -> List[Issue]:
    """Check flattened `values` against the registry: unknown keys (with a suggestion) and types."""
    where = where or {}
    issues = []  # type: List[Issue]
    for name in values:
        key = reg.get(name)
        line = where.get(name)
        if key is None:
            sug = reg.suggest(name)
            hint = " (did you mean %s?)" % " or ".join(repr(s) for s in sug) if sug else ""
            issues.append(Issue("error", name, "unknown key%s" % hint, line))
            continue
        problem = key.check_value(values[name])
        if problem:
            issues.append(Issue("error", name, problem, line))
        if key.deprecated_by:
            issues.append(Issue("warning", name, "deprecated, use %r" % key.deprecated_by, line))
    return issues


class Config(object):
    def __init__(self, explicit: Dict[str, Any], reg: Registry, issues: List[Issue],
                 source: str = "<text>", lines: Optional[Dict[str, int]] = None) -> None:
        self.reg = reg
        self.explicit = dict(explicit)
        self.issues = issues
        self.source = source
        self.lines = lines or {}
        self.warnings = [i for i in issues if i.level == "warning"]
        self.errors = [i for i in issues if i.level == "error"]
        self._apply_deprecations()

    def _apply_deprecations(self) -> None:
        for name in list(self.explicit):
            key = self.reg.get(name)
            if key is not None and key.deprecated_by and key.deprecated_by not in self.explicit:
                self.explicit[key.deprecated_by] = self.explicit[name]

    # -- access ----------------------------------------------------------------
    def is_set(self, name: str) -> bool:
        return name in self.explicit

    def get(self, name: str, default: Any = None) -> Any:
        """The explicit value, else the registry default, else `default`."""
        if name in self.explicit:
            return self.explicit[name]
        key = self.reg.get(name)
        if key is not None:
            return key.default
        return default

    def _typed(self, name: str, types: tuple, label: str) -> Any:
        v = self.get(name)
        if isinstance(v, bool) and bool not in types:
            raise ConfigError("%s: expected %s, got a boolean" % (name, label))
        if not isinstance(v, types):
            raise ConfigError("%s: expected %s, got %r" % (name, label, v))
        return v

    def get_str(self, name: str) -> str:
        return self._typed(name, (str,), "a string")

    def get_int(self, name: str) -> int:
        return self._typed(name, (int,), "an integer")

    def get_float(self, name: str) -> float:
        return float(self._typed(name, (int, float), "a number"))

    def get_bool(self, name: str) -> bool:
        return self._typed(name, (bool,), "true or false")

    def get_list(self, name: str) -> List[Any]:
        return list(self._typed(name, (list,), "a list"))

    def is_placeholder(self, name: str) -> bool:
        """True if a command key still holds the `<command>` template placeholder (or is empty)."""
        v = self.get(name)
        return isinstance(v, str) and (v.strip() == PLACEHOLDER or not v.strip())

    def gate(self, name: str) -> str:
        """`gates.<name>` as on | report | off. Unset means on. An invalid value fails closed to on."""
        v = self.get("gates." + name, "on")
        return v if v in GATE_MODES else "on"

    def gates(self) -> Dict[str, str]:
        out = {}  # type: Dict[str, str]
        for n in self.explicit:
            if n.startswith("gates.") and n.count(".") == 1:
                out[n.split(".", 1)[1]] = self.gate(n.split(".", 1)[1])
        return out

    def effective(self) -> Dict[str, Any]:
        """Every concrete registry key with its effective value, plus explicit wildcard members."""
        out = self.reg.defaults()
        out.update(self.explicit)
        return out

    def h6_values(self) -> Dict[str, Any]:
        """Effective values of the H6-protected keys (explicit and defaulted)."""
        eff = self.effective()
        return {n: v for n, v in eff.items() if self.reg.is_h6(n)}

    def evidence_gated_violations(self) -> List[str]:
        """Names of keys set to a value that needs the evidence gate (IMP-1)."""
        out = []
        for n, v in self.explicit.items():
            key = self.reg.get(n)
            if key is not None and key.is_gated_value(v):
                out.append(n)
        return sorted(out)

    def require_valid(self) -> "Config":
        if self.errors:
            raise ConfigError("%s has %d error(s):\n  %s" % (self.source, len(self.errors),
                                                             "\n  ".join(str(i) for i in self.errors)), self.errors)
        return self


def load_text(text: str, reg: Optional[Registry] = None, source: str = "<text>") -> Config:
    """Parse and validate config markdown. Raises ConfigError for an unreadable file;
    validation findings are on `Config.issues` (use `.require_valid()` for strictness)."""
    reg = reg or registry_mod.load_registry()
    yaml_text, offset = extract_yaml_block(text)
    try:
        data, keylines = miniyaml.parse_with_lines(yaml_text, offset)
    except miniyaml.YamlError as exc:
        raise ConfigError("%s: %s" % (source, exc))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("%s: the yaml block must be a mapping of keys" % source)
    values, where = flatten(data, reg, keylines)
    return Config(values, reg, validate(values, reg, where), source, where)


def load_file(path: str, reg: Optional[Registry] = None) -> Config:
    """Load a config file from disk. Raises FileNotFoundError if it is missing
    (the Orchestrator refuses to start in that case)."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return load_text(text, reg, source=path)


def load_at_sha(repo: str, sha: str, reg: Optional[Registry] = None, path: str = CONFIG_PATH) -> Config:
    """The config as committed at `sha` (`git show <sha>:<path>`). Raises ConfigError if absent."""
    text = gitutil.show_file(sha, path, repo)
    if text is None:
        raise ConfigError("%s does not exist at %s" % (path, sha))
    return load_text(text, reg, source="%s:%s" % (sha[:12], path))


def h6_changes(base: Config, head: Config) -> List[Tuple[str, Any, Any]]:
    """H6-protected keys whose effective value differs: [(name, base_value, head_value)].

    A change here must route to a human (report §6.1 H6)."""
    b, h = base.h6_values(), head.h6_values()
    out = []
    for name in sorted(set(b) | set(h)):
        if b.get(name) != h.get(name):
            out.append((name, b.get(name), h.get(name)))
    return out
