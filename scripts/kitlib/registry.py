"""The `loop.config` key registry (IMP-11, report §9).

Every key lives in `kitlib/schema/<phase>.json`. Each phase adds its own file, so keys
never conflict. `load_registry()` merges every `schema/*.json` (v1 first, then the rest
alphabetically) and rejects duplicate key names and duplicate section ids.

File shape:

    {
      "schema_version": 1,
      "phase": "v1",
      "sections": [ {"id", "title", "comment", "example_title", "example_comment"} ],
      "keys": [ {"name", "type", "default", "owner_phase", "h6_protected",
                 "evidence_gated", "doc", "deprecated_by", ...} ]
    }

See `schema/README.md` for the field reference.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["RegistryError", "Key", "Section", "Registry", "load_registry", "load_files",
           "SCHEMA_DIR", "TYPES", "PLACEHOLDER"]

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")
TYPES = ("string", "command", "int", "number", "bool", "enum", "list", "map")
PLACEHOLDER = "<command>"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.(?:[a-z][a-z0-9_]*|\*))*\Z")
_SECTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")
_KEY_FIELDS = {"name", "type", "default", "owner_phase", "h6_protected", "evidence_gated", "doc",
               "deprecated_by", "choices", "section", "example", "after_doc"}
_REQUIRED = ("name", "type", "default", "owner_phase", "h6_protected", "evidence_gated", "doc", "deprecated_by")


class RegistryError(ValueError):
    pass


def _lines(value: Any, what: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(value.split("\n")) if value != "" else ()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise RegistryError("%s must be a string or a list of strings" % what)


class Key(object):
    """One registry entry."""

    def __init__(self, raw: Dict[str, Any], source: str) -> None:
        self.source = source
        for f in _REQUIRED:
            if f not in raw:
                raise RegistryError("%s: key %r is missing required field %r" % (source, raw.get("name"), f))
        extra = set(raw) - _KEY_FIELDS
        if extra:
            raise RegistryError("%s: key %r has unknown field(s) %s" % (source, raw["name"], sorted(extra)))
        self.name = raw["name"]
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise RegistryError("%s: bad key name %r" % (source, self.name))
        self.type = raw["type"]
        if self.type not in TYPES:
            raise RegistryError("%s: key %s has unknown type %r" % (source, self.name, self.type))
        self.default = raw["default"]
        self.owner_phase = raw["owner_phase"]
        if not isinstance(self.owner_phase, str) or not self.owner_phase:
            raise RegistryError("%s: key %s needs a non-empty owner_phase" % (source, self.name))
        self.h6_protected = raw["h6_protected"]
        if not isinstance(self.h6_protected, bool):
            raise RegistryError("%s: key %s h6_protected must be a boolean" % (source, self.name))
        eg = raw["evidence_gated"]
        if not (isinstance(eg, bool) or (isinstance(eg, list) and all(isinstance(v, (str, int, bool)) for v in eg))):
            raise RegistryError("%s: key %s evidence_gated must be a boolean or a list of gated values" % (source, self.name))
        self.evidence_gated = eg
        self.doc = _lines(raw["doc"], "%s: key %s doc" % (source, self.name))
        self.after_doc = _lines(raw.get("after_doc"), "%s: key %s after_doc" % (source, self.name))
        self.deprecated_by = raw["deprecated_by"]
        if self.deprecated_by is not None and not isinstance(self.deprecated_by, str):
            raise RegistryError("%s: key %s deprecated_by must be a key name or null" % (source, self.name))
        self.section = raw.get("section")
        choices = raw.get("choices")
        if self.type == "enum":
            if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
                raise RegistryError("%s: enum key %s needs a non-empty list of string choices" % (source, self.name))
        elif choices is not None:
            raise RegistryError("%s: key %s has choices but is not an enum" % (source, self.name))
        self.choices = tuple(choices) if choices else ()
        if self.is_wildcard and self.type == "map":
            raise RegistryError("%s: a wildcard key cannot be a map" % source)
        problem = self.check_value(self.default)
        if problem:
            raise RegistryError("%s: default of %s is invalid: %s" % (source, self.name, problem))
        ex = raw.get("example")
        if ex is None or ex is False:
            self.example = None  # type: Optional[Dict[str, Any]]
        elif ex is True:
            self.example = {"value": self.default, "doc": list(self.doc), "after_doc": list(self.after_doc)}
        elif isinstance(ex, dict):
            if set(ex) - {"value", "doc", "after_doc"}:
                raise RegistryError("%s: key %s example has unknown field(s)" % (source, self.name))
            val = ex.get("value", self.default)
            problem = self.check_value(val)
            if problem:
                raise RegistryError("%s: example value of %s is invalid: %s" % (source, self.name, problem))
            self.example = {"value": val, "doc": list(_lines(ex.get("doc"), "example doc")),
                            "after_doc": list(_lines(ex.get("after_doc"), "example after_doc"))}
        else:
            raise RegistryError("%s: key %s example must be true, false or an object" % (source, self.name))

    # -- semantics -------------------------------------------------------------
    @property
    def is_wildcard(self) -> bool:
        return self.name.endswith(".*")

    @property
    def generated(self) -> bool:
        """Wildcard keys describe a family (`gates.*`) and are not emitted as a line."""
        return not self.is_wildcard

    def matches(self, name: str) -> bool:
        if not self.is_wildcard:
            return name == self.name
        prefix = self.name[:-1]
        return name.startswith(prefix) and "." not in name[len(prefix):] and len(name) > len(prefix)

    def check_value(self, value: Any) -> Optional[str]:
        """None if `value` fits this key's type, else a short problem description."""
        t = self.type
        if t in ("string", "command"):
            return None if isinstance(value, str) else "expected a string, got %s" % _tname(value)
        if t == "int":
            return None if isinstance(value, int) and not isinstance(value, bool) else "expected an integer, got %s" % _tname(value)
        if t == "number":
            return None if isinstance(value, (int, float)) and not isinstance(value, bool) else "expected a number, got %s" % _tname(value)
        if t == "bool":
            return None if isinstance(value, bool) else "expected true or false, got %s" % _tname(value)
        if t == "enum":
            if value in self.choices and isinstance(value, str):
                return None
            return "expected one of %s, got %r" % (" | ".join(self.choices), value)
        if t == "list":
            return None if isinstance(value, list) else "expected a list, got %s" % _tname(value)
        if t == "map":
            return None if isinstance(value, dict) else "expected a mapping, got %s" % _tname(value)
        return "unknown type"

    def is_gated_value(self, value: Any) -> bool:
        """True if setting this key to `value` needs the evidence gate (IMP-1).

        evidence_gated: true      -> any value other than the default
        evidence_gated: [v1, v2]  -> exactly those values
        """
        eg = self.evidence_gated
        if eg is True:
            return value != self.default
        if isinstance(eg, list):
            return value in eg
        return False


def _tname(v: Any) -> str:
    return "null" if v is None else type(v).__name__


class Section(object):
    def __init__(self, raw: Dict[str, Any], source: str) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _SECTION_ID_RE.match(raw["id"]):
            raise RegistryError("%s: bad section %r" % (source, raw))
        extra = set(raw) - {"id", "title", "comment", "example_title", "example_comment"}
        if extra:
            raise RegistryError("%s: section %s has unknown field(s) %s" % (source, raw["id"], sorted(extra)))
        self.id = raw["id"]
        self.title = raw.get("title") or raw["id"]
        self.comment = _lines(raw.get("comment"), "section %s comment" % self.id)
        self.example_title = raw.get("example_title") or self.title
        self.example_comment = _lines(raw["example_comment"], "section %s example_comment" % self.id) \
            if "example_comment" in raw else self.comment
        self.source = source


class Registry(object):
    def __init__(self) -> None:
        self.keys = {}  # type: Dict[str, Key]
        self.order = []  # type: List[str]
        self.sections = {}  # type: Dict[str, Section]
        self.section_order = []  # type: List[str]
        self.files = []  # type: List[str]

    def add_file(self, path: str) -> None:
        source = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError as exc:
            raise RegistryError("%s: not valid JSON: %s" % (source, exc))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise RegistryError("%s: expected an object with schema_version 1" % source)
        extra = set(data) - {"schema_version", "phase", "sections", "keys"}
        if extra:
            raise RegistryError("%s: unknown top-level field(s) %s" % (source, sorted(extra)))
        for raw in data.get("sections", []):
            sec = Section(raw, source)
            if sec.id in self.sections:
                raise RegistryError("duplicate section id %r (in %s and %s)" % (sec.id, self.sections[sec.id].source, source))
            self.sections[sec.id] = sec
            self.section_order.append(sec.id)
        for raw in data.get("keys", []):
            if not isinstance(raw, dict):
                raise RegistryError("%s: every key entry must be an object" % source)
            key = Key(raw, source)
            if key.name in self.keys:
                raise RegistryError("duplicate key %r (in %s and %s)" % (key.name, self.keys[key.name].source, source))
            self.keys[key.name] = key
            self.order.append(key.name)
        self.files.append(source)

    def finish(self) -> "Registry":
        """Cross-checks that need every file loaded."""
        for key in self.keys.values():
            if key.section is not None and key.section not in self.sections:
                raise RegistryError("%s: key %s names unknown section %r" % (key.source, key.name, key.section))
            if key.deprecated_by is not None:
                target = self.keys.get(key.deprecated_by)
                if target is None:
                    raise RegistryError("%s: key %s is deprecated_by unknown key %r" % (key.source, key.name, key.deprecated_by))
                if target.deprecated_by is not None:
                    raise RegistryError("%s: deprecated_by chains (%s -> %s -> %s) are not allowed"
                                        % (key.source, key.name, target.name, target.deprecated_by))
            if key.is_wildcard:
                continue
            parts = key.name.split(".")
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                if parent in self.keys and not self.keys[parent].is_wildcard:
                    raise RegistryError("%s: key %s sits under key %s, which is a leaf" % (key.source, key.name, parent))
        return self

    # -- lookup --------------------------------------------------------------
    def get(self, name: str) -> Optional[Key]:
        """Exact key, or the wildcard family that covers `name`."""
        k = self.keys.get(name)
        if k is not None:
            return k
        parent = name.rsplit(".", 1)[0] if "." in name else None
        if parent is not None:
            w = self.keys.get(parent + ".*")
            if w is not None and w.matches(name):
                return w
        return None

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def names(self) -> List[str]:
        return list(self.order)

    def __iter__(self):
        return (self.keys[n] for n in self.order)

    def h6_keys(self) -> List[str]:
        return [n for n in self.order if self.keys[n].h6_protected]

    def evidence_gated_keys(self) -> List[str]:
        return [n for n in self.order if self.keys[n].evidence_gated]

    def is_h6(self, name: str) -> bool:
        k = self.get(name)
        return bool(k and k.h6_protected)

    def defaults(self) -> Dict[str, Any]:
        """Flat {name: default} for concrete (non-wildcard) keys."""
        return {n: self.keys[n].default for n in self.order if self.keys[n].generated}

    def suggest(self, name: str, n: int = 3) -> List[str]:
        import difflib
        pool = [k for k in self.order if self.keys[k].generated]
        return difflib.get_close_matches(name, pool, n=n, cutoff=0.6)

    def keys_in_section(self, section_id: str) -> List[Key]:
        return [self.keys[n] for n in self.order if self.keys[n].section == section_id]


def _sorted_files(directory: str) -> List[str]:
    names = [n for n in os.listdir(directory) if n.endswith(".json")]
    names.sort(key=lambda n: (0 if n == "v1.json" else 1, n))
    return [os.path.join(directory, n) for n in names]


def load_files(paths: Iterable[str]) -> Registry:
    reg = Registry()
    for p in paths:
        reg.add_file(p)
    return reg.finish()


def load_registry(schema_dir: Optional[str] = None) -> Registry:
    """Merge every `schema/*.json`. Raises RegistryError on any inconsistency."""
    d = schema_dir or SCHEMA_DIR
    return load_files(_sorted_files(d))
