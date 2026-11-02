"""Phase 11b checks: the L5 prerequisites, the remote lock, G10, and the fork-mode environment (report 6.2 L5, 10.3, 9 G10, 11.3; IMP-1; V18).

    l5.merge-policy   `merge_policy: auto-low-risk` is refused (an error) until the evidence gate, the promotion bar, mutation testing and B5 hold.
                      The default `human` passes. NOTHING in this kit merges for you whatever the value says: the doctor is the second wall.
    l5.b5             L5 is never available on a repo that deploys on merge to main without a human gate, nor on one with an ungated deploy on any push (B5, RC-1).
    lock.remote       the remote named in `remote_lock` is a configured remote (never a URL); the lock is advisory (RC-12).
    g10.config        a G10 that is switched on has at least one check, and no check command uses shell syntax (they run without a shell).
    fork.env          the fork-mode and background-task variables in the headless profile the driver builds (and in any --settings file given). [probe] V18: the
                      NAMES are the report's assumption. Until the probe confirms them the loop does not depend on them: dispatch is event-driven (report file
                      plus state transition), so a wrong name costs nothing; this check exists so that a right name is never silently lost.

Adds checks only. The doctor core and the effective-level logic (`levelcalc.py`) are not rewritten: levelcalc imports `l5_prerequisites` from here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from kitlib import evidence

from . import deploy, settings
from .model import Context, Result, check, failed, passed, skipped, unknown, ERROR, WARN

# [probe] V18: the names and values are the report's (11.3). The driver sets exactly these in every child's --settings env (loopctl/launch.py FORK_ENV).
FORK_ENV_EXPECTED = {"CLAUDE_CODE_FORK_SUBAGENT": "0", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
G10_KEYS = ("g10_a11y_cmd", "g10_perf_cmd", "g10_pii_cmd", "g10_observability_cmd")
_SHELL_SYNTAX = re.compile(r"[;&|<>`$\\(){}*?!]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def _explicit(ctx: Context, name: str) -> Any:
    cfg = ctx.config
    return cfg.explicit.get(name) if cfg is not None else None


def l5_prerequisites(ctx: Context) -> List[Tuple[str, bool, str]]:
    """What L5 needs on top of L4 that levelcalc's list does not already say: the policy switch itself and the low-risk globs."""
    policy = str(ctx.cfg("merge_policy", "human") or "human")
    paths = ctx.cfg("low_risk_paths", None)
    return [
        ("merge_policy is auto-low-risk", policy == "auto-low-risk",
         "merge_policy is %s: a person merges every pull request (the default). L5 is an opt-in, and this build merges nothing even then" % policy),
        ("low_risk_paths names something", isinstance(paths, list) and bool(paths), "low_risk_paths is empty: nothing could ever be low-risk"),
    ]


def b5_blockers(ctx: Context) -> List[str]:
    surface = ctx.fact("deploy_surface")
    ev = deploy.evaluate(ctx, surface)
    caps = deploy.level_caps(ctx, surface, ev)
    return ["%s (caps at L%d)" % (reason, lvl) for lvl, reason in caps if lvl < 5]


@check("l5.merge-policy", "config", emits=("l5.b5",), doc="merge_policy: auto-low-risk is refused until the evidence gate, promotion bar, mutation and B5 hold")
def check_merge_policy(ctx: Context) -> List[Result]:
    cfg = ctx.config
    if cfg is None:
        return [skipped("l5.merge-policy", "no loop.config")]
    policy = str(ctx.cfg("merge_policy", "human") or "human")
    configured = str(ctx.cfg("autonomy_level", "L1"))
    out = []  # type: List[Result]
    if policy == "human":
        out.append(passed("l5.merge-policy", "merge_policy is human: a person merges every pull request (the default)"))
    else:
        blockers = []  # type: List[str]
        st = evidence.check(ctx.repo)
        if not st:
            blockers.append("the evidence gate has not passed (%s)" % st.reason)
        if not _DATE_RE.match(str(ctx.cfg("promotion_attested", "") or "").strip()):
            blockers.append("promotion_attested is not a date (the 6.3 bar is not attested)")
        if _explicit(ctx, "gates.g4") != "on":
            blockers.append("gates.g4 is not explicitly on (mutation testing)")
        blockers += b5_blockers(ctx)
        if configured != "L5":
            blockers.append("autonomy_level is %s, not L5" % configured)
        if blockers:
            out.append(failed("l5.merge-policy", "merge_policy: %s is refused: %s" % (policy, "; ".join(blockers[:5])) + (" (+%d more)" % (len(blockers) - 5) if len(blockers) > 5 else ""),
                              "leave merge_policy: human. L5 auto-merge is an opt-in for a repo that has earned it; nothing in this build merges for you either way", severity=ERROR))
        else:
            out.append(passed("l5.merge-policy", "merge_policy is %s and every L5 prerequisite holds; this build still merges nothing (a person merges every pull request; L5 enabling is design-only)" % policy))
    if policy != "human" or configured == "L5":
        bl = b5_blockers(ctx)
        if bl:
            out.append(failed("l5.b5", "L5 is never available here: %s" % "; ".join(bl[:3]),
                              "gate the deploy with an Environment that has required reviewers, or accept that this repo stays below L5 (B5, RC-1)", severity=ERROR if policy != "human" else WARN))
        else:
            out.append(passed("l5.b5", "no ungated deploy on any push or on main, and the deployment surface is attested (B5)"))
    return out


@check("lock.remote", "config", doc="remote_lock names a configured git remote (advisory, never a URL)")
def check_remote_lock(ctx: Context) -> Result:
    remote = ctx.cfg("remote_lock", "")
    if not remote:
        return skipped("lock.remote", "remote_lock is empty: the local run lock alone")
    if not isinstance(remote, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z", remote):
        return failed("lock.remote", "remote_lock %r is not a remote name (a URL is never accepted)" % (remote,), "name a remote from `git remote`, e.g. origin")
    rc, out, _err = ctx.git(["remote"])
    if rc != 0:
        return unknown("lock.remote", "git remote could not be read", "run the doctor inside the repository", severity=WARN)
    if remote not in out.split():
        return failed("lock.remote", "remote_lock %r is not a configured remote of this repository" % remote, "add the remote, or fix the name")
    return passed("lock.remote", "remote_lock %s is a configured remote. The lock is advisory (RC-12): anyone with contents: write can move refs/loopctl/*" % remote)


@check("g10.config", "config", doc="a switched-on G10 has checks, and no check command uses shell syntax")
def check_g10(ctx: Context) -> Result:
    mode = _explicit(ctx, "gates.g10")
    if mode not in ("on", "report"):
        return skipped("g10.config", "gates.g10 is off (OPT-IN, EXPERIMENTAL)")
    cmds = [(k, ctx.cfg(k, "")) for k in G10_KEYS]
    cmds = [(k, v) for k, v in cmds if isinstance(v, str) and v.strip() and v.strip() != "<command>"]
    if not cmds:
        return failed("g10.config", "gates.g10 is %s but no g10_*_cmd is set: the kit bundles no product-check tool" % mode,
                      "set at least one of %s, or gates.g10: off" % ", ".join(G10_KEYS))
    bad = [(k, m.group(0)) for k, v in cmds for m in [_SHELL_SYNTAX.search(v)] if m]
    if bad:
        return failed("g10.config", "%s uses shell syntax (%r): G10 commands are run without a shell" % bad[0],
                      "write the program and its arguments only, and wrap anything else in a script of yours")
    return passed("g10.config", "gates.g10 is %s with %d check(s): %s" % (mode, len(cmds), ", ".join(k[4:-4] for k, _v in cmds)))


# -- fork-mode environment (V18) -------------------------------------------------------------------------------------------------------

def _env_problems(env: Any) -> List[str]:
    env = env if isinstance(env, dict) else {}
    return ["%s is %s, not %r" % (k, repr(env[k]) if k in env else "missing", v) for k, v in sorted(FORK_ENV_EXPECTED.items()) if str(env.get(k, "")) != v]


@check("fork.env", "settings", emits=("fork.env.", ), doc="the fork-mode and background-task env in the headless profile the driver builds [probe V18]")
def check_fork_env(ctx: Context) -> List[Result]:
    out = []  # type: List[Result]
    runner = str(ctx.cfg("runner", "sequential") or "sequential")
    note = " [probe V18: these names are the report's assumption; dispatch is event-driven either way]"
    if runner == "driver":
        try:
            import os
            from kitlib import runstate
            from loopctl import launch
            rs = runstate.RunState.for_path(ctx.repo)
            tdir = ctx.repo_path(".claude", "templates", "settings")
            for kind in ("engineer", "critic"):
                s, _skipped = launch.build_settings(kind, rs, ctx.config, template_dir=tdir if os.path.isdir(tdir) else None)
                bad = _env_problems(s.get("env"))
                rid = "fork.env.%s" % kind
                out.append(failed(rid, "the %s profile the driver builds does not pin fork mode: %s%s" % (kind, "; ".join(bad), note),
                                  "the driver sets these in launch.FORK_ENV; a profile that lacks them was built by something else") if bad
                           else passed(rid, "the %s profile pins CLAUDE_CODE_FORK_SUBAGENT=0 and CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1%s" % (kind, note)))
        except Exception as exc:  # noqa: BLE001 - a profile that cannot be built is a finding, not a crash
            out.append(unknown("fork.env.driver", "the driver's headless profile could not be built here (%s: %s)" % (type(exc).__name__, str(exc)[:100]),
                               "run the doctor in a repository that has the kit's .claude/templates/settings/", severity=WARN))
    else:
        data = ctx.fact("settings")
        have = {k: settings.effective(data, "env." + k) for k in FORK_ENV_EXPECTED}
        have = {k: v for k, v in have.items() if v is not None}
        for k in FORK_ENV_EXPECTED:
            if k in ctx.env:
                have.setdefault(k, ctx.env[k])
        bad = _env_problems(have)
        if not bad:
            out.append(passed("fork.env.conductor", "the conductor's settings pin both variables%s" % note))
        elif "CLAUDE_CODE_FORK_SUBAGENT" in have and str(have["CLAUDE_CODE_FORK_SUBAGENT"]) != "0":
            out.append(failed("fork.env.conductor", "CLAUDE_CODE_FORK_SUBAGENT is %r: with fork mode on, the parent cannot block on a subagent%s" % (have["CLAUDE_CODE_FORK_SUBAGENT"], note),
                              "unset it, or set it to 0 in the settings env", severity=WARN))
        else:
            out.append(unknown("fork.env.conductor", "runner is %s and the settings do not pin the fork-mode variables (%s); the loop does not depend on them%s" % (runner, "; ".join(bad), note),
                               "optional: set the two variables in the settings env", severity=WARN))
    for path in ctx.extra_settings:
        data, err = settings.load_json_file(path)
        rid = "fork.env.file"
        if err or not isinstance(data, dict):
            out.append(unknown(rid, "%s could not be read: %s" % (path, err or "not an object"), "check the file", severity=WARN))
            continue
        bad = _env_problems(data.get("env"))
        out.append(failed(rid, "%s does not pin fork mode: %s%s" % (path, "; ".join(bad), note), "add the two variables to its env block") if bad
                   else passed(rid, "%s pins both fork-mode variables%s" % (path, note)))
    return out
