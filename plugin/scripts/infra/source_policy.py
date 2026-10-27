#!/usr/bin/env python3
"""source_policy: the pre-plan check on Terraform / OpenTofu source (report §8.1 Layer 5, RC-3, IMP-25).

`terraform plan` executes provider and module code, so a check on the plan JSON alone comes too
late. This runs BEFORE `init` and `plan`, reads the source with the kit's HCL scanner (`hcl.py`,
no terraform binary), and refuses:

  data.external / data.http        data sources that run a program or fetch from the network
  data.remote-state-http           a `terraform_remote_state` read over the `http` backend (a network fetch)
  expr.file-outside-tree           file(), templatefile() and friends (and data.local_file) reading an absolute,
                                   `~` or `..` path: the plan runs with credentials in its environment
  provisioner.*                    local-exec, remote-exec and every other provisioner
  module.*                         a module `source` that is not pinned: registry without an exact
                                   `version`, git without a full-commit `ref`, an archive without a
                                   checksum, a local path that leaves the repository, a non-literal
  provider.new                     a provider (declared or implied by a resource type) that the base
                                   ref does not already use and the policy file does not allow
  provider.banned                  the `external` and `http` providers in any form
  provider.endpoint-override       `endpoint(s)`, `*_endpoint`, proxy and CA-bundle settings in a
                                   provider, backend or remote-state block: a way to send the
                                   short-lived credentials to a host the PR chose
  provider.exec                    an `exec` block in a provider (kubernetes, helm): it runs a command
  backend.changed                  a backend type the base ref does not use and the policy does not allow
  tree.*                           symlinks, a committed `.terraform/` or `terraform.d/` directory, provider binaries,
                                   terragrunt configuration
                                   (its hooks and generate blocks run commands: not judged here)
  parse.unparsed                   a file the scanner could not read: manual review, never a pass

Run it from the BASE ref's copy of this script, against the PR checkout as data:

    python3 base/.claude/scripts/infra/source_policy.py --root pr --base base \\
        --policy base/.claude/infra-policy.json
    # after `terraform init -backend=false`, before any plan:
    python3 base/.claude/scripts/infra/source_policy.py --root pr --base base --policy ... --post-init

`--base` is the checked-out base ref (what `main` already deploys). Without `--base`, new
providers and backends cannot be judged and the run fails unless you say `--initial` (the first
infrastructure PR in a repository: a human reviews every provider and backend by hand).

The policy file is read from the base ref, so a pull request cannot widen its own allowance.
Exit 0: nothing found. Exit 1: an error finding or an unparsed file. Exit 2: usage. `--json`
prints `source_policy.v1`.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import stat
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra import hcl  # noqa: E402

ERROR, WARN = "error", "warn"
TF_SUFFIXES = (".tf", ".tofu", ".tf.json", ".tofu.json", ".tftest.hcl", ".tftest.json")
TERRAGRUNT_RE = re.compile(r"(?:^|\.)terragrunt(?:\.stack)?\.hcl(?:\.json)?$", re.I)
FILE_FUNCS = ("file", "filebase64", "filebase64sha256", "filebase64sha512", "filemd5", "filesha1", "filesha256", "filesha512", "templatefile")
LOCAL_FILE_TYPES = ("local_file", "local_sensitive_file")
BANNED_DATA = ("external", "http")
BANNED_PROVIDER_TYPES = ("external", "http")
LOCK_NAME = ".terraform.lock.hcl"
CLI_CONFIG_NAMES = (".terraformrc", "terraform.rc", ".tofurc", "tofu.rc")
POLICY_KEYS = {"schema", "allowed_providers", "allowed_backends", "allow_version_ranges", "allow_git_tag_refs"}
_FULL_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_EXACT_VERSION = re.compile(r"^=?\s*\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_CHECKSUM = re.compile(r"^(?:sha256:[0-9a-fA-F]{64}|sha512:[0-9a-fA-F]{128})$")
_REG_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_DEFAULT_REGISTRY = "registry.terraform.io"


class Finding(object):
    __slots__ = ("rule", "severity", "file", "line", "message")

    def __init__(self, rule: str, severity: str, file: str, line: int, message: str) -> None:
        self.rule, self.severity, self.file, self.line, self.message = rule, severity, file, line, message

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "file": self.file, "line": self.line, "message": self.message}

    def __str__(self) -> str:
        loc = self.file + (":%d" % self.line if self.line else "")
        return "%s: %s: %s: %s" % (self.severity.upper(), self.rule, loc, self.message)


class Policy(object):
    def __init__(self, allowed_providers: Sequence[str] = (), allowed_backends: Sequence[str] = (),
                 allow_version_ranges: bool = False, allow_git_tag_refs: bool = False) -> None:
        self.allowed_providers = {normalize_source(p) for p in allowed_providers}
        self.allowed_backends = {b.lower() for b in allowed_backends}
        self.allow_version_ranges = allow_version_ranges
        self.allow_git_tag_refs = allow_git_tag_refs


def load_policy(path: Optional[str]) -> Tuple[Policy, List[Finding]]:
    """The policy file. Unknown keys and wrong types are errors: a typo must not silently widen the allowance."""
    if not path:
        return Policy(), []
    bad = []  # type: List[Finding]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return Policy(), [Finding("policy.invalid", ERROR, path, 0, "cannot read the policy file (%s); nothing is allowed" % exc)]
    if not isinstance(data, dict):
        return Policy(), [Finding("policy.invalid", ERROR, path, 0, "the policy file is not a JSON object")]
    for k in data:
        if k not in POLICY_KEYS:
            bad.append(Finding("policy.invalid", ERROR, path, 0, "unknown key %r" % k))
    lists = {}  # type: Dict[str, List[str]]
    for k in ("allowed_providers", "allowed_backends"):
        v = data.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            bad.append(Finding("policy.invalid", ERROR, path, 0, "%s must be a list of non-empty strings" % k))
            v = []
        lists[k] = v
    flags = {}
    for k in ("allow_version_ranges", "allow_git_tag_refs"):
        v = data.get(k, False)
        if not isinstance(v, bool):
            bad.append(Finding("policy.invalid", ERROR, path, 0, "%s must be true or false" % k))
            v = False
        flags[k] = v
    return Policy(lists["allowed_providers"], lists["allowed_backends"], flags["allow_version_ranges"], flags["allow_git_tag_refs"]), bad


# --- provider source addresses ---------------------------------------------------------------------------

def normalize_source(src: str) -> str:
    """`aws` / `hashicorp/aws` / `registry.terraform.io/hashicorp/aws` -> `registry.terraform.io/hashicorp/aws` (lower-case)."""
    s = src.strip().lower()
    parts = s.split("/")
    if len(parts) == 1:
        return "%s/hashicorp/%s" % (_DEFAULT_REGISTRY, parts[0])
    if len(parts) == 2:
        return "%s/%s/%s" % (_DEFAULT_REGISTRY, parts[0], parts[1])
    return s


def source_type(norm: str) -> str:
    return norm.rsplit("/", 1)[-1]


# --- module sources --------------------------------------------------------------------------------------

def classify_source(src: str) -> Tuple[str, str]:
    """(kind, rest). kinds: local, git, hg, archive, registry, file, unknown, invalid."""
    if not src or src != src.strip():
        return "invalid", src
    if src.startswith("./") or src.startswith("../"):
        return "local", src
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)::(.*)$", src)
    if m:
        scheme, rest = m.group(1).lower(), m.group(2)
        if scheme == "git":
            return "git", rest
        if scheme == "hg":
            return "hg", rest
        if scheme in ("s3", "gcs", "http", "https"):
            return "archive", rest
        if scheme == "file":
            return "file", rest
        return "unknown", src
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", src)
    if m:
        scheme = m.group(1).lower()
        if scheme in ("http", "https"):
            return "archive", src
        if scheme in ("ssh", "git"):
            return "git", src
        if scheme == "file":
            return "file", src
        return "unknown", src
    if re.match(r"^(?:[\w.-]+@)?[\w.-]+:[^/\\]", src):
        return "git", src
    head = src.split("/", 1)[0].lower()
    if head in ("github.com", "bitbucket.org"):
        return "git", src
    addr = src.split("//", 1)[0]
    parts = addr.split("/")
    if len(parts) == 3 or (len(parts) == 4 and "." in parts[0]):
        body = parts[1:] if len(parts) == 4 else parts
        if all(_REG_PART.match(p) for p in body):
            return "registry", src
    return "unknown", src


def _query(rest: str) -> List[Tuple[str, str]]:
    if "?" not in rest:
        return []
    return parse_qsl(rest.split("?", 1)[1], keep_blank_values=True)


def module_source_findings(src: Optional[str], version: Optional[str], version_present: bool, policy: Policy,
                           file_rel: str, root_rel_dir: str, strict: bool) -> List[Tuple[str, str, str]]:
    """(rule, severity, message) for one module call. `root_rel_dir` is the calling file's directory relative to the scan root.
    `strict` False (downloaded third-party modules) downgrades the version-pinning rules to warnings."""
    sev = ERROR if strict else WARN
    out = []  # type: List[Tuple[str, str, str]]
    if src is None:
        return [("module.source-nonliteral", ERROR, "module source is not a plain string literal (a variable, template or expression): the source cannot be checked")]
    kind, rest = classify_source(src)
    if kind == "invalid":
        return [("module.source-unknown", ERROR, "module source %r is empty or has surrounding whitespace" % src)]
    if kind == "unknown":
        return [("module.source-unknown", ERROR, "module source %r is not a local path, registry address, git, or archive address the policy understands" % src)]
    if kind == "file":
        return [("module.local-escape", ERROR, "module source %r reads from the local filesystem outside the repository" % src)]
    if kind == "local":
        target = posixpath.normpath(posixpath.join(root_rel_dir or ".", src))
        if target == ".." or target.startswith("../") or posixpath.isabs(target):
            return [("module.local-escape", ERROR, "local module path %r leaves the scanned tree" % src)]
        return []
    if kind == "registry":
        if not version_present:
            return [("module.registry-no-version", sev, "registry module %r has no `version`: the newest release is used" % src)]
        if version is None:
            return [("module.registry-nonliteral", ERROR, "registry module %r has a `version` that is not a string literal" % src)]
        if not _EXACT_VERSION.match(version):
            if policy.allow_version_ranges:
                return []
            return [("module.registry-range", sev, "registry module %r has version %r, a range: a release published tomorrow would be used (pin an exact version)" % (src, version))]
        return []
    q = _query(rest)
    if kind == "git":
        refs = [v for k, v in q if k == "ref"]
        if not refs:
            return [("module.git-unpinned", sev, "git module %r has no `?ref=`: the default branch is used" % src)]
        if len(refs) > 1:
            return [("module.git-unpinned", ERROR, "git module %r has more than one `ref`" % src)]
        if _FULL_SHA.match(refs[0]):
            return []
        if policy.allow_git_tag_refs and refs[0] and not refs[0].lower() in ("head", "main", "master"):
            return []
        return [("module.git-tag", sev, "git module %r pins ref %r, a branch or tag that can move (pin a full commit SHA)" % (src, refs[0]))]
    if kind == "hg":
        revs = [v for k, v in q if k == "rev"]
        if len(revs) == 1 and _FULL_SHA.match(revs[0]):
            return []
        return [("module.archive-unpinned", sev, "hg module %r is not pinned to a full revision" % src)]
    # archive (http, https, s3, gcs)
    sums = [v for k, v in q if k == "checksum"]
    if len(sums) == 1 and _CHECKSUM.match(sums[0]):
        return []
    return [("module.archive-unpinned", sev, "archive module %r has no `checksum=sha256:...`: whatever the URL serves is used" % src)]


# --- per-file model --------------------------------------------------------------------------------------

class Model(object):
    """What one file declares, reduced to the facts the rules need."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.dir = posixpath.dirname(rel)
        self.required = []  # type: List[Tuple[str, Optional[str], int]]       (local name, normalized source or None, line)
        self.used = []  # type: List[Tuple[str, int]]                           provider local names used
        self.datas = []  # type: List[Tuple[str, int]]
        self.provisioners = []  # type: List[Tuple[str, int]]
        self.modules = []  # type: List[Tuple[str, Optional[str], Optional[str], bool, int]]  (name, source, version, version_present, line)
        self.backends = []  # type: List[Tuple[str, int, str]]                  (type, line, fingerprint)
        self.endpoint_hits = []  # type: List[Tuple[str, str, int]]            (block, name, line)
        self.exec_hits = []  # type: List[Tuple[str, int]]
        self.file_hits = []  # type: List[Tuple[str, str, int]]                 (rule, message, line)


def _prefix(type_name: str) -> str:
    return type_name.split("_", 1)[0]


def _fingerprint(block: hcl.Block) -> str:
    parts = []  # type: List[str]
    for it in block.body:
        if isinstance(it, hcl.Block):
            parts.append("{%s:%s}" % (it.type, _fingerprint(it)))
        elif it.from_json:
            parts.append("%s=%s" % (it.name, json.dumps(it.value, sort_keys=True)))
        else:
            parts.append("%s=%s" % (it.name, " ".join(t.text for t in (it.toks or []))))
    return "|".join(sorted(parts))


def _endpointish(name: str) -> bool:
    n = name.lower()
    if n.startswith("use_"):
        return False
    return (n in ("endpoint", "endpoints", "http_proxy", "https_proxy", "no_proxy", "proxy", "proxy_url", "custom_ca_bundle", "ca_bundle")
            or n.endswith("_endpoint") or n.endswith("_endpoints"))


def extract(items: Sequence[Any], rel: str) -> Model:
    m = Model(rel)
    for blk in hcl.walk_blocks(items):
        t = blk.type
        if t == "provisioner":
            m.provisioners.append((blk.labels[0] if blk.labels else "(unlabelled)", blk.line))
        elif t == "dynamic" and blk.labels and blk.labels[0] == "provisioner":
            m.provisioners.append(("dynamic", blk.line))
        elif t == "required_providers":
            for it in blk.body:
                if isinstance(it, hcl.Attr):
                    lit = hcl.literal_of(it)
                    obj = hcl.object_of(it)
                    if lit is not None:
                        m.required.append((it.name, normalize_source(it.name), it.line))   # legacy "version" shorthand: implied hashicorp/<name>
                    elif obj is not None:
                        s = obj.get("source")
                        if "source" not in obj:
                            m.required.append((it.name, normalize_source(it.name), it.line))
                        elif isinstance(s, str) and s.strip():
                            m.required.append((it.name, normalize_source(s), it.line))
                        else:
                            m.required.append((it.name, None, it.line))
                    else:
                        m.required.append((it.name, None, it.line))
        elif t == "provider" and blk.labels:
            m.used.append((blk.labels[0], blk.line))
            for name, line in hcl.names_in(blk):
                if _endpointish(name):
                    m.endpoint_hits.append(("provider \"%s\"" % blk.labels[0], name, line))
                if name == "exec":
                    m.exec_hits.append((blk.labels[0], line))
        elif t in ("resource", "data") and blk.labels:
            if t == "data":
                m.datas.append((blk.labels[0], blk.line))
            m.used.append((_prefix(blk.labels[0]), blk.line))
            for a in blk.attrs("provider"):
                if a.toks:
                    first = a.toks[0]
                    if first.kind == "ID" and first.val:
                        m.used.append((first.val, a.line))
                elif a.from_json and isinstance(a.value, str):
                    m.used.append((a.value.split(".")[0], a.line))
            if t == "data" and blk.labels[0] == "terraform_remote_state":
                for name, line in hcl.names_in(blk):
                    if _endpointish(name):
                        m.endpoint_hits.append(("data \"terraform_remote_state\"", name, line))
                backend = [hcl.literal_of(a) for a in blk.attrs("backend")]
                if not backend or backend[0] is None or backend[0].lower() == "http":
                    m.datas.append(("terraform_remote_state/http", blk.line))
            if t == "data" and blk.labels[0] in LOCAL_FILE_TYPES:
                for a in blk.attrs("filename"):
                    m.file_hits.extend(_file_arg_findings(_arg_of_attr(a), a.line, "data \"%s\" filename" % blk.labels[0]))
        elif t == "module" and blk.labels:
            srcs = blk.attrs("source")
            vers = blk.attrs("version")
            # a repeated `source` or `version` is invalid Terraform; if the scanner and terraform disagree about which one wins, the
            # scanner must not be the one that picked the harmless value, so a repeat reads as "not a literal"
            src = hcl.literal_of(srcs[0]) if len(srcs) == 1 else None
            ver = hcl.literal_of(vers[0]) if len(vers) == 1 else None
            m.modules.append((blk.labels[0], src, ver, bool(vers), blk.line))
        elif t == "backend" and blk.labels:
            m.backends.append((blk.labels[0].lower(), blk.line, _fingerprint(blk)))
            for name, line in hcl.names_in(blk):
                if _endpointish(name):
                    m.endpoint_hits.append(("backend \"%s\"" % blk.labels[0], name, line))
        elif t == "cloud":
            m.backends.append(("cloud", blk.line, _fingerprint(blk)))
            for name, line in hcl.names_in(blk):
                if _endpointish(name):
                    m.endpoint_hits.append(("cloud", name, line))
    for blk in hcl.walk_blocks(items):
        for it in blk.body:
            if isinstance(it, hcl.Attr):
                if it.from_json:
                    for text in _json_strings(it.value):
                        m.file_hits.extend(_file_calls_in_text(text, it.line))
                else:
                    m.file_hits.extend(_file_calls_in_tokens(it.toks or [], it.line))
    for it in items:
        if isinstance(it, hcl.Attr) and not it.from_json:
            m.file_hits.extend(_file_calls_in_tokens(it.toks or [], it.line))
    return m


# --- file(), templatefile() and friends -----------------------------------------------------------------------

def _json_strings(v: Any, depth: int = 0) -> Iterable[str]:
    if depth > hcl.MAX_DEPTH:
        return
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            for t in _json_strings(x, depth + 1):
                yield t
    elif isinstance(v, list):
        for x in v:
            for t in _json_strings(x, depth + 1):
                yield t


def _arg_of_attr(a: hcl.Attr) -> List[hcl.Tok]:
    return list(a.toks or [])


def _path_problem(inner: str, has_interp: bool) -> Optional[str]:
    """None if the path stays under the module; else a short reason. `inner` is the string's source text without its quotes."""
    rest = inner
    for prefix in ("${path.module}/", "${path.root}/", "${path.cwd}/"):
        if inner.startswith(prefix):
            rest = inner[len(prefix):]
            break
    else:
        if has_interp:
            return "dynamic"
    if rest.startswith("/") or rest.startswith("~") or rest.startswith("\\") or re.match(r"^[A-Za-z]:", rest):
        return "absolute"
    if ".." in re.split(r"[/\\]", rest):
        return "parent"
    if "${" in rest:
        return "dynamic-tail"
    return None


def _file_arg_findings(arg: List[hcl.Tok], line: int, what: str) -> List[Tuple[str, str, int]]:
    if len(arg) == 1 and arg[0].kind == "STR":
        raw = arg[0].text
        inner = raw[1:-1] if len(raw) >= 2 else raw
        problem = _path_problem(inner, arg[0].val is None)
        if problem in ("absolute", "parent"):
            return [("expr.file-outside-tree", "%s reads %r, a path outside the module tree: the plan job's credentials are in its environment, and files such as /proc/self/environ expose them" % (what, inner[:80]), line)]
        if problem and problem.startswith("dynamic"):
            return [("expr.file-dynamic", "%s reads a path computed at plan time (%r): it can be set to leave the tree" % (what, inner[:80]), line)]
        return []
    if not arg:
        return []
    return [("expr.file-dynamic", "%s reads a path computed at plan time: it can be set to leave the tree" % what, line)]


def _file_calls_in_tokens(toks: Sequence[hcl.Tok], line: int) -> List[Tuple[str, str, int]]:
    out = []  # type: List[Tuple[str, str, int]]
    for i, t in enumerate(toks[:-1]):
        if t.kind == "ID" and t.val in FILE_FUNCS and toks[i + 1].kind == "P" and toks[i + 1].val == "(":
            depth = 0
            arg = []  # type: List[hcl.Tok]
            for u in toks[i + 2:]:
                if u.kind == "P" and u.val in ("(", "[", "{"):
                    depth += 1
                elif u.kind == "P" and u.val in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and u.kind == "P" and u.val == ",":
                    break
                if u.kind != "NL":
                    arg.append(u)
            out.extend(_file_arg_findings(arg, t.line or line, "%s()" % t.val))
    return out


def _file_calls_in_text(text: str, line: int) -> List[Tuple[str, str, int]]:
    out = []  # type: List[Tuple[str, str, int]]
    for m in re.finditer(r"\b(%s)\(\s*(\"((?:[^\"\\]|\\.)*)\")?" % "|".join(FILE_FUNCS), text):
        if m.group(2) is None:
            out.append(("expr.file-dynamic", "%s() reads a path computed at plan time" % m.group(1), line))
            continue
        inner = m.group(3).replace('\\"', '"')
        problem = _path_problem(inner, "${" in inner)
        if problem in ("absolute", "parent"):
            out.append(("expr.file-outside-tree", "%s() reads %r, outside the module tree" % (m.group(1), inner[:80]), line))
        elif problem:
            out.append(("expr.file-dynamic", "%s() reads a path computed at plan time" % m.group(1), line))
    return out


# --- tree reading ----------------------------------------------------------------------------------------

class ScannedFile(object):
    def __init__(self, rel: str, model: Optional[Model], error: Optional[str]) -> None:
        self.rel, self.model, self.error = rel, model, error


class Tree(object):
    def __init__(self, root: str) -> None:
        self.root = root
        self.files = []  # type: List[ScannedFile]
        self.findings = []  # type: List[Finding]
        self.lock_providers = set()  # type: Set[str]
        self.lock_bytes = None  # type: Optional[bytes]
        self.module_caches = []  # type: List[str]


def _read_text(path: str) -> str:
    size = os.path.getsize(path)
    if size > hcl.MAX_BYTES:
        raise hcl.HclError("file is larger than %d bytes" % hcl.MAX_BYTES)
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise hcl.HclError("not valid UTF-8")


def scan_tree(root: str, post_init: bool = False) -> Tree:
    tree = Tree(root)
    if not os.path.isdir(root):
        tree.findings.append(Finding("tree.missing", ERROR, root, 0, "directory does not exist"))
        return tree
    def on_error(exc: OSError) -> None:
        tree.findings.append(Finding("tree.unreadable", ERROR, str(getattr(exc, "filename", root)), 0, "cannot list the directory (%s): manual review" % exc.strerror))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            rel = posixpath.join(rel_dir, d) if rel_dir else d
            if os.path.islink(full):
                tree.findings.append(Finding("tree.symlink", ERROR, rel, 0, "a symbolic link is followed by terraform but cannot be checked here: manual review"))
                continue
            if d == ".git":
                continue
            if d in ("terraform.d", ".terraform.d"):
                tree.findings.append(Finding("tree.plugin-dir", ERROR, rel, 0, "a terraform.d directory can hold provider binaries that terraform would use as a local mirror: remove it"))
                continue
            if d == ".terraform":
                if post_init:
                    modules = os.path.join(full, "modules")
                    if os.path.isdir(modules) and not os.path.islink(modules):
                        tree.module_caches.append(modules)
                else:
                    tree.findings.append(Finding("tree.dot-terraform", ERROR, rel, 0,
                                                 "a committed .terraform directory (provider binaries and module caches) would be used by init: remove it"))
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = posixpath.join(rel_dir, f) if rel_dir else f
            if os.path.islink(full):
                tree.findings.append(Finding("tree.symlink", ERROR, rel, 0, "a symbolic link is followed by terraform but cannot be checked here: manual review"))
                continue
            low = f.lower()
            try:
                regular = stat.S_ISREG(os.lstat(full).st_mode)
            except OSError:
                regular = False
            if not regular:
                if low.endswith(TF_SUFFIXES) or f == LOCK_NAME:
                    tree.findings.append(Finding("tree.special-file", ERROR, rel, 0, "not a regular file: manual review"))
                continue
            if f.lower().startswith("terraform-provider-"):
                tree.findings.append(Finding("tree.provider-binary", ERROR, rel, 0, "a provider binary in the source tree"))
                continue
            if TERRAGRUNT_RE.search(f):
                tree.findings.append(Finding("tree.terragrunt", ERROR, rel, 0, "terragrunt configuration (hooks and generate blocks run commands and write files) cannot be judged by this check: manual review"))
                continue
            if f in CLI_CONFIG_NAMES:
                tree.findings.append(Finding("tree.cli-config", WARN, rel, 0, "a terraform CLI configuration file in the tree (provider_installation overrides): not read by default, but do not point TF_CLI_CONFIG_FILE at it"))
            if f == LOCK_NAME:
                try:
                    text = _read_text(full)
                    tree.lock_bytes = text.encode("utf-8")
                    for it in hcl.walk_blocks(hcl.parse_native(text)):
                        if it.type == "provider" and it.labels:
                            tree.lock_providers.add(normalize_source(it.labels[0]))
                except hcl.HclError as exc:
                    tree.files.append(ScannedFile(rel, None, str(exc)))
                continue
            if not low.endswith(TF_SUFFIXES):
                continue
            try:
                text = _read_text(full)
                items = hcl.parse_file_text(text, f)
                tree.files.append(ScannedFile(rel, extract(items, rel), None))
            except hcl.HclError as exc:
                tree.files.append(ScannedFile(rel, None, str(exc)))
            except RecursionError:
                tree.files.append(ScannedFile(rel, None, "nesting too deep"))
            except OSError as exc:
                tree.files.append(ScannedFile(rel, None, "unreadable (%s)" % exc))
            except Exception as exc:  # a scanner bug must read as "could not read this file", never as a pass
                tree.files.append(ScannedFile(rel, None, "internal scanner error (%s: %s)" % (type(exc).__name__, exc)))
    return tree


def providers_used(tree: Tree) -> Dict[str, Tuple[str, int]]:
    """normalized provider source -> (first file, line). Local names resolve through the same directory's required_providers."""
    by_dir = {}  # type: Dict[str, Dict[str, Optional[str]]]
    for sf in tree.files:
        if sf.model:
            d = by_dir.setdefault(sf.model.dir, {})
            for local, src, _ in sf.model.required:
                d[local] = src
    out = {}  # type: Dict[str, Tuple[str, int]]
    for sf in tree.files:
        if not sf.model:
            continue
        mp = by_dir.get(sf.model.dir, {})
        for local, line in sf.model.used:
            if local == "terraform":
                continue          # the built-in provider
            src = mp.get(local, normalize_source(local))
            if src:
                out.setdefault(src, (sf.rel, line))
        for local, src, line in sf.model.required:
            if src:
                out.setdefault(src, (sf.rel, line))
    return out


def backends_used(tree: Tree) -> Dict[str, Tuple[str, str, int]]:
    """backend type -> (fingerprint of its settings, file, line)."""
    out = {}  # type: Dict[str, Tuple[str, str, int]]
    for sf in tree.files:
        if sf.model:
            for typ, line, fp in sf.model.backends:
                out[typ] = (fp, sf.rel, line)
    return out


# --- the rules -------------------------------------------------------------------------------------------

def check_tree(tree: Tree, base: Optional[Tree], policy: Policy, initial: bool, strict: bool = True,
               top: bool = True) -> List[Finding]:
    out = list(tree.findings)
    for sf in tree.files:
        if sf.error:
            out.append(Finding("parse.unparsed", ERROR, sf.rel, 0, "unparsed: manual review (%s)" % sf.error))
            continue
        m = sf.model
        assert m is not None
        for typ, line in m.datas:
            if typ in BANNED_DATA:
                out.append(Finding("data.%s" % typ, ERROR, sf.rel, line,
                                   "data source %r %s" % (typ, "runs a program during plan" if typ == "external" else "fetches from the network during plan")))
            elif typ == "terraform_remote_state/http":
                out.append(Finding("data.remote-state-http", ERROR, sf.rel, line, "terraform_remote_state over the http backend (or a backend that is not a literal) fetches from a URL during plan"))
        for rule, msg, line in m.file_hits:
            out.append(Finding(rule, ERROR if rule == "expr.file-outside-tree" else WARN, sf.rel, line, msg))
        for label, line in m.provisioners:
            rule = "provisioner.%s" % (label if label in ("local-exec", "remote-exec") else "other")
            out.append(Finding(rule, ERROR, sf.rel, line, "provisioner %r runs a command; provisioners are not allowed" % label))
        for name, src, ver, ver_present, line in m.modules:
            for rule, sev, msg in module_source_findings(src, ver, ver_present, policy, sf.rel, m.dir, strict):
                out.append(Finding(rule, sev, sf.rel, line, "module %r: %s" % (name, msg)))
        for blk, name, line in m.endpoint_hits:
            out.append(Finding("provider.endpoint-override", ERROR, sf.rel, line,
                               "%s sets %r: the plan role's credentials could be sent to a host this PR chose" % (blk, name)))
        for prov, line in m.exec_hits:
            out.append(Finding("provider.exec", ERROR, sf.rel, line, "provider %r has an exec block, which runs a command during plan" % prov))
        for local, src, line in m.required:
            if src is None:
                out.append(Finding("provider.non-literal-source", ERROR, sf.rel, line, "required_providers entry %r has a source that is not a string literal" % local))
    used = providers_used(tree)
    for src, (f, line) in sorted(used.items()):
        if source_type(src) in BANNED_PROVIDER_TYPES:
            out.append(Finding("provider.banned", ERROR, f, line, "the %r provider runs programs or fetches from the network during plan" % src))
    backends = backends_used(tree)
    if base is None:
        if not initial:
            out.append(Finding("provider.no-baseline", ERROR, tree.root, 0,
                               "no --base given, so new providers and backends cannot be judged; pass the base ref's checkout, or --initial for the first infrastructure PR"))
        else:
            for src, (f, line) in sorted(used.items()):
                out.append(Finding("provider.initial", WARN, f, line, "initial run: provider %s needs a human to approve it" % src))
    else:
        known = set(providers_used(base)) | base.lock_providers | policy.allowed_providers
        for src, (f, line) in sorted(used.items()):
            if src not in known and source_type(src) not in BANNED_PROVIDER_TYPES:
                out.append(Finding("provider.new", ERROR, f, line,
                                   "provider %s is not used by the base ref and is not allowed by the policy file: a human adds it in a separate change" % src))
        base_backends = backends_used(base)
        for typ in sorted(backends if top else []):
            fp, f, line = backends[typ]
            if typ not in base_backends and typ not in policy.allowed_backends:
                out.append(Finding("backend.changed", ERROR, f, line,
                                   "backend %r is new: the state location is changed only by a human, outside the PR path" % typ))
            elif typ in base_backends and base_backends[typ][0] != fp:
                out.append(Finding("backend.attrs-changed", WARN, f, line, "backend %r settings differ from the base ref: check the bucket, key and region" % typ))
        if top and base.lock_bytes is not None and tree.lock_bytes is not None and base.lock_bytes != tree.lock_bytes:
            out.append(Finding("lock.changed", WARN, LOCK_NAME, 0, "%s differs from the base ref: provider versions or hashes changed (an H3 path: a code owner reviews it)" % LOCK_NAME))
        for sf in (base.files if top else []):
            if sf.error:
                out.append(Finding("base.unparsed", ERROR, sf.rel, 0, "the base ref has a file that cannot be read (%s): the baseline is unreliable" % sf.error))
    return out


def run(root: str, base_root: Optional[str], policy_path: Optional[str], post_init: bool, initial: bool) -> List[Finding]:
    policy, out = load_policy(policy_path)
    base = scan_tree(base_root) if base_root else None
    if base_root and base is not None and base.findings and any(f.rule == "tree.missing" for f in base.findings):
        out.extend(base.findings)
        base = None
    tree = scan_tree(root, post_init=post_init)
    out.extend(check_tree(tree, base, policy, initial))
    if post_init:
        if not tree.module_caches:
            out.append(Finding("tree.no-module-cache", WARN, root, 0, "--post-init found no .terraform/modules directory (no modules, or init has not run)"))
        for cache in tree.module_caches:
            sub = scan_tree(cache)
            rel_cache = os.path.relpath(cache, root).replace(os.sep, "/")
            for f in check_tree(sub, base, policy, initial, strict=False, top=False):
                if f.rule in ("provider.no-baseline", "provider.initial"):
                    continue
                f.file = posixpath.join(rel_cache, f.file) if f.file != sub.root else rel_cache
                out.append(f)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Pre-plan source policy for Terraform / OpenTofu (report §8.1 Layer 5).")
    ap.add_argument("--root", default=".", help="the pull request's checkout (read as data)")
    ap.add_argument("--base", help="the base ref's checkout, for the new-provider and backend comparison")
    ap.add_argument("--policy", help="infra-policy.json; read it from the base ref, not the PR")
    ap.add_argument("--post-init", action="store_true", help="also scan the modules `terraform init -backend=false` downloaded")
    ap.add_argument("--initial", action="store_true", help="no baseline exists (first infrastructure PR): providers and backends are listed for a human, not judged")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    findings = run(args.root, args.base, args.policy, args.post_init, args.initial)
    errors = [f for f in findings if f.severity == ERROR]
    if args.json:
        print(json.dumps({"schema": "source_policy.v1", "ok": not errors, "findings": [f.to_dict() for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f)
        print("source_policy: %d error(s), %d warning(s)" % (len(errors), len(findings) - len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - an internal error must not read as "clean"
        sys.stderr.write("source_policy: internal error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
