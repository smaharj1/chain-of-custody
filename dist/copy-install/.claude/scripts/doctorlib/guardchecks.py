"""Guard wiring checks (Phase 2; report §7.1, §7.2, §5.8; IMP-13, IMP-16, IMP-21).

    guards.manifest      the hooks manifest loads, every guard it names exists, the generated settings.v2.json is current
    guards.wiring        the effective settings carry every hook handler the manifest generates (copy install, managed
                         settings or user settings); the staged-but-not-active case is reported as such (IMP-13)
    guards.on-failure    every blocking-capable handler carries `onFailure: block` (RC-30), otherwise a crashed guard fails open
    guards.selftest      the SessionStart self-test ran and passed; a failed self-test caps the level at L0 (§7.1)
    guards.deny-drift    the effective deny rules cover the suggested deny block (§7.2: "the doctor diffs the effective settings
                         against the suggested deny block and sandbox profile, and reports any drift")

Registered by importing this module (doctor.py does). It adds one cap, `guards`, to the effective-level computation:
L0 when the self-test failed, L1 while the wiring is not proven, otherwise no cap. It reads only files; it never runs a guard.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from kitlib import levels

from . import levelcalc
from .model import Context, Result, check, failed, passed, skipped, unknown, ERROR, WARN

MANIFEST_REL = (".claude", "scripts", "guards", "hooks.manifest.json")
STAGED_REL = (".claude", "settings.v2.json")
SELFTEST_STALE_NOTE = "recorded against a different manifest"
BLOCKING_EVENTS = ("PreToolUse", "ConfigChange", "PermissionRequest", "UserPromptSubmit")


def _wiring():
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from guards import wiring  # lazy: the doctor must run even if guards/ was not copied
    return wiring


def _load_manifest(ctx: Context) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = ctx.repo_path(*MANIFEST_REL)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _wiring().check_manifest(data)
        return data, None
    except FileNotFoundError:
        return None, "%s is missing: the v2 guards are not installed" % "/".join(MANIFEST_REL)
    except (ValueError, OSError, KeyError) as exc:
        return None, "%s is invalid: %s" % ("/".join(MANIFEST_REL), exc)


def _handlers(settings_files: List[Dict[str, Any]]) -> List[Tuple[str, str, str, Dict[str, Any], str]]:
    """(event, matcher, command, handler, file) for every command hook in every settings file the doctor can see."""
    out = []   # type: List[Tuple[str, str, str, Dict[str, Any], str]]
    for f in settings_files:
        data = f.get("data")
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for h in g.get("hooks", []) if isinstance(g.get("hooks"), list) else []:
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        out.append((event, str(g.get("matcher", "")), h["command"], h, f.get("path", "")))
    return out


def _expected(manifest: Dict[str, Any]) -> List[Tuple[str, str, str, bool]]:
    hooks = _wiring().build_hooks(manifest)
    out = []   # type: List[Tuple[str, str, str, bool]]
    for event, groups in hooks.items():
        for g in groups:
            for h in g["hooks"]:
                out.append((event, g.get("matcher", ""), h["command"], event in BLOCKING_EVENTS))
    return out


def wiring_state(ctx: Context) -> Tuple[str, str]:
    """('active' | 'partial' | 'staged' | 'v1' | 'none' | 'missing', message)."""
    manifest, err = _load_manifest(ctx)
    if manifest is None:
        return "missing", err or ""
    have = {(e, m, c) for e, m, c, _, _ in _handlers(ctx.fact("settings")["files"])}
    want = _expected(manifest)
    present = [w for w in want if (w[0], w[1], w[2]) in have]
    if len(present) == len(want):
        return "active", "all %d guard handlers are wired" % len(want)
    if present:
        missing = [w for w in want if w not in present]
        return "partial", "%d of %d handlers are wired; missing %s" % (len(present), len(want), ", ".join("%s[%s]" % (w[0], w[1] or "*") for w in missing[:4]))
    v1 = any("guard-git.sh" in c or "guard-main-edit.sh" in c for _, _, c, _, _ in _handlers(ctx.fact("settings")["files"]))
    staged = os.path.isfile(ctx.repo_path(*STAGED_REL))
    if v1:
        return "v1", "the live settings still wire the v1 shell hooks%s" % ("; the v2 wiring is staged in .claude/settings.v2.json" if staged else "")
    return ("staged" if staged else "none"), ("no guard hook is wired%s" % ("; the v2 wiring is staged in .claude/settings.v2.json" if staged else ""))


@check("guards.manifest", "settings", "hooks.manifest.json is valid and the staged wiring is current")
def check_manifest(ctx: Context) -> List[Result]:
    manifest, err = _load_manifest(ctx)
    if manifest is None:
        return [failed("guards.manifest", err or "invalid", "restore .claude/scripts/guards/ from the kit release (a kit upgrade, H6)", severity=WARN)]
    out = []   # type: List[Result]
    try:
        from guards import engine
        bad = [g for e in manifest["entries"] for g in e.get("guards", []) if g not in engine.GUARDS and g not in ("injection", "telemetry", "selftest")]
        if bad:
            out.append(failed("guards.manifest", "the manifest names guards that do not exist: %s" % ", ".join(sorted(set(bad))), "reinstall the kit's guards"))
    except Exception as exc:
        out.append(failed("guards.manifest", "the guard package does not import: %s: %s" % (type(exc).__name__, exc), "reinstall the kit's guards"))
    staged = ctx.repo_path(*STAGED_REL)
    if os.path.isfile(staged):
        try:
            with open(staged, "r", encoding="utf-8") as fh:
                cur = fh.read()
            want = _wiring().dumps(_wiring().build_settings(manifest))
            if cur != want:
                out.append(failed("guards.manifest", ".claude/settings.v2.json differs from what the manifest generates", "python3 .claude/scripts/guards/wiring.py --write",
                                  severity=WARN))
        except (OSError, ValueError) as exc:
            out.append(failed("guards.manifest", "cannot read .claude/settings.v2.json: %s" % exc, "", severity=WARN))
    return out or [passed("guards.manifest", "hooks manifest valid: %d entries" % len(manifest["entries"]))]


@check("guards.wiring", "settings", "the effective settings wire every guard handler")
def check_wiring(ctx: Context) -> List[Result]:
    state, msg = wiring_state(ctx)
    if state == "active":
        return [passed("guards.wiring", msg)]
    if state == "partial":
        return [failed("guards.wiring", "inconsistent guard wiring: " + msg, "regenerate with python3 .claude/scripts/guards/wiring.py --print and merge the hooks block (H6)")]
    if state in ("v1", "staged", "none"):
        return [failed("guards.wiring", msg + " (guard activation is the release stage, IMP-13)", "merge .claude/settings.v2.json into .claude/settings.json in one reviewed commit and remove the v1 hooks",
                       severity=WARN)]
    return [failed("guards.wiring", msg, "install the kit's .claude/scripts/guards/", severity=WARN)]


@check("guards.on-failure", "settings", "blocking-capable guard handlers carry onFailure: block")
def check_on_failure(ctx: Context) -> List[Result]:
    manifest, err = _load_manifest(ctx)
    if manifest is None:
        return [skipped("guards.on-failure", "no guard manifest")]
    kit = {c for _, _, c, _ in _expected(manifest)}
    bad = []   # type: List[str]
    seen = 0
    for event, matcher, cmd, h, path in _handlers(ctx.fact("settings")["files"]):
        if cmd in kit and event in BLOCKING_EVENTS:
            seen += 1
            if h.get("onFailure") != "block":
                bad.append("%s[%s]" % (event, matcher or "*"))
    if bad:
        return [failed("guards.on-failure", "guard handlers without onFailure: block: %s. A crashed or missing handler would fail open (RC-30)" % ", ".join(bad[:5]),
                       "add \"onFailure\": \"block\" to each (wiring.py --print emits it)")]
    return [passed("guards.on-failure", "%d blocking-capable guard handler(s) carry onFailure: block" % seen)] if seen else [skipped("guards.on-failure", "no guard handler is wired yet")]


@check("guards.selftest", "settings", "the SessionStart guard self-test ran and passed")
def check_selftest(ctx: Context) -> List[Result]:
    from kitlib import runstate
    st = runstate.RunState.for_path(ctx.repo)
    if st is None:
        return [skipped("guards.selftest", "not a git repository")]
    path = os.path.join(st.root, "selftest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        return [unknown("guards.selftest", "the guard self-test has not run in this repository: the SessionStart hook is not wired or no session has started",
                        "wire the guards (guards.wiring) and start a session")]
    except (ValueError, OSError) as exc:
        return [failed("guards.selftest", "selftest.json is unreadable: %s" % exc, "start a new session; the self-test rewrites it")]
    if not isinstance(rec, dict) or rec.get("ok") is not True:
        fails = rec.get("failures") if isinstance(rec, dict) and isinstance(rec.get("failures"), list) else []
        detail = "; ".join(str(x) for x in fails[:3]) or "no detail"
        return [failed("guards.selftest", "the last guard self-test FAILED: %s" % detail,
                       "the guards are not trustworthy: the effective level is L0 until a session's self-test passes (report §7.1)")]
    manifest, _ = _load_manifest(ctx)
    if manifest is not None:
        try:
            import hashlib
            with open(ctx.repo_path(*MANIFEST_REL), "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            if rec.get("manifest_sha256") and rec["manifest_sha256"] != sha:
                return [failed("guards.selftest", "the self-test passed, but %s" % SELFTEST_STALE_NOTE, "start a new session", severity=WARN)]
        except OSError:
            pass
    return [passed("guards.selftest", "the last guard self-test passed (%s cases, %s)" % (rec.get("cases", "?"), rec.get("at", "?")))]


@check("guards.deny-drift", "settings", "the effective deny rules cover the suggested deny block")
def check_deny_drift(ctx: Context) -> List[Result]:
    try:
        baseline = _wiring().DENY_BASELINE
    except Exception:
        return [skipped("guards.deny-drift", "the guard package is not installed")]
    from . import settings as dsettings
    have = set(dsettings.union_list(ctx.fact("settings"), "permissions.deny", "disallowedTools"))
    missing = [d for d in baseline if d not in have]
    if not missing:
        return [passed("guards.deny-drift", "the effective deny rules cover all %d entries of the suggested deny block" % len(baseline))]
    return [failed("guards.deny-drift", "%d of %d suggested deny entries are missing from the effective settings (for example %s)" %
                   (len(missing), len(baseline), ", ".join(missing[:3])),
                   "merge .claude/templates/settings/deny-baseline.json into your settings (the guards still deny these; the deny block is the second layer, and works in bypass mode)",
                   severity=WARN)]


# -- the cap ---------------------------------------------------------------------------------------------------------------

def cap_guards(ctx: Context, rs: "levelcalc.R") -> levels.Cap:
    st_rec = rs.first("guards.selftest")
    if st_rec is not None and st_rec.status == "fail" and st_rec.severity == ERROR:
        return levels.Cap("guards", levels.L0, "the guard self-test failed: " + st_rec.message[:120])
    w = rs.first("guards.wiring")
    if w is None or w.status != "pass":
        return levels.Cap("guards", levels.L1, "guards v2 are not wired in the effective settings (guards.wiring); L2 and above need active guards")
    return levels.Cap("guards", levels.L5, "guards v2 wired")


if cap_guards not in levelcalc.CAPS:
    levelcalc.CAPS.append(cap_guards)
