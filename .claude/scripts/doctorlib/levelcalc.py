"""The effective level and its caps (report §6.2, §6.3, §8.4, IMP-21).

    effective = min(configured, prerequisites, credentials, sandbox, mcp, B5, platform, evidence, version)

Each cap prints its reason whether or not it binds, so `doctor --level` shows where the
next level is blocked. Everything here reads `Result` objects and facts; it does no I/O of its own.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from kitlib import evidence, levels, runstate

from . import deploy
from .model import Context, Result, check, failed, passed, skipped, ERROR, WARN, PASS, FAIL, UNKNOWN

L = levels


class R(object):
    """Index over results by id prefix."""

    def __init__(self, results: List[Result]) -> None:
        self.results = results

    def with_prefix(self, prefix: str) -> List[Result]:
        return [r for r in self.results if r.id == prefix or r.id.startswith(prefix + ".") or r.id.startswith(prefix)]

    def errors(self, prefix: str) -> List[Result]:
        return [r for r in self.with_prefix(prefix) if r.status == FAIL and r.severity == ERROR]

    def not_ok(self, prefix: str) -> List[Result]:
        return [r for r in self.with_prefix(prefix) if r.status in (FAIL, UNKNOWN)]

    def all_pass(self, prefix: str) -> bool:
        rs = self.with_prefix(prefix)
        return bool(rs) and all(r.status == PASS or r.status == "skipped" for r in rs) and any(r.status == PASS for r in rs)

    def first(self, prefix: str) -> Optional[Result]:
        rs = self.with_prefix(prefix)
        return rs[0] if rs else None


def attested(ctx: Context) -> List[str]:
    v = ctx.cfg("platform_attested", []) or []
    return [str(x) for x in v] if isinstance(v, list) else []


def env_reviewers_available(ctx: Context) -> bool:
    gh = ctx.fact("gh")
    if gh.get("environments"):
        for e in gh["environments"]:
            for r in e.get("protection_rules") or []:
                if isinstance(r, dict) and r.get("type") == "required_reviewers" and r.get("reviewers"):
                    return True
    return "environment_reviewers" in attested(ctx)


def configured_level(ctx: Context) -> int:
    if ctx.configured_level_override:
        return L.parse_level(ctx.configured_level_override)
    try:
        return L.parse_level(str(ctx.cfg("autonomy_level", "L1")))
    except ValueError:
        return L.L1


def _short(rs: List[Result], n: int = 3) -> str:
    return "; ".join("%s: %s" % (r.id, r.message) for r in rs[:n]) + (" (+%d more)" % (len(rs) - n) if len(rs) > n else "")


# --- the caps -------------------------------------------------------------------------------------------

def cap_credentials(ctx: Context, rs: R) -> L.Cap:
    errs = [r for r in rs.errors("cred.")]
    if errs:
        return L.Cap("credentials", L.L1, "reachable credential(s): " + _short(errs))
    meta = rs.first("cred.net.metadata")
    if meta is not None and meta.status != PASS:
        return L.Cap("credentials", L.L1, "metadata endpoints not probed and not denied by the sandbox (run --credentials --live, or deny 169.254.0.0/16)")
    return L.Cap("credentials", L.L5, "no reachable credentials found")


def cap_sandbox(ctx: Context, rs: R) -> L.Cap:
    iso = str(ctx.cfg("agent_isolation", "none") or "none")
    if iso != "none":
        return L.Cap("sandbox", L.L5, "agent_isolation: %s (attested)" % iso)
    cfg_ok = all(r.status == PASS for r in rs.with_prefix("sandbox.") if r.id in ("sandbox.enabled", "sandbox.failIfUnavailable",
                                                                                   "sandbox.allowUnsandboxedCommands", "sandbox.excludedCommands"))
    att = rs.first("sandbox.attestation")
    if cfg_ok and att is not None and att.status == PASS:
        return L.Cap("sandbox", L.L5, "sandbox configured fail-closed and attested")
    why = [r.id for r in rs.with_prefix("sandbox.") if r.status != PASS and r.id != "sandbox.deny-paths"]
    return L.Cap("sandbox", L.L1, "sandbox not proven (V19): " + (", ".join(why) + " not satisfied" if why else "no attestation"))


def cap_mcp(ctx: Context, rs: R) -> L.Cap:
    errs = rs.errors("mcp.")
    if errs:
        return L.Cap("mcp", L.L1, "deploy-capable MCP server or plugin enabled: " + _short(errs))
    return L.Cap("mcp", L.L5, "no deploy-capable MCP servers or plugins")


def cap_b5(ctx: Context, rs: R) -> L.Cap:
    surface = ctx.fact("deploy_surface")
    ev = deploy.evaluate(ctx, surface)
    caps = deploy.level_caps(ctx, surface, ev)
    if not caps:
        return L.Cap("b5-deploy-surface", L.L5, "deployment surface attested; no ungated deploy")
    lvl = min(c[0] for c in caps)
    reasons = []  # type: List[str]
    for c_lvl, c_reason in caps:
        if c_lvl == lvl and c_reason not in reasons:
            reasons.append(c_reason)
    return L.Cap("b5-deploy-surface", lvl, "; ".join(reasons[:4]) + (" (+%d more)" % (len(reasons) - 4) if len(reasons) > 4 else ""))


def cap_platform(ctx: Context, rs: R) -> L.Cap:
    o = ctx.fact("origin")
    if not o["is_github"]:
        return L.Cap("platform", L.L1, "not a GitHub-hosted repo: every server-side control here is a GitHub feature (§8.4)")
    mp = rs.first("gh.main-protection")
    if mp is None or mp.status != PASS:
        msg = mp.message if mp is not None else "no data"
        return L.Cap("platform", L.L1, "no protection on the default branch is proven (unknown means unavailable): " + msg)
    cr = rs.first("gh.codeowner-review")
    if cr is None or cr.status != PASS:
        return L.Cap("platform", L.L2, "code-owner review is not proven: " + (cr.message if cr is not None else "no data"))
    if ctx.fact("deploy_surface")["iac"] and not env_reviewers_available(ctx):
        return L.Cap("platform", L.L3, "apply gate = human dispatch: no Environment with required reviewers (RC-2); IaC projects stay at L3")
    return L.Cap("platform", L.L5, "platform features available")


def cap_evidence(ctx: Context, rs: R) -> L.Cap:
    st = evidence.check(ctx.repo)
    return L.evidence_cap(bool(st), st.reason)


def cap_version(ctx: Context, rs: R) -> L.Cap:
    cv = ctx.fact("claude_version")
    return L.version_cap(cv["status"], "claude %s, range %s to %s" % (cv["text"] or "unknown", cv["min"], cv["tested_max"]))


# --- prerequisites (what is left after the caps above) ----------------------------------------------------------------

def _nonces_present(ctx: Context) -> bool:
    st = runstate.RunState.for_path(ctx.repo)
    if st is None:
        return False
    try:
        return any(os.path.isfile(os.path.join(st.nonces_dir, n)) for n in os.listdir(st.nonces_dir))
    except OSError:
        return False


def prerequisites(ctx: Context, rs: R) -> Dict[int, List[Tuple[str, bool, str]]]:
    kind = str(ctx.cfg("bot.kind", "none"))
    bot_errs = rs.errors("bot.identity")
    runner = ctx.cfg("runner", None)
    l2 = [
        ("B1 bot identity", kind != "none" and not bot_errs and rs.first("bot.identity") is not None and rs.first("bot.identity").status != UNKNOWN,
         "bot.kind is %s%s" % (kind, "; " + bot_errs[0].message if bot_errs else "")),
        ("hook-liveness canary", _nonces_present(ctx), "no SessionStart nonce is recorded: the session_start hook is not wired or never ran (§5.8)"),
        ("held-out isolation (RC-6)", runner == "driver" or "g3_ci_only" in attested(ctx),
         "runner is not driver and G3 held-out tests are not attested CI-only (add g3_ci_only to platform_attested once the heldout job exists)"),
    ]
    cod = rs.first("codeowners.coverage")
    l3 = [
        ("B1 bot is a GitHub App", kind == "github-app", "bot.kind is %s: a machine-user PAT is acceptable at L2 only" % kind),
        ("B4 rulesets: no bypass for the bot, review required", rs.all_pass("gh.main-protection") and not rs.not_ok("gh.bypass") and rs.first("gh.bypass") is not None,
         "gh.bypass / gh.main-protection: " + (_short(rs.not_ok("gh.bypass") + rs.not_ok("gh.main-protection")) or "no data")),
        ("required checks (verify, evidence, secrets scan)", rs.first("gh.required-checks") is not None and rs.first("gh.required-checks").status == PASS,
         "gh.required-checks is not proven"),
        ("CODEOWNERS covers guardrail paths", cod is not None and cod.status == PASS and not rs.errors("codeowners"),
         "codeowners: " + (_short(rs.not_ok("codeowners")) or "no CODEOWNERS")),
        ("split CI (ci_lint clean)", not rs.errors("ci.") and not rs.errors("infra."), "ci_lint errors: " + _short(rs.errors("ci.") + rs.errors("infra."))),
        ("conductor has no web tools", rs.first("settings.web-tools") is not None and rs.first("settings.web-tools").status == PASS, "WebFetch/WebSearch are not denied"),
        ("managed settings or credential-free container", rs.first("managed.settings") is not None and rs.first("managed.settings").status == PASS,
         "managed.settings is not proven"),
    ]
    iac = ctx.fact("deploy_surface")["iac"]
    if iac:
        l3.append(("IaC apply gate the bot cannot trigger", env_reviewers_available(ctx) or "apply_human_dispatch" in attested(ctx),
                   "IaC found (%s) but neither Environment reviewers nor a human-only dispatch is attested (apply_human_dispatch)" % ", ".join(iac)))
    loopctl_dir = os.path.join(ctx.repo, ".claude", "scripts", "loopctl")
    l4 = [
        ("loopctl installed", os.path.isdir(loopctl_dir), "loopctl is not installed (Phase 4)"),
        ("spend ceiling set", ctx.config is not None and ctx.config.is_set("run_budget_usd"), "run_budget_usd is not set in loop.config"),
    ]
    explicit = ctx.config.explicit if ctx.config is not None else {}
    l5 = [
        ("promotion bar attested", bool(str(ctx.cfg("promotion_attested", "") or "").strip()), "promotion_attested is empty (§6.3)"),
        ("mutation testing on", explicit.get("gates.g4") == "on", "gates.g4 is not explicitly on"),
    ]
    return {2: l2, 3: l3, 4: l4, 5: l5}


def cap_prerequisites(ctx: Context, rs: R) -> L.Cap:
    prereq = prerequisites(ctx, rs)
    reached = L.L1
    for lvl in (2, 3, 4, 5):
        failing = [p for p in prereq[lvl] if not p[1]]
        if failing:
            return L.Cap("prerequisites", reached, "L%d needs: %s" % (lvl, "; ".join("%s (%s)" % (n, why) for n, ok, why in failing[:4])))
        reached = lvl
    return L.Cap("prerequisites", L.L5, "all prerequisites pass")


CAPS = [cap_prerequisites, cap_credentials, cap_sandbox, cap_mcp, cap_b5, cap_platform, cap_evidence, cap_version]


def compute(ctx: Context, results: List[Result]) -> L.EffectiveLevel:
    rs = R(results)
    caps = [fn(ctx, rs) for fn in CAPS]
    return L.effective_level(configured_level(ctx), caps)


def next_blocker(eff: L.EffectiveLevel) -> str:
    if eff.effective >= eff.configured:
        return "configured level reached" if eff.configured < L.L5 else "L5 reached"
    b = eff.binding
    return "; ".join("%s caps at %s: %s" % (c.name, L.level_name(c.level), c.reason) for c in b[:3])


# --- config checks ---------------------------------------------------------------------------------------------------

@check("config.load", "config")
def check_config_load(ctx: Context) -> List[Result]:
    cfg = ctx.config
    if cfg is None:
        return [failed("config.load", "loop.config could not be read: %s" % ctx.config_error, "create .claude/loop.config.md from the kit template", severity=WARN)]
    out = []  # type: List[Result]
    for i in cfg.errors:
        out.append(failed("config.load", str(i), "fix the key (see .claude/scripts/kitlib/schema/*.json)", severity=WARN))
    return out or [passed("config.load", "loop.config parsed with no errors")]


@check("config.evidence-gate", "config")
def check_evidence_gate(ctx: Context) -> List[Result]:
    cfg = ctx.config
    if cfg is None:
        return [skipped("config.evidence-gate", "no loop.config")]
    st = evidence.check(ctx.repo)
    bad = cfg.evidence_gated_violations()
    if bad and not st:
        return [failed("config.evidence-gate", "%s need the evidence gate, which has not passed (%s)" % (", ".join(bad), st.reason),
                       "unset them, or commit docs/baseline/evidence.json when 3 clean fixture runs and 1 real feature are done (IMP-1)")]
    if bad:
        return [passed("config.evidence-gate", "evidence-gated keys (%s) are allowed: the evidence gate passed" % ", ".join(bad))]
    return [passed("config.evidence-gate", "no evidence-gated key is set")]


@check("config.dirty", "config")
def check_config_dirty(ctx: Context) -> Result:
    """Attestations count only when a human committed them (H6). An uncommitted edit may be the agent's."""
    if ctx.config_ref:
        return passed("config.dirty", "loop.config is read from %s, not the working tree" % ctx.config_ref)
    rc, out, err = ctx.git(["status", "--porcelain", "--", ".claude/loop.config.md"])
    if rc != 0:
        return skipped("config.dirty", "not a git repository, or git status failed")
    if out.strip():
        return failed("config.dirty", "loop.config.md has uncommitted changes: attestations in it are not reviewed",
                      "commit it through review, or run the doctor with --config-ref <base branch> to read the committed version", severity=WARN)
    return passed("config.dirty", "loop.config.md matches HEAD")
