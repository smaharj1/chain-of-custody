"""Secrets policy checks (report §5.4, CO-2): where secrets may live, and that they are not in git.

  * no tracked `.env*` (except `.env.example`), keys, state files or credential files,
  * `.gitignore` covers `.env*` (not `.env.example`), key files and state,
  * `.worktreeinclude` lists only `.env.local` and `.env.test`,
  * a secrets scanner (gitleaks or trufflehog) is wired somewhere a PR must pass.

Credentials lying in the working tree (untracked `.env.production`, `*.pem`) are `cred.tree.*`.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from kitlib import redact

from . import safeio
from .model import Context, Result, check, failed, gatherer, passed, skipped, unknown, ERROR, WARN

ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template", ".env.dist")
TRACKED_BAD = [
    ("env-file", re.compile(r"(?:^|/)\.env(?:\.[^/]+)?$"), "an env file"),
    ("key-file", re.compile(r"(?:^|/)[^/]*\.(?:pem|key|p12|pfx|jks|keystore)$|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"), "a key file"),
    ("tfstate", re.compile(r"(?:^|/)[^/]*\.tfstate(?:\.backup)?$"), "Terraform state"),
    ("credentials", re.compile(r"(?:^|/)(?:\.netrc|\.pypirc|\.git-credentials|credentials\.json|\.aws/credentials|\.docker/config\.json)$"), "a credentials file"),
    ("tfvars", re.compile(r"(?:^|/)[^/]*\.auto\.tfvars(?:\.json)?$|(?:^|/)terraform\.tfvars(?:\.json)?$"), "Terraform variables"),
]

IGNORE_MUST = [".env", ".env.local", ".env.test", ".env.development.local", ".env.production", ".env.production.local", "secrets.pem", "server.key"]
IGNORE_MUST_NOT = [".env.example"]


@gatherer("tracked_files")
def gather_tracked(ctx: Context) -> Dict[str, Any]:
    rc, out, err = ctx.git(["ls-files", "-z"], 15.0)
    if rc != 0:
        return {"ok": False, "files": [], "error": (err or "git ls-files failed").strip().split("\n")[0]}
    return {"ok": True, "files": [f for f in out.split("\0") if f], "error": ""}


@check("secrets.tracked", "secrets")
def check_tracked(ctx: Context) -> List[Result]:
    t = ctx.fact("tracked_files")
    if not t["ok"]:
        return [unknown("secrets.tracked", "cannot list tracked files: %s" % t["error"], "run the doctor inside a git repository", severity=WARN)]
    out = []  # type: List[Result]
    for f in t["files"]:
        base = os.path.basename(f)
        for rid, rx, what in TRACKED_BAD:
            if rx.search(f):
                if rid == "env-file" and base in ENV_TEMPLATES:
                    break
                sev = WARN if (rid == "env-file" and base == ".env.test") or rid == "tfvars" else ERROR
                if rid == "key-file":
                    head = safeio.read_text(os.path.join(ctx.repo, f), 4096)
                    if head is not None and "PRIVATE KEY" not in head:
                        break
                out.append(failed("secrets.tracked", "%s is tracked and looks like %s" % (f, what),
                                  "git rm --cached it, add it to .gitignore, and rotate anything it held; local env files hold dev and test values only", severity=sev))
                break
    return out or [passed("secrets.tracked", "no env files, key files, state files or credential files are tracked")]


@check("secrets.gitignore", "secrets")
def check_gitignore(ctx: Context) -> List[Result]:
    t = ctx.fact("tracked_files")
    if not t["ok"]:
        return [unknown("secrets.gitignore", "not a git repository", "", severity=WARN)]
    rc, so, _ = ctx.git(["check-ignore", "--no-index", "--stdin"], 10.0, "\n".join(IGNORE_MUST + IGNORE_MUST_NOT) + "\n")
    ignored = set(l.strip() for l in so.split("\n") if l.strip())
    missing = [p for p in IGNORE_MUST if p not in ignored]
    wrong = [p for p in IGNORE_MUST_NOT if p in ignored]
    out = []  # type: List[Result]
    if missing:
        out.append(failed("secrets.gitignore", ".gitignore does not cover: %s" % ", ".join(missing),
                          "append .claude/templates/security/gitignore.snippet to .gitignore (.env* with !.env.example, key files, state)"))
    if wrong:
        out.append(failed("secrets.gitignore", ".gitignore hides %s, which should be committed as the template" % ", ".join(wrong), "add !.env.example", severity=WARN))
    return out or [passed("secrets.gitignore", ".gitignore covers .env*, key files and local env files, and keeps .env.example")]


@check("secrets.worktreeinclude", "secrets")
def check_worktreeinclude(ctx: Context) -> Result:
    text = safeio.read_text(ctx.repo_path(".worktreeinclude"), 65536)
    if text is None:
        return skipped("secrets.worktreeinclude", "no .worktreeinclude")
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("#")]
    extra = [l for l in lines if l not in (".env.local", ".env.test")]
    if extra:
        return failed("secrets.worktreeinclude", ".worktreeinclude copies more than .env.local and .env.test into worktrees: %s" % ", ".join(extra[:5]),
                      "list only .env.local and .env.test; production files and keys are never copied (report §5.4)")
    return passed("secrets.worktreeinclude", ".worktreeinclude lists only .env.local and .env.test")


def _slurp(path: str) -> str:
    return safeio.read_text(path, 1 << 20) or ""


@check("secrets.scanner", "secrets")
def check_scanner(ctx: Context) -> Result:
    hay = []  # type: List[str]
    for rel in (".pre-commit-config.yaml", ".gitleaks.toml", "gitleaks.toml", ".github/workflows"):
        p = ctx.repo_path(*rel.split("/"))
        if os.path.isfile(p):
            hay.append(rel + " " + _slurp(p))
        elif os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                fp = os.path.join(p, n)
                if os.path.isfile(fp):
                    hay.append(rel + "/" + n + " " + _slurp(fp))
    text = "\n".join(hay).lower()
    if "gitleaks" in text or "trufflehog" in text:
        return passed("secrets.scanner", "a secrets scanner (gitleaks or trufflehog) is configured")
    return failed("secrets.scanner", "no gitleaks or trufflehog in pre-commit or CI", "add one as a pre-commit hook and a required CI check (report §5.4)", severity=WARN)


@check("secrets.in-kit-files", "secrets")
def check_kit_files_for_secrets(ctx: Context) -> Result:
    """Secret-shaped strings in the files the kit writes into git: the config, primers and plan."""
    bad = []  # type: List[str]
    for rel in (".claude/loop.config.md", ".claude/context", "docs/features"):
        p = ctx.repo_path(*rel.split("/"))
        files = [p] if os.path.isfile(p) else ([os.path.join(p, n) for n in sorted(os.listdir(p))] if os.path.isdir(p) else [])
        for fp in files:
            if not os.path.isfile(fp):
                continue
            text = safeio.read_text(fp, 1 << 20)
            if text is None:
                continue
            if redact.find_secrets(text):
                bad.append(os.path.relpath(fp, ctx.repo))
    if bad:
        return failed("secrets.in-kit-files", "secret-shaped strings in: %s" % ", ".join(bad[:6]), "remove and rotate; briefs, reports and config never hold secrets (report §5.4)")
    return passed("secrets.in-kit-files", "no secret-shaped strings in loop.config, primers or feature docs")
