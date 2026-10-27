"""The reproducibility manifest (report 16.1 row 11, F-26; 19.7 `env.manifest`; CO-45). OPT-IN: written by a command, never by a gate.

WHAT IT IS. One JSON file that says exactly what a piece of the loop ran on, so that someone else (or you in a year) can see it and check it:

    repo        the base sha and the head sha
    kit         KIT_VERSION at the base, loopctl's own version
    config      sha256 of loop.config.md AT THE BASE, every gate's mode, and the keys that decide how the loop behaves
    plan        the plan digest and the item's digest, both at the base
    toolchain   the version pins at the head: .nvmrc, .tool-versions, .python-version, package.json engines, pyproject requires-python
    lockfiles   git object id of each lockfile at the head
    verify      sha256 and a short head of each kit-owned verify command
    evidence    the git object id of docs/baseline/evidence.json at the base
    gates       what the state recorded for the item (status, mode, head)            [recorded, not re-derivable]
    environment python and platform of the machine that wrote it, and the claude version it was told   [recorded, not re-derivable]
    hash        sha256 of the canonical form of everything above

HOW IT IS CHECKED (`loopctl manifest verify`). Two independent things, and a forger has to defeat both:

  1. SEAL. The recorded `hash` must equal the hash of the document. Editing any field, or the hash itself, without recomputing the other breaks it.
  2. FACTS. Everything marked re-derivable (repo, kit, config, plan, toolchain, lockfiles, verify, evidence) is read again from git AT THE RECORDED SHAS
     and compared. A manifest that was edited AND re-sealed still has to match the repository, so it cannot claim a config, a plan, a lockfile or an
     evidence file that those commits do not hold. Facts that git cannot re-derive (gates, environment) are protected by the seal and, if you
     pass `--expect-hash` (from the PR's audit block or any channel the writer cannot edit), by that.

The event log also records every hash `manifest write` made; `verify` reports whether this machine wrote the one it is shown. That log is local and
forgeable (CS-21), so it is a convenience, not the proof. Nothing here is a signature: a person who controls the repository can write a manifest that
is true about a repository they made up. The manifest answers "what exactly was this run on?", not "who says so?".
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kitlib import config as kconfig
from kitlib import gitutil, jsonio

from . import VERSION, common, gitio, plan as plan_mod
from .common import Refused, UsageError

__all__ = ["SCHEMA", "collect", "seal", "body_hash", "verify", "REDERIVABLE", "RECORDED_ONLY", "manifests_dir", "write", "read_file", "find",
           "TOOLCHAIN_FILES", "LOCKFILES"]

SCHEMA = "loopctl.manifest.v1"
MAX_BYTES = 256 * 1024
REDERIVABLE = ("repo", "kit", "config", "plan", "toolchain", "lockfiles", "verify", "evidence")
RECORDED_ONLY = ("gates", "environment")
TOOLCHAIN_FILES = (".nvmrc", ".node-version", ".tool-versions", ".python-version", ".ruby-version", ".go-version", "rust-toolchain.toml", "rust-toolchain")
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "poetry.lock", "Pipfile.lock", "uv.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", ".terraform.lock.hcl", "packages.lock.json")
_VERIFY_KEYS = ("verify_build", "verify_lint", "verify_typecheck", "verify_tests", "verify_run", "verify_dev")
_BEHAVIOUR_KEYS = ("runner", "autonomy", "autonomy_level", "model_profile", "merge_policy", "parallel_mode", "report_trailers", "delivery", "git_autonomy",
                   "loopctl_tier2", "remote_lock")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.json\Z")


def body_hash(doc: Dict[str, Any]) -> str:
    """sha256 over the canonical JSON of the document without its own `hash` field."""
    return common.sha256_hex(common.canonical_json({k: v for k, v in doc.items() if k != "hash"}))


def seal(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out.pop("hash", None)
    out["hash"] = body_hash(out)
    return out


# -- collecting, from git at given shas ----------------------------------------------------------------------------------------------

def _blob_oids(repo: str, rev: str) -> Dict[str, str]:
    try:
        rows = gitio.ls_tree(repo, rev, [], recursive=True)
    except ValueError:
        raise Refused("cannot list the tree of %s" % common.clean_text(rev, 20))
    return {str(r["path"]): str(r["sha"]) for r in rows if r["type"] == "blob" and r["mode"] in ("100644", "100755")}


def _head_line(text: Optional[str], n: int = 120) -> Optional[str]:
    return common.clean_text(text.strip().splitlines()[0], n) if text and text.strip() else None


def collect(repo: str, base: str, head: str, item_id: Optional[str], plan_name: Optional[str] = None) -> Dict[str, Any]:
    """The re-derivable part of a manifest, read from git objects at `base` and `head` (never from the working tree)."""
    for what, sha in (("base", base), ("head", head)):
        if not gitutil.valid_oid(sha):
            raise UsageError("%s must be a full commit id" % what)
    cfg_text = gitutil.show_file(base, kconfig.CONFIG_PATH, repo, isolated=True)
    try:
        cfg = kconfig.load_text(cfg_text, source="%s:loop.config" % base[:12]) if cfg_text is not None else None
    except kconfig.ConfigError:
        cfg = None
    lp = plan_mod.load_at_rev(repo, base)
    item = lp.items.get(item_id) if item_id and lp.items else None
    head_blobs = _blob_oids(repo, head)
    base_blobs = _blob_oids(repo, base) if base != head else head_blobs
    pins = {}  # type: Dict[str, Any]
    want = [p for p in head_blobs if p in TOOLCHAIN_FILES or p in ("package.json", "pyproject.toml")]
    bodies = gitio.cat_blobs(repo, [head_blobs[p] for p in want], 1 << 20)
    for p in sorted(want):
        text = (bodies.get(head_blobs[p]) or b"").decode("utf-8", "replace")
        if p in TOOLCHAIN_FILES:
            pins[p] = _head_line(text)
        elif p == "package.json":
            try:
                eng = json.loads(text).get("engines")
            except (ValueError, AttributeError):
                eng = None
            if isinstance(eng, dict):
                pins["package.json#engines"] = {str(k): common.clean_text(v, 40) for k, v in sorted(eng.items())[:8]}
        elif p == "pyproject.toml":
            m = re.search(r'^\s*requires-python\s*=\s*"([^"\n]{1,40})"', text, re.M)
            if m:
                pins["pyproject.toml#requires-python"] = m.group(1)
    locks = {p: o for p, o in sorted(head_blobs.items()) if os.path.basename(p) in LOCKFILES}
    kit_text = gitutil.show_file(base, ".claude/KIT_VERSION", repo, isolated=True)
    verify = {}  # type: Dict[str, Any]
    gates_modes = {}  # type: Dict[str, str]
    behaviour = {}  # type: Dict[str, Any]
    if cfg is not None:
        for k in _VERIFY_KEYS:
            v = cfg.get(k)
            if isinstance(v, str) and v.strip() and v.strip() != "<command>":
                verify[k] = {"sha256": common.sha256_hex(v.strip()), "head": common.clean_text(v.strip(), 80)}
        gates_modes = {n: cfg.gate(n) for n in sorted(cfg.gates())}
        for k in _BEHAVIOUR_KEYS:
            if cfg.reg.get(k) is not None:
                v = cfg.get(k)
                behaviour[k] = v if isinstance(v, (str, int, float, bool)) or v is None else common.clean_text(v, 60)
    return {
        "repo": {"base": base, "head": head},
        "kit": {"version": _head_line(kit_text), "loopctl": VERSION},
        "config": {"sha256": common.sha256_hex(cfg_text) if cfg_text is not None else None, "gates": gates_modes, "keys": behaviour},
        "plan": {"name": lp.name or plan_name, "digest": plan_mod.plan_digest(lp) if lp.plan else None,
                 "item": item_id, "item_digest": common.sha256_hex(common.canonical_json(item)) if item else None},
        "toolchain": pins,
        "lockfiles": locks,
        "verify": verify,
        "evidence": {"blob": base_blobs.get("docs/baseline/evidence.json")},
    }


def _environment(env: Dict[str, str]) -> Dict[str, Any]:
    return {"python": "%d.%d.%d" % sys.version_info[:3], "platform": sys.platform, "claude_version": common.clean_text(env.get("KIT_CLAUDE_VERSION") or "unknown", 40)}


def build(repo: str, base: str, head: str, item_id: Optional[str], state_item: Optional[Dict[str, Any]], env: Dict[str, str], run_id: Optional[str] = None,
          plan_name: Optional[str] = None) -> Dict[str, Any]:
    doc = {"schema": SCHEMA, "created": common.iso(), "run": run_id}  # type: Dict[str, Any]
    doc.update(collect(repo, base, head, item_id, plan_name))
    gates = {}  # type: Dict[str, Any]
    for n, g in sorted(((state_item or {}).get("gates") or {}).items()):
        if isinstance(g, dict):
            gates[n] = {"status": g.get("status"), "mode": g.get("mode"), "head": g.get("head")}
    doc["gates"] = gates
    doc["environment"] = _environment(env)
    return seal(doc)


# -- storing ---------------------------------------------------------------------------------------------------------------------------

def manifests_dir(rs: Any) -> str:
    return os.path.join(rs.root, "manifests")


def write(rs: Any, doc: Dict[str, Any]) -> str:
    plan = (doc.get("plan") or {}).get("name") or "plan"
    item = (doc.get("plan") or {}).get("item") or "all"
    name = "%s-%s-%s.json" % (plan, item, str((doc.get("repo") or {}).get("head"))[:12])
    if not _NAME_RE.match(name):
        raise UsageError("the manifest name %r is not a plain file name" % common.clean_text(name, 60))
    d = manifests_dir(rs)
    jsonio.ensure_dir(rs.root)
    jsonio.ensure_dir(d)
    path = os.path.join(d, name)
    jsonio.atomic_write_json(path, doc, 0o600)
    rs.append_event("loopctl", {"event": "manifest", "hash": doc["hash"], "name": name, "item": item})
    return path


def read_file(path: str) -> Dict[str, Any]:
    """A manifest from a file: a regular file (no symlink) of at most 256 KiB, strict JSON."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise Refused("cannot read %s: %s" % (common.clean_text(path, 100), common.clean_text(exc.strerror or exc, 60)))
    import stat as stat_mod
    if not stat_mod.S_ISREG(st.st_mode):
        raise Refused("%s is not a regular file" % common.clean_text(path, 100))
    if st.st_size > MAX_BYTES:
        raise Refused("%s is larger than %d bytes" % (common.clean_text(path, 100), MAX_BYTES))
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    try:
        doc = common.strict_loads(raw.decode("utf-8", "replace"), max_depth=8, max_bytes=MAX_BYTES)
    except (ValueError, RecursionError) as exc:
        raise Refused("%s is not strict JSON (%s)" % (common.clean_text(path, 100), common.clean_text(exc, 80)))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise Refused("%s is not a %s document" % (common.clean_text(path, 100), SCHEMA))
    return doc


def find(rs: Any, which: str) -> str:
    """The path of a stored manifest: a file name in the manifests directory, or `latest`."""
    d = manifests_dir(rs)
    if which == "latest":
        try:
            names = [n for n in os.listdir(d) if _NAME_RE.match(n)]
        except OSError:
            names = []
        if not names:
            raise Refused("no manifest has been written yet: `loopctl manifest write`")
        names.sort(key=lambda n: os.stat(os.path.join(d, n)).st_mtime)
        return os.path.join(d, names[-1])
    if not _NAME_RE.match(which):
        raise UsageError("a stored manifest is named like plan-item-head.json; give a path for any other file")
    return os.path.join(d, which)


# -- verifying ---------------------------------------------------------------------------------------------------------------------------

def _diff(path: str, recorded: Any, now: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(recorded, dict) and isinstance(now, dict):
        for k in sorted(set(recorded) | set(now)):
            _diff(path + "." + str(k), recorded.get(k), now.get(k), out)
    elif recorded != now:
        out.append({"field": path, "recorded": common.clean_text(json.dumps(recorded, sort_keys=True), 100), "git_holds": common.clean_text(json.dumps(now, sort_keys=True), 100)})


def verify(doc: Dict[str, Any], repo: str, expect_hash: Optional[str] = None, anchored: Optional[bool] = None,
           redo: Optional[Callable[[str, str, str, Optional[str]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{ok, seal, facts[], anchored, expect}. `ok` needs the seal, every re-derivable fact, and (if given) the expected hash to hold."""
    recorded_hash = doc.get("hash")
    sealed = isinstance(recorded_hash, str) and recorded_hash == body_hash(doc)
    result = {"schema": "loopctl.manifest-verify.v1", "seal": sealed, "recorded_hash": recorded_hash if isinstance(recorded_hash, str) else None,
              "computed_hash": body_hash(doc), "facts": [], "checked": [], "not_rederivable": list(RECORDED_ONLY), "anchored": anchored, "expect": None}  # type: Dict[str, Any]
    problems = []  # type: List[str]
    if not sealed:
        problems.append("the recorded hash does not match the document: it was edited after it was written")
    if expect_hash is not None:
        ok_e = isinstance(recorded_hash, str) and recorded_hash == expect_hash
        result["expect"] = ok_e
        if not ok_e:
            problems.append("the hash is not the one you expected")
    repo_part = doc.get("repo") if isinstance(doc.get("repo"), dict) else {}
    base, head = repo_part.get("base"), repo_part.get("head")
    if not (isinstance(base, str) and gitutil.valid_oid(base) and isinstance(head, str) and gitutil.valid_oid(head)):
        problems.append("repo.base and repo.head are not commit ids")
    else:
        missing = [s for s in (base, head) if gitutil.rev_parse(s + "^{commit}", repo, isolated=True) is None]
        if missing:
            problems.append("this repository does not have commit %s: the facts cannot be re-derived here" % missing[0][:12])
        else:
            item = (doc.get("plan") or {}).get("item") if isinstance(doc.get("plan"), dict) else None
            plan_name = (doc.get("plan") or {}).get("name") if isinstance(doc.get("plan"), dict) else None
            now = (redo or collect)(repo, base, head, item if isinstance(item, str) else None, plan_name)
            diffs = []  # type: List[Dict[str, Any]]
            for part in REDERIVABLE:
                _diff(part, doc.get(part), now.get(part), diffs)
            result["facts"] = diffs
            result["checked"] = list(REDERIVABLE)
            if diffs:
                problems.append("%d fact(s) differ from what git holds at the recorded commits" % len(diffs))
    result["problems"] = problems
    result["ok"] = not problems
    return result
