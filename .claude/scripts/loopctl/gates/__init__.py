"""The gate plug-in interface and the runner (report section 9, IMP-22). Phase 4a defines it; Phase 4b implements the gates.

A gate is a module in this package that defines:

    NAME           the toggle name: `gates.<NAME>: on | report | off` in loop.config (g0, g1, g2, g3, g4, g5, g6, g7, g9,
                   g10, g11, evidence). `g8` is a view, not a gate.
    OWNER_METRIC   the metric that justifies the gate (CS-34); a gate whose metric never moves is a candidate for removal
    run(ctx)       -> GateResult, given a GateContext

and optionally:

    DEFAULT_MODE   `on` (the default) or `off` for an OPT-IN gate (G4, the G5 AST lint, G10). Used when loop.config does not
                   set the toggle.
    EVIDENCE_GATED True for a gate that refuses to run until `docs/baseline/evidence.json` is complete (IMP-1).
    REQUIRED_FOR   transitions `((from_status, to_status), ...)` that need this gate to have passed when its mode is `on`
                   (for example G6 for `verifying -> reviewing`). `loopctl advance` enforces it.

The RUNNER, not the gate, applies the toggle and reads it from the BASE sha (CO-11): `off` skips without running, `report`
runs and records but never blocks, `on` blocks on `fail` and `error`. A gate that raises, returns nonsense or takes the wrong
shape becomes an `error` result with failure class `env` (it could not judge: fail closed), never a pass. A gate cannot
change its own toggle, and the context it receives carries the config as read at the base sha.

`GateResult.status`: pass | fail | error | skipped. `failure_class`: none | platform | transient | env | item
(`kitlib`-independent; the taxonomy of failure.py).

Per-AC evidence goes in `GateResult.evidence["ac_results"] = {"AC-F1-1": {"result": "pass"|"fail", "evidence": "<test name>"}}`;
loopctl folds it into the AC x evidence matrix (G1, G8 view). Review verdicts go in `evidence["review"] = {"code": ..., "security": ...}`.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import common, failure

__all__ = ["KNOWN_GATES", "STATUSES", "GateResult", "GateContext", "GateNotInstalled", "discover", "register", "unregister",
           "installed", "get", "mode_for", "run_gate", "required_for"]

KNOWN_GATES = ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "g7", "g9", "g10", "g11", "evidence")
STATUSES = ("pass", "fail", "error", "skipped")
_FAILURE_CLASSES = ("none",) + failure.CLASSES
_registered = {}  # type: Dict[str, Any]
_discovered = None  # type: Optional[Dict[str, Any]]


class GateNotInstalled(common.LoopctlError):
    exit_code = common.EXIT_USAGE


class GateResult(object):
    def __init__(self, status: str = "pass", findings: Optional[List[Dict[str, Any]]] = None,
                 evidence: Optional[Dict[str, Any]] = None, failure_class: str = "none") -> None:
        self.status = status
        self.findings = list(findings or [])
        self.evidence = dict(evidence or {})
        self.failure_class = failure_class

    @classmethod
    def passed(cls, **evidence: Any) -> "GateResult":
        return cls("pass", evidence=evidence)

    @classmethod
    def failed(cls, findings: Sequence[Dict[str, Any]], failure_class: str = "item", **evidence: Any) -> "GateResult":
        return cls("fail", findings=list(findings), evidence=evidence, failure_class=failure_class)

    @classmethod
    def errored(cls, message: str, failure_class: str = "env") -> "GateResult":
        return cls("error", findings=[{"id": "gate.error", "severity": "error", "message": message}], failure_class=failure_class)

    @classmethod
    def skipped(cls, message: str = "") -> "GateResult":
        return cls("skipped", findings=[{"id": "gate.skipped", "severity": "info", "message": message}] if message else [])

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "findings": self.findings, "evidence": self.evidence, "failure_class": self.failure_class}


class GateContext(object):
    """What a gate sees. `cfg` is the loop.config as read at `base_sha`, never from the branch under judgement."""

    def __init__(self, repo: str, item_id: str, item: Dict[str, Any], attempt: int, base_sha: str, head_sha: Optional[str],
                 cfg: Any, plan: Any, state_item: Dict[str, Any], paths: Dict[str, str], runner: str, level: Optional[str],
                 env: Dict[str, str], evidence_passed: bool, worktree: Optional[str] = None) -> None:
        self.repo = repo
        self.item_id = item_id
        self.item = item                    # the plan item (definition, read at the base sha)
        self.attempt = attempt
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.cfg = cfg
        self.plan = plan
        self.state_item = state_item        # a copy of the item's state record
        self.paths = paths                  # repo, runstate, heldout, reports (all absolute)
        self.runner = runner                # sequential | driver
        self.level = level                  # effective level "L1".."L5" or None when not computed
        self.env = env
        self.evidence_passed = evidence_passed
        self.worktree = worktree or repo

    def git(self, args: Sequence[str], **kw: Any) -> Any:
        """Run git in the worktree with the pinned, isolated settings. Fixed argv only."""
        from kitlib import gitutil
        return gitutil.run_git(list(args), cwd=self.worktree, isolated=True, pin_hooks=True, **kw)

    def to_dict(self) -> Dict[str, Any]:
        return {"item": self.item_id, "attempt": self.attempt, "base_sha": self.base_sha, "head_sha": self.head_sha,
                "runner": self.runner, "level": self.level}


# -- registry ----------------------------------------------------------------------------------------------------------------

def register(module: Any) -> None:
    """Register a gate module by hand (tests, or a project gate outside this package)."""
    _check_module(module)
    _registered[module.NAME] = module


def unregister(name: str) -> None:
    _registered.pop(name, None)


def _check_module(module: Any) -> None:
    name = getattr(module, "NAME", None)
    if not isinstance(name, str) or not name or not callable(getattr(module, "run", None)) \
            or not isinstance(getattr(module, "OWNER_METRIC", None), str):
        raise common.LoopctlError("gate module %r must define NAME, OWNER_METRIC and run(ctx)" % getattr(module, "__name__", module))


def discover(refresh: bool = False) -> Dict[str, Any]:
    """Every gate module in this package (and the hand-registered ones), by NAME. A module that fails to import is
    reported as a gate that errors when run, never skipped silently."""
    global _discovered
    if _discovered is not None and not refresh:
        out = dict(_discovered)
        out.update(_registered)
        return out
    found = {}  # type: Dict[str, Any]
    here = os.path.dirname(os.path.abspath(__file__))
    for info in pkgutil.iter_modules([here]):
        if info.name.startswith("_") or info.name == "base":
            continue
        try:
            mod = importlib.import_module("%s.%s" % (__name__, info.name))
            _check_module(mod)
            found[mod.NAME] = mod
        except Exception as exc:  # noqa: BLE001 - surfaced when the gate is run
            found["!" + info.name] = exc
    _discovered = found
    out = dict(found)
    out.update(_registered)
    return out


def installed() -> List[str]:
    return sorted(n for n in discover() if not n.startswith("!"))


def get(name: str) -> Any:
    mods = discover()
    if name in mods:
        return mods[name]
    broken = mods.get("!" + name)
    if isinstance(broken, Exception):
        raise GateNotInstalled("gate %s is installed but fails to import: %s" % (name, common.clean_text(broken, 120)))
    raise GateNotInstalled("gate %s is not installed (installed: %s)" % (name, ", ".join(installed()) or "none"))


def mode_for(cfg: Any, name: str, module: Any = None) -> str:
    """The toggle as read from the config passed in (the base sha's). Unset: the module's DEFAULT_MODE, else `on`."""
    if cfg.is_set("gates." + name):
        return cfg.gate(name)
    default = getattr(module, "DEFAULT_MODE", "on") if module is not None else "on"
    return default if default in ("on", "report", "off") else "on"


def required_for(cfg: Any, frm: str, to: str) -> List[str]:
    """Installed gates whose mode is `on` and that must have passed for the transition frm -> to."""
    out = []  # type: List[str]
    for name in installed():
        mod = get(name)
        if (frm, to) in tuple(getattr(mod, "REQUIRED_FOR", ())) and mode_for(cfg, name, mod) == "on":
            out.append(name)
    return out


def run_gate(name: str, gctx: GateContext) -> Dict[str, Any]:
    """Run one gate with the toggle applied. Returns the recorded dict (never raises for a gate's own failure)."""
    mod = get(name)
    cfg = gctx.cfg
    mode = mode_for(cfg, name, mod)
    base = {"name": name, "owner_metric": getattr(mod, "OWNER_METRIC", ""), "mode": mode, "head": gctx.head_sha}
    if mode == "off":
        return dict(base, status="skipped", blocking=False, findings=[{"id": "gate.off", "severity": "info", "message": "gates.%s is off" % name}],
                    evidence={}, failure_class="none", duration_s=0.0)
    if getattr(mod, "EVIDENCE_GATED", False) and not gctx.evidence_passed:
        return dict(base, status="error", blocking=True, evidence={}, failure_class="env", duration_s=0.0,
                    findings=[{"id": "gate.evidence-required", "severity": "error",
                               "message": "gate %s is OPT-IN and evidence-gated: it refuses to run until docs/baseline/evidence.json is complete (IMP-1)" % name}])
    t0 = time.time()
    try:
        raw = mod.run(gctx)
        res = _coerce(raw)
    except Exception as exc:  # noqa: BLE001 - a crashing gate must read as an error, never as a pass
        res = GateResult.errored("gate %s crashed: %s: %s" % (name, type(exc).__name__, common.clean_text(exc, 160)))
    dur = round(time.time() - t0, 3)
    blocking = mode == "on" and res.status in ("fail", "error")
    findings = [dict(f, message=common.clean_text(f.get("message", ""), 400)) for f in res.findings if isinstance(f, dict)][:50]
    return dict(base, status=res.status, blocking=blocking, findings=findings, evidence=res.evidence,
                failure_class=res.failure_class, duration_s=dur)


def _coerce(raw: Any) -> GateResult:
    if isinstance(raw, dict):
        raw = GateResult(raw.get("status", ""), raw.get("findings"), raw.get("evidence"), raw.get("failure_class", "none"))
    if not isinstance(raw, GateResult):
        return GateResult.errored("gate returned %s instead of a GateResult" % type(raw).__name__)
    if raw.status not in STATUSES:
        return GateResult.errored("gate returned an unknown status %r" % common.clean_text(raw.status, 20))
    if raw.failure_class not in _FAILURE_CLASSES:
        return GateResult.errored("gate returned an unknown failure class %r" % common.clean_text(raw.failure_class, 20))
    if not isinstance(raw.findings, list) or not isinstance(raw.evidence, dict):
        return GateResult.errored("gate returned malformed findings or evidence")
    if raw.status == "fail" and raw.failure_class == "none":
        raw.failure_class = "item"
    return raw
