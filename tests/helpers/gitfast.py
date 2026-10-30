"""Answer the kit's read-only git questions from the repository's files, not by starting git (task 13a).

Why this exists. A `git` start costs about 4 ms of CPU on a Mac and the machine does not start processes much faster than
~600 a second however many cores there are, so the suite's wall time is its process count divided by that rate. loopctl asks
git hundreds of read-only questions per scenario (is this ref a commit, what is in this tree, who is the merge base), and those
questions are most of the suite's processes. Git stores their answers in plain files: loose objects (zlib) and ref files.
This module reads them directly.

What it does. `install()` wraps `kitlib.gitutil.run_git` (the one door every kit git call goes through). For a call whose
arguments match one of the shapes below, on an ordinary repository, it returns what git would have printed. For anything
else, and for any surprise (a packed object, a symbolic ref that points nowhere, a repository with unusual config), it returns
nothing and the real git runs. So a wrong guess can only cost speed, never change what a test sees; and the only answers it
gives are positive ones, plus the plain "no" of `rev-parse --verify -q` / `show-ref --verify -q` on a ref that exists nowhere git
would look, and of `merge-base --is-ancestor`.

Shapes: rev-parse (--verify [-q] REV with REV a full ref, HEAD or a full object id, optionally ^{commit}/^{object}; --show-toplevel;
--git-common-dir; --git-dir; --abbrev-ref HEAD), symbolic-ref [-q] [--short] NAME, show-ref --verify -q REF,
for-each-ref --format=%(refname) PATTERN..., merge-base A B, merge-base --is-ancestor A B, ls-tree -z [-l] [-r] [--full-tree] REV
[-- PATH...], cat-file --batch, show REV:PATH (a blob).

Writes: only the three that loopctl's state commit makes, `hash-object -w --stdin`, `mktree` and `commit-tree` (a loose object is a
pure function of its content, so there is nothing for the shim to get subtly wrong but the bytes, and those are tested). Refs are
never written here: `update-ref`, `commit`, `merge`, `switch` ... still run git, so compare-and-swap, locks and reflogs are git's.
Environment: TESTS_GITFAST=0 turns the shim off (everything runs real git); =verify runs both and raises if they ever differ (a
commit-tree without a pinned date is compared apart from its clock), which `tests/test_gitfast.py` and the conductor use to prove it.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import subprocess
import sys
import tempfile
import zlib
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

__all__ = ["install", "uninstall", "emulate", "run_argv", "Unsupported", "stats"]

_HEX = re.compile(r"^[0-9a-f]{40}\Z")
_NAME_OK = re.compile(r"^[0-9a-f]{40}(\^)?(:[^\0\n]*)?\Z")
# The only GIT_* variables that do not change what a read prints. Anything else (GIT_DIR, GIT_OBJECT_DIRECTORY, GIT_TRACE ...) -> real git.
_HARMLESS_ENV = frozenset(["GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                           "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_DATE",
                           "GIT_COMMITTER_DATE", "GIT_ASKPASS", "GIT_SSH_COMMAND", "GIT_EDITOR", "GIT_PAGER"])
_MAX_CACHED_BODY = 1 << 20

stats = {"emulated": 0, "fallback": 0}


class Unsupported(Exception):
    """Not a shape this module answers (or the repository is not an ordinary one): run real git."""


# ---------------------------------------------------------------------------------------------------------------------------------
# repository files
# ---------------------------------------------------------------------------------------------------------------------------------
_OBJ_CACHE = {}  # type: Dict[str, Tuple[str, bytes]]       sha -> (type, body); a sha always names the same content
_COMMITS = {}  # type: Dict[str, Tuple[str, Tuple[str, ...]]]  commit sha -> (tree, parents)
_ANCESTORS = {}  # type: Dict[str, FrozenSet[str]]               commit sha -> itself and every ancestor


def _is_hex(s: str) -> bool:
    return bool(_HEX.match(s))


class Repo(object):
    def __init__(self, top: str) -> None:
        self.top = top
        self.gitdir = os.path.join(top, ".git")

    @staticmethod
    def find(cwd: str) -> "Repo":
        if not os.path.isdir(cwd):
            raise Unsupported("no such directory")
        d = os.path.realpath(cwd)
        start = d
        while True:
            g = os.path.join(d, ".git")
            if os.path.isdir(g):
                if start == g or start.startswith(g + os.sep):
                    raise Unsupported("inside the git directory")
                repo = Repo(d)
                repo.check_ordinary()
                return repo
            if os.path.lexists(g):
                raise Unsupported("a .git file (worktree or submodule)")
            if os.path.exists(os.path.join(d, "HEAD")) and os.path.isdir(os.path.join(d, "objects")):
                raise Unsupported("a bare repository")
            parent = os.path.dirname(d)
            if parent == d:
                raise Unsupported("not in a repository")
            d = parent

    def check_ordinary(self) -> None:
        g = self.gitdir
        for rel in ("commondir", "shallow", os.path.join("objects", "info", "alternates"), os.path.join("info", "grafts"),
                    os.path.join("refs", "replace")):
            if os.path.lexists(os.path.join(g, rel)):
                raise Unsupported(rel)
        try:
            with open(os.path.join(g, "config"), encoding="utf-8", errors="replace") as fh:
                cfg = fh.read().lower()
        except OSError:
            raise Unsupported("config")
        for word in ("worktree", "extensions", "objectformat", "reftable", "refstorage", "replace", "graft", "includeif", "[include"):
            if word in cfg:
                raise Unsupported("config " + word)

    # -- refs ------------------------------------------------------------------------------------------------------------------
    def _ref_path(self, name: str) -> str:
        parts = name.split("/")
        if any((not p) or p.startswith(".") or p.endswith(".lock") or "\\" in p or "\0" in p or " " in p or "~" in p or "^" in p
               or ":" in p or "?" in p or "*" in p or "[" in p for p in parts) or ".." in name or "@{" in name:
            raise Unsupported("odd ref name")
        return os.path.join(self.gitdir, *parts)

    def _packed(self) -> Dict[str, str]:
        out = {}  # type: Dict[str, str]
        try:
            with open(os.path.join(self.gitdir, "packed-refs"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line or line[0] in "#^":
                        continue
                    sha, _, ref = line.partition(" ")
                    if not _is_hex(sha):
                        raise Unsupported("packed-refs")
                    out[ref] = sha
        except FileNotFoundError:
            pass
        except OSError:
            raise Unsupported("packed-refs")
        return out

    def read_ref(self, name: str, depth: int = 0) -> Optional[str]:
        """The object id `name` (HEAD or a full ref name) points at, or None when no such ref exists."""
        if depth > 5:
            raise Unsupported("symref depth")
        if name != "HEAD" and not name.startswith("refs/"):
            raise Unsupported("not a full ref name")
        path = self._ref_path(name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return self._packed().get(name)
        except NotADirectoryError:
            return None                                       # a path through a file names no ref
        except IsADirectoryError:
            raise Unsupported("ref is a directory")
        except OSError:
            raise Unsupported("ref unreadable")
        text = text.rstrip("\n")
        if text.startswith("ref: "):
            return self.read_ref(text[5:].strip(), depth + 1)
        if _is_hex(text):
            return text
        raise Unsupported("ref content")

    def exists(self, name: str) -> bool:
        """Whether a ref of exactly this name exists (a path that is a directory or runs through a file is no ref)."""
        try:
            return self.read_ref(name) is not None
        except Unsupported as exc:
            if str(exc) == "ref is a directory":
                return False
            raise

    def head_target(self) -> Optional[str]:
        """The ref HEAD names ('refs/heads/main'), or None when HEAD is detached."""
        try:
            with open(os.path.join(self.gitdir, "HEAD"), encoding="utf-8") as fh:
                text = fh.read().rstrip("\n")
        except OSError:
            raise Unsupported("HEAD")
        if text.startswith("ref: "):
            return text[5:].strip()
        if _is_hex(text):
            return None
        raise Unsupported("HEAD content")

    def all_refs(self) -> Dict[str, str]:
        out = dict(self._packed())
        base = os.path.join(self.gitdir, "refs")
        for dp, _dn, fns in os.walk(base):
            for fn in fns:
                if fn.endswith(".lock"):
                    raise Unsupported("a ref lock exists")
                full = os.path.join(dp, fn)
                name = os.path.relpath(full, self.gitdir).replace(os.sep, "/")
                sha = self.read_ref(name)
                if sha is None:
                    raise Unsupported("dangling symref")
                out[name] = sha
        return out

    # -- objects ---------------------------------------------------------------------------------------------------------------
    def read_object(self, sha: str) -> Tuple[str, bytes]:
        if not _is_hex(sha):
            raise Unsupported("not a full id")
        path = os.path.join(self.gitdir, "objects", sha[:2], sha[2:])
        cached = _OBJ_CACHE.get(sha)
        if cached is not None:
            if not os.path.exists(path):
                raise Unsupported("loose object gone")
            return cached
        try:
            with open(path, "rb") as fh:
                raw = zlib.decompress(fh.read())
        except FileNotFoundError:
            raise Unsupported("object not loose")
        except (OSError, zlib.error):
            raise Unsupported("object unreadable")
        nul = raw.find(b"\0")
        if nul < 0:
            raise Unsupported("object header")
        try:
            typ, size = raw[:nul].decode("ascii").split(" ")
            if int(size) != len(raw) - nul - 1:
                raise ValueError
        except ValueError:
            raise Unsupported("object header")
        body = raw[nul + 1:]
        if typ not in ("blob", "tree", "commit", "tag"):
            raise Unsupported("object type")
        if len(body) <= _MAX_CACHED_BODY:
            _OBJ_CACHE[sha] = (typ, body)
        return typ, body

    def write_object(self, typ: str, body: bytes) -> str:
        """Store a loose object the way git does (zlib, mode 0444, written aside and renamed) and return its id."""
        raw = ("%s %d" % (typ, len(body))).encode("ascii") + b"\0" + body
        sha = hashlib.sha1(raw).hexdigest()
        d = os.path.join(self.gitdir, "objects", sha[:2])
        path = os.path.join(d, sha[2:])
        if not os.path.exists(path):
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="tmp_obj_", dir=d)           # unique: threads (and processes) may store the same object at once
            with os.fdopen(fd, "wb") as fh:
                fh.write(zlib.compress(raw, 1))
            os.chmod(tmp, 0o444)
            try:
                os.rename(tmp, path)
            except OSError:
                if not os.path.exists(path):
                    raise
                os.unlink(tmp)
        if len(body) <= _MAX_CACHED_BODY:
            _OBJ_CACHE[sha] = (typ, body)
        return sha

    def check_writable_config(self) -> None:
        with open(os.path.join(self.gitdir, "config"), encoding="utf-8", errors="replace") as fh:
            cfg = fh.read().lower()
        for word in ("gpg", "signingkey", "encoding", "hooks", "sharedrepository", "compression", "fsync", "safecrlf", "autocrlf"):
            if word in cfg:
                raise Unsupported("config " + word)

    def commit(self, sha: str) -> Tuple[str, Tuple[str, ...]]:
        got = _COMMITS.get(sha)
        if got is not None and os.path.exists(os.path.join(self.gitdir, "objects", sha[:2], sha[2:])):
            return got
        typ, body = self.read_object(sha)
        if typ != "commit":
            raise Unsupported("not a commit")
        tree = ""
        parents = []  # type: List[str]
        for line in body.split(b"\n"):
            if not line:
                break
            if line.startswith(b"tree "):
                tree = line[5:].decode("ascii")
            elif line.startswith(b"parent "):
                parents.append(line[7:].decode("ascii"))
        if not _is_hex(tree) or not all(_is_hex(p) for p in parents):
            raise Unsupported("commit header")
        _COMMITS[sha] = (tree, tuple(parents))
        return _COMMITS[sha]

    def peel(self, sha: str, want: str) -> str:
        """`sha^{want}` for want in ('commit', 'object', 'tree')."""
        typ, body = self.read_object(sha)
        for _ in range(6):
            if want == "object":
                return sha
            if typ == want:
                return sha
            if typ == "tag":
                first = body.split(b"\n", 1)[0]
                if not first.startswith(b"object "):
                    raise Unsupported("tag")
                sha = first[7:].decode("ascii")
                typ, body = self.read_object(sha)
                continue
            if typ == "commit" and want == "tree":
                return self.commit(sha)[0]
            raise Unsupported("peel")
        raise Unsupported("peel depth")

    def ancestors(self, sha: str) -> FrozenSet[str]:
        got = _ANCESTORS.get(sha)
        if got is not None:
            return got
        seen = set()  # type: set
        stack = [sha]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            known = _ANCESTORS.get(c)
            if known is not None:
                seen |= known
                continue
            seen.add(c)
            stack.extend(self.commit(c)[1])
        result = frozenset(seen)
        _ANCESTORS[sha] = result
        return result

    # -- revision names --------------------------------------------------------------------------------------------------------
    def resolve(self, base: str) -> Optional[str]:
        """HEAD, a full ref name or a full object id -> object id (None: the ref does not exist)."""
        if _is_hex(base):
            return base
        if base == "HEAD":
            return self.read_ref("HEAD")
        if base.startswith("refs/"):
            sha = self.read_ref(base)
            if sha is None:
                # git also tries refs/<n>, refs/tags/<n>, refs/heads/<n>, refs/remotes/<n>, refs/remotes/<n>/HEAD for a name it
                # cannot find as given; only a clean miss on all of them is a plain "no such ref"
                for pre in ("refs/", "refs/tags/", "refs/heads/", "refs/remotes/"):
                    if self.exists(pre + base):
                        raise Unsupported("dwim")
                if self.exists("refs/remotes/" + base + "/HEAD"):
                    raise Unsupported("dwim")
            return sha
        raise Unsupported("revision name")

    def rev(self, text: str) -> Optional[str]:
        """BASE[^{commit}][^{object}] as `rev-parse --verify` would print it, None when BASE is a ref that does not exist."""
        base = text
        suffixes = []  # type: List[str]
        for suf in ("^{object}", "^{commit}"):
            while base.endswith(suf):
                suffixes.append(suf)
                base = base[:-len(suf)]
        if "^" in base or "~" in base or ":" in base or "@" in base or base.startswith("-") or not base:
            raise Unsupported("revision syntax")
        sha = self.resolve(base)
        if sha is None:
            return None
        self.read_object(sha)                   # git refuses to print an id whose object it cannot read
        for suf in reversed(suffixes):
            sha = self.peel(sha, "commit" if suf == "^{commit}" else "object")
        return sha

    def path_in_tree(self, tree: str, path: str) -> Tuple[str, str, str]:
        """(mode, type, sha) of `path` below the tree `tree`."""
        parts = path.split("/")
        if not path or any(p in ("", ".", "..") for p in parts):
            raise Unsupported("path")
        cur = tree
        for i, part in enumerate(parts):
            entries = self.tree_entries(cur)
            hit = None
            for mode, name, esha in entries:
                if name == part.encode("utf-8"):
                    hit = (mode, esha)
                    break
            if hit is None:
                raise Unsupported("path not in tree")
            mode, esha = hit
            if i == len(parts) - 1:
                return mode, _mode_type(mode), esha
            if _mode_type(mode) != "tree":
                raise Unsupported("path through a file")
            cur = esha
        raise Unsupported("path")

    def tree_entries(self, sha: str) -> List[Tuple[str, bytes, str]]:
        typ, body = self.read_object(sha)
        if typ != "tree":
            raise Unsupported("not a tree")
        out = []  # type: List[Tuple[str, bytes, str]]
        pos = 0
        n = len(body)
        while pos < n:
            sp = body.index(b" ", pos)
            nul = body.index(b"\0", sp)
            mode = body[pos:sp].decode("ascii")
            name = body[sp + 1:nul]
            esha = body[nul + 1:nul + 21].hex()
            if len(esha) != 40:
                raise Unsupported("tree")
            out.append((mode, name, esha))
            pos = nul + 21
        return out


def _mode_type(mode: str) -> str:
    if mode == "40000":
        return "tree"
    if mode == "160000":
        return "commit"
    if mode in ("100644", "100755", "120000", "100664"):
        return "blob"
    raise Unsupported("mode " + mode)


# ---------------------------------------------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------------------------------------------
Out = Tuple[int, bytes]


def _lines(*items: str) -> bytes:
    return "".join(i + "\n" for i in items).encode("utf-8")


def _rev_parse(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    a = list(args)
    at_top = os.path.realpath(cwd) == repo.top
    if a[:1] == ["--verify"]:
        a = a[1:]
        quiet = False
        if a[:1] == ["-q"]:
            quiet = True
            a = a[1:]
        if len(a) != 1:
            raise Unsupported("rev-parse args")
        sha = repo.rev(a[0])
        if sha is None:
            if not quiet:
                raise Unsupported("a miss without -q prints an error")
            return 1, b""
        return 0, _lines(sha)
    if a == ["--abbrev-ref", "HEAD"]:
        target = repo.head_target()
        if target is None:
            return 0, _lines("HEAD")
        if repo.read_ref("HEAD") is None:
            raise Unsupported("unborn")
        return 0, _lines(_short(repo, target))
    out = []  # type: List[str]
    for flag in a:
        if flag == "--show-toplevel":
            out.append(repo.top)
        elif flag == "--git-common-dir" and at_top:
            out.append(".git")
        elif flag == "--git-dir" and at_top:
            out.append(".git")
        else:
            raise Unsupported("rev-parse flag")
    if not out:
        raise Unsupported("rev-parse")
    return 0, _lines(*out)


def _short(repo: Repo, ref: str) -> str:
    """The shortest unambiguous name git prints for `ref` (only the plain cases; anything ambiguous -> real git)."""
    for pre in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if ref.startswith(pre):
            name = ref[len(pre):]
            if not name:
                raise Unsupported("short")
            for other in ("refs/" + name, "refs/tags/" + name, "refs/heads/" + name, "refs/remotes/" + name, "refs/remotes/" + name + "/HEAD"):
                if other != ref and repo.exists(other):
                    raise Unsupported("ambiguous short name")
            return name
    raise Unsupported("short")


def _symbolic_ref(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    a = list(args)
    quiet = short = False
    while a and a[0] in ("-q", "--short", "--quiet"):
        if a[0] in ("-q", "--quiet"):
            quiet = True
        else:
            short = True
        a = a[1:]
    if len(a) != 1 or not quiet:
        raise Unsupported("symbolic-ref args")
    name = a[0]
    if name == "HEAD":
        target = repo.head_target()
    elif name.startswith("refs/"):
        try:
            with open(repo._ref_path(name), encoding="utf-8") as fh:
                text = fh.read().rstrip("\n")
        except FileNotFoundError:
            if repo._packed().get(name) is not None:
                return 1, b""
            return 1, b""
        except OSError:
            raise Unsupported("symbolic-ref read")
        target = text[5:].strip() if text.startswith("ref: ") else None
    else:
        raise Unsupported("symbolic-ref name")
    if target is None:
        return 1, b""
    return 0, _lines(_short(repo, target) if short else target)


def _show_ref(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    if list(args[:2]) != ["--verify", "-q"] or len(args) != 3 or not args[2].startswith("refs/"):
        raise Unsupported("show-ref args")
    sha = repo.read_ref(args[2])
    if sha is None:
        return 1, b""
    repo.read_object(sha)
    return 0, b""


def _for_each_ref(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    if not args or args[0] != "--format=%(refname)":
        raise Unsupported("for-each-ref format")
    pats = list(args[1:])
    for p in pats:
        if not p.startswith("refs/") or any(c in p for c in "*?[\\") or "//" in p:
            raise Unsupported("for-each-ref pattern")
    refs = repo.all_refs()
    names = []
    for name in sorted(refs):
        if not pats or any(name == p or name.startswith(p.rstrip("/") + "/") or (p.endswith("/") and name.startswith(p)) for p in pats):
            repo.read_object(refs[name])
            names.append(name)
    return 0, _lines(*names)


def _commit_arg(repo: Repo, text: str) -> str:
    sha = repo.resolve(text)
    if sha is None:
        raise Unsupported("unresolved")
    repo.read_object(sha)
    return repo.peel(sha, "commit")


def _merge_base(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    a = list(args)
    if a[:1] == ["--is-ancestor"]:
        if len(a) != 3:
            raise Unsupported("merge-base args")
        x, y = _commit_arg(repo, a[1]), _commit_arg(repo, a[2])
        return (0 if x in repo.ancestors(y) else 1), b""
    if len(a) != 2 or a[0].startswith("-") or a[1].startswith("-"):
        raise Unsupported("merge-base args")
    x, y = _commit_arg(repo, a[0]), _commit_arg(repo, a[1])
    common = repo.ancestors(x) & repo.ancestors(y)
    if not common:
        return 1, b""
    covered = set()  # type: set
    for c in common:
        for p in repo.commit(c)[1]:
            covered |= repo.ancestors(p)
    best = [c for c in common if c not in covered]
    if len(best) != 1:
        raise Unsupported("several best merge bases")
    return 0, _lines(best[0])


def _ls_tree(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    a = list(args)
    zero = long = recursive = False
    while a and a[0].startswith("-") and a[0] != "--":
        f = a.pop(0)
        if f == "-z":
            zero = True
        elif f == "-l":
            long = True
        elif f == "-r":
            recursive = True
        elif f == "--full-tree":
            pass
        else:
            raise Unsupported("ls-tree flag")
    if not zero or not a:
        raise Unsupported("ls-tree needs -z and a revision")
    rev = a.pop(0)
    specs = []  # type: List[str]
    if a:
        if a[0] != "--":
            raise Unsupported("ls-tree args")
        specs = a[1:]
    if rev.startswith("-") or rev.endswith("^") or ":" in rev or "~" in rev or "@" in rev:
        raise Unsupported("ls-tree rev")
    for s in specs:
        if not s or s.startswith(":") or s.startswith("/") or any(c in s for c in "*?[\\") or any(p in ("", ".", "..") for p in s.rstrip("/").split("/")):
            raise Unsupported("pathspec")
    if specs and not recursive:
        raise Unsupported("non-recursive pathspec")
    sha = repo.resolve(rev)
    if sha is None:
        raise Unsupported("ls-tree rev missing")
    tree = repo.peel(sha, "tree") if repo.read_object(sha)[0] != "tree" else sha
    clean = [s.rstrip("/") for s in specs]
    out = []  # type: List[bytes]

    def emit(mode: str, typ: str, esha: str, path: bytes) -> None:
        size = ""
        if long:
            if typ == "blob":
                size = "%7d" % len(repo.read_object(esha)[1])
            else:
                size = "%7s" % "-"
        head = "%06d %s %s" % (int(mode), typ, esha)
        if long:
            head += " " + size
        out.append(head.encode("ascii") + b"\t" + path + b"\0")

    def wanted(path: str) -> bool:
        if not clean:
            return True
        return any(path == s or path.startswith(s + "/") for s in clean)

    def might_contain(path: str) -> bool:
        """A directory worth descending into for the pathspecs (it is inside a spec, or a spec is inside it)."""
        if not clean:
            return True
        return any(path == s or path.startswith(s + "/") or s.startswith(path + "/") for s in clean)

    def walk(tsha: str, prefix: str) -> None:
        for mode, name, esha in repo.tree_entries(tsha):
            try:
                nm = name.decode("utf-8")
            except UnicodeDecodeError:
                raise Unsupported("non-utf8 name")
            full = prefix + nm
            typ = _mode_type(mode)
            if typ == "tree":
                if recursive:
                    if might_contain(full):
                        walk(esha, full + "/")
                elif wanted(full):
                    emit(mode, typ, esha, full.encode("utf-8"))
            elif wanted(full):
                emit(mode, typ, esha, full.encode("utf-8"))

    walk(tree, "")
    return 0, b"".join(out)


def _cat_file(repo: Repo, cwd: str, args: Sequence[str], input_text: Optional[str]) -> Out:
    if list(args) != ["--batch"] or input_text is None:
        raise Unsupported("cat-file args")
    out = []  # type: List[bytes]
    for line in input_text.split("\n"):
        if line == "":
            continue
        if not _NAME_OK.match(line):
            raise Unsupported("cat-file name")
        sha = line[:40]
        rest = line[40:]
        caret = rest.startswith("^")
        if caret:
            rest = rest[1:]
        path = rest[1:] if rest.startswith(":") else None
        if caret:
            typ, _ = repo.read_object(sha)
            if typ != "commit":
                raise Unsupported("caret on a non-commit")
            parents = repo.commit(sha)[1]
            if not parents:
                out.append(line.encode() + b" missing\n")
                continue
            sha = parents[0]
        if path is not None:
            tree = repo.peel(sha, "tree") if repo.read_object(sha)[0] != "tree" else sha
            try:
                _mode, _typ, sha = repo.path_in_tree(tree, path)
            except Unsupported as exc:
                if str(exc) == "path not in tree":
                    out.append(line.encode() + b" missing\n")
                    continue
                raise
        typ, body = repo.read_object(sha)         # an object that is not loose (it may be in a pack, or missing) is git's to answer
        out.append(("%s %s %d\n" % (sha, typ, len(body))).encode("ascii") + body + b"\n")
    return 0, b"".join(out)


def _show(repo: Repo, cwd: str, args: Sequence[str]) -> Out:
    if len(args) != 1 or ":" not in args[0]:
        raise Unsupported("show args")
    rev, _, path = args[0].partition(":")
    if not _is_hex(rev):
        raise Unsupported("show rev")
    sha = rev
    tree = repo.peel(sha, "tree") if repo.read_object(sha)[0] != "tree" else sha
    _mode, typ, esha = repo.path_in_tree(tree, path)
    if typ != "blob" or _mode == "120000":
        raise Unsupported("show of a non-blob")
    return 0, repo.read_object(esha)[1]


# -- writes: pure functions of their input (the object id is a hash of the content), so nothing else can differ ------------------
def _hash_object(repo: Repo, cwd: str, args: Sequence[str], input_text: Optional[str]) -> Out:
    if list(args) != ["-w", "--stdin"] or input_text is None:
        raise Unsupported("hash-object args")
    repo.check_writable_config()
    return 0, _lines(repo.write_object("blob", input_text.encode("utf-8")))


def _mktree(repo: Repo, cwd: str, args: Sequence[str], input_text: Optional[str]) -> Out:
    if list(args) or input_text is None:
        raise Unsupported("mktree args")
    repo.check_writable_config()
    entries = []  # type: List[Tuple[bytes, str, str]]
    for line in input_text.split("\n"):
        if not line:
            continue
        m = re.match(r"^(100644|100755|040000|40000) (blob|tree) ([0-9a-f]{40})\t([^\0\n/]+)\Z", line)
        if not m:
            raise Unsupported("mktree line")
        mode, typ, sha, name = m.groups()
        mode = "40000" if mode in ("040000", "40000") else mode
        if (typ == "tree") != (mode == "40000") or repo.read_object(sha)[0] != typ or name in (".", "..", ".git"):
            raise Unsupported("mktree entry")
        entries.append((name.encode("utf-8"), mode, sha))
    if len(set(e[0] for e in entries)) != len(entries):
        raise Unsupported("duplicate names")
    entries.sort(key=lambda e: e[0] + (b"/" if e[1] == "40000" else b""))
    body = b"".join(m.encode("ascii") + b" " + n + b"\0" + bytes.fromhex(sha) for n, m, sha in entries)
    return 0, _lines(repo.write_object("tree", body))


def _git_date(value: Optional[str]) -> str:
    if value is None:
        now = int(time.time())
        off = time.localtime(now).tm_gmtoff or 0
        sign = "+" if off >= 0 else "-"
        off = abs(off)
        return "%d %s%02d%02d" % (now, sign, off // 3600, (off % 3600) // 60)
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)([+-])(\d\d)(\d\d)\Z", value)
    if not m:
        raise Unsupported("date format")
    import calendar
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    off = (int(m.group(8)) * 3600 + int(m.group(9)) * 60) * (1 if m.group(7) == "+" else -1)
    return "%d %s%s%s" % (calendar.timegm((y, mo, d, h, mi, se)) - off, m.group(7), m.group(8), m.group(9))


def _commit_tree(repo: Repo, cwd: str, args: Sequence[str], env: Dict[str, str]) -> Out:
    a = list(args)
    if len(a) < 3 or a[0].startswith("-") or not _is_hex(a[0]):
        raise Unsupported("commit-tree args")
    tree = a.pop(0)
    parents = []  # type: List[str]
    message = None  # type: Optional[str]
    while a:
        flag = a.pop(0)
        if flag == "-p" and a and _is_hex(a[0]):
            parents.append(a.pop(0))
        elif flag == "-m" and a and message is None:
            message = a.pop(0)
        else:
            raise Unsupported("commit-tree flag")
    if message is None or "\0" in message:
        raise Unsupported("commit-tree message")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        v = env.get(k, "")
        if not v or any(c in v for c in "<>\n\0"):
            raise Unsupported("identity")
    repo.check_writable_config()
    if repo.read_object(tree)[0] != "tree" or any(repo.read_object(p)[0] != "commit" for p in parents):
        raise Unsupported("commit-tree objects")
    lines = ["tree " + tree] + ["parent " + p for p in parents]
    lines.append("author %s <%s> %s" % (env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"], _git_date(env.get("GIT_AUTHOR_DATE"))))
    lines.append("committer %s <%s> %s" % (env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"], _git_date(env.get("GIT_COMMITTER_DATE"))))
    body = ("\n".join(lines) + "\n\n" + message + ("" if message.endswith("\n") else "\n")).encode("utf-8")
    return 0, _lines(repo.write_object("commit", body))


def emulate(args: Sequence[str], cwd: Optional[str], env: Dict[str, str], input_text: Optional[str] = None) -> Tuple[int, bytes]:
    """(returncode, stdout bytes) as git would produce, or raise Unsupported."""
    for k in env:
        if k.startswith("GIT_") and k not in _HARMLESS_ENV:
            raise Unsupported("environment " + k)
    a = list(args)
    while len(a) >= 2 and a[0] == "-c":
        a = a[2:]
    if not a or a[0].startswith("-"):
        raise Unsupported("no subcommand")
    cmd, rest = a[0], a[1:]
    if cmd not in _COMMANDS:
        raise Unsupported(cmd)
    repo = Repo.find(cwd or os.getcwd())
    here = cwd or os.getcwd()
    if cmd == "cat-file":
        return _cat_file(repo, here, rest, input_text)
    if cmd == "hash-object":
        return _hash_object(repo, here, rest, input_text)
    if cmd == "mktree":
        return _mktree(repo, here, rest, input_text)
    if cmd == "commit-tree":
        return _commit_tree(repo, here, rest, env)
    return _COMMANDS[cmd](repo, here, rest)


_COMMANDS = {"rev-parse": _rev_parse, "symbolic-ref": _symbolic_ref, "show-ref": _show_ref, "for-each-ref": _for_each_ref,
             "merge-base": _merge_base, "ls-tree": _ls_tree, "cat-file": None, "show": _show,
             "hash-object": None, "mktree": None, "commit-tree": None}  # type: Dict[str, Any]


# ---------------------------------------------------------------------------------------------------------------------------------
# the wrapper
# ---------------------------------------------------------------------------------------------------------------------------------
_installed = []  # type: List[Callable[..., Any]]
_refcount = [0]


def _mode() -> str:
    return os.environ.get("TESTS_GITFAST", "1")


def _fast_run_git(original: Callable[..., Any]) -> Callable[..., Any]:
    from kitlib import gitutil

    def run_git(args: Sequence[str], cwd: Optional[str] = None, check: bool = False, env: Optional[Dict[str, str]] = None,
                input_text: Optional[str] = None, isolated: bool = False, timeout: Optional[float] = 60,
                pin_hooks: bool = False, binary: bool = False) -> Any:
        mode = _mode()
        if mode != "0":
            try:
                rc, out = emulate(args, cwd, gitutil.git_env(env, isolated=isolated), input_text)
                if not (check and rc != 0):
                    stats["emulated"] += 1
                    proc = subprocess.CompletedProcess(["git"] + list(args), rc, out if binary else out.decode("utf-8", "replace"),
                                                       b"" if binary else "")
                    if mode == "verify":
                        real = original(args, cwd=cwd, check=check, env=env, input_text=input_text, isolated=isolated, timeout=timeout,
                                        pin_hooks=pin_hooks, binary=binary)
                        clocked = "commit-tree" in args and not (env or {}).get("GIT_AUTHOR_DATE")
                        same_out = real.stdout == proc.stdout or (clocked and len(real.stdout.strip()) == len(proc.stdout.strip()))
                        if real.returncode != proc.returncode or not same_out or real.stderr != proc.stderr:
                            sys.stderr.write("GITFAST MISMATCH %r in %r\n" % (list(args), cwd))
                            raise AssertionError("gitfast differs from git for %r (cwd %r):\nfast: %r %r\ngit : %r %r %r" % (
                                list(args), cwd, proc.returncode, proc.stdout[:400], real.returncode, real.stdout[:400], real.stderr[:200]))
                    return proc
            except Unsupported:
                pass
        stats["fallback"] += 1
        return original(args, cwd=cwd, check=check, env=env, input_text=input_text, isolated=isolated, timeout=timeout,
                        pin_hooks=pin_hooks, binary=binary)

    return run_git


def run_argv(args: Sequence[str], cwd: Optional[str], env: Optional[Dict[str, str]], input_text: Optional[str],
             real: Callable[[], Tuple[int, str, str]]) -> Tuple[int, str, str]:
    """For a process seam that starts `git ARGS` itself (the doctor's `run`): (returncode, stdout, stderr) from the shim when it can
    answer, else `real()`. TESTS_GITFAST applies as it does to the wrapped run_git."""
    mode = _mode()
    if mode != "0":
        try:
            rc, out = emulate(args, cwd, dict(os.environ if env is None else env), input_text)
        except Unsupported:
            pass
        else:
            stats["emulated"] += 1
            fast = (rc, out.decode("utf-8", "replace"), "")
            if mode == "verify":
                got = tuple(real())
                if got != fast:
                    sys.stderr.write("GITFAST MISMATCH %r in %r\n" % (list(args), cwd))
                    raise AssertionError("gitfast differs from git for %r (cwd %r):\nfast: %r\ngit : %r" % (list(args), cwd, fast, got))
            return fast
    stats["fallback"] += 1
    return real()


def install() -> None:
    """Route kit git reads through the shim (reference counted: every `install()` needs an `uninstall()`)."""
    from kitlib import gitutil
    _refcount[0] += 1
    if _refcount[0] == 1:
        _installed.append(gitutil.run_git)
        gitutil.run_git = _fast_run_git(gitutil.run_git)


def uninstall() -> None:
    from kitlib import gitutil
    if _refcount[0] <= 0:
        return
    _refcount[0] -= 1
    if _refcount[0] == 0 and _installed:
        gitutil.run_git = _installed.pop()
