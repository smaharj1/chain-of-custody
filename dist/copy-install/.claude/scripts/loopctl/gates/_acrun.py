"""Running and reading the item's acceptance tests, shared by G2 (red on stubs) and G3 (green on the build)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from kitlib import jsonio

from . import _acparse, _astscan, _lib

__all__ = ["allowed_for", "visible_files", "uncovered", "run_visible", "assertion_findings", "pinned_changes", "write_pin", "read_pin", "read_authorized", "stage_failure"]


def visible_files(ctx: Any, spec: Dict[str, Any], rev: Optional[str] = None) -> List[str]:
    files = _lib.tree_files(ctx, rev or _lib.head_of(ctx))
    return [f for f in files if _lib.match_any(spec["visible"], f)] if spec["visible"] else []


def uncovered(ctx: Any, files: List[str], ids: List[str], rev: Optional[str] = None) -> List[str]:
    """Criteria no visible file names."""
    pats = {a: _lib.ac_pattern(a) for a in ids}
    seen = set()
    for f in files:
        text = _lib.blob(ctx, rev or _lib.head_of(ctx), f)
        if text is None:
            continue
        for a, rx in pats.items():
            if a not in seen and rx.search(text):
                seen.add(a)
    return [a for a in ids if a not in seen]


def _read_junit(ctx: Any, rel: str) -> Optional[str]:
    full = os.path.join(ctx.worktree, rel)
    try:
        st = os.lstat(full)
        if os.path.islink(full) or not os.path.isfile(full) or st.st_size > 2 * 1024 * 1024:
            return None
        real, top = os.path.realpath(full), os.path.realpath(ctx.worktree)
        if not real.startswith(top + os.sep):
            return None
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def allowed_for(ctx: Any, files: List[str], rev: Optional[str] = None) -> Optional[_acparse.Allowed]:
    """The tests the pinned acceptance files define, for filtering a run's output. None when they define nothing this module can read (a
    language without a name reader): then the output is taken as it is."""
    names, mods = set(), set()
    for f in files:
        text = _lib.blob(ctx, rev or _lib.head_of(ctx), f)
        if text is None or _lib.is_binary(text):
            continue
        for n in _astscan.test_names(f, text):
            names.add(n.rsplit(".", 1)[-1])
        mods.add(f)
        if f.endswith(".py"):
            mods.add(f[:-3].replace("/", "."))
    a = _acparse.Allowed(names, mods)
    return a if a else None


def run_visible(ctx: Any, spec: Dict[str, Any], phase: str, command: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], str, _acparse.Parsed]:
    """Run the acceptance command and read per-criterion results from its output (and from the JUnit file, if the plan names one)."""
    ids = _lib.ac_ids_of(ctx.item)
    env = {"LOOPCTL_ITEM": ctx.item_id, "LOOPCTL_PHASE": phase}
    env.update(extra_env or {})
    junit_path = None
    if spec.get("junit"):
        junit_path = os.path.join(ctx.worktree, spec["junit"])
        try:
            os.unlink(junit_path)           # a stale file from an earlier run must not be read as this run's result
        except OSError:
            pass
    stage, text, _f = _lib.run_command(ctx, "acceptance", command or spec["cmd"], env)
    allowed = allowed_for(ctx, visible_files(ctx, spec)) if spec.get("visible") else None
    parsed = _acparse.parse(text, ids, allowed)
    if spec.get("junit"):
        xml = _read_junit(ctx, spec["junit"])
        jp = _acparse.parse_junit(xml, ids, allowed) if xml else None
        if jp is not None:
            for a, r in jp.results.items():
                for t in r["tests"] or [""]:
                    parsed.add(a, r["verdict"], t)
    return stage, text, parsed


def stage_failure(stage: Dict[str, Any], what: str) -> Optional[Tuple[str, str]]:
    """(finding id, failure class) when the command could not produce a verdict at all (timeout, not found, collected nothing)."""
    if stage["status"] == "timeout":
        return "%s.timeout" % what, "transient"
    if stage.get("exit_code") in (126, 127):
        return "%s.command-not-found" % what, "env"
    if stage.get("tests_collected") == 0:
        return "%s.collected-nothing" % what, "item"
    return None


def assertion_findings(ctx: Any, files: List[str], ids: List[str], rev: Optional[str] = None) -> List[Dict[str, Any]]:
    """G3's assertion check on the visible tests: every test that names a criterion has at least one real assertion and no constant one
    (`assert True`, `expect(true)`, `assertRaises(Exception)`), and none is skipped. Python is read with `ast` (errors); other languages
    with regexes (warnings, low confidence)."""
    pats = {a: _lib.ac_pattern(a) for a in ids}
    findings = []  # type: List[Dict[str, Any]]
    proven = set()
    for f in files:
        text = _lib.blob(ctx, rev or _lib.head_of(ctx), f)
        if text is None or _lib.is_binary(text):
            continue
        lang = _astscan.language(f)
        if lang == "python":
            fns = _astscan.py_test_functions(text, pats)
            if fns is None:
                findings.append(_lib.finding("g3.unparsable-test", "error", "%s does not parse: the assertion check cannot read it" % f))
                continue
            for fn in fns:
                if not fn["acs"]:
                    continue
                if fn["skipped"]:
                    findings.append(_lib.finding("g3.skipped-ac-test", "error", "%s::%s names %s and is skipped, xfail or todo" % (f, fn["name"], ", ".join(fn["acs"]))))
                    continue
                for reason in fn["reasons"]:
                    findings.append(_lib.finding("g3.fake-assertion", "error", "%s::%s: %s" % (f, fn["name"], reason)))
                if fn["real"] == 0 and not fn["helper"]:
                    findings.append(_lib.finding("g3.no-assertion", "error", "%s::%s names %s but asserts nothing" % (f, fn["name"], ", ".join(fn["acs"]))))
                elif fn["real"] > 0 or fn["helper"]:
                    proven.update(fn["acs"])
        elif lang == "javascript":
            named = [a for a, rx in pats.items() if rx.search(text)]
            if not named:
                continue
            probs = _astscan.js_assertion_problems(text)
            if _astscan.skip_markers(f, text):
                findings.append(_lib.finding("g3.skipped-ac-test", "warn", "%s has skip, only or todo markers (low confidence: regex reading)" % f, confidence="low"))
            if probs["real"] <= 0:
                findings.append(_lib.finding("g3.no-assertion", "warn", "%s names %s but no real assertion was found (low confidence: regex reading)"
                                             % (f, ", ".join(named)), confidence="low"))
            else:
                proven.update(named)
            if probs["fake"]:
                findings.append(_lib.finding("g3.fake-assertion", "warn", "%s asserts constants (expect(true) and the like; low confidence: regex reading)" % f, confidence="low"))
        else:
            proven.update(a for a, rx in pats.items() if rx.search(text))
    for a in ids:
        if a not in proven and files:
            findings.append(_lib.finding("g3.ac-no-real-assertion", "error" if any(_astscan.language(f) == "python" for f in files) else "warn",
                                         "%s has no test with a real assertion among the visible tests" % a))
    return findings


def _pin_path(ctx: Any) -> str:
    return os.path.join(_lib.runstate_of(ctx).root, "pins", "%s-a%d.json" % (ctx.item_id, ctx.attempt))


def read_pin(ctx: Any) -> Optional[Dict[str, str]]:
    """{path: blob id} of the acceptance tests as G2 pinned them for this attempt, or None when G2 has not pinned anything."""
    try:
        d = jsonio.read_json_opt(_pin_path(ctx))
    except (ValueError, OSError):
        return None
    if isinstance(d, dict) and d.get("base") == ctx.base_sha and isinstance(d.get("files"), dict):
        return {str(k): str(v) for k, v in d["files"].items()}
    return None


def read_authorized(ctx: Any) -> Dict[str, str]:
    """{path: blob id} of every test, config and scaffolding file the acceptance-author committed before G2 passed. G5 does not count
    these as the engineer's edits as long as they are byte-identical."""
    try:
        d = jsonio.read_json_opt(_pin_path(ctx))
    except (ValueError, OSError):
        return {}
    if isinstance(d, dict) and d.get("base") == ctx.base_sha and isinstance(d.get("authorized"), dict):
        return {str(k): str(v) for k, v in d["authorized"].items()}
    return {}


def write_pin(ctx: Any, files: Dict[str, str], authorized: Optional[Dict[str, str]] = None) -> None:
    jsonio.ensure_dir(os.path.dirname(_pin_path(ctx)), 0o700)
    jsonio.atomic_write_json(_pin_path(ctx), {"schema": "loopctl.pin.v1", "item": ctx.item_id, "attempt": ctx.attempt, "base": ctx.base_sha,
                                              "files": files, "authorized": dict(authorized or {})}, 0o600)


def pinned_changes(ctx: Any, spec: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], Dict[str, str]]:
    """([(path, how)] of acceptance-test files that changed, the pin or {}).

    With a pin (G2 passed for this attempt) the files must be byte-identical to what G2 pinned: edited, deleted, or a new file under a pinned
    glob are all changes. Without one (the tests rode in the plan PR) the base is the reference: any edit, deletion or addition since the base
    counts. A pinned file identical to its pin is never a change, wherever it came from."""
    if not spec["visible"]:
        return [], {}
    pin = read_pin(ctx) or {}
    head = _lib.tree_entries(ctx, _lib.head_of(ctx))
    out = []  # type: List[Tuple[str, str]]
    if pin:
        for path, oid in pin.items():
            if path not in head:
                out.append((path, "deleted"))
            elif head[path] != oid:
                out.append((path, "edited"))
        for path in head:
            if path not in pin and _lib.match_any(spec["visible"], path):
                out.append((path, "added after the pin"))
        for st, path, _o in _lib.diff_status(ctx):              # the working tree: an uncommitted edit or new file
            if _lib.match_any(spec["visible"], path) and st == "A" and path not in head:
                out.append((path, "added (uncommitted)"))
        dirty = _lib.git_ok(ctx, ["status", "--porcelain=v1", "-z", "--untracked-files=no"]) or ""
        for rec in dirty.split("\0"):
            if len(rec) > 3 and rec[3:] in pin and (rec[0] != " " or rec[1] != " "):
                out.append((rec[3:], "edited (uncommitted)"))
        return sorted(set(out)), pin
    for st, path, _o in _lib.diff_status(ctx):
        if _lib.match_any(spec["visible"], path):
            out.append((path, {"A": "added", "D": "deleted"}.get(st, "edited")))
    return sorted(set(out)), {}
