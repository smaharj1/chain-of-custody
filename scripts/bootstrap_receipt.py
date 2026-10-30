#!/usr/bin/env python3
"""bootstrap_receipt: the committed record of what /bootstrap did (Phase 11a; report 19.7 Q6, F-15).

`.claude/bootstrap-receipt.json` holds, per step, the command, its exit code and the SHA-256 of its output. It is a record for the
reviewer who reads the pull request that installs the kit, not a gate: it catches mistakes and a half-finished bootstrap, and a
person who can write the file can write a receipt that checks out. Entries are chained (each holds a hash of the one before) so that
an edit, a deletion or a reordering in the middle shows up in `check`.

    bootstrap_receipt.py [--repo DIR] init   --mode greenfield|existing [--stack key=value]... [--force]
    bootstrap_receipt.py [--repo DIR] run    --id ID --title TEXT -- python3 .claude/scripts/doctor.py --json
    bootstrap_receipt.py [--repo DIR] record --id ID --title TEXT --command TEXT --exit N [--output-file F | --output-stdin | --no-output]
                                             [--status ok|failed|skipped] [--note TEXT]
    bootstrap_receipt.py [--repo DIR] emit   --id ID --kind human-script|config-plan --path .claude/bootstrap/NAME
    bootstrap_receipt.py [--repo DIR] finish        mark complete (needs the `detect` and `doctor` steps and no unresolved failure)
    bootstrap_receipt.py [--repo DIR] check         validate the file and the chain; exit 1 on any problem
    bootstrap_receipt.py [--repo DIR] show          a readable summary and the commands still yours to run
    bootstrap_receipt.py [--repo DIR] status        {"state": none|in-progress|complete|invalid, ...} as JSON

`run` is the only way this script executes anything, and it runs only the kit's own read-only scripts, by an exact relative path, with
the flags in ALLOWED below, in a scrubbed environment (no token or cloud variable), without a shell, with a timeout. It cannot run
`gh`, `git`, a package manager, a project script, `verify.py`, or `probe_run.py --go`. A command you ran another way goes in with
`record`; the entry says `source: recorded` so a reader knows it was reported, not run here.

Safety, because the receipt is a file in a repository an attacker may have shaped:
  * the receipt path is fixed (`<repo>/.claude/bootstrap-receipt.json`); a symlink, pipe, directory or oversized file there is refused,
    and so is a `.claude` that is a symlink out of the repository;
  * every field of an existing receipt is validated before anything is appended to it: a hostile or damaged receipt is never extended;
    `init --force` moves it aside (by rename, never following a link) and starts a new one;
  * files named on the command line (`--output-file`, `emit --path`) must resolve inside the repository, be regular files, and not be
    symlinks; ids are `[a-z][a-z0-9_.-]{0,39}`; free text is length-limited, stripped of control and invisible characters, and redacted
    for secrets (kitlib.redact) before it is stored;
  * writes are atomic (temp file, fsync, rename) under an exclusive lock file.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio  # noqa: E402
from kitlib import gitutil, jsonio, redact, semver  # noqa: E402

SCHEMA = "bootstrap_receipt.v1"
RECEIPT_REL = os.path.join(".claude", "bootstrap-receipt.json")
EMIT_DIR = os.path.join(".claude", "bootstrap")
MAX_RECEIPT = 1 << 20
MAX_STEPS = 500
MAX_OUTPUT_FILE = 8 << 20
RUN_TIMEOUT = 120
OUTPUT_CAP = 1 << 20

_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,39}\Z")
_HEX = re.compile(r"^[0-9a-f]{64}\Z")
_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_EMIT_PATH = re.compile(r"^\.claude/bootstrap/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
MODES = ("greenfield", "existing")
SOURCES = ("ran", "recorded")
STATUSES = ("ok", "failed", "skipped")
KINDS = ("human-script", "config-plan")
TOP_FIELDS = {"schema", "kit_version", "created_at", "updated_at", "mode", "stack", "steps", "complete"}
STEP_FIELDS = {"type", "id", "title", "at", "command", "exit_code", "output_sha256", "output_bytes", "source", "status", "note", "chain"}
EMIT_FIELDS = {"type", "id", "title", "at", "kind", "path", "sha256", "bytes", "chain"}


class ReceiptError(Exception):
    """A refusal: the message says what and why."""


# ------------------------------------------------------------------------------------------------ text and time

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def stamp(now: Optional[datetime.datetime] = None) -> str:
    return (now or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def printable(text: str) -> bool:
    return all(unicodedata.category(c)[0] != "C" for c in text)


def clean_text(value: Any, what: str, limit: int, allow_empty: bool = True) -> str:
    """Free text for a field: a string, no control or invisible characters, at most `limit`, secrets redacted."""
    if not isinstance(value, str):
        raise ReceiptError("%s must be text" % what)
    if len(value) > limit:
        raise ReceiptError("%s is longer than %d characters" % (what, limit))
    if not printable(value):
        raise ReceiptError("%s contains a control or invisible character" % what)
    value = redact.redact(value).strip()
    if not allow_empty and not value:
        raise ReceiptError("%s is empty" % what)
    return value


def safe_show(text: Any) -> str:
    """Text from a receipt, for a terminal: anything that is not printable becomes '?'."""
    return "".join(c if unicodedata.category(c)[0] != "C" else "?" for c in str(text))


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def chain_seed(doc: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical({"schema": doc.get("schema"), "created_at": doc.get("created_at"), "mode": doc.get("mode"),
                                     "kit_version": doc.get("kit_version")}).encode("utf-8")).hexdigest()


def chain_next(prev: str, entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "chain"}
    return hashlib.sha256((prev + "\n" + canonical(body)).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ paths

def resolve_root(repo: str) -> str:
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        raise ReceiptError("%s is not a directory" % repo)
    top = gitutil.toplevel(repo, isolated=True)
    return os.path.realpath(top) if top else os.path.realpath(repo)


def _claude_dir(root: str) -> str:
    d = os.path.join(root, ".claude")
    try:
        st = os.lstat(d)
    except OSError:
        raise ReceiptError(".claude/ does not exist in this repository: install the kit first")
    if not stat.S_ISDIR(st.st_mode) or os.path.realpath(d) != os.path.join(root, ".claude"):
        raise ReceiptError(".claude is a symlink or not a directory: refusing to write through it")
    return d


def receipt_path(root: str) -> str:
    _claude_dir(root)
    return os.path.join(root, RECEIPT_REL)


def inside(root: str, path: str) -> Optional[str]:
    """The real path of `path` if it is inside `root`, else None. Relative paths are taken from `root`."""
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    full = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(full)
    if real == root or real.startswith(root + os.sep):
        return real
    return None


def regular_file(root: str, path: str, what: str) -> str:
    """The real path of a regular, non-symlink file inside the repository, or a ReceiptError."""
    real = inside(root, path)
    if real is None:
        raise ReceiptError("%s %r is outside the repository" % (what, safe_show(path)[:80]))
    full = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        if stat.S_ISLNK(os.lstat(full).st_mode):
            raise ReceiptError("%s %r is a symlink" % (what, safe_show(path)[:80]))
        if not stat.S_ISREG(os.lstat(real).st_mode):
            raise ReceiptError("%s %r is not a regular file" % (what, safe_show(path)[:80]))
    except OSError:
        raise ReceiptError("%s %r does not exist" % (what, safe_show(path)[:80]))
    return real


# ------------------------------------------------------------------------------------------------ validation

def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(doc: Any) -> List[str]:
    """Every problem found in a receipt document (empty list: valid). Checks shape, limits, characters and the chain."""
    if not isinstance(doc, dict):
        return ["the receipt is not a JSON object"]
    p = []  # type: List[str]
    extra = sorted(str(k) for k in set(doc) - TOP_FIELDS)
    missing = sorted(TOP_FIELDS - set(doc))
    if extra:
        p.append("unknown top-level field(s): %s" % ", ".join(safe_show(e)[:30] for e in extra[:5]))
    if missing:
        p.append("missing top-level field(s): %s" % ", ".join(missing))
    if p:
        return p
    if doc["schema"] != SCHEMA:
        p.append("schema is %r, expected %r" % (safe_show(doc["schema"])[:40], SCHEMA))
    if doc["mode"] not in MODES:
        p.append("mode must be greenfield or existing")
    for f in ("created_at", "updated_at"):
        if not (isinstance(doc[f], str) and _TIME.match(doc[f])):
            p.append("%s is not a UTC timestamp" % f)
    kv = doc["kit_version"]
    if kv is not None and not (isinstance(kv, str) and len(kv) <= 40 and printable(kv)):
        p.append("kit_version is not a short text or null")
    if not isinstance(doc["complete"], bool):
        p.append("complete must be true or false")
    st = doc["stack"]
    if not isinstance(st, dict) or len(st) > 24:
        p.append("stack must be an object of at most 24 entries")
    else:
        for k, v in st.items():
            if not (isinstance(k, str) and _KEY.match(k) and isinstance(v, str) and len(v) <= 120 and printable(v)):
                p.append("stack entry %r is not key: short text" % safe_show(k)[:30])
    steps = doc["steps"]
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        p.append("steps must be a list of at most %d entries" % MAX_STEPS)
        return p
    if p:
        return p
    prev = chain_seed(doc)
    for i, e in enumerate(steps):
        where = "step %d" % (i + 1)
        if not isinstance(e, dict):
            p.append("%s is not an object" % where)
            return p
        kind = e.get("type")
        fields = STEP_FIELDS if kind == "step" else EMIT_FIELDS if kind == "emit" else None
        if fields is None:
            p.append("%s has an unknown type" % where)
            return p
        extra = sorted(str(k) for k in set(e) - fields)
        missing = sorted(f for f in fields if f not in e and not (kind == "step" and f == "note"))
        if extra or missing:
            p.append("%s: unknown field(s) %s, missing field(s) %s" % (where, [safe_show(x)[:20] for x in extra[:3]], missing))
            return p
        if not (isinstance(e["id"], str) and _ID.match(e["id"])):
            p.append("%s: bad id" % where)
        if not (isinstance(e["title"], str) and len(e["title"]) <= 120 and printable(e["title"])):
            p.append("%s: bad title" % where)
        if not (isinstance(e["at"], str) and _TIME.match(e["at"])):
            p.append("%s: bad timestamp" % where)
        if kind == "step":
            if not (isinstance(e["command"], str) and len(e["command"]) <= 300 and printable(e["command"])):
                p.append("%s: bad command" % where)
            if not (_int(e["exit_code"]) and -255 <= e["exit_code"] <= 255):
                p.append("%s: bad exit_code" % where)
            if not (isinstance(e["output_sha256"], str) and _HEX.match(e["output_sha256"])):
                p.append("%s: bad output_sha256" % where)
            if not (_int(e["output_bytes"]) and 0 <= e["output_bytes"] < 1 << 40):
                p.append("%s: bad output_bytes" % where)
            if e["source"] not in SOURCES or e["status"] not in STATUSES:
                p.append("%s: bad source or status" % where)
            if "note" in e and not (isinstance(e["note"], str) and len(e["note"]) <= 200 and printable(e["note"])):
                p.append("%s: bad note" % where)
        else:
            if e["kind"] not in KINDS:
                p.append("%s: bad kind" % where)
            if not (isinstance(e["path"], str) and _EMIT_PATH.match(e["path"]) and ".." not in e["path"]):
                p.append("%s: path is not under .claude/bootstrap/" % where)
            if not (isinstance(e["sha256"], str) and _HEX.match(e["sha256"])):
                p.append("%s: bad sha256" % where)
            if not (_int(e["bytes"]) and 0 <= e["bytes"] < 1 << 40):
                p.append("%s: bad bytes" % where)
        want = chain_next(prev, e)
        if e.get("chain") != want:
            p.append("%s: the chain does not match (an earlier entry was edited, removed or reordered)" % where)
            return p
        prev = want
        if len(p) >= 8:
            break
    return p


# ------------------------------------------------------------------------------------------------ load and save

def load(root: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(document, None) for a valid receipt, (None, None) when there is none, (None, problem) for a hostile or damaged one."""
    path = receipt_path(root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "cannot stat the receipt: %s" % exc.strerror
    if stat.S_ISLNK(st.st_mode):
        return None, "the receipt path is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return None, "the receipt path is not a regular file"
    if st.st_size > MAX_RECEIPT:
        return None, "the receipt is larger than %d bytes" % MAX_RECEIPT
    data, problem = safeio.read_bytes(path, MAX_RECEIPT + 1)
    if data is None:
        return None, "cannot read the receipt: %s" % problem
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, "the receipt is not valid JSON"
    problems = validate(doc)
    if problems:
        return None, "; ".join(problems[:3])
    return doc, None


def status(root: str) -> Dict[str, Any]:
    try:
        doc, problem = load(root)
    except ReceiptError as exc:
        return {"state": "none", "steps": 0, "problem": str(exc)}
    if problem:
        return {"state": "invalid", "steps": 0, "problem": safe_show(problem)[:300]}
    if doc is None:
        return {"state": "none", "steps": 0, "problem": None}
    return {"state": "complete" if doc["complete"] else "in-progress", "steps": len(doc["steps"]), "problem": None}


class Lock(object):
    """An exclusive lock file beside the receipt; held for one read-modify-write."""

    def __init__(self, root: str, wait: float = 5.0) -> None:
        self.path = receipt_path(root) + ".lock"
        self.wait = wait
        self.fd = None  # type: Optional[int]

    def __enter__(self) -> "Lock":
        deadline = time.monotonic() + self.wait
        while True:
            try:
                self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise ReceiptError("another bootstrap_receipt run holds %s; if none is running, delete that file" % os.path.relpath(self.path))
                time.sleep(0.05)
            except OSError as exc:
                raise ReceiptError("cannot take the receipt lock: %s" % exc.strerror)

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


def save(root: str, doc: Dict[str, Any]) -> None:
    problems = validate(doc)
    if problems:  # a bug in this script must not write an invalid receipt
        raise ReceiptError("internal: refusing to write an invalid receipt (%s)" % problems[0])
    jsonio.atomic_write_json(receipt_path(root), doc)


def kit_version(root: str) -> Optional[str]:
    text = safeio.read_text(os.path.join(root, ".claude", "KIT_VERSION"), 8192)
    if not text:
        return None
    try:
        v = semver.parse_kit_version_file(text).get("version")
    except ValueError:
        return None
    return v if isinstance(v, str) and len(v) <= 40 and printable(v) else None


def _require(root: str) -> Dict[str, Any]:
    doc, problem = load(root)
    if problem:
        raise ReceiptError("the existing receipt is not usable (%s). Nothing was added. Look at it, then `init --force` to start again "
                           "(the old file is renamed, not deleted)" % safe_show(problem)[:200])
    if doc is None:
        raise ReceiptError("there is no receipt yet: run `init` first")
    return doc


# ------------------------------------------------------------------------------------------------ operations

def init(root: str, mode: str, stack: Dict[str, str], force: bool = False, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    if mode not in MODES:
        raise ReceiptError("--mode must be greenfield or existing")
    if len(stack) > 24:
        raise ReceiptError("at most 24 --stack entries")
    clean_stack = {}  # type: Dict[str, str]
    for k, v in stack.items():
        if not _KEY.match(k):
            raise ReceiptError("--stack key %r must be lower-case letters, digits and _" % safe_show(k)[:30])
        clean_stack[k] = clean_text(v, "--stack " + k, 120)
    with Lock(root):
        path = receipt_path(root)
        existing = os.path.lexists(path)
        if existing and not force:
            raise ReceiptError("a receipt already exists. Use `show` or `check`; `init --force` renames it and starts a new one")
        if existing:
            base = path[:-len(".json")] + "." + stamp(now).replace(":", "").replace("-", "") + ".old"
            old = base + ".json"
            n = 0
            while os.path.lexists(old):
                n += 1
                old = "%s.%d.json" % (base, n)
            os.rename(path, old)  # renames the entry itself; a symlink is moved, never followed
        t = stamp(now)
        doc = {"schema": SCHEMA, "kit_version": kit_version(root), "created_at": t, "updated_at": t, "mode": mode, "stack": clean_stack,
               "steps": [], "complete": False}  # type: Dict[str, Any]
        save(root, doc)
    return doc


def _append(root: str, entry: Dict[str, Any], now: Optional[datetime.datetime]) -> Dict[str, Any]:
    with Lock(root):
        doc = _require(root)
        if len(doc["steps"]) >= MAX_STEPS:
            raise ReceiptError("the receipt already has %d entries" % MAX_STEPS)
        prev = doc["steps"][-1]["chain"] if doc["steps"] else chain_seed(doc)
        entry["at"] = stamp(now)
        entry["chain"] = chain_next(prev, entry)
        doc["steps"].append(entry)
        doc["updated_at"] = entry["at"]
        doc["complete"] = False  # new work reopens a finished receipt
        save(root, doc)
    return entry


def add_step(root: str, step_id: str, title: str, command: str, exit_code: int, output: bytes, source: str,
             status_: Optional[str] = None, note: str = "", now: Optional[datetime.datetime] = None, output_bytes: Optional[int] = None) -> Dict[str, Any]:
    if not _ID.match(step_id or ""):
        raise ReceiptError("--id must be a lower-case id like `doctor` or `labels-dry-run`")
    if not _int(exit_code) or not -255 <= exit_code <= 255:
        raise ReceiptError("--exit must be an integer between -255 and 255")
    if source not in SOURCES:
        raise ReceiptError("bad source")
    st = status_ or ("ok" if exit_code == 0 else "failed")
    if st not in STATUSES:
        raise ReceiptError("--status must be ok, failed or skipped")
    entry = {"type": "step", "id": step_id, "title": clean_text(title, "--title", 120, allow_empty=False),
             "command": clean_text(command, "--command", 300, allow_empty=False), "exit_code": exit_code,
             "output_sha256": hashlib.sha256(output).hexdigest(), "output_bytes": len(output) if output_bytes is None else output_bytes,
             "source": source, "status": st}  # type: Dict[str, Any]
    if note:
        entry["note"] = clean_text(note, "--note", 200)
    return _append(root, entry, now)


def add_emit(root: str, step_id: str, kind: str, rel_path: str, title: str = "", now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    if not _ID.match(step_id or ""):
        raise ReceiptError("--id must be a lower-case id")
    if kind not in KINDS:
        raise ReceiptError("--kind must be human-script or config-plan")
    if not isinstance(rel_path, str) or not _EMIT_PATH.match(rel_path) or ".." in rel_path:
        raise ReceiptError("--path must be .claude/bootstrap/<name> (letters, digits, '.', '_' and '-')")
    real = regular_file(root, rel_path, "--path")
    bdir = os.path.realpath(os.path.join(root, EMIT_DIR))
    if not real.startswith(bdir + os.sep):
        raise ReceiptError("--path resolves outside .claude/bootstrap/")
    data, problem = safeio.read_bytes(real, MAX_OUTPUT_FILE)
    if data is None:
        raise ReceiptError("cannot read %s: %s" % (rel_path, problem))
    entry = {"type": "emit", "id": step_id, "title": clean_text(title or ("emitted " + os.path.basename(rel_path)), "--title", 120, allow_empty=False),
             "kind": kind, "path": rel_path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}  # type: Dict[str, Any]
    return _append(root, entry, now)


def unresolved_failures(doc: Dict[str, Any]) -> List[str]:
    last = {}  # type: Dict[str, str]
    for e in doc["steps"]:
        if e["type"] == "step":
            last[e["id"]] = e["status"]
    return sorted(i for i, s in last.items() if s == "failed")


def finish(root: str, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    with Lock(root):
        doc = _require(root)
        ids = {e["id"] for e in doc["steps"] if e["type"] == "step" and e["status"] != "failed"}
        absent = [i for i in ("detect", "doctor") if i not in ids]
        if absent:
            raise ReceiptError("cannot finish: no successful step with id %s" % ", ".join(absent))
        bad = unresolved_failures(doc)
        if bad:
            raise ReceiptError("cannot finish: the last attempt of %s failed" % ", ".join(bad))
        doc["complete"] = True
        doc["updated_at"] = stamp(now)
        save(root, doc)
    return doc


# ------------------------------------------------------------------------------------------------ run (restricted)

class Rule(object):
    def __init__(self, bare: Sequence[str] = (), values: Optional[Dict[str, str]] = None, positional: str = "none",
                 first: Sequence[str] = (), ok_codes: Sequence[int] = (0,)) -> None:
        self.bare, self.values, self.positional, self.first, self.ok_codes = set(bare), values or {}, positional, tuple(first), tuple(ok_codes)


ALLOWED = {
    ".claude/scripts/doctor.py": Rule(bare=("--json", "--credentials", "--deploy-surface", "--level"), values={"--repo": "path", "--gh-fixture": "path"},
                                      positional="level", ok_codes=(0, 1)),
    ".claude/scripts/ci_lint.py": Rule(bare=("--json",), values={"--repo": "path"}, positional="path", ok_codes=(0, 1)),
    ".claude/scripts/bootstrap_detect.py": Rule(bare=("--json",), values={"--repo": "path"}),
    ".claude/scripts/probe_run.py": Rule(bare=("--no-config", "--json"), values={"--repo": "path", "--cmd": "text"}, ok_codes=(0, 3)),
    ".claude/scripts/guards/wiring.py": Rule(bare=("--check",)),
    ".claude/scripts/loopctl": Rule(bare=("--json",), first=("doctor", "validate", "status"), ok_codes=(0, 1)),
}
_LEVEL = re.compile(r"^L[0-5]\Z")


def check_run_argv(root: str, argv: Sequence[str]) -> Tuple[List[str], Rule]:
    """The exact argv to execute (python -I -B <script> ...) and its rule, or a ReceiptError naming why it is refused."""
    if len(argv) < 2 or argv[0] not in ("python3", "python"):
        raise ReceiptError("run executes only `python3 <kit script> ...`; record other commands with `record`")
    script = argv[1]
    rule = ALLOWED.get(script)
    if rule is None:
        raise ReceiptError("run does not execute %r. Allowed: %s" % (safe_show(script)[:60], ", ".join(sorted(ALLOWED))))
    real = inside(root, script)
    if real is None or not (os.path.isfile(real) or os.path.isfile(os.path.join(real, "__main__.py"))):
        raise ReceiptError("%s is not in this repository (is the kit installed?)" % script)
    out = [sys.executable, "-I", "-B", real]
    args = list(argv[2:])
    i = 0
    first_done = not rule.first
    while i < len(args):
        a = args[i]
        if not isinstance(a, str) or "\0" in a or not printable(a) or len(a) > 300:
            raise ReceiptError("an argument has a control character or is too long")
        if not first_done:
            if a not in rule.first:
                raise ReceiptError("%s runs only these subcommands: %s" % (script, ", ".join(rule.first)))
            first_done = True
        elif a in rule.bare:
            pass
        elif a in rule.values:
            i += 1
            if i >= len(args):
                raise ReceiptError("%s needs a value" % a)
            v = args[i]
            if rule.values[a] == "path":
                if inside(root, v) is None:
                    raise ReceiptError("%s %r is outside the repository" % (a, safe_show(v)[:60]))
            elif not v or len(v) > 300 or not printable(v) or "\0" in v:
                raise ReceiptError("%s needs a value" % a)
            out += [a, v]
            i += 1
            continue
        elif a.startswith("-"):
            raise ReceiptError("flag %s is not allowed for %s" % (safe_show(a)[:40], script))
        elif rule.positional == "level" and _LEVEL.match(a):
            pass
        elif rule.positional == "path":
            if inside(root, a) is None:
                raise ReceiptError("path %r is outside the repository" % safe_show(a)[:60])
        else:
            raise ReceiptError("argument %r is not allowed for %s" % (safe_show(a)[:40], script))
        out.append(a)
        i += 1
    if rule.first and not first_done:
        raise ReceiptError("%s needs a subcommand: %s" % (script, ", ".join(rule.first)))
    return out, rule


def scrubbed_env(env: Dict[str, str]) -> Dict[str, str]:
    keep = {}  # type: Dict[str, str]
    for k, v in env.items():
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT") \
                or k.startswith("KIT_") or k.startswith("LC_"):
            keep[k] = v
    keep["PYTHONDONTWRITEBYTECODE"] = "1"
    keep["GIT_TERMINAL_PROMPT"] = "0"
    return keep


def run_and_record(root: str, step_id: str, title: str, argv: Sequence[str], env: Optional[Dict[str, str]] = None,
                   now: Optional[datetime.datetime] = None, timeout: float = RUN_TIMEOUT) -> Dict[str, Any]:
    real_argv, rule = check_run_argv(root, argv)
    proc = subprocess.Popen(real_argv, cwd=root, env=scrubbed_env(dict(os.environ if env is None else env)), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        rc, timed_out = 124, True
    blob = (out or b"") + (err or b"")
    total = len(blob)
    blob = blob[:OUTPUT_CAP]
    status_ = "ok" if rc in rule.ok_codes and not timed_out else "failed"
    entry = add_step(root, step_id, title, " ".join(shlex.quote(a) for a in argv), rc, blob, "ran", status_,
                     "timed out after %ds" % int(timeout) if timed_out else "", now, output_bytes=total)
    entry["_output"] = blob  # for the CLI to show; never stored
    return entry


# ------------------------------------------------------------------------------------------------ display

def next_steps(doc: Dict[str, Any]) -> List[str]:
    out = []  # type: List[str]
    for e in doc["steps"]:
        if e["type"] != "emit":
            continue
        if e["kind"] == "human-script":
            out.append("sh %s            (a dry run: it prints every command), read it, then: sh %s --apply   [from a plain terminal, not a Claude session]" % (e["path"], e["path"]))
        else:
            out.append("python3 .claude/scripts/bootstrap_config.py apply %s            (a dry run), then add --write   [from a plain terminal]" % e["path"])
    return out


def render(doc: Dict[str, Any]) -> str:
    lines = ["bootstrap receipt (%s): %s, mode %s, kit %s, %s" % (doc["schema"], "COMPLETE" if doc["complete"] else "in progress", doc["mode"],
                                                                  safe_show(doc["kit_version"]), doc["updated_at"])]
    for k, v in sorted(doc["stack"].items()):
        lines.append("  stack %s: %s" % (k, safe_show(v)))
    for i, e in enumerate(doc["steps"], 1):
        if e["type"] == "step":
            lines.append("  %2d. [%-7s] %-18s exit %-3d %s  %s" % (i, e["status"], e["id"], e["exit_code"], e["output_sha256"][:12], safe_show(e["title"])))
        else:
            lines.append("  %2d. [emitted] %-18s %s  sha256 %s  %s" % (i, e["id"], e["path"], e["sha256"][:12], e["kind"]))
    ns = next_steps(doc)
    if ns:
        lines.append("Still yours to do:")
        lines += ["  - " + s for s in ns]
    bad = unresolved_failures(doc)
    if bad:
        lines.append("Unresolved failures: " + ", ".join(bad))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ CLI

def parse_stack(items: Sequence[str]) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for it in items:
        if "=" not in it:
            raise ReceiptError("--stack wants key=value, got %r" % safe_show(it)[:40])
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def read_output(root: str, args: argparse.Namespace) -> bytes:
    if args.no_output:
        return b""
    if args.output_stdin:
        return sys.stdin.buffer.read(MAX_OUTPUT_FILE)
    if args.output_file:
        real = regular_file(root, args.output_file, "--output-file")
        data, problem = safeio.read_bytes(real, MAX_OUTPUT_FILE)
        if data is None:
            raise ReceiptError("cannot read --output-file: %s" % problem)
        return data
    raise ReceiptError("say where the output is: --output-file, --output-stdin or --no-output")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bootstrap_receipt.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="repository (default: current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init")
    a.add_argument("--mode", required=True, choices=MODES)
    a.add_argument("--stack", action="append", default=[], metavar="key=value")
    a.add_argument("--force", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--id", required=True)
    r.add_argument("--title", required=True)
    r.add_argument("argv", nargs=argparse.REMAINDER)
    c = sub.add_parser("record")
    c.add_argument("--id", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--command", required=True)
    c.add_argument("--exit", dest="exit_code", type=int, required=True)
    c.add_argument("--output-file")
    c.add_argument("--output-stdin", action="store_true")
    c.add_argument("--no-output", action="store_true")
    c.add_argument("--status", choices=STATUSES)
    c.add_argument("--note", default="")
    e = sub.add_parser("emit")
    e.add_argument("--id", required=True)
    e.add_argument("--kind", required=True, choices=KINDS)
    e.add_argument("--path", required=True)
    e.add_argument("--title", default="")
    for name in ("finish", "check", "show", "status"):
        sub.add_parser(name)
    return ap


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = resolve_root(args.repo)
        if args.cmd == "init":
            doc = init(root, args.mode, parse_stack(args.stack), args.force)
            print("bootstrap_receipt: started %s (mode %s)" % (RECEIPT_REL, doc["mode"]))
        elif args.cmd == "run":
            cmd = list(args.argv)
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            entry = run_and_record(root, args.id, args.title, cmd, env)
            sys.stdout.write(entry.pop("_output").decode("utf-8", "replace"))
            print("\nbootstrap_receipt: step %s %s (exit %d, output sha256 %s)" % (entry["id"], entry["status"], entry["exit_code"], entry["output_sha256"][:12]))
            return 0 if entry["status"] != "failed" else 1
        elif args.cmd == "record":
            entry = add_step(root, args.id, args.title, args.command, args.exit_code, read_output(root, args), "recorded", args.status, args.note)
            print("bootstrap_receipt: recorded %s (%s, reported, not run here)" % (entry["id"], entry["status"]))
        elif args.cmd == "emit":
            entry = add_emit(root, args.id, args.kind, args.path, args.title)
            print("bootstrap_receipt: recorded %s sha256 %s (%d bytes)" % (entry["path"], entry["sha256"], entry["bytes"]))
        elif args.cmd == "finish":
            finish(root)
            print("bootstrap_receipt: complete")
        elif args.cmd == "check":
            doc, problem = load(root)
            if problem:
                sys.stderr.write("bootstrap_receipt: INVALID: %s\n" % safe_show(problem)[:400])
                return 1
            if doc is None:
                sys.stderr.write("bootstrap_receipt: no receipt\n")
                return 1
            print("bootstrap_receipt: ok (%d entries, %s)" % (len(doc["steps"]), "complete" if doc["complete"] else "in progress"))
        elif args.cmd == "show":
            doc, problem = load(root)
            if problem or doc is None:
                sys.stderr.write("bootstrap_receipt: %s\n" % (safe_show(problem)[:400] if problem else "no receipt"))
                return 1
            print(render(doc))
        elif args.cmd == "status":
            print(json.dumps(status(root), sort_keys=True))
        return 0
    except ReceiptError as exc:
        sys.stderr.write("bootstrap_receipt: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
