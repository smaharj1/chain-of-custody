"""CODEOWNERS parsing and coverage (H3, H6, B4; report §6.1, §16.1 Phase 1).

CODEOWNERS only means something when the ruleset requires code-owner review (`gh.codeowner-review`),
so this module checks the file itself: that the paths whose change is a guardrail change
(`.claude/`, `.mcp.json`, plugin dirs, CI, scripts, IaC, the deploy-surface floor, CODEOWNERS
itself) have an owner rule of their own, that no rule un-owns them, and that the bot is never an owner.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from . import deploy
from . import safeio
from .model import Context, Result, check, failed, gatherer, passed, skipped, WARN

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

# Paths whose change is a guardrail change (H3/H6). Samples, not globs: each must be owned.
SENSITIVE_SAMPLES = [
    ".claude/settings.json", ".claude/loop.config.md", ".claude/agents/engineer.md", ".claude/hooks/guard.sh", ".claude/scripts/doctor.py",
    ".mcp.json", ".claude-plugin/plugin.json", "plugin/hooks/hooks.json", "scripts/deploy.sh", ".github/workflows/ci.yml", ".github/CODEOWNERS",
]


def parse(text: str) -> List[Dict[str, object]]:
    rules = []  # type: List[Dict[str, object]]
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.split(" #", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        parts = line.split()
        rules.append({"pattern": parts[0], "owners": parts[1:], "line": n})
    return rules


def _to_regex(pattern: str) -> "re.Pattern":
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    out = ""
    i = 0
    while i < len(body):
        c = body[i]
        if body[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
            continue
        if body[i:i + 2] == "**":
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile("^" + prefix + out + "$")


def matches(pattern: str, path: str) -> bool:
    """gitignore-style: a pattern that matches a directory owns everything under it."""
    rx = _to_regex(pattern)
    parts = path.strip("/").split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    if pattern.endswith("/"):
        candidates = candidates[:-1] if len(candidates) > 1 else []
    return any(rx.match(c) for c in candidates)


def owner_of(rules: List[Dict[str, object]], path: str) -> Optional[Dict[str, object]]:
    """Last matching rule wins."""
    found = None
    for r in rules:
        if matches(str(r["pattern"]), path):
            found = r
    return found


@gatherer("codeowners")
def gather_codeowners(ctx: Context) -> Dict[str, object]:
    for rel in CODEOWNERS_LOCATIONS:
        p = ctx.repo_path(*rel.split("/"))
        text = safeio.read_text(p, 1 << 20)
        if text is not None:
            return {"path": rel, "rules": parse(text)}
    return {"path": None, "rules": []}


def sample_paths(ctx: Context) -> List[str]:
    samples = list(SENSITIVE_SAMPLES)
    surface = ctx.fact("deploy_surface")
    for f in surface["files"]:
        if not f.startswith(".github/") and f not in samples and len(samples) < 40:
            samples.append(f)
    iac = [f.replace(os.sep, "/") for f in ctx.fact("repo_files") if f.endswith((".tf", ".tofu", "cdk.json", "Pulumi.yaml"))]
    for f in iac[:6]:
        if f not in samples:
            samples.append(f)
    return samples


@check("codeowners.file", "codeowners")
def check_file(ctx: Context) -> Result:
    co = ctx.fact("codeowners")
    if co["path"] is None:
        return failed("codeowners.file", "no CODEOWNERS file: nobody is required to review changes to .claude/, CI or IaC",
                      "copy .claude/templates/security/CODEOWNERS.template to .github/CODEOWNERS and fill in the owners", severity=WARN)
    return passed("codeowners.file", "%s has %d rule(s)" % (co["path"], len(co["rules"])))


@check("codeowners.coverage", "codeowners")
def check_coverage(ctx: Context) -> List[Result]:
    co = ctx.fact("codeowners")
    if co["path"] is None:
        return [skipped("codeowners.coverage", "no CODEOWNERS file")]
    out = []  # type: List[Result]
    rules = co["rules"]
    unowned, catchall = [], []  # type: List[str], List[str]
    for path in sample_paths(ctx):
        r = owner_of(rules, path)
        if r is None or not r["owners"]:
            unowned.append(path)
        elif str(r["pattern"]) in ("*", "/*", "**", "/**"):
            catchall.append(path)
    if unowned:
        out.append(failed("codeowners.coverage", "no owner for: %s" % ", ".join(unowned[:8]) + (" ..." if len(unowned) > 8 else ""),
                          "add path rules for .claude/, plugin/, .claude-plugin/, .mcp.json, scripts/, .github/ and IaC (CODEOWNERS.template has them)"))
    if catchall and not unowned:
        out.append(failed("codeowners.coverage", "guardrail paths are covered only by the catch-all rule (%d path(s), e.g. %s): any reviewer approves a guardrail change"
                          % (len(catchall), catchall[0]), "add explicit rules so guardrail paths name a smaller set of owners", severity=WARN))
    if not unowned and not catchall:
        out.append(passed("codeowners.coverage", "every guardrail path has an explicit owner rule"))
    return out


@check("codeowners.bot", "codeowners")
def check_bot_not_owner(ctx: Context) -> Result:
    co = ctx.fact("codeowners")
    bot = str(ctx.cfg("bot.login", "") or "").strip().lower().replace("[bot]", "")
    if co["path"] is None or not bot:
        return skipped("codeowners.bot", "nothing to check (no CODEOWNERS or no bot.login)")
    for r in co["rules"]:
        for o in r["owners"]:
            if o.lower().lstrip("@").replace("[bot]", "") == bot:
                return failed("codeowners.bot", "the bot %s is a code owner (line %s): its review could satisfy the code-owner requirement" % (o, r["line"]),
                              "remove it from CODEOWNERS")
    return passed("codeowners.bot", "the bot is not a code owner")


@check("codeowners.syntax", "codeowners")
def check_syntax(ctx: Context) -> Result:
    co = ctx.fact("codeowners")
    if co["path"] is None:
        return skipped("codeowners.syntax", "no CODEOWNERS file")
    bad = []  # type: List[str]
    for r in co["rules"]:
        for o in r["owners"]:
            if not re.match(r"^@[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$|^[^@\s]+@[^@\s]+\.[^@\s]+$", o):
                bad.append("line %s: %r" % (r["line"], o))
            if o.startswith("@OWNER") or o.startswith("@your-") or "@ORG" in o or "PLACEHOLDER" in o.upper():
                bad.append("line %s: placeholder %r" % (r["line"], o))
    if bad:
        return failed("codeowners.syntax", "invalid or placeholder owners: %s" % "; ".join(bad[:5]), "fix the owner handles (a bad owner line is skipped silently by GitHub)")
    return passed("codeowners.syntax", "owner handles look valid")
