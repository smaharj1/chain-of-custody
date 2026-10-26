#!/usr/bin/env python3
"""doctor: the boundary check for the Chain of Custody kit (Phase 1, report §5, §6.2, §8.4, §14; IMP-21, IMP-22).

Standalone, Python 3.9+ stdlib only, no loopctl. It answers one question: given what this
machine and this repo look like right now, what autonomy level is safe?

    python3 .claude/scripts/doctor.py                       everything, human-readable
    python3 .claude/scripts/doctor.py --json                the doctor.v1 document
    python3 .claude/scripts/doctor.py --credentials         B2: credentials the agent's OS user could reach
    python3 .claude/scripts/doctor.py --deploy-surface      B5: how a commit becomes a deploy, and the attestation
    python3 .claude/scripts/doctor.py --level [L3]          the effective level and every cap with its reason
    python3 .claude/scripts/doctor.py --gh-fixture FILE     read GitHub facts from recorded `gh api` JSON
    python3 .claude/scripts/doctor.py --live                human only: also run sts, gh auth status, git credential fill,
                                                            ssh -T, and probe the metadata endpoints

The effective level is min(configured, prerequisites, credentials, sandbox, MCP, B5, platform,
evidence, version). Out of the box, with nothing attested, that is L1.

Exit codes: 0 no errors, 1 errors present (the level is capped), 2 usage or internal error.

Safety: the doctor never prints a secret value (names and paths only). It never runs `claude`
inside a Claude Code session, and `--live` refuses to run there (agents never run it).
Run it from a plain terminal, as the OS user the agent runs as.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitlib import gitutil, levels  # noqa: E402

from doctorlib import (ci, codeowners, creds, deploy, github, guardchecks, levelcalc, secrets, settings, versions)  # noqa: E402,F401
from doctorlib.model import CHECKS, ERROR, FAIL, UNKNOWN, WARN, Context, Result, default_reach, default_run, run_checks  # noqa: E402

SCHEMA = "doctor.v1"

# CLI mode -> check groups shown
MODE_GROUPS = {
    "credentials": {"credentials"},
    "deploy-surface": {"deploy-surface"},
}
GROUP_TITLES = [("credentials", "Credentials reachable by the agent (B2)"), ("deploy-surface", "Deployment surface (B5)"),
                ("github", "GitHub: identity, rulesets, platform (B1, B4)"), ("codeowners", "CODEOWNERS (H3, H6)"), ("ci", "CI (split CI, RC-3)"),
                ("settings", "Settings, MCP and containers"), ("sandbox", "Sandbox (B3)"), ("mcp", "MCP (CS-10)"), ("secrets", "Secrets policy (§5.4)"),
                ("version", "Versions (§14)"), ("config", "loop.config")]


class UsageError(Exception):
    """Bad invocation: exit 2 with the message on stderr."""


def build_context(args: argparse.Namespace, env: Optional[Dict[str, str]] = None) -> Context:
    env = dict(os.environ if env is None else env)
    home = args.home or env.get("HOME") or os.path.expanduser("~")
    if args.home:
        env["HOME"] = args.home
    repo = os.path.abspath(args.repo)
    top = gitutil.toplevel(repo) if os.path.isdir(repo) else None
    if top:
        repo = top
    if args.live and "CLAUDECODE" in env:
        raise UsageError("--live is human-only and refuses to run inside a Claude Code session (CLAUDECODE is set). Run it from a plain terminal.")
    ghapi = None
    if args.gh_fixture:
        with open(args.gh_fixture, "r", encoding="utf-8") as fh:
            ghapi = github.fixture_ghapi(json.load(fh))
    elif args.live:
        ghapi = github.live_ghapi(default_run, env)
    ctx = Context(repo=repo, home=home, env=env, live=bool(args.live), run=default_run,
                  reach=default_reach if args.live else None, ghapi=ghapi, claude_version=args.claude_version,
                  config_ref=args.config_ref, extra_settings=args.settings, workflow_exempt=args.human_profile,
                  configured_level=args.level if isinstance(args.level, str) else None)
    ctx.allow_path_claude = "CLAUDECODE" not in env  # type: ignore[attr-defined]
    return ctx


def attestations(ctx: Context) -> List[Dict[str, str]]:
    out = []  # type: List[Dict[str, str]]

    def add(key: str, status: str, detail: str) -> None:
        out.append({"key": key, "status": status, "detail": detail})
    surface = ctx.fact("deploy_surface")
    state, msg = deploy.attestation_state(ctx, surface)
    add("deploy_surface", state, msg)
    for key, label in (("sandbox_verified", "sandbox"), ("managed_settings_verified", "managed settings"), ("promotion_attested", "promotion bar")):
        v = str(ctx.cfg(key, "") or "")
        add(key, "set" if v else "missing", v or "%s not attested" % label)
    iso = str(ctx.cfg("agent_isolation", "none") or "none")
    add("agent_isolation", "set" if iso != "none" else "missing", iso)
    att = ctx.cfg("platform_attested", []) or []
    add("platform_attested", "set" if att else "missing", ", ".join(str(a) for a in att) or "no GitHub features attested")
    add("bot", "set" if str(ctx.cfg("bot.kind", "none")) != "none" else "missing",
        "%s %s" % (ctx.cfg("bot.kind", "none"), ctx.cfg("bot.login", "") or ""))
    return out


def run(ctx: Context, groups: Optional[set]) -> Dict[str, Any]:
    results = run_checks(ctx)
    eff = levelcalc.compute(ctx, results)
    shown = [r for r in results if groups is None or _group_of(r) in groups]
    return {"schema": SCHEMA, "repo": ctx.repo, "level": eff.to_dict(), "next": levelcalc.next_blocker(eff),
            "checks": [r.to_dict() for r in shown], "attestations": attestations(ctx), "_eff": eff, "_results": shown}


def _group_of(r: Result) -> str:
    best = None
    for c in CHECKS:
        if r.id == c["id"] or r.id.startswith(c["id"] + ".") or c["id"].startswith(r.id):
            if best is None or len(c["id"]) > len(best["id"]):
                best = c
    if best is None:
        for c in CHECKS:
            if r.id.split(".")[0] == c["id"].split(".")[0]:
                return c["group"]
        return "other"
    return best["group"]


_CONTROL_RE = None


def clean(text: str) -> str:
    """Strip control characters (ANSI escapes included) from text that came from the repo: a hostile file name must not drive the terminal."""
    global _CONTROL_RE
    if _CONTROL_RE is None:
        import re
        _CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
    return _CONTROL_RE.sub("?", text)


def format_text(doc: Dict[str, Any], ctx: Context, show_level_only: bool = False) -> str:
    eff = doc["_eff"]
    lines = []  # type: List[str]
    if not show_level_only:
        results = doc["_results"]
        for group, title in GROUP_TITLES:
            rs = [r for r in results if _group_of(r) == group]
            if not rs:
                continue
            lines.append("== %s ==" % title)
            for r in rs:
                mark = {"pass": "ok  ", "skipped": "skip", "unknown": "??  ", "fail": "FAIL"}[r.status]
                sev = "" if r.status in ("pass", "skipped") else " [%s]" % r.severity
                lines.append("  %s %s%s: %s" % (mark, r.id, sev, r.message))
                if r.remediation and r.status in ("fail", "unknown"):
                    lines.append("       -> %s" % r.remediation)
            lines.append("")
        lines.append("== Attestations (a human commits these under H6) ==")
        for a in doc["attestations"]:
            lines.append("  %-26s %-8s %s" % (a["key"], a["status"], a["detail"]))
        lines.append("")
    lines.append("== Autonomy level ==")
    lines.append(eff.explain())
    lines.append("next: " + doc["next"])
    errors = sum(1 for r in doc["_results"] if r.status == FAIL and r.severity == ERROR)
    warns = sum(1 for r in doc["_results"] if r.severity == WARN and r.status in (FAIL, UNKNOWN))
    lines.append("")
    lines.append("%d error(s), %d warning(s)" % (errors, warns))
    return clean("\n".join(lines))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="doctor.py", description="Boundary check for the Chain of Custody kit (report §5, §6.2, §8.4).")
    ap.add_argument("--repo", default=".", help="repository to check (default: current directory)")
    ap.add_argument("--home", default=None, help="treat this directory as HOME (default: $HOME)")
    ap.add_argument("--json", action="store_true", help="print the doctor.v1 JSON document")
    ap.add_argument("--credentials", action="store_true", help="show only the credential checks (B2)")
    ap.add_argument("--deploy-surface", dest="deploy_surface", action="store_true", help="show only the deployment-surface checks (B5)")
    ap.add_argument("--level", nargs="?", const=True, default=False, metavar="Lx",
                    help="show the effective level and its caps; with a value, evaluate as if that level were configured")
    ap.add_argument("--live", action="store_true", help="human only: also run sts, gh auth status, git credential fill, ssh -T and probe metadata endpoints")
    ap.add_argument("--gh-fixture", default=None, metavar="FILE", help="read GitHub facts from recorded `gh api` JSON instead of the network")
    ap.add_argument("--settings", action="append", default=[], metavar="FILE", help="also check this settings file (a driver child's --settings); repeatable")
    ap.add_argument("--human-profile", action="append", default=[], metavar="FILE", help="a settings file that is the human-run /orchestrate profile (may allow Workflow)")
    ap.add_argument("--config-ref", default=None, metavar="REV", help="read loop.config from this git revision, not the working tree (base-sha rule, report §9)")
    ap.add_argument("--claude-version", default=None, metavar="V", help="the installed Claude Code version (the doctor does not run claude inside a Claude Code session)")
    ap.add_argument("--list-checks", action="store_true", help="print every check id and group, then exit")
    return ap


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if args.list_checks:
        for c in CHECKS:
            print("%-34s %-15s %s" % (c["id"], c["group"], c["doc"]))
            for e in c["emits"]:
                print("  %-32s %-15s (also reported by %s)" % (e + ("<suffix>" if e.endswith(".") else ""), c["group"], c["id"]))
        return 0
    if isinstance(args.level, str):
        try:
            levels.parse_level(args.level)
        except ValueError as exc:
            ap.error(str(exc))
    try:
        ctx = build_context(args, env)
    except UsageError as exc:
        sys.stderr.write("doctor: %s\n" % exc)
        return 2
    except FileNotFoundError as exc:
        sys.stderr.write("doctor: %s\n" % exc)
        return 2
    except ValueError as exc:
        sys.stderr.write("doctor: bad input: %s\n" % exc)
        return 2
    groups = None  # type: Optional[set]
    selected = set()  # type: set
    if args.credentials:
        selected |= MODE_GROUPS["credentials"]
    if args.deploy_surface:
        selected |= MODE_GROUPS["deploy-surface"]
    if selected:
        groups = selected
    try:
        doc = run(ctx, groups)
    except Exception as exc:  # an internal error is exit 2, never a silent pass
        sys.stderr.write("doctor: internal error: %s: %s\n" % (type(exc).__name__, exc))
        return 2
    if args.json:
        out = {k: v for k, v in doc.items() if not k.startswith("_")}
        print(json.dumps(out, indent=2, sort_keys=False))
    else:
        level_only = bool(args.level) and not selected
        print(format_text(doc, ctx, show_level_only=level_only))
        if args.deploy_surface:
            print("\ndeploy-surface fingerprint: %s" % ctx.fact("deploy_surface")["fingerprint"])
    has_error = any(r.status == FAIL and r.severity == ERROR for r in doc["_results"])
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
