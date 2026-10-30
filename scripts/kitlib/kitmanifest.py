"""kitmanifest: recognise the files of an old copy install, so migrate.py can move a project to the plugin (IMP-3, report CO-33).

Before 2.0 the kit was installed by copying `.claude/` (and, in v1, the root adapters `AGENTS.md`, `.cursor/rules/`, `skills/`) into
a project. The kit is a plugin now and a project's `.claude/` holds only the project's own files. To take the old copy out without
losing anything the person changed, migrate.py needs the SHA-256 of every file the old version shipped:

  * a v2 copy install carries them in `.claude/KIT_MANIFEST` (written by the old build, schema `kit-manifest/1`);
  * a v1.x install has none, so the kit ships the v1.2.0 tree (`migrate/v1.2.0.tar`, `git archive v1.2.0`) and the hashes come from it.

A file whose content still has the old hash is the kit's and is removed; any other content is the person's edit. The class says what
the file was for:

    kit      the kit's own file (agents, scripts, skills, hooks, kit context, spec, templates, the adapters)
    seed     the project's from the start (CLAUDE.md, loop.config.md, the domain primers, rules/): always kept
    frozen   `.claude/settings.json` and `settings.local.json`: kept; migrate removes only the kit's hook entries

Standard library only, Python 3.9.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from typing import Any, Dict, Optional

__all__ = ["SCHEMA", "MANIFEST_REL", "VERSION_REL", "CLASS_KIT", "CLASS_SEED", "CLASS_FROZEN", "PRIMERS", "classify", "sha256_bytes",
           "build_manifest", "dumps", "loads", "parse_version", "read_tar_tree"]

SCHEMA = "kit-manifest/1"
# where an old copy install kept its bookkeeping, inside the project
MANIFEST_REL = ".claude/KIT_MANIFEST"
VERSION_REL = ".claude/KIT_VERSION"
BOOKKEEPING = (MANIFEST_REL, VERSION_REL)

CLASS_KIT = "kit"
CLASS_SEED = "seed"
CLASS_FROZEN = "frozen"

# Project primers: the kit ships them as templates (seed/.claude/context/), the Architect and the Tech Lead derive them from the code.
PRIMERS = ("admin-app", "app", "architect", "backend", "database", "infra", "local-dev", "shared-frontend")

_SEED_EXACT = {".claude/CLAUDE.md", ".claude/loop.config.md"}
_FROZEN_EXACT = {".claude/settings.json", ".claude/settings.local.json"}


def classify(rel: str) -> str:
    """The class of a project-relative path of a copy install (forward slashes)."""
    if rel in _FROZEN_EXACT:
        return CLASS_FROZEN
    if rel in _SEED_EXACT or rel.startswith(".claude/rules/"):
        return CLASS_SEED
    m = re.match(r"^\.claude/context/([A-Za-z0-9_-]+)\.md$", rel)
    if m and m.group(1) in PRIMERS:
        return CLASS_SEED
    return CLASS_KIT


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(version: str, files: Dict[str, bytes]) -> Dict[str, Any]:
    """The manifest dict for `files` (project-relative path -> content). Bookkeeping files are left out."""
    entries = {}   # type: Dict[str, Dict[str, str]]
    for rel in sorted(files):
        if rel in BOOKKEEPING:
            continue
        entries[rel] = {"sha256": sha256_bytes(files[rel]), "class": classify(rel)}
    return {"schema": SCHEMA, "version": version, "files": entries}


def dumps(manifest: Dict[str, Any]) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def loads(text: str) -> Dict[str, Any]:
    """Parse and check a manifest. Raises ValueError with the problem."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError("KIT_MANIFEST is not JSON: %s" % exc)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("KIT_MANIFEST has schema %r, expected %r" % (doc.get("schema") if isinstance(doc, dict) else None, SCHEMA))
    files = doc.get("files")
    if not isinstance(files, dict) or not isinstance(doc.get("version"), str):
        raise ValueError("KIT_MANIFEST needs a string `version` and a `files` object")
    for rel, ent in files.items():
        if (not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/") or "\\" in rel or not isinstance(ent, dict)
                or not re.match(r"^[0-9a-f]{64}$", str(ent.get("sha256", ""))) or ent.get("class") not in (CLASS_KIT, CLASS_SEED, CLASS_FROZEN)):
            raise ValueError("KIT_MANIFEST entry %r is malformed" % (rel if isinstance(rel, str) else "?"))
    return doc


def parse_version(text: str) -> Optional[str]:
    """`1.2.0 (2026-07-30)` -> `1.2.0`. None when the text does not start with a version."""
    m = re.match(r"^\s*v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", text or "")
    return m.group(1) if m else None


def read_tar_tree(path_or_bytes: Any) -> Dict[str, bytes]:
    """The regular files of a tar archive as {path: content}. Refuses absolute paths, `..` and links, so a hostile archive cannot
    name a file outside the tree."""
    fh = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb")
    out = {}   # type: Dict[str, bytes]
    with fh:
        with tarfile.open(fileobj=fh, mode="r:*") as tf:
            for m in tf:
                if m.isdir():
                    continue
                name = m.name
                if not m.isreg() or name.startswith("/") or ".." in name.split("/") or "\\" in name:
                    raise ValueError("archive member %r is not a plain relative file" % name)
                f = tf.extractfile(m)
                out[name] = f.read() if f is not None else b""
    return out
