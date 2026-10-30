"""kitmanifest: the shape of `KIT_MANIFEST` and the rules for who owns which installed file (Phase 9; IMP-3, report CO-33).

A copy install is `.claude/` (plus, optionally, the root adapters and the docs the kit's prose points at) copied into a project.
`tools/build-dist.py` writes `.claude/KIT_MANIFEST`: one entry per shipped file with its SHA-256 and its class. `migrate.py` reads
the manifest of the kit being installed, the manifest of the version already installed (derived from a shipped base archive such
as `.claude/migrate/v1.2.0.tar`), and the project's files, and decides per file what to do. This module is the one place that
knows the format and the classes, so the builder and the migrator cannot disagree.

Classes (who may change the installed file):

    kit      the kit owns it. An update replaces it when the project has not touched it, and three-way merges it when the project has.
    seed     the project owns it once installed (CLAUDE.md, loop.config.md, the domain primers). Added when absent, replaced only when
             the project's copy is byte-identical to the base (it was never filled in), otherwise left alone.
    frozen   a guardrail file (H6): `.claude/settings.json` and `settings.local.json`. Added when absent, never changed.

`KIT_VERSION` and `KIT_MANIFEST` are bookkeeping: they are not listed in the manifest and migrate.py writes them last.
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
           "build_manifest", "dumps", "loads", "parse_version", "read_tar_tree", "tree_manifest"]

SCHEMA = "kit-manifest/1"
MANIFEST_REL = ".claude/KIT_MANIFEST"
VERSION_REL = ".claude/KIT_VERSION"
BOOKKEEPING = (MANIFEST_REL, VERSION_REL)

CLASS_KIT = "kit"
CLASS_SEED = "seed"
CLASS_FROZEN = "frozen"

# Project primers: the kit ships them as templates, the Architect and the Tech Lead derive them from the project's code.
PRIMERS = ("admin-app", "app", "architect", "backend", "database", "infra", "local-dev", "shared-frontend")

_SEED_EXACT = {".claude/CLAUDE.md", ".claude/loop.config.md"}
_FROZEN_EXACT = {".claude/settings.json", ".claude/settings.local.json"}


def classify(rel: str) -> str:
    """The class of an install-relative path (forward slashes)."""
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
    """The manifest dict for `files` (install-relative path -> content). Bookkeeping files are left out."""
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
    name a file outside the tree. File modes are not kept (migrate.py copies the mode from the live source tree)."""
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


def tree_manifest(version: str, tree: Dict[str, bytes]) -> Dict[str, Any]:
    """Manifest of an archive's tree, for the base version. Same shape as the shipped one."""
    return build_manifest(version, tree)
