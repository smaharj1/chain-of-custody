"""Render `loop.config.md` and `examples/loop.config.md` from the registry (IMP-11).

Inputs: the merged registry (`schema/*.json`) and hand-written templates in
`schema/templates/`:

    header.md / notes.md                   text before the yaml fence / after it (full config)
    header.example.md / notes.example.md   the same for examples/loop.config.md

The yaml body is generated: sections in registry order, each key as
`name: value   # first doc line`, further doc lines aligned beneath it. Dotted names
(`gates.g1`) render as a nested mapping. The example file lists only keys that carry an
`example` entry in the registry, using the example value and example docs.

`tools/gen-config.py --write|--check` is the command-line front end.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from . import miniyaml
from .registry import Key, Registry, SCHEMA_DIR, RegistryError

__all__ = ["render", "render_body", "TEMPLATES_DIR", "COMMENT_COL"]

TEMPLATES_DIR = os.path.join(SCHEMA_DIR, "templates")
COMMENT_COL = 33


def _comment(line: str) -> str:
    return ("# " + line).rstrip() if line else "#"


def _key_lines(key: Key, value, doc: List[str], after_doc: List[str], depth: int) -> List[str]:
    leaf = key.name.rsplit(".", 1)[-1]
    text = "  " * depth + leaf + ": " + miniyaml.dump_scalar(value)
    pad = COMMENT_COL - len(text) if len(text) <= COMMENT_COL - 2 else 3
    out = []  # type: List[str]
    docs = list(doc)
    if docs and docs[0] != "":
        out.append((text + " " * pad + "# " + docs[0]).rstrip())
        rest = docs[1:]
    else:
        out.append(text)
        rest = docs[1:] if docs else []
    for line in rest:
        out.append(" " * COMMENT_COL + _comment(line))
    for line in after_doc:
        out.append(_comment(line))
    return out


def render_body(reg: Registry, example: bool = False) -> str:
    blocks = []  # type: List[str]
    emitted_parents = set()  # type: set
    for sid in reg.section_order:
        sec = reg.sections[sid]
        keys = [k for k in reg.keys_in_section(sid) if k.generated and (k.example is not None or not example)]
        if not keys:
            continue
        title = sec.example_title if example else sec.title
        comment = sec.example_comment if example else sec.comment
        lines = ["# --- %s ---" % title]
        lines += [_comment(c) for c in comment]
        open_parents = []  # type: List[str]
        for key in keys:
            parts = key.name.split(".")
            parents = parts[:-1]
            if parents != open_parents:
                # emit any parent mappings not already open
                common = 0
                while common < min(len(parents), len(open_parents)) and parents[common] == open_parents[common]:
                    common += 1
                for d in range(common, len(parents)):
                    path = ".".join(parents[:d + 1])
                    if path in emitted_parents:
                        raise RegistryError("keys under %r are not contiguous in the registry" % path)
                    emitted_parents.add(path)
                    lines.append("  " * d + parents[d] + ":")
                open_parents = parents
            if example:
                ex = key.example
                lines += _key_lines(key, ex["value"], ex["doc"], ex["after_doc"], len(parents))
            else:
                lines += _key_lines(key, key.default, list(key.doc), list(key.after_doc), len(parents))
        blocks.append("\n".join(lines))
    for k in reg:
        if k.generated and k.section is None:
            raise RegistryError("%s: key %s has no section, so it cannot be rendered" % (k.source, k.name))
    return "\n\n".join(blocks) + "\n"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def render(reg: Registry, example: bool = False, templates_dir: Optional[str] = None) -> str:
    d = templates_dir or TEMPLATES_DIR
    suffix = ".example" if example else ""
    header = _read(os.path.join(d, "header%s.md" % suffix))
    notes = _read(os.path.join(d, "notes%s.md" % suffix))
    if not header.endswith("```yaml\n"):
        raise RegistryError("header%s.md must end with the opening ```yaml fence line" % suffix)
    return header + render_body(reg, example) + "```\n" + notes
