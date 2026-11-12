"""Guard wiring checks (Phase 2; report §7.1, §7.2, §5.8; IMP-13, IMP-16, IMP-21).

    guards.manifest      the kit's hooks manifest loads, every guard it names exists, the plugin's hooks/hooks.json is what the
                         manifest generates and every script it runs exists
    guards.wiring        nothing switches the plugin's hooks off: no settings file the doctor sees sets `disableAllHooks`, or sets
                         the kit to false in `enabledPlugins`; and no settings file wires kit hook scripts a second time (the
                         plugin's hooks/hooks.json is the only wiring; a copy-install hooks block is a residue)
    guards.on-failure    every blocking-capable handler in hooks/hooks.json carries `onFailure: block` (RC-30), otherwise a crashed
                         guard fails open
    guards.selftest      the SessionStart self-test ran and passed; a failed self-test caps the level at L0 (§7.1)
    guards.deny-drift    the effective deny rules cover the suggested deny block (§7.2: "the doctor diffs the effective settings
                         against the suggested deny block and sandbox profile, and reports any drift")
    guards.v1-residue    no v1 shell hook (guard-git.sh, guard-main-edit.sh) is wired or shipped: both layers would run, and the
                         weaker one is a bypass list (D10, GD-7)

Registered by importing this module (doctor.py does). It adds one cap, `guards`, to the effective-level computation:
L0 when the self-test failed, L1 while the wiring is not proven, otherwise no cap. It reads only files; it never runs a guard.
The kit files it reads are the kit's own (KIT_ROOT), never the project's.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from kitlib import kitpaths, levels

from . import levelcalc
from .model import Context, Result, check, failed, passed, skipped, unknown, ERROR, WARN

KIT_ROOT = kitpaths.KIT_ROOT          # a module attribute so a test can point the checks at another kit tree
MANIFEST_REL = ("scripts", "guards", "hooks.manifest.json")
HOOKS_REL = ("hooks", "hooks.json")
SELFTEST_STALE_NOTE = "recorded against a different manifest"
BLOCKING_EVENTS = ("PreToolUse", "ConfigChange", "PermissionRequest", "UserPromptSubmit")


def _wiring():
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from guards import wiring  # lazy: the doctor must still run if the guard package does not import
    return wiring


def _kit(*parts: str) -> str:
    return os.path.join(KIT_ROOT, *parts)


def _show(*parts: str) -> str:
    return "${CLAUDE_PLUGIN_ROOT}/" + "/".join(parts)


def _load_manifest(ctx: Context) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        with open(_kit(*MANIFEST_REL), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _wiring().check_manifest(data)
        return data, None
    except FileNotFoundError:
        return None, "%s is missing: the kit's guards are not installed" % _show(*MANIFEST_REL)
    except (ValueError, OSError, KeyError) as exc:
        return None, "%s is invalid: %s" % (_show(*MANIFEST_REL), exc)


def _load_hooks() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        with open(_kit(*HOOKS_REL), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
            return None, "%s has no hooks object" % _show(*HOOKS_REL)
        return data, None
    except FileNotFoundError:
        return None, "%s is missing: no guard runs" % _show(*HOOKS_REL)
    except (ValueError, OSError) as exc:
        return None, "%s is invalid: %s" % (_show(*HOOKS_REL), exc)


def _handlers(data: Any) -> List[Tuple[str, str, Dict[str, Any]]]:
    """(event, matcher, handler) for every command hook in a hooks object."""
    out = []   # type: List[Tuple[str, str, Dict[str, Any]]]
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for event, groups in (hooks.items() if isinstance(hooks, dict) else []):
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") if isinstance(g.get("hooks"), list) else []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.append((event, str(g.get("matcher", "")), h))
    return out


def kit_problems(manifest: Dict[str, Any]) -> List[str]:
    """hooks/hooks.json differs from what the manifest generates, or a script it runs is missing."""
    w = _wiring()
    data, err = _load_hooks()
    out = [err] if err else []   # type: List[str]
    if data is not None and data != w.build_plugin_hooks(manifest):
        out.append("%s differs from what the manifest generates" % _show(*HOOKS_REL))
    for rel in w.hook_scripts(manifest):
        if not os.path.isfile(_kit("scripts", rel)):
            out.append("%s is missing: a hook runs it" % _show("scripts", rel))
    return out


def _switches(ctx: Context, manifest: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(kind, message) for every settings file the doctor sees that turns the guards off or wires them twice.
    `disableAllHooks` and each `enabledPlugins` key follow settings precedence: the highest scope that sets the value decides."""
    from . import settings as dsettings
    w = _wiring()
    files = [f for f in ctx.fact("settings")["files"] if f.get("data") is not None]
    ordered = sorted(files, key=lambda f: dsettings.SCOPE_ORDER.index(f["scope"]) if f["scope"] in dsettings.SCOPE_ORDER else 99)
    out = []   # type: List[Tuple[str, str]]
    for f in ordered:
        if "disableAllHooks" in f["data"]:
            if f["data"]["disableAllHooks"] is True:
                out.append(("disable-all-hooks", "%s (%s) sets disableAllHooks: no guard runs" % (f["path"], f["scope"])))
            break
    decided = set()   # type: set
    for f in ordered:
        enabled = f["data"].get("enabledPlugins")
        for k in w.plugin_keys(enabled):
            if k in decided:
                continue
            decided.add(k)
            if enabled[k] is False:
                out.append(("plugin-disabled", "%s (%s) sets enabledPlugins %s to false: the kit and its guards are off" % (f["path"], f["scope"], k)))
    for f in files:
        if f["scope"] == "managed" and f["data"].get("allowManagedHooksOnly") is True:
            continue        # the plugin's hooks are off there and the managed copy is the wiring (managed.settings checks it has the guards)
        out.extend(p for p in w.settings_problems({"hooks": f["data"].get("hooks")}, "%s (%s)" % (f["path"], f["scope"]), manifest)
                   if p[0] == "double-wired")
    return out


def wiring_state(ctx: Context) -> Tuple[str, str]:
    """('active' | 'off' | 'double' | 'broken' | 'missing', message)."""
    manifest, err = _load_manifest(ctx)
    if manifest is None:
        return "missing", err or ""
    bad = kit_problems(manifest)
    if bad:
        return "broken", "; ".join(bad)
    sw = _switches(ctx, manifest)
    off = [m for k, m in sw if k in ("disable-all-hooks", "plugin-disabled")]
    if off:
        return "off", "; ".join(off)
    if sw:
        return "double", "; ".join(m for _, m in sw)
    n = len(_handlers(_load_hooks()[0]))
    return "active", "the plugin wires all %d guard handlers and no settings file switches them off" % n


@check("guards.manifest", "settings", "the kit's hooks manifest is valid and hooks/hooks.json is what it generates")
def check_manifest(ctx: Context) -> List[Result]:
    manifest, err = _load_manifest(ctx)
    if manifest is None:
        return [failed("guards.manifest", err or "invalid", "reinstall the kit plugin (claude plugin install)")]
    out = []   # type: List[Result]
    try:
        from guards import engine
        bad = [g for e in manifest["entries"] for g in e.get("guards", []) if g not in engine.GUARDS and g not in ("injection", "telemetry", "selftest")]
        if bad:
            out.append(failed("guards.manifest", "the manifest names guards that do not exist: %s" % ", ".join(sorted(set(bad))), "reinstall the kit plugin"))
    except Exception as exc:
        out.append(failed("guards.manifest", "the guard package does not import: %s: %s" % (type(exc).__name__, exc), "reinstall the kit plugin"))
    for msg in kit_problems(manifest):
        out.append(failed("guards.manifest", msg, "reinstall the kit plugin; in a kit checkout run python3 %s --write" % kitpaths.script("guards/wiring.py")))
    return out or [passed("guards.manifest", "hooks manifest valid: %d entries; hooks/hooks.json matches it" % len(manifest["entries"]))]


@check("guards.wiring", "settings", "nothing switches the plugin's guard hooks off or wires them twice")
def check_wiring(ctx: Context) -> List[Result]:
    state, msg = wiring_state(ctx)
    if state == "active":
        return [passed("guards.wiring", msg)]
    if state == "off":
        return [failed("guards.wiring", "the guards are switched off: " + msg,
                       "remove disableAllHooks, and the kit's false entry in enabledPlugins, from that settings file (a kit checkout's own "
                       "settings switch the plugin off on purpose; that checkout runs at L1)")]
    if state == "double":
        return [failed("guards.wiring", "the guards are wired twice: " + msg,
                       "delete that hooks block: the plugin's hooks/hooks.json is the only wiring (a copy-install residue; the migrate step removes it)")]
    return [failed("guards.wiring", msg, "reinstall the kit plugin")]


@check("guards.on-failure", "settings", "blocking-capable guard handlers carry onFailure: block")
def check_on_failure(ctx: Context) -> List[Result]:
    data, err = _load_hooks()
    if data is None:
        return [skipped("guards.on-failure", err or "no hooks/hooks.json")]
    bad = []   # type: List[str]
    seen = 0
    for event, matcher, h in _handlers(data):
        if event in BLOCKING_EVENTS:
            seen += 1
            if h.get("onFailure") != "block":
                bad.append("%s[%s]" % (event, matcher or "*"))
    if bad:
        return [failed("guards.on-failure", "guard handlers without onFailure: block: %s. A crashed or missing handler would fail open (RC-30)" % ", ".join(bad[:5]),
                       "reinstall the kit plugin (wiring.py emits onFailure: block)")]
    return [passed("guards.on-failure", "%d blocking-capable guard handler(s) carry onFailure: block" % seen)] if seen else [skipped("guards.on-failure", "no blocking guard handler")]


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
            with open(_kit(*MANIFEST_REL), "rb") as fh:
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
                   "merge %s into your settings (the guards still deny these; the deny block is the second layer, and works in bypass mode)"
                   % kitpaths.kit("templates", "settings", "deny-baseline.json"), severity=WARN)]


V1_HOOK_FILES = ("guard-git.sh", "guard-main-edit.sh")


@check("guards.v1-residue", "settings", "no v1 shell hook is wired or shipped next to the v2 guards")
def check_v1_residue(ctx: Context) -> List[Result]:
    """The v1 hooks (`guard-git.sh`, `guard-main-edit.sh`) are weak: `sh -c`, `eval`, `env`, an absolute path, an alias or `xargs` get past them and a missing
    python3 means allow. The plugin's guards close all of that, so a v1 hook that is still wired or present is a regression (D10)."""
    wired = sorted({f for sf in ctx.fact("settings")["files"] for _, _, h in _handlers(sf.get("data")) for f in V1_HOOK_FILES if f in h["command"]})
    on_disk = [f for f in V1_HOOK_FILES if os.path.isfile(ctx.repo_path(".claude", "hooks", f))]
    if not wired and not on_disk:
        return [passed("guards.v1-residue", "no v1 shell hook is wired or present")]
    what = []   # type: List[str]
    if wired:
        what.append("settings still wire %s" % ", ".join(wired))
    if on_disk:
        what.append("%s still exist%s under .claude/hooks/" % (", ".join(on_disk), "s" if len(on_disk) == 1 else ""))
    fix = "remove the v1 hook entries from .claude/settings.json and delete .claude/hooks/guard-git.sh and guard-main-edit.sh in one reviewed commit"
    return [failed("guards.v1-residue", "%s. The plugin's guards run too, and the weaker layer is a bypass list (sh -c, eval, env, absolute path, alias, xargs)"
                   % "; ".join(what), fix)]


# -- the cap ---------------------------------------------------------------------------------------------------------------

def cap_guards(ctx: Context, rs: "levelcalc.R") -> levels.Cap:
    st_rec = rs.first("guards.selftest")
    if st_rec is not None and st_rec.status == "fail" and st_rec.severity == ERROR:
        return levels.Cap("guards", levels.L0, "the guard self-test failed: " + st_rec.message[:120])
    w = rs.first("guards.wiring")
    if w is None or w.status != "pass":
        return levels.Cap("guards", levels.L1, "the plugin's guard hooks are not proven active (guards.wiring); L2 and above need active guards")
    return levels.Cap("guards", levels.L5, "the plugin's guard hooks are active")


if cap_guards not in levelcalc.CAPS:
    levelcalc.CAPS.append(cap_guards)
