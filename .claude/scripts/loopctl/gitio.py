"""Batched git reads: one process for many blobs (loopctl runs git a lot; every call is a fork)."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from kitlib import gitutil

__all__ = ["cat_blobs", "cat_objects", "ls_tree"]

_NAME_RE = __import__("re").compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\^?(?::[A-Za-z0-9._/-]{1,200})?\Z")


def cat_blobs(repo: str, shas: Sequence[str], limit: int = 4 * 1024 * 1024) -> Dict[str, Optional[bytes]]:
    """`git cat-file --batch` for the given object ids. A missing or oversize object maps to None."""
    for s in shas:
        if not gitutil.valid_oid(s):
            raise ValueError("not an object id: %r" % (s,))
    return cat_objects(repo, shas, limit, ("blob",))


def cat_objects(repo: str, names: Sequence[str], limit: int = 4 * 1024 * 1024, types: Sequence[str] = ("blob", "commit")
                ) -> Dict[str, Optional[bytes]]:
    """`git cat-file --batch` for object names: a full object id, `<sha>^` (first parent) or `<sha>[^]:<path>`. Names are
    validated (no newline, no option, no ref name). Missing, oversize or unexpected-type objects map to None."""
    out = {}  # type: Dict[str, Optional[bytes]]
    shas = [s for s in dict.fromkeys(names)]
    for s in shas:
        if not _NAME_RE.match(s):
            raise ValueError("not a plain object name: %r" % (s,))
    if not shas:
        return out
    proc = gitutil.run_git(["cat-file", "--batch"], cwd=repo, isolated=True, pin_hooks=True, input_text="\n".join(shas) + "\n", binary=True)
    buf = proc.stdout
    pos = 0
    for s in shas:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            out[s] = None
            continue
        header = buf[pos:nl].decode("ascii", "replace").split()
        pos = nl + 1
        if len(header) == 2 and header[1] == "missing":
            out[s] = None
            continue
        if len(header) != 3 or not header[2].isdigit():
            out[s] = None
            continue
        size = int(header[2])
        body = buf[pos:pos + size]
        pos += size + 1
        out[s] = body if header[1] in types and size <= limit else None
    return out


def ls_tree(repo: str, rev: str, paths: Sequence[str], recursive: bool = True) -> List[Dict[str, object]]:
    """Entries of `git ls-tree -z -l [-r] --full-tree rev -- paths`: [{mode, type, sha, size, path}]."""
    if rev.startswith("-"):
        raise ValueError("revision may not start with '-'")
    args = ["ls-tree", "-z", "-l", "--full-tree"] + (["-r"] if recursive else []) + [rev, "--"] + list(paths)
    proc = gitutil.run_git(args, cwd=repo, isolated=True, pin_hooks=True, binary=True)
    if proc.returncode != 0:
        raise ValueError("git ls-tree failed")
    out = []  # type: List[Dict[str, object]]
    for rec in proc.stdout.split(b"\0"):
        if not rec:
            continue
        try:
            meta, path = rec.decode("utf-8", "replace").split("\t", 1)
            mode, typ, sha, size = meta.split()
        except ValueError:
            continue
        out.append({"mode": mode, "type": typ, "sha": sha, "size": int(size) if size.isdigit() else -1, "path": path})
    return out
