#!/usr/bin/env python3
"""pr_prepare: validate a finished feature branch and print the commands a HUMAN runs to deliver it
(Phase 3; IMP-25, report §16.1 Phase 3, §19.7 trailers, revert detection, CI-fix, Doc Delta, labels).

THIS SCRIPT NEVER PUSHES AND NEVER OPENS A PULL REQUEST. It runs read-only git only (the allowlist is in
`READ_ONLY_GIT` and is enforced on every call), never runs `gh`, and prints the exact `git push` and
`gh pr create --draft --body-file` commands for the human to run, from a terminal or with `!` in the
session. The repo's guard-git hook blocks `git push` for agents; do not work around it.

Subcommands
-----------
  init      write the PR body from `.claude/templates/pr-body.md`, pre-filled with what git, the engineer
            reports and the verify result already know (branch, shas, engineers, Doc Delta, human-gated
            paths). The conductor fills the rest.
  check     validate the body and the git state; print findings; exit 1 on any error.
  prepare   `check`, and when it is clean, print the commands for the configured `delivery`.
  reverts   find reverts of given commits (git log, or recorded GitHub PR JSON): human-revert detection (L-5).

Every subcommand takes --repo, --slug, --branch, --base, --remote, --body-file (default
`.claude/pr/<slug>.md`, gitignored), --verify-json (default `.claude/pr/<slug>.verify.json`), --json.

What `check` enforces
---------------------
  body      no `<fill: ...>` marker left, no empty field, the audit fields hold valid values, Verify says
            PASS, every engineer row is APPROVED with a passing code review, every acceptance criterion has
            evidence and PASS, Doc Delta and Primer Delta are written (NONE needs a `checked:` line), no
            secret-shaped text, within GitHub's size limit.
  git       a valid non-protected branch, a clean tree, at least one commit ahead of the base, the body's
            head sha is the current HEAD (a stale audit block is an error), WIP commits carry a `Feature:`
            trailer, no diverged remote branch (never force-push).
  reports   `docs/features/<slug>/reports/*.md` are well formed and APPROVED, and each engineer appears in
            the body; their Doc Delta and Primer Delta are not dropped.
  verify    the `verify.v1` result is a complete pass for this HEAD, read from the base sha.
  H3        every changed path that needs a human reviewer (migrations, auth/payments via
            `always_human_paths`, `.github/`, `.claude/`, lockfiles, test and CI config, deploy files) is
            listed in the body, and "NONE" is refused when the diff touches one.
  CI-fix    commits with a `CI-Fix-Of:` trailer stay within `max_ci_fix_attempts`, do not touch workflows,
            tests or lockfiles, and are named in the body.
  labels    `--label` is refused for approver-only labels (`factory:*`, `plan-ok`, `preview`; RC-20).

Importable API (loopctl reuses it): `FIELDS`, `parse_body`, `validate_body`, `parse_report`, `classify_paths`,
`find_reverts`, `reverts_from_prs`, `check`, `init_body`, `build_commands`.

JSON output, schema `pr_prepare.v1`:

    {"schema": "pr_prepare.v1", "mode": "check" | "prepare", "ok": bool,
     "repo": str, "slug": str, "branch": str, "base": str, "base_sha": sha | null, "head_sha": sha | null,
     "delivery": "local" | "push-branch" | "draft-pr", "git_autonomy": str, "body_file": str, "title": str,
     "ahead": int, "changed_paths": int,
     "human_gated": [{"path", "reason"}],
     "ci_fix": {"count": int, "max": int, "commits": [sha]},
     "reports": [{"engineer", "path", "status", "ok"}],
     "findings": [{"id", "severity": "error" | "warn" | "info", "message"}],
     "commands": [str]}             only when mode is "prepare" and ok; each is a line for the human to run

Exit codes: 0 clean (warnings allowed), 1 errors found, 2 usage or internal error.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitlib import config as kconfig  # noqa: E402
from kitlib import gitutil, jsonio, redact  # noqa: E402

SCHEMA = "pr_prepare.v1"
BODY_LIMIT = 60000          # GitHub rejects bodies over 65536 characters
TITLE_LIMIT = 72
TEMPLATE_REL = os.path.join(".claude", "templates", "pr-body.md")
PR_DIR_REL = os.path.join(".claude", "pr")

# --------------------------------------------------------------------------------------------------
# read-only git (IMP-25: this script cannot push)
# --------------------------------------------------------------------------------------------------

READ_ONLY_GIT = frozenset(["rev-parse", "rev-list", "log", "diff", "diff-tree", "show", "status", "merge-base", "show-ref",
                           "for-each-ref", "ls-files", "cat-file", "symbolic-ref", "config", "remote"])
_CONFIG_READ_FLAGS = frozenset(["--get", "--get-all", "--list", "-l", "--get-regexp"])
_REMOTE_READ = frozenset(["get-url", "-v", "show"])


class PrError(Exception):
    """An input problem that prevents the check from running at all."""


def _git(repo: str, args: Sequence[str]):
    """Run one read-only git command. Anything outside the allowlist is a bug and raises."""
    if not args or args[0] not in READ_ONLY_GIT:
        raise PrError("internal: git %s is not on the read-only allowlist" % (args[0] if args else ""))
    if args[0] == "config" and not (len(args) > 1 and args[1] in _CONFIG_READ_FLAGS):
        raise PrError("internal: git config is allowed only with a read flag")
    if args[0] == "remote" and not (len(args) > 1 and args[1] in _REMOTE_READ):
        raise PrError("internal: git remote is allowed only with get-url, -v or show")
    # A repository can carry config that runs a program when git looks at it (core.fsmonitor, external diff drivers, textconv):
    # pin those off, as the doctor does for hostile repositories (docs/security/git-escalation.md).
    extra = ["--no-ext-diff", "--no-textconv"] if args[0] in ("diff", "log", "show", "diff-tree") else []
    return gitutil.run_git(["-c", "core.fsmonitor=false"] + [args[0]] + extra + list(args[1:]), cwd=repo, pin_hooks=True)


def _out(repo: str, args: Sequence[str]) -> Optional[str]:
    proc = _git(repo, args)
    return proc.stdout if proc.returncode == 0 else None


# --------------------------------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------------------------------

class Finding(object):
    __slots__ = ("id", "severity", "message")

    def __init__(self, fid: str, severity: str, message: str) -> None:
        self.id, self.severity, self.message = fid, severity, message

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.id, "severity": self.severity, "message": self.message}

    def __repr__(self) -> str:
        return "Finding(%s, %s)" % (self.id, self.severity)


def _err(out: List[Finding], fid: str, msg: str) -> None:
    out.append(Finding(fid, "error", msg))


def _warn(out: List[Finding], fid: str, msg: str) -> None:
    out.append(Finding(fid, "warn", msg))


# --------------------------------------------------------------------------------------------------
# the PR body: fields, parsing, validation (pure text)
# --------------------------------------------------------------------------------------------------

_FILL_RE = re.compile(r"<fill:[^>]*>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}\Z")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_SHORT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_BRANCH_PREFIXES = ("feat/", "quick/", "fix/", "chore/", "docs/")
PROTECTED_BRANCHES = ("main", "master", "develop", "trunk", "production", "prod")
APPROVER_ONLY_LABEL_RE = re.compile(r"^(factory:|plan-ok\Z|preview\Z)", re.I)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 :._/-]{0,48}\Z")
DELIVERIES = ("local", "push-branch", "draft-pr")


class FieldSpec(object):
    def __init__(self, name: str, kind: str, doc: str = "", none_ok: bool = False) -> None:
        self.name, self.kind, self.doc, self.none_ok = name, kind, doc, none_ok


# Order matters: pr-body.md lists them in this order (a test checks).
FIELDS = [
    FieldSpec("Feature", "slug", "feature slug"),
    FieldSpec("Lane", "enum:full,quick"),
    FieldSpec("Delivery", "enum:local,push-branch,draft-pr"),
    FieldSpec("Branch", "branch"),
    FieldSpec("Base", "branch"),
    FieldSpec("Base sha", "sha"),
    FieldSpec("Head sha", "sha"),
    FieldSpec("Design critic", "prefix:DESIGN_APPROVED,N/A"),
    FieldSpec("Verify (G6)", "prefix:PASS"),
    FieldSpec("Security review", "prefix:SECURITY_APPROVED,N/A,NOT RUN"),
    FieldSpec("Human-gated paths", "free", none_ok=True),
    FieldSpec("CI-Fix-Of", "free", none_ok=True),
    FieldSpec("Waivers", "free", none_ok=True),
    FieldSpec("CI evidence", "prefix:PENDING,https://"),
]
REQUIRED_SECTIONS = ("Summary", "Audit block", "Engineers", "Acceptance matrix", "Doc Delta", "Primer Delta", "Notes")
CODE_REVIEW_OK = ("CODE_REVIEW_APPROVED", "SKIPPED (trivial")
REPORT_SECTIONS = ("Status", "Summary", "Files Changed", "Tests Added", "API Contract Status", "Iteration",
                   "Open Questions / Brief Gaps", "Primer Delta", "Doc Delta", "Verification")
REPORT_STATUSES = ("APPROVED", "PARTIAL", "NEEDS_CLARIFICATION", "CONTRACT_DEVIATION", "CONTRACT_INCONSISTENCY",
                   "DESIGN_DEVIATION", "ESCALATION_NEEDED")


class ParsedBody(object):
    def __init__(self) -> None:
        self.title = ""
        self.sections = {}  # type: Dict[str, str]
        self.fields = {}  # type: Dict[str, str]
        self.duplicates = []  # type: List[str]
        self.engineers = []  # type: List[Dict[str, str]]
        self.matrix = []  # type: List[Dict[str, str]]
        self.has_audit_markers = False
        self.hidden_comments = []  # type: List[str]


_FENCE_RE = re.compile(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)")
_ALLOWED_COMMENTS = ("kit:pr-body", "audit:begin", "audit:end")


def strip_fences(text: str) -> str:
    """Fenced code blocks removed: a table inside one renders as code, so it must not count as a field."""
    return _FENCE_RE.sub("", text)


def _tables(text: str) -> List[Dict[str, str]]:
    """Rows of every markdown table in `text` as {header: cell}. A table row starts with `|` in column 0; an indented or
    quoted one renders differently on GitHub, so it is not read (fail closed)."""
    rows = []  # type: List[Dict[str, str]]
    block = []  # type: List[str]
    blocks = []  # type: List[List[str]]
    for ln in text.split("\n"):
        if ln.startswith("|"):
            block.append(ln.rstrip())
        else:
            if block:
                blocks.append(block)
            block = []
    if block:
        blocks.append(block)

    def cells(ln: str) -> List[str]:
        inner = ln[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        return [c.strip() for c in inner.split("|")]

    for b in blocks:
        if len(b) < 2:
            continue
        head = cells(b[0])
        body = b[2:] if re.match(r"^\|\s*:?-{2,}", b[1]) else b[1:]
        for ln in body:
            c = cells(ln)
            rows.append({head[i]: (c[i] if i < len(c) else "") for i in range(len(head))})
    return rows


def parse_body(text: str) -> ParsedBody:
    pb = ParsedBody()
    visible = strip_fences(text)
    comments = list(re.finditer(r"<!--(.*?)-->", visible, re.S))
    begins = [m for m in comments if m.group(1).strip().startswith("audit:begin")]
    ends = [m for m in comments if m.group(1).strip().startswith("audit:end")]
    pb.hidden_comments = [m.group(1).strip()[:40] for m in comments if not m.group(1).strip().startswith(_ALLOWED_COMMENTS)]
    region = ""
    if len(begins) == 1 and len(ends) == 1 and begins[0].end() <= ends[0].start():
        pb.has_audit_markers = True
        region = visible[begins[0].end():ends[0].start()]
    clean = _COMMENT_RE.sub("", visible)
    m = re.search(r"^# (.+?)\s*$", clean, re.M)
    pb.title = m.group(1).strip() if m else ""
    parts = re.split(r"^## (.+?)\s*$", clean, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        pb.sections[parts[i].strip()] = parts[i + 1].strip()
    for row in _tables(_COMMENT_RE.sub("", region)):
        name = row.get("Field", "").strip()
        if not name:
            continue
        if name in pb.fields:
            pb.duplicates.append(name)
        pb.fields[name] = row.get("Value", "").strip()
    pb.engineers = _tables(pb.sections.get("Engineers", ""))
    pb.matrix = _tables(pb.sections.get("Acceptance matrix", ""))
    return pb


def _check_value(spec: FieldSpec, value: str) -> Optional[str]:
    """None if `value` is acceptable for the field, else a short reason."""
    kind = spec.kind
    if spec.none_ok and value.split(None, 1)[0:1] == ["NONE"]:
        return None
    if kind == "slug":
        return None if _SLUG_RE.match(value) else "must be a lower-case slug (letters, digits, . _ -)"
    if kind.startswith("enum:"):
        choices = kind[5:].split(",")
        return None if value in choices else "must be one of %s" % ", ".join(choices)
    if kind == "branch":
        ok = _BRANCH_RE.match(value) and ".." not in value and "//" not in value and not value.endswith(("/", ".", ".lock")) and "@{" not in value
        return None if ok else "is not a valid branch name"
    if kind == "sha":
        return None if _SHA_RE.match(value) else "must be a full 40-character sha"
    if kind.startswith("prefix:"):
        allowed = kind[7:].split(",")
        return None if any(value.startswith(a) for a in allowed) else "must start with %s" % " or ".join(allowed)
    return None


def validate_body(text: str, lane: Optional[str] = None) -> List[Finding]:
    """Textual rules only (no git). `lane` overrides the body's own Lane field."""
    out = []  # type: List[Finding]
    if len(text) > BODY_LIMIT:
        _err(out, "audit.too-long", "the body is %d characters; GitHub's limit is 65536 (keep it under %d)" % (len(text), BODY_LIMIT))
    pb = parse_body(text)
    clean = _COMMENT_RE.sub("", text)
    fills = _FILL_RE.findall(clean)
    for f in fills[:3]:
        _err(out, "audit.placeholder", "unfilled marker left in the body: %s" % f[:70])
    if len(fills) > 3:
        _err(out, "audit.placeholder", "%d more unfilled markers" % (len(fills) - 3))
    if redact.find_secrets(text):
        _err(out, "audit.secret", "the body contains secret-shaped text; remove it (PR bodies are public to everyone with repo access)")

    t = pb.title
    if not t:
        _err(out, "audit.title", "the body has no title (a first-level heading)")
    elif _FILL_RE.search(t):
        pass  # already reported
    else:
        if len(t) > TITLE_LIMIT:
            _err(out, "audit.title", "the title is %d characters; keep it at most %d" % (len(t), TITLE_LIMIT))
        if t.startswith("-") or any(ord(c) < 32 for c in t):
            _err(out, "audit.title", "the title may not start with '-' or hold control characters")

    for name in REQUIRED_SECTIONS:
        if name not in pb.sections:
            _err(out, "audit.section-missing", "the section '## %s' is missing" % name)
        elif not pb.sections[name].strip() and name != "Audit block":
            _err(out, "audit.empty-section", "the section '## %s' is empty" % name)
    if not pb.has_audit_markers:
        _err(out, "audit.markers", "the audit block needs exactly one audit:begin and one audit:end marker, outside any code block")
    for hc in pb.hidden_comments:
        _err(out, "audit.hidden-comment", "an HTML comment (%r) hides text from reviewers; what is checked must be what is shown" % hc)
    for name in sorted(set(pb.duplicates)):
        _err(out, "audit.duplicate-field", "audit field '%s' appears more than once" % name)

    for spec in FIELDS:
        if spec.name not in pb.fields:
            _err(out, "audit.field-missing", "audit field '%s' is missing" % spec.name)
            continue
        value = pb.fields[spec.name]
        if not value:
            _err(out, "audit.empty-field", "audit field '%s' is empty" % spec.name)
            continue
        if _FILL_RE.search(value):
            continue  # reported as a placeholder
        problem = _check_value(spec, value)
        if problem:
            _err(out, "audit.bad-value", "audit field '%s' %s (got %r)" % (spec.name, problem, value[:60]))

    the_lane = lane or pb.fields.get("Lane", "")
    if the_lane == "quick" and not pb.fields.get("Design critic", "N/A").startswith(("N/A", "<fill")):
        _warn(out, "audit.quick-lane-critic", "a quick-lane change normally has no design critic; Design critic should be N/A")
    sec = pb.fields.get("Security review", "")
    if sec.startswith("NOT RUN"):
        _warn(out, "audit.security-not-run", "security review was not run: %s" % sec[:80])
    ver = pb.fields.get("Verify (G6)", "")
    if ver and not ver.startswith("PASS") and not _FILL_RE.search(ver):
        _err(out, "audit.verify-not-pass", "Verify (G6) must start with PASS; fix the failure and rerun verify.py instead of shipping it")

    if not pb.engineers:
        _err(out, "audit.no-engineers", "the Engineers table has no rows")
    for row in pb.engineers:
        who = row.get("Engineer", "?")
        if any(_FILL_RE.search(v) for v in row.values()):
            continue
        for col in ("Engineer", "Status", "Iterations", "Code review", "Report", "Commits"):
            if not row.get(col, "").strip():
                _err(out, "audit.empty-field", "Engineers row '%s': '%s' is empty" % (who, col))
        if row.get("Status", "") and row.get("Status") != "APPROVED":
            _err(out, "audit.engineer-not-approved", "engineer '%s' has status %s; only APPROVED work ships" % (who, row.get("Status")))
        cr = row.get("Code review", "")
        if cr and not cr.startswith(CODE_REVIEW_OK):
            _err(out, "audit.review-not-approved", "engineer '%s': code review is '%s'; it must be CODE_REVIEW_APPROVED" % (who, cr[:50]))
        if row.get("Iterations", "").strip() and not row["Iterations"].strip().isdigit():
            _err(out, "audit.bad-value", "engineer '%s': Iterations must be a number" % who)

    if not pb.matrix:
        _err(out, "audit.no-criteria", "the Acceptance matrix has no rows")
    for row in pb.matrix:
        if any(_FILL_RE.search(v) for v in row.values()):
            continue
        crit = row.get("Criterion", "?")[:50]
        for col in ("Criterion", "Evidence", "Result"):
            if not row.get(col, "").strip():
                _err(out, "audit.empty-field", "Acceptance matrix row '%s': '%s' is empty" % (crit, col))
        if row.get("Result", "").strip() and row["Result"].strip() != "PASS":
            _err(out, "audit.criterion-not-pass", "criterion '%s' is %s; every criterion needs PASS before a PR" % (crit, row["Result"].strip()))

    for name in ("Doc Delta", "Primer Delta"):
        body = pb.sections.get(name, "")
        if body and not _FILL_RE.search(body) and body.split(None, 1)[0:1] == ["NONE"] and "checked:" not in body:
            _err(out, "audit.none-without-checked", "'## %s' says NONE without a 'checked:' line naming what was compared" % name)
    return out


# --------------------------------------------------------------------------------------------------
# engineer reports
# --------------------------------------------------------------------------------------------------

def parse_report(text: str) -> Dict[str, Any]:
    """Sections of an engineer report (engineer-protocol §7). Returns {status, iteration, sections, missing, problems}."""
    parts = re.split(r"^## (.+?)\s*$", text, flags=re.M)
    sections = {}  # type: Dict[str, str]
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip()
    missing = [r for r in REPORT_SECTIONS if not any(k == r or k.startswith(r) for k in sections)]
    status = (sections.get("Status", "").split("\n", 1)[0] or "").strip()
    problems = []  # type: List[str]
    if "Status" in sections and status not in REPORT_STATUSES:
        problems.append("Status %r is not a protocol value" % status[:40])
    for name in ("Primer Delta", "Doc Delta"):
        body = sections.get(name, "")
        if body.split(None, 1)[0:1] == ["NONE"] and "checked:" not in body:
            problems.append("%s says NONE without a checked: line" % name)
    m = re.match(r"\d+", sections.get("Iteration", "").strip())
    return {"status": status, "iteration": m.group(0) if m else "", "sections": sections, "missing": missing, "problems": problems}


def _delta_text(sections: Dict[str, str], name: str) -> str:
    body = sections.get(name, "").strip()
    return "" if not body or body.split(None, 1)[0] == "NONE" else body


def read_reports(repo: str, slug: str) -> List[Dict[str, Any]]:
    d = os.path.join(repo, "docs", "features", slug, "reports")
    out = []  # type: List[Dict[str, Any]]
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rep = parse_report(fh.read(1 << 20))
        except OSError:
            continue
        rep["engineer"] = fn[:-3]
        rep["path"] = os.path.relpath(path, repo).replace(os.sep, "/")
        out.append(rep)
    return out


# --------------------------------------------------------------------------------------------------
# paths a human must review (H3)
# --------------------------------------------------------------------------------------------------

def glob_to_regex(pat: str) -> "re.Pattern":
    """gitignore-like: `**` crosses directories, `*` and `?` do not; a pattern with no slash matches at any depth."""
    anchored = "/" in pat.strip("/")
    body = pat.strip("/")
    out = ""
    i = 0
    while i < len(body):
        if body[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif body[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif body[i] == "*":
            out += "[^/]*"
            i += 1
        elif body[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(body[i])
            i += 1
    return re.compile(("^" if anchored else "^(?:.*/)?") + out + "$")


_LOCKFILES = ["package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "poetry.lock", "Pipfile.lock",
              "uv.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", ".terraform.lock.hcl", "packages.lock.json"]
HUMAN_PATTERNS = ([
    (".claude/**", "kit, agents and guardrails (H6)"), (".github/**", "CI and repository config (H3)"),
    (".mcp.json", "MCP configuration (H6)"), (".claude-plugin/**", "plugin manifest (H6)"), ("plugin/**", "plugin (H6)"),
    ("skills/**", "skill directory (H6)"), ("CODEOWNERS", "code owners (H6)"),
    ("**/migrations/**", "database migration (H3)"), ("db/migrate/**", "database migration (H3)"), ("**/*.tf", "infrastructure as code (H3)"),
    ("infra/**", "infrastructure (H3)"), ("terraform/**", "infrastructure (H3)"), ("packages/infra/**", "infrastructure (H3)"),
    ("package.json", "dependencies and scripts (H3)"), ("pyproject.toml", "dependencies and tool config (H3)"),
    ("conftest.py", "test config (H3)"), ("pytest.ini", "test config (H3)"), ("tox.ini", "test config (H3)"), ("setup.cfg", "test and tool config (H3)"),
    ("jest.config.*", "test config (H3)"), ("vitest.config.*", "test config (H3)"), ("playwright.config.*", "test config (H3)"),
    (".eslintrc*", "lint config (H3)"), ("eslint.config.*", "lint config (H3)"), (".prettierrc*", "lint config (H3)"), ("ruff.toml", "lint config (H3)"),
    (".flake8", "lint config (H3)"), ("mypy.ini", "lint config (H3)"), (".pre-commit-config.yaml", "hooks config (H3)"),
    (".husky/**", "git hooks (H3)"), (".envrc", "environment script (H3)"), (".env*", "environment file (H3)"), (".gitattributes", "git filters (H3)"),
] + [(name, "lockfile (H3)") for name in _LOCKFILES])


def _deploy_floor() -> List[str]:
    try:
        from doctorlib import deploy  # the Phase 1 deploy-surface floor (report §6.1, RC-1)
        return list(deploy.FLOOR_PATTERNS)
    except Exception:  # noqa: BLE001 - the floor is an extra; the kit list above still applies
        return []


def classify_paths(paths: Iterable[str], extra_globs: Sequence[str] = ()) -> List[Dict[str, str]]:
    """The changed paths a human must review: [{"path", "reason"}], in input order, each path once."""
    rules = [(glob_to_regex(p), why) for p, why in HUMAN_PATTERNS]
    rules += [(glob_to_regex(g), "project always-human path (always_human_paths)") for g in extra_globs]
    floor = _deploy_floor()
    out = []  # type: List[Dict[str, str]]
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        why = None
        for rx, reason in rules:
            if rx.match(path):
                why = reason
                break
        if why is None:
            for pat in floor:
                if fnmatch.fnmatchcase(path, pat):
                    why = "deploy surface (H3, RC-1)"
                    break
        if why:
            out.append({"path": path, "reason": why})
    return out


_CI_FIX_FORBIDDEN = ([(".github/workflows/**", "a CI workflow"), (".github/actions/**", "a CI action")] +
                     [(n, "a lockfile") for n in _LOCKFILES] +
                     [(p, "a test (tests are immutable during CI-fix)") for p in
                      ("**/test/**", "**/tests/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*", "**/test_*.py", "**/*_test.py",
                       "**/*_test.go", "conftest.py", "**/acceptance/**", "**/e2e/**", "**/snapshots/**", "**/__snapshots__/**")])


def ci_fix_violations(paths: Iterable[str], allow_lockfiles: bool = False) -> List[Tuple[str, str]]:
    out = []  # type: List[Tuple[str, str]]
    for path in paths:
        for pat, why in _CI_FIX_FORBIDDEN:
            if allow_lockfiles and why == "a lockfile":
                continue
            if glob_to_regex(pat).match(path):
                out.append((path, why))
                break
    return out


# --------------------------------------------------------------------------------------------------
# commits, trailers, reverts
# --------------------------------------------------------------------------------------------------

_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]+(.+?)\s*$")
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})")


def parse_trailers(message: str) -> Dict[str, List[str]]:
    """git-style trailers: the last paragraph, if every line in it is `Key: value`."""
    paras = [p for p in re.split(r"\n\s*\n", message.strip("\n")) if p.strip()]
    if len(paras) < 2:
        return {}
    lines = [ln for ln in paras[-1].split("\n") if ln.strip()]
    if not lines or not all(_TRAILER_RE.match(ln) for ln in lines):
        return {}
    out = {}  # type: Dict[str, List[str]]
    for ln in lines:
        m = _TRAILER_RE.match(ln)
        if m:
            out.setdefault(m.group(1).lower(), []).append(m.group(2))
    return out


def log_commits(repo: str, rev_range: str) -> List[Dict[str, Any]]:
    raw = _out(repo, ["log", "--reverse", "--format=%H%x1f%P%x1f%s%x1f%B%x1e", rev_range])
    out = []  # type: List[Dict[str, Any]]
    for rec in (raw or "").split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        f = rec.split("\x1f", 3)
        if len(f) < 4:
            continue
        out.append({"sha": f[0].strip(), "parents": f[1].split(), "subject": f[2], "body": f[3], "trailers": parse_trailers(f[3])})
    return out


def find_reverts(repo: str, shas: Sequence[str], rev: str = "HEAD") -> Dict[str, List[str]]:
    """{sha: [shas of commits reachable from `rev` that say "This reverts commit <sha>"]} (L-5, local delivery)."""
    wanted = set(s for s in shas if _SHA_RE.match(s))
    out = {}  # type: Dict[str, List[str]]
    if not wanted:
        return out
    raw = _out(repo, ["log", "--format=%H%x1f%B%x1e", rev]) or ""
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if "\x1f" not in rec:
            continue
        sha, body = rec.split("\x1f", 1)
        for m in _REVERTS_RE.finditer(body):
            if m.group(1) in wanted:
                out.setdefault(m.group(1), []).append(sha.strip())
    return out


def reverts_from_prs(prs: Sequence[Dict[str, Any]], pr_number: int) -> List[int]:
    """Numbers of pull requests that revert PR `pr_number`, from recorded `gh pr list --json number,title,body` data (L-5, PR delivery).

    A revert PR is one whose title starts with "Revert" and whose title or body names #N or ends a /pull/N URL."""
    out = []  # type: List[int]
    ref = re.compile(r"(?<![\w/])#%d\b|/pull/%d\b" % (pr_number, pr_number))
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("number") == pr_number:
            continue
        title = str(pr.get("title", ""))
        if title.lower().startswith("revert") and (ref.search(title) or ref.search(str(pr.get("body", "") or ""))):
            out.append(int(pr["number"]))
    return out


# --------------------------------------------------------------------------------------------------
# context: config at the base, branch, base ref
# --------------------------------------------------------------------------------------------------

def _valid_branch_name(name: str) -> bool:
    return bool(_BRANCH_RE.match(name)) and ".." not in name and "//" not in name and not name.endswith(("/", ".", ".lock")) and "@{" not in name


def default_base(repo: str, remote: str) -> str:
    head = _out(repo, ["symbolic-ref", "-q", "--short", "refs/remotes/%s/HEAD" % remote])
    if head and head.strip().startswith(remote + "/"):
        return head.strip()[len(remote) + 1:]
    for cand in ("main", "master"):
        if _out(repo, ["rev-parse", "--verify", "-q", "refs/heads/" + cand]) is not None:
            return cand
    return "main"


def _first(s: Optional[str]) -> Optional[str]:
    return s.strip().splitlines()[0] if s and s.strip() else None


def resolve_base_ref(repo: str, remote: str, base: str) -> Optional[str]:
    for ref in ("refs/remotes/%s/%s" % (remote, base), "refs/heads/%s" % base):
        if _first(_out(repo, ["rev-parse", "--verify", "-q", ref + "^{commit}"])):
            return ref
    return None


class Context(object):
    """Everything check/prepare/init need, resolved once."""

    def __init__(self) -> None:
        self.repo = ""
        self.slug = ""
        self.branch = ""
        self.base = "main"
        self.remote = "origin"
        self.base_ref = None  # type: Optional[str]
        self.base_sha = None  # type: Optional[str]
        self.head_sha = None  # type: Optional[str]
        self.body_file = ""
        self.verify_json = ""
        self.delivery = "local"
        self.git_autonomy = "none"
        self.max_ci_fix = 2
        self.human_globs = []  # type: List[str]
        self.findings = []  # type: List[Finding]


def build_context(repo: str, slug: Optional[str] = None, branch: Optional[str] = None, base: Optional[str] = None,
                  remote: str = "origin", body_file: Optional[str] = None, verify_json: Optional[str] = None,
                  delivery: Optional[str] = None, human_paths: Sequence[str] = ()) -> Context:
    top = gitutil.toplevel(repo)
    if not top:
        raise PrError("%s is not inside a git repository" % repo)
    c = Context()
    c.repo = top
    c.remote = remote
    if not re.match(r"^[A-Za-z0-9._-]+\Z", remote):
        raise PrError("--remote %r is not a plain remote name" % remote)
    cur = _first(_out(top, ["rev-parse", "--abbrev-ref", "HEAD"]))
    c.branch = branch or (cur if cur and cur != "HEAD" else "")
    if not c.branch:
        raise PrError("HEAD is detached; check out the feature branch or pass --branch")
    c.slug = slug or (c.branch.split("/", 1)[1] if "/" in c.branch else c.branch)
    c.base = base or default_base(top, remote)
    c.head_sha = _first(_out(top, ["rev-parse", "--verify", "-q", "refs/heads/%s^{commit}" % c.branch])) if _valid_branch_name(c.branch) else None
    c.base_ref = resolve_base_ref(top, remote, c.base) if _valid_branch_name(c.base) else None
    if c.base_ref and c.head_sha:
        c.base_sha = _first(_out(top, ["merge-base", c.head_sha, c.base_ref]))
    pr_dir = os.path.join(top, PR_DIR_REL)
    safe_slug = re.sub(r"[^A-Za-z0-9._-]", "-", c.slug)
    c.body_file = os.path.abspath(body_file) if body_file else os.path.join(pr_dir, safe_slug + ".md")
    c.verify_json = os.path.abspath(verify_json) if verify_json else os.path.join(pr_dir, safe_slug + ".verify.json")
    # Delivery settings come from the config at the base sha (H6 keys are never read from the head).
    cfg = None
    if c.base_sha:
        try:
            cfg = kconfig.load_at_sha(top, c.base_sha)
        except kconfig.ConfigError:
            cfg = None
    if cfg is None:
        try:
            from kitlib import registry
            cfg = kconfig.load_text("```yaml\n```\n", registry.load_registry())
        except Exception:  # noqa: BLE001
            raise PrError("cannot load the config registry")
        _warn(c.findings, "config.missing-at-base", "no valid .claude/loop.config.md at the base sha: delivery settings fall back to the defaults "
              "(delivery: local, git_autonomy: none, max_ci_fix_attempts: 2)")
    elif cfg.errors:
        _warn(c.findings, "config.invalid-at-base", "loop.config.md at the base sha has errors (%s); using the values it does define" % cfg.errors[0])
    c.delivery = delivery or str(cfg.get("delivery"))
    if c.delivery not in DELIVERIES:
        raise PrError("delivery %r is not one of %s" % (c.delivery, ", ".join(DELIVERIES)))
    c.git_autonomy = str(cfg.get("git_autonomy"))
    c.max_ci_fix = cfg.get_int("max_ci_fix_attempts")
    c.human_globs = [str(g) for g in cfg.get_list("always_human_paths")] + list(human_paths)
    return c


# --------------------------------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------------------------------

def changed_paths(repo: str, base_sha: str, head: str) -> List[str]:
    raw = _out(repo, ["diff", "--name-only", "--no-renames", "-z", "%s..%s" % (base_sha, head)])
    return [p for p in (raw or "").split("\0") if p]


def _dirty_paths(repo: str, ignore: Sequence[str], untracked_ok_prefix: str = "") -> List[str]:
    """Uncommitted paths. The body and verify files are ignored, and so are UNTRACKED files under the feature
    folder (`docs/features/<slug>/`): a project may keep that folder out of git; a modified tracked file there counts."""
    raw = _out(repo, ["status", "--porcelain=v1", "--untracked-files=all", "-z"]) or ""
    out = []  # type: List[str]
    ign = set(os.path.realpath(p) for p in ignore)
    for ent in raw.split("\0"):
        if len(ent) < 4:
            continue
        path = ent[3:]
        if os.path.realpath(os.path.join(repo, path)) in ign:
            continue
        if ent.startswith("??") and untracked_ok_prefix and path.startswith(untracked_ok_prefix):
            continue
        out.append(path)
    return out


def check(repo: str, mode: str = "check", lane: Optional[str] = None, labels: Sequence[str] = (), pr_number: Optional[int] = None,
          dependency_item: bool = False, **ctx_options: Any) -> Dict[str, Any]:
    """Validate the branch and the PR body. Returns the pr_prepare.v1 dict (see the module docstring)."""
    ctx = build_context(repo, **ctx_options)
    f = ctx.findings
    res = {"schema": SCHEMA, "mode": mode, "ok": False, "repo": ctx.repo, "slug": ctx.slug, "branch": ctx.branch, "base": ctx.base,
           "base_sha": ctx.base_sha, "head_sha": ctx.head_sha, "delivery": ctx.delivery, "git_autonomy": ctx.git_autonomy,
           "body_file": ctx.body_file, "title": "", "ahead": 0, "changed_paths": 0, "human_gated": [],
           "ci_fix": {"count": 0, "max": ctx.max_ci_fix, "commits": []}, "reports": [], "findings": f, "commands": []}  # type: Dict[str, Any]

    # --- branch and labels
    if not _valid_branch_name(ctx.branch):
        _err(f, "git.bad-branch", "branch %r is not a plain branch name; refusing to print commands for it" % ctx.branch)
    elif ctx.branch in PROTECTED_BRANCHES or ctx.branch.startswith(("release/", "dead/", "blocked/")):
        _err(f, "git.protected-branch", "branch %s is not a feature branch; deliver work from feat/<slug> or quick/<name>" % ctx.branch)
    elif not ctx.branch.startswith(_BRANCH_PREFIXES):
        _warn(f, "git.branch-name", "branch %s does not start with feat/, quick/, fix/, chore/ or docs/" % ctx.branch)
    if not _valid_branch_name(ctx.base):
        _err(f, "git.bad-base", "base %r is not a plain branch name" % ctx.base)
    for lab in labels:
        if APPROVER_ONLY_LABEL_RE.match(lab):
            _err(f, "label.approver-only", "label %r may only be applied by a factory approver (label-guard removes it otherwise); do not pass it" % lab)
        elif not _LABEL_RE.match(lab):
            _err(f, "label.bad", "label %r is not a plain label name" % lab)

    # --- git state
    if not ctx.head_sha:
        _err(f, "git.no-head", "cannot resolve the tip of %s" % ctx.branch)
    if not ctx.base_ref:
        _err(f, "git.no-base", "base branch %s not found (neither %s/%s nor a local branch); fetch it or pass --base" % (ctx.base, ctx.remote, ctx.base))
    elif not ctx.base_sha:
        _err(f, "git.no-merge-base", "%s and %s share no history" % (ctx.branch, ctx.base_ref))
    cur = _first(_out(ctx.repo, ["rev-parse", "--abbrev-ref", "HEAD"]))
    if ctx.head_sha and cur != ctx.branch:
        _err(f, "git.not-checked-out", "HEAD is on %s, not %s; check the feature branch out before preparing it" % (cur, ctx.branch))
    dirty = _dirty_paths(ctx.repo, [ctx.body_file, ctx.verify_json], "docs/features/%s/" % ctx.slug)
    if dirty:
        _err(f, "git.dirty", "the working tree has %d uncommitted path(s) (%s); they would not be in the PR" % (len(dirty), ", ".join(dirty[:4])))
    commits = []  # type: List[Dict[str, Any]]
    paths = []  # type: List[str]
    if ctx.base_sha and ctx.head_sha:
        commits = log_commits(ctx.repo, "%s..%s" % (ctx.base_sha, ctx.head_sha))
        paths = changed_paths(ctx.repo, ctx.base_sha, ctx.head_sha)
    res["ahead"] = len(commits)
    res["changed_paths"] = len(paths)
    if ctx.base_sha and ctx.head_sha and not commits:
        _err(f, "git.nothing-to-deliver", "%s has no commits ahead of %s" % (ctx.branch, ctx.base_ref))
    if any(len(c["parents"]) > 1 for c in commits):
        _warn(f, "git.merge-commits", "the branch contains merge commits; a squash merge hides them, a rebase merge does not")
    for c in commits:
        if c["subject"].startswith(("fixup!", "squash!")):
            _warn(f, "git.fixup-commit", "commit %s is a %s commit" % (c["sha"][:10], c["subject"].split("!")[0]))
        if c["subject"].startswith("wip(") and ctx.slug not in c["trailers"].get("feature", []):
            _warn(f, "git.trailer-missing", "WIP commit %s has no 'Feature: %s' trailer" % (c["sha"][:10], ctx.slug))

    # --- remote branch state (never force)
    remote_ref = "refs/remotes/%s/%s" % (ctx.remote, ctx.branch)
    remote_sha = _first(_out(ctx.repo, ["rev-parse", "--verify", "-q", remote_ref + "^{commit}"])) if _valid_branch_name(ctx.branch) else None
    res["remote_branch_exists"] = bool(remote_sha)
    if remote_sha and ctx.head_sha and remote_sha != ctx.head_sha:
        anc = _git(ctx.repo, ["merge-base", "--is-ancestor", remote_sha, ctx.head_sha]).returncode == 0
        if not anc:
            _err(f, "git.diverged", "%s has commits that %s does not (history was rewritten?). This script never prints a force push; "
                 "ask a human to resolve it" % (remote_ref, ctx.branch))

    # --- H3 paths and revert scan
    gated = classify_paths(paths, ctx.human_globs)
    res["human_gated"] = gated
    if commits:
        feature_shas = [c["sha"] for c in commits]
        rev = find_reverts(ctx.repo, feature_shas, ctx.head_sha or "HEAD")
        for sha, by in rev.items():
            _warn(f, "git.revert-in-branch", "commit %s was reverted by %s inside this branch" % (sha[:10], ", ".join(b[:10] for b in by)))

    # --- CI-fix commits
    cifix = [c for c in commits if "ci-fix-of" in c["trailers"]]
    res["ci_fix"] = {"count": len(cifix), "max": ctx.max_ci_fix, "commits": [c["sha"] for c in cifix]}
    if len(cifix) > ctx.max_ci_fix:
        _err(f, "ci-fix.cap", "%d CI-fix commits; max_ci_fix_attempts is %d. Stop and hand the failure to a human" % (len(cifix), ctx.max_ci_fix))
    for c in cifix:
        refs = c["trailers"]["ci-fix-of"]
        for r in refs:
            if not _SHORT_SHA_RE.fullmatch(r.strip()):
                _err(f, "ci-fix.trailer", "CI-fix commit %s: 'CI-Fix-Of: %s' is not a commit sha" % (c["sha"][:10], r[:30]))
        touched = [p for p in (_out(ctx.repo, ["diff-tree", "-r", "--no-commit-id", "--name-only", "--no-renames", "-z", "-m", c["sha"]]) or "").split("\0") if p]
        for p, why in ci_fix_violations(touched, allow_lockfiles=dependency_item):
            _err(f, "ci-fix.forbidden-path", "CI-fix commit %s edits %s, %s" % (c["sha"][:10], p, why))

    # --- reports
    reports = []  # type: List[Dict[str, Any]]
    pb = None  # type: Optional[ParsedBody]
    body_text = None  # type: Optional[str]
    try:
        with open(ctx.body_file, "r", encoding="utf-8") as fh:
            body_text = fh.read(1 << 20)
    except OSError:
        _err(f, "audit.no-body", "no PR body at %s; run `python3 .claude/scripts/pr_prepare.py init` and fill it in" % ctx.body_file)
    if body_text is not None:
        pb = parse_body(body_text)
        res["title"] = pb.title
        the_lane = lane or pb.fields.get("Lane", "")
        f.extend(validate_body(body_text, lane=lane))
    else:
        the_lane = lane or ""
    reports = read_reports(ctx.repo, ctx.slug)
    for r in reports:
        ok = r["status"] == "APPROVED" and not r["missing"] and not r["problems"]
        res["reports"].append({"engineer": r["engineer"], "path": r["path"], "status": r["status"], "ok": ok})
        if r["missing"]:
            _err(f, "report.malformed", "%s is missing section(s): %s" % (r["path"], ", ".join(r["missing"])))
        for p in r["problems"]:
            _err(f, "report.malformed", "%s: %s" % (r["path"], p))
        if r["status"] != "APPROVED":
            _err(f, "report.not-approved", "%s has Status %s; only APPROVED work ships" % (r["path"], r["status"] or "(none)"))
    if not reports and the_lane != "quick":
        _err(f, "report.none", "no engineer reports in docs/features/%s/reports/ (a quick-lane change reports inline: set Lane to quick)" % ctx.slug)
    if pb is not None:
        listed = [row.get("Engineer", "").strip() for row in pb.engineers]
        for r in reports:
            if r["engineer"] not in listed:
                _err(f, "audit.engineer-missing", "the Engineers table does not list '%s' (report %s)" % (r["engineer"], r["path"]))
        for name in ("Doc Delta", "Primer Delta"):
            owed = [r["engineer"] for r in reports if _delta_text(r["sections"], name)]
            body = pb.sections.get(name, "")
            if owed and body.split(None, 1)[0:1] == ["NONE"]:
                _err(f, "audit.delta-dropped", "'## %s' says NONE but the report(s) of %s list a delta" % (name, ", ".join(owed)))
        # fields that git can verify
        fv = pb.fields
        if fv.get("Head sha") and _SHA_RE.match(fv["Head sha"]) and ctx.head_sha and fv["Head sha"] != ctx.head_sha:
            _err(f, "audit.stale-head", "the audit block was written for %s but %s is now at %s; update the block (rerun verify.py and review "
                 "if code changed)" % (fv["Head sha"][:10], ctx.branch, ctx.head_sha[:10]))
        if fv.get("Base sha") and _SHA_RE.match(fv["Base sha"]) and ctx.base_sha and fv["Base sha"] != ctx.base_sha:
            _warn(f, "audit.stale-base", "Base sha %s is not the merge-base %s now" % (fv["Base sha"][:10], ctx.base_sha[:10]))
        for field, actual in (("Feature", ctx.slug), ("Branch", ctx.branch), ("Base", ctx.base), ("Delivery", ctx.delivery)):
            if fv.get(field) and not _FILL_RE.search(fv[field]) and fv[field] != actual:
                _err(f, "audit.mismatch", "audit field '%s' is %r but this run resolved %r" % (field, fv[field][:40], actual))
        hg = fv.get("Human-gated paths", "")
        if hg and not _FILL_RE.search(hg):
            if gated and hg.split(None, 1)[0:1] == ["NONE"]:
                _err(f, "audit.h3-mismatch", "Human-gated paths says NONE but the diff touches %d path(s) a human must review: %s"
                     % (len(gated), ", ".join(g["path"] for g in gated[:5])))
            elif gated:
                missing = [g["path"] for g in gated if g["path"] not in hg]
                if missing:
                    _err(f, "audit.h3-unlisted", "Human-gated paths does not list: %s" % ", ".join(missing[:6]))
        ci = fv.get("CI-Fix-Of", "")
        if ci and not _FILL_RE.search(ci):
            none = ci.split(None, 1)[0:1] == ["NONE"]
            if cifix and none:
                _err(f, "audit.cifix-mismatch", "CI-Fix-Of says NONE but %d commit(s) carry a CI-Fix-Of trailer" % len(cifix))
            elif not cifix and not none:
                _warn(f, "audit.cifix-mismatch", "CI-Fix-Of lists shas but no commit in the branch carries the trailer")

    # --- verify result
    _check_verify(ctx, pb, f)

    errors = [x for x in f if x.severity == "error"]
    res["ok"] = not errors
    res["findings"] = [x.to_dict() for x in f]
    if mode == "prepare" and res["ok"]:
        res["commands"] = build_commands(ctx, res["title"], remote_exists=bool(remote_sha), labels=labels, pr_number=pr_number)
    return res


def _check_verify(ctx: Context, pb: Optional[ParsedBody], f: List[Finding]) -> None:
    claims = bool(pb and pb.fields.get("Verify (G6)", "").startswith("PASS"))
    try:
        with open(ctx.verify_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        if claims:
            _warn(f, "verify.no-result", "no verify result at %s, so the Verify claim cannot be checked against HEAD; save "
                  "`verify.py --json > that file`" % ctx.verify_json)
        return
    except ValueError:
        _err(f, "verify.unreadable", "%s is not valid JSON" % ctx.verify_json)
        return
    if not isinstance(data, dict) or data.get("schema") != "verify.v1":
        _err(f, "verify.unreadable", "%s is not a verify.v1 result" % ctx.verify_json)
        return
    if data.get("status") != "pass" or not data.get("ok") or not data.get("complete"):
        _err(f, "verify.not-pass", "the saved verify result is %s (ok=%s, complete=%s); fix it and rerun verify.py" %
             (data.get("status"), data.get("ok"), data.get("complete")))
    if data.get("head") and ctx.head_sha and data["head"] != ctx.head_sha:
        _err(f, "verify.stale", "the verify result is for %s but the branch is at %s; rerun verify.py" % (str(data["head"])[:10], ctx.head_sha[:10]))
    if data.get("dirty"):
        _err(f, "verify.dirty", "verify ran on a dirty working tree, so it did not verify the committed code; commit and rerun")
    if not data.get("trusted", False):
        _err(f, "verify.untrusted", "verify read its definition from the working tree, not the base sha; rerun it against a committed config")
    if data.get("base") and ctx.base_sha and data["base"] != ctx.base_sha:
        _warn(f, "verify.base-differs", "verify used base %s but the merge-base is %s" % (str(data["base"])[:10], ctx.base_sha[:10]))


# --------------------------------------------------------------------------------------------------
# commands for the human
# --------------------------------------------------------------------------------------------------

def build_commands(ctx: Context, title: str, remote_exists: bool = False, labels: Sequence[str] = (),
                   pr_number: Optional[int] = None) -> List[str]:
    """The lines a human runs. Nothing here is executed by this script. No force flag is ever produced."""
    q = shlex.quote
    cd = "cd %s" % q(ctx.repo)
    lines = ["# Run these yourself. pr_prepare.py did not push anything.", cd]
    if ctx.delivery == "local":
        lines += ["git switch %s" % q(ctx.base),
                  "git merge --no-ff %s -m %s" % (q(ctx.branch), q(title or ctx.slug)),
                  "# delivery is local: nothing is pushed. Squash or fast-forward instead if you prefer."]
        return lines
    if remote_exists:
        lines.append("git push %s %s" % (q(ctx.remote), q(ctx.branch)))
    else:
        lines.append("git push --set-upstream %s %s" % (q(ctx.remote), q(ctx.branch)))
    if ctx.delivery == "draft-pr":
        if pr_number is not None:
            lines.append("gh pr edit %d --title %s --body-file %s" % (pr_number, q(title), q(ctx.body_file)))
        else:
            cmd = "gh pr create --draft --base %s --head %s --title %s --body-file %s" % (q(ctx.base), q(ctx.branch), q(title), q(ctx.body_file))
            for lab in labels:
                cmd += " --label %s" % q(lab)
            lines.append(cmd)
        lines.append("# CI (verify, review) then runs on the PR. Failures come back to the conductor as CI-fix commits, at most %d." % ctx.max_ci_fix)
    return lines


# --------------------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------------------

def _verify_summary(path: str, head: Optional[str]) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("schema") != "verify.v1" or d.get("status") != "pass" or not d.get("ok"):
        return None
    if head and d.get("head") != head:
        return None
    ran = [s["name"] for s in d.get("stages", []) if s.get("status") == "pass"]
    return "PASS (verify.v1; base %s; stages: %s)" % (str(d.get("base"))[:10], ", ".join(ran) or "none")


def _requirements_title(repo: str, slug: str) -> Optional[str]:
    try:
        with open(os.path.join(repo, "docs", "features", slug, "requirements.md"), "r", encoding="utf-8") as fh:
            for ln in fh:
                m = re.match(r"^#\s+(.+?)\s*$", ln)
                if m:
                    t = re.sub(r"^(requirements|feature)\s*[:\-]\s*", "", m.group(1), flags=re.I).strip()
                    return t[:TITLE_LIMIT] or None
    except OSError:
        pass
    return None


def init_body(repo: str, lane: Optional[str] = None, title: Optional[str] = None, **ctx_options: Any) -> Tuple[str, Context]:
    """The template text with every fact git, the reports and the verify result already hold filled in."""
    ctx = build_context(repo, **ctx_options)
    try:
        with open(os.path.join(ctx.repo, TEMPLATE_REL), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        raise PrError("template %s not found" % TEMPLATE_REL)
    reports = read_reports(ctx.repo, ctx.slug)
    the_lane = lane or ("full" if reports else "quick")
    paths = changed_paths(ctx.repo, ctx.base_sha, ctx.head_sha) if ctx.base_sha and ctx.head_sha else []
    gated = classify_paths(paths, ctx.human_globs)
    commits = log_commits(ctx.repo, "%s..%s" % (ctx.base_sha, ctx.head_sha)) if ctx.base_sha and ctx.head_sha else []
    cifix = [c["sha"] for c in commits if "ci-fix-of" in c["trailers"]]
    values = {
        "Feature": ctx.slug, "Lane": the_lane, "Delivery": ctx.delivery, "Branch": ctx.branch, "Base": ctx.base,
        "Base sha": ctx.base_sha, "Head sha": ctx.head_sha,
        "Verify (G6)": _verify_summary(ctx.verify_json, ctx.head_sha),
        "Human-gated paths": "; ".join("%s (%s)" % (g["path"], g["reason"]) for g in gated) if gated else "NONE",
        "CI-Fix-Of": ", ".join(sorted(set(r.strip() for c in commits if "ci-fix-of" in c["trailers"] for r in c["trailers"]["ci-fix-of"]))) if cifix else "NONE",
    }  # type: Dict[str, Optional[str]]
    if the_lane == "quick":
        values["Design critic"] = "N/A (quick lane)"
    for name, val in values.items():
        if val:
            text = re.sub(r"^(\| %s \| )<fill:[^>]*>( \|)$" % re.escape(name), lambda m: m.group(1) + val.replace("\\", "\\\\") + m.group(2), text, flags=re.M)
    t = title or _requirements_title(ctx.repo, ctx.slug)
    if t:
        text = re.sub(r"^# <fill:[^>]*>$", lambda m: "# " + t, text, count=1, flags=re.M)
    if reports:
        rows = []
        for r in reports:
            shas = [c["sha"][:10] for c in commits if r["engineer"] in c["trailers"].get("engineer", [])]
            rows.append("| %s | %s | %s | <fill: CODE_REVIEW_APPROVED, or SKIPPED (trivial change)> | %s | %s |" % (
                r["engineer"], r["status"] or "<fill: status>", r["iteration"] or "<fill: number>", r["path"],
                ", ".join(shas) or "<fill: short shas of the WIP commits>"))
        text = re.sub(r"^\| <fill: database, backend.*\|$", lambda m: "\n".join(rows), text, count=1, flags=re.M)
        for name in ("Doc Delta", "Primer Delta"):
            owed = ["- **%s:** %s" % (r["engineer"], _delta_text(r["sections"], name).replace("\n", " ")) for r in reports if _delta_text(r["sections"], name)]
            if owed:
                body = "\n".join(owed)
            elif all(not r["problems"] for r in reports):
                checks = sorted(set(re.sub(r"^checked:\s*", "", ln.strip()) for r in reports
                                    for ln in r["sections"].get(name, "").split("\n") if ln.strip().startswith("checked:")))
                body = "NONE\nchecked: %s" % ("; ".join(checks) if checks else "<fill: what was compared>")
            else:
                continue
            text = re.sub(r"(^## %s\n\n)<fill:[^>]*>" % re.escape(name), lambda m: m.group(1) + body.replace("\\", "\\\\"), text, count=1, flags=re.M)
    return text, ctx


def write_body(path: str, text: str, overwrite: bool = False) -> None:
    if os.path.exists(path) and not overwrite:
        raise PrError("%s already exists; pass --overwrite to replace it (your edits there would be lost)" % path)
    jsonio.ensure_dir(os.path.dirname(path), 0o755)
    jsonio.atomic_write_text(path, text, 0o644)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--repo", default=".")
    ap.add_argument("--slug", help="feature slug (default: the branch name after the first slash)")
    ap.add_argument("--branch", help="feature branch (default: the checked-out branch)")
    ap.add_argument("--base", help="branch the PR targets (default: the remote's HEAD, else main or master)")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--body-file", help="PR body (default: .claude/pr/<slug>.md, gitignored)")
    ap.add_argument("--verify-json", help="saved `verify.py --json` result (default: .claude/pr/<slug>.verify.json)")
    ap.add_argument("--delivery", choices=DELIVERIES, help="override `delivery` from loop.config")
    ap.add_argument("--human-path", action="append", default=[], metavar="GLOB", help="extra always-human path glob (repeatable)")
    ap.add_argument("--json", action="store_true")


def _ctx_args(a: argparse.Namespace) -> Dict[str, Any]:
    return dict(slug=a.slug, branch=a.branch, base=a.base, remote=a.remote, body_file=a.body_file, verify_json=a.verify_json,
                delivery=a.delivery, human_paths=a.human_path)


def format_text(res: Dict[str, Any]) -> str:
    lines = ["pr_prepare %s: %s  branch=%s base=%s delivery=%s ahead=%d" % (
        res["mode"], "READY" if res["ok"] else "NOT READY", res["branch"], res["base"], res["delivery"], res["ahead"])]
    for x in res["findings"]:
        lines.append("  [%s] %s: %s" % (x["severity"] if isinstance(x, dict) else x.severity, x["id"] if isinstance(x, dict) else x.id,
                                         x["message"] if isinstance(x, dict) else x.message))
    if res.get("human_gated"):
        lines.append("  human-gated paths (%d): %s" % (len(res["human_gated"]), ", ".join(g["path"] for g in res["human_gated"][:8])))
    if res["commands"]:
        lines.append("")
        lines += res["commands"]
    elif res["mode"] == "prepare" and not res["ok"]:
        lines.append("  No commands are printed while there are errors.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a feature branch and print the commands a human runs to deliver it. Never pushes.")
    sub = ap.add_subparsers(dest="cmd")
    p_init = sub.add_parser("init", help="write the PR body from the template")
    _common(p_init)
    p_init.add_argument("--lane", choices=("full", "quick"))
    p_init.add_argument("--title")
    p_init.add_argument("--overwrite", action="store_true")
    for name, helptext in (("check", "validate the body and the git state"), ("prepare", "check, then print the commands")):
        sp = sub.add_parser(name, help=helptext)
        _common(sp)
        sp.add_argument("--lane", choices=("full", "quick"))
        sp.add_argument("--label", action="append", default=[], help="a label for `gh pr create` (never factory:*, plan-ok or preview)")
        sp.add_argument("--pr", type=int, help="number of the existing PR: print `gh pr edit` instead of `gh pr create`")
        sp.add_argument("--dependency-item", action="store_true", help="a dependency update: lockfile edits are allowed in CI-fix commits")
    p_rev = sub.add_parser("reverts", help="find reverts of commits or of a PR")
    p_rev.add_argument("--repo", default=".")
    p_rev.add_argument("--sha", action="append", default=[], help="a commit to look for reverts of (repeatable)")
    p_rev.add_argument("--rev", default="HEAD", help="where to look (default HEAD)")
    p_rev.add_argument("--prs-json", help="recorded `gh pr list --json number,title,body` output")
    p_rev.add_argument("--pr", type=int, help="the PR number to find revert PRs of (with --prs-json)")
    p_rev.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_usage(sys.stderr)
        return 2
    try:
        repo = os.path.abspath(a.repo)
        if a.cmd == "reverts":
            out = {"schema": "pr_prepare.reverts.v1", "commits": {}, "prs": []}  # type: Dict[str, Any]
            if a.sha:
                top = gitutil.toplevel(repo)
                if not top:
                    raise PrError("%s is not inside a git repository" % repo)
                out["commits"] = find_reverts(top, a.sha, a.rev)
            if a.prs_json:
                if a.pr is None:
                    raise PrError("--prs-json needs --pr")
                with open(a.prs_json, "r", encoding="utf-8") as fh:
                    out["prs"] = reverts_from_prs(json.load(fh), a.pr)
            if a.json:
                print(json.dumps(out, indent=2))
            else:
                print("reverted commits: %s" % (", ".join("%s by %s" % (k[:10], ",".join(x[:10] for x in v)) for k, v in out["commits"].items()) or "none"))
                print("revert PRs: %s" % (", ".join("#%d" % n for n in out["prs"]) or "none"))
            return 1 if (out["commits"] or out["prs"]) else 0
        if a.cmd == "init":
            text, ctx = init_body(repo, lane=a.lane, title=a.title, **_ctx_args(a))
            write_body(ctx.body_file, text, a.overwrite)
            left = len(_FILL_RE.findall(_COMMENT_RE.sub("", text)))
            print("wrote %s (%d field(s) still to fill)" % (ctx.body_file, left))
            return 0
        res = check(repo, mode=a.cmd, lane=a.lane, labels=a.label, pr_number=a.pr, dependency_item=a.dependency_item, **_ctx_args(a))
        print(json.dumps(res, indent=2) if a.json else format_text(res))
        return 0 if res["ok"] else 1
    except PrError as exc:
        sys.stderr.write("pr_prepare: %s\n" % exc)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # an internal error must never read as "ready"
        sys.stderr.write("pr_prepare: internal error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
