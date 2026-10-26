#!/usr/bin/env python3
"""dependency_report: the deterministic half of the dependency-check workflow (Phase 11a; report 19.7 "Primer and lockfile drift").

The workflow runs the package managers and the scanner and saves their JSON. This script reads that JSON and the lockfile, decides
what is worth an issue, and writes the issue texts. It runs no package manager and makes no network call. Its checks:

  majors   `npm outdated --json` / `pip list --outdated --format=json`: a package whose latest version has a higher MAJOR than the
           one in use. Patch and minor bumps are counted as low risk and get no issue (a dev dependency with green CI qualifies).
  vulns    `osv-scanner --format json`: one issue per package and advisory id.
  peers    package-lock.json peerDependencies that the installed versions do not satisfy (for example a resolver package built
           for another major of the schema library). Optional peers are skipped. Range syntax it cannot read is skipped, never guessed.
  drift    a major written in a primer (.claude/context/*.md) or in .claude/CLAUDE.md as `name@3`, `name v3` or `name 3.x`, where the
           lockfile has another major. Only names that are real dependencies are looked for, so prose cannot trigger it.
           Lockfiles read: package-lock.json (v2, v3) and poetry.lock. For other lockfiles drift and peers are not checked.

    python3 .claude/scripts/dependency_report.py --repo . --out-dir .deps [--npm-outdated F] [--pip-outdated F] [--osv F]
                                                 [--existing-issues F] [--max-issues 10]

Writes `report.json` (schema dependency_report.v1), `issues.json` (a list of {title, body, labels}, already de-duplicated against the open
`factory:deps` issues in --existing-issues and capped at --max-issues) and `summary.md`. Every package name, version and advisory id is
checked against a strict pattern before it reaches a title or body; anything else is dropped and counted in `skipped_invalid`.
Exit 0 whenever it wrote the files; 2 for unusable arguments.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio  # noqa: E402

MAX_JSON = 8 * 1024 * 1024
MAX_PRIMER = 512 * 1024
LABEL = "factory:deps"
MARKER = "<!-- deps-check:v1 -->"
_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,213}/)?[a-z0-9][a-z0-9._-]{0,213}\Z")
_PY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}\Z")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,63}\Z")
_ADVISORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}\Z")
_RANGE = re.compile(r"^[0-9A-Za-z .^~<>=|*,-]{1,100}\Z")


# ---------------------------------------------------------------------------------------------------- versions

def vtuple(text: Any) -> Optional[Tuple[int, int, int]]:
    """(major, minor, patch) from '1.2.3', 'v1.2', '1.2.3-beta.1+x'; None if it does not start like a version."""
    if not isinstance(text, str):
        return None
    m = re.match(r"^[v=]?\s*(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?", text.strip())
    if not m:
        return None

    def n(g: Optional[str]) -> int:
        return int(g) if g and g.isdigit() else 0
    return (n(m.group(1)), n(m.group(2)), n(m.group(3)))


def _wild(token: str) -> int:
    """How many of the three parts of a partial version are wildcards or missing (0 for a full x.y.z)."""
    t = token.lstrip("v=^~<>").strip()
    parts = t.split(".")
    count = 3 - len(parts) if len(parts) < 3 else 0
    for p in parts[:3]:
        if p in ("x", "X", "*", ""):
            count += 1
    return count


def _atom(version: Tuple[int, int, int], token: str) -> Optional[bool]:
    """Does `version` satisfy one comparator token? None when the token is not understood."""
    token = token.strip()
    if token in ("", "*", "x", "X", "latest"):
        return True
    op = ""
    for cand in (">=", "<=", ">", "<", "^", "~", "="):
        if token.startswith(cand):
            op, token = cand, token[len(cand):].strip()
            break
    base = vtuple(token)
    if base is None:
        return None
    wild = _wild(token)
    if op in ("", "="):
        if wild == 0:
            return version == base
        keep = 3 - wild
        return version[:keep] == base[:keep]
    if op == ">=":
        return version >= base
    if op == ">":
        return version > base if wild == 0 else version[:3 - wild] > base[:3 - wild]
    if op == "<=":
        return version <= base if wild == 0 else version[:3 - wild] <= base[:3 - wild]
    if op == "<":
        return version < base
    if op == "~":
        keep = 1 if wild >= 2 else 2
        return version >= base and version[:keep] == base[:keep]
    if op == "^":
        if base[0] > 0 or wild >= 2:
            return version >= base and version[0] == base[0]
        if base[1] > 0 or wild == 1:
            return version >= base and version[:2] == base[:2]
        return version == base
    return None


def satisfies(version_text: str, rng: str) -> Optional[bool]:
    """True/False for an npm-style range; None when the version or any part of the range is not understood."""
    v = vtuple(version_text)
    if v is None or not isinstance(rng, str) or not _RANGE.match(rng):
        return None
    saw_unknown = False
    for alt in rng.split("||"):
        alt = alt.strip()
        if " - " in alt:
            lo, hi = [p.strip() for p in alt.split(" - ", 1)]
            results = [_atom(v, ">=" + lo), _atom(v, "<=" + hi)]
        else:
            results = [_atom(v, t) for t in alt.split()] or [True]
        if any(r is None for r in results):
            saw_unknown = True
            continue
        if all(results):
            return True
    return None if saw_unknown else False


# ---------------------------------------------------------------------------------------------------- inputs

def _read_json(path: Optional[str]) -> Any:
    if not path:
        return None
    data, problem = safeio.read_bytes(path, MAX_JSON)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None


def npm_lock(repo: str) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """({name: version}, {name: lock entry}) for the top-level packages of package-lock.json."""
    doc = _read_json(os.path.join(repo, "package-lock.json"))
    versions, entries = {}, {}  # type: Dict[str, str], Dict[str, Dict[str, Any]]
    pk = doc.get("packages") if isinstance(doc, dict) else None
    if isinstance(pk, dict):
        for key, ent in pk.items():
            if not isinstance(key, str) or not key.startswith("node_modules/") or key.count("node_modules/") != 1 or not isinstance(ent, dict):
                continue
            name = key[len("node_modules/"):]
            if _NPM_NAME.match(name) and isinstance(ent.get("version"), str) and _VERSION.match(ent["version"]):
                versions[name] = ent["version"]
                entries[name] = ent
    return versions, entries


def poetry_lock(repo: str) -> Dict[str, str]:
    data, _ = safeio.read_bytes(os.path.join(repo, "poetry.lock"), MAX_JSON)
    out = {}  # type: Dict[str, str]
    if data is None:
        return out
    for m in re.finditer(r'(?m)^\[\[package\]\]\s*\nname = "([^"\n]+)"\s*\nversion = "([^"\n]+)"', data.decode("utf-8", "replace")):
        if _PY_NAME.match(m.group(1)) and _VERSION.match(m.group(2)):
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def lock_versions(repo: str) -> Dict[str, str]:
    v, _ = npm_lock(repo)
    for k, ver in poetry_lock(repo).items():
        v.setdefault(k, ver)
    return v


# ---------------------------------------------------------------------------------------------------- findings

class Findings(object):
    def __init__(self) -> None:
        self.majors = []  # type: List[Dict[str, str]]
        self.low_risk = []  # type: List[str]
        self.vulns = []  # type: List[Dict[str, str]]
        self.peers = []  # type: List[Dict[str, str]]
        self.drift = []  # type: List[Dict[str, str]]
        self.skipped_invalid = 0


def check_outdated(npm: Any, pip: Any, f: Findings) -> None:
    if isinstance(npm, dict):
        for name, info in sorted(npm.items()):
            if not isinstance(info, dict) or not _NPM_NAME.match(str(name)):
                f.skipped_invalid += 1
                continue
            cur = info.get("current") if vtuple(info.get("current")) else info.get("wanted")
            latest = info.get("latest")
            a, b = vtuple(cur), vtuple(latest)
            if a is None or b is None or not _VERSION.match(str(cur)) or not _VERSION.match(str(latest)):
                f.skipped_invalid += 1
                continue
            if b[0] > a[0]:
                f.majors.append({"name": name, "current": str(cur), "latest": str(latest), "ecosystem": "npm"})
            elif b > a:
                f.low_risk.append(name)
    if isinstance(pip, list):
        for info in pip:
            name = info.get("name") if isinstance(info, dict) else None
            cur, latest = (info.get("version"), info.get("latest_version")) if isinstance(info, dict) else (None, None)
            a, b = vtuple(cur), vtuple(latest)
            if not isinstance(name, str) or not _PY_NAME.match(name) or a is None or b is None \
                    or not _VERSION.match(str(cur)) or not _VERSION.match(str(latest)):
                f.skipped_invalid += 1
                continue
            if b[0] > a[0]:
                f.majors.append({"name": name, "current": str(cur), "latest": str(latest), "ecosystem": "pypi"})
            elif b > a:
                f.low_risk.append(name)


def _items(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def check_osv(doc: Any, f: Findings) -> None:
    seen = set()  # type: set
    for res in _items(doc.get("results") if isinstance(doc, dict) else None):
        for pk in _items(res.get("packages") if isinstance(res, dict) else None):
            if not isinstance(pk, dict):
                continue
            p = pk.get("package") if isinstance(pk.get("package"), dict) else {}
            name, ver = p.get("name"), p.get("version")
            for vul in _items(pk.get("vulnerabilities")):
                vid = vul.get("id") if isinstance(vul, dict) else None
                if not (isinstance(name, str) and (_NPM_NAME.match(name) or _PY_NAME.match(name)) and isinstance(ver, str) and _VERSION.match(ver)
                        and isinstance(vid, str) and _ADVISORY.match(vid)):
                    f.skipped_invalid += 1
                    continue
                if (name, vid) not in seen:
                    seen.add((name, vid))
                    f.vulns.append({"name": name, "version": ver, "id": vid})


def check_peers(repo: str, f: Findings) -> None:
    versions, entries = npm_lock(repo)
    for name, ent in sorted(entries.items()):
        peers = ent.get("peerDependencies")
        meta = ent.get("peerDependenciesMeta") if isinstance(ent.get("peerDependenciesMeta"), dict) else {}
        if not isinstance(peers, dict):
            continue
        for peer, rng in sorted(peers.items()):
            if not isinstance(peer, str) or not isinstance(rng, str):
                continue
            if isinstance(meta.get(peer), dict) and meta[peer].get("optional"):
                continue
            if peer not in versions or not _NPM_NAME.match(peer):
                continue
            ok = satisfies(versions[peer], rng)
            if ok is False:
                f.peers.append({"name": name, "peer": peer, "range": rng.strip(), "installed": versions[peer]})


def check_drift(repo: str, f: Findings) -> None:
    versions = lock_versions(repo)
    if not versions:
        return
    files = sorted(glob.glob(os.path.join(repo, ".claude", "context", "*.md"))) + [os.path.join(repo, ".claude", "CLAUDE.md")]
    seen = set()  # type: set
    for path in files:
        text, _ = safeio.read_text_status(path, MAX_PRIMER)
        if not text:
            continue
        text = text.replace("`", "").lower()
        for name, installed in sorted(versions.items()):
            have = vtuple(installed)
            if have is None:
                continue
            rx = re.compile(r"(?<![\w@/.-])" + re.escape(name.lower()) + r"(?:@|\s+v|\s+(?=\d+\.[\dx*]))(\d+)(?:\.(?:\d+|x|\*))*(?![\w-])")
            for m in rx.finditer(text):
                declared = int(m.group(1))
                if declared != have[0] and (name, declared) not in seen:
                    seen.add((name, declared))
                    f.drift.append({"name": name, "declared": str(declared), "installed": installed, "file": os.path.relpath(path, repo)})


# ---------------------------------------------------------------------------------------------------- issues

def _body(lines: List[str]) -> str:
    return "\n".join([MARKER] + lines + ["",
                      "Found by the dependency-check workflow. This issue changes nothing by itself: a human or the loop plans the update.",
                      "A major version is never treated as low risk. Patch and minor bumps of dev dependencies with green CI are.", ""])


def build_issues(f: Findings, existing: Sequence[str], max_issues: int) -> Tuple[List[Dict[str, Any]], int]:
    cands = []  # type: List[Tuple[str, str]]
    for v in f.vulns:
        cands.append(("deps: security advisory %s in %s" % (v["id"], v["name"]),
                      _body(["Advisory %s affects %s %s." % (v["id"], v["name"], v["version"])])))
    for m in f.majors:
        cands.append(("deps: %s %s -> %s (major)" % (m["name"], m["current"], m["latest"]),
                      _body(["%s (%s) is at %s; the latest is %s, a new major version." % (m["name"], m["ecosystem"], m["current"], m["latest"])])))
    for p in f.peers:
        cands.append(("deps: peer mismatch %s needs %s %s" % (p["name"], p["peer"], p["range"]),
                      _body(["%s declares the peer %s %s, and %s is installed." % (p["name"], p["peer"], p["range"], p["installed"])])))
    for d in f.drift:
        cands.append(("deps: %s is %s in the primer, %s in the lockfile" % (d["name"], d["declared"], d["installed"]),
                      _body(["%s names %s at major %s; the lockfile has %s. Update the primer (or the dependency)." % (d["file"], d["name"], d["declared"], d["installed"])])))
    have = set(t.strip().lower() for t in existing)
    fresh = [(t, b) for t, b in cands if t.lower() not in have]
    out = [{"title": t, "body": b, "labels": [LABEL]} for t, b in fresh[:max(0, max_issues)]]
    return out, max(0, len(fresh) - len(out))


def summary(f: Findings, issues: List[Dict[str, Any]], overflow: int) -> str:
    lines = ["# Dependency check", "",
             "- majors: %d" % len(f.majors), "- advisories: %d" % len(f.vulns), "- peer mismatches: %d" % len(f.peers),
             "- primer and lockfile drift: %d" % len(f.drift), "- low risk (patch or minor): %d" % len(f.low_risk),
             "- new issues: %d%s" % (len(issues), " (%d more not opened: raise --max-issues)" % overflow if overflow else ""),
             "- skipped (unreadable name or version): %d" % f.skipped_invalid, ""]
    return "\n".join(lines)


def existing_titles(doc: Any) -> List[str]:
    out = []  # type: List[str]
    if isinstance(doc, list):
        for it in doc:
            if isinstance(it, dict) and isinstance(it.get("title"), str):
                out.append(it["title"])
    return out


def run(repo: str, npm: Any = None, pip: Any = None, osv: Any = None, existing: Any = None,
        max_issues: int = 10) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    f = Findings()
    check_outdated(npm, pip, f)
    check_osv(osv, f)
    check_peers(repo, f)
    check_drift(repo, f)
    issues, overflow = build_issues(f, existing_titles(existing), max_issues)
    report = {"schema": "dependency_report.v1", "majors": f.majors, "low_risk": {"count": len(f.low_risk), "names": sorted(set(f.low_risk))[:50]},
              "vulns": f.vulns, "peers": f.peers, "drift": f.drift, "overflow": overflow, "skipped_invalid": f.skipped_invalid}
    return report, issues, summary(f, issues, overflow)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--npm-outdated")
    ap.add_argument("--pip-outdated")
    ap.add_argument("--osv")
    ap.add_argument("--existing-issues")
    ap.add_argument("--max-issues", type=int, default=10)
    args = ap.parse_args(argv)
    if not 0 <= args.max_issues <= 50:
        sys.stderr.write("dependency_report: --max-issues must be between 0 and 50\n")
        return 2
    report, issues, text = run(os.path.abspath(args.repo), _read_json(args.npm_outdated), _read_json(args.pip_outdated),
                               _read_json(args.osv), _read_json(args.existing_issues), args.max_issues)
    try:
        os.makedirs(args.out_dir, exist_ok=True)
        for name, payload in (("report.json", report), ("issues.json", issues)):
            with open(os.path.join(args.out_dir, name), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
        with open(os.path.join(args.out_dir, "summary.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        sys.stderr.write("dependency_report: cannot write %s: %s\n" % (args.out_dir, exc.strerror))
        return 2
    print("dependency_report: %d issue(s) to open, %d overflow" % (len(issues), report["overflow"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
