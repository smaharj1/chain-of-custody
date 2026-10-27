"""G11: secrets (report section 5.4, section 9 G11, CO-2). No secret reaches a PR.

Two scans, both required to be clean:

1. **Built in** (always runs, offline, deterministic): the lines this item ADDED between the base and HEAD, every commit message in the range,
   the item's committed and uncommitted engineer and review reports, and the verify logs, are read with the shapes in `kitlib/redact.py`
   (cloud keys, GitHub, Slack, Stripe and npm tokens, private keys, JWTs, credentials in URLs, bearer headers, quoted `password = "..."`).
   Placeholders (`changeme`, `example`, `<your-key>`, `${VAR}`, the documented AWS example key) are not secrets. A fixture that must hold a
   fake secret is allowed by the plan: `ext.g11.allow: ["tests/fixtures/**"]` (the plan is read at the base sha and reviewed by a human).
2. **External** (`secrets_scan_cmd`, for example gitleaks): run in the worktree with `LOOPCTL_BASE` and `LOOPCTL_HEAD`; a non-zero exit is a
   failure. gitleaks in the required CI check is the real scanner (section 5.4); this gate is the local first wall.

A finding names the kind and the file and line, never the value. Leftovers in the event files (which are redacted when written) are warnings.
"""
from __future__ import annotations

import glob as _glob
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from kitlib import redact

from .. import common
from . import GateResult
from . import _lib

NAME = "g11"
OWNER_METRIC = "secrets caught before a PR (should be 0 reaching a PR)"
REQUIRED_FOR = (("verifying", "reviewing"),)

MAX_BYTES = 6 * 1024 * 1024
_PLACEHOLDER = re.compile(r"(?i)change[-_ ]?me|example|placeholder|dummy|your[-_ ]|<[^>]*>|\$\{|\$\(|\$[A-Z_]+|xxxx|\*{4}|REDACTED|not[-_ ]a[-_ ]secret|todo|"
                          r"fake|sample|test[-_]?(?:key|token|secret|password)|os\.environ|process\.env|getenv|secrets\.|\bnull\b|\bnone\b|\bfalse\b|\btrue\b")
_KNOWN_EXAMPLES = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")


def scan(text: str) -> List[Tuple[str, int]]:
    """[(kind, line number)] of secret-shaped text. Never returns a value."""
    hits = []  # type: List[Tuple[str, int]]
    seen = set()
    for kind, start, end in redact.find_secrets(text):
        value = text[start:end]
        if any(k in value for k in _KNOWN_EXAMPLES):
            continue
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        if kind in ("secret-assignment", "bearer", "basic-auth", "url-credentials"):
            raw = value.strip("\"'")
            if len(raw) < 8 or _PLACEHOLDER.search(raw):          # the VALUE only: a trailing comment saying "example" must not excuse a real one
                continue
            if kind == "secret-assignment" and not (value.startswith(("\"", "'")) and re.search(r"[0-9]", raw) and re.search(r"[A-Za-z]", raw)):
                continue                    # an unquoted value is a variable or an expression, not a literal
            if kind == "bearer" and not re.search(r"[0-9]", raw):
                continue
            if kind == "url-credentials" and raw.lower() in ("password", "pass", "secret"):
                continue
        n = text.count("\n", 0, start) + 1
        if (kind, n) not in seen:
            seen.add((kind, n))
            hits.append((kind, n))
    return hits


def _added_chunks(ctx: Any, skipped: List[str]) -> List[Tuple[str, str, str]]:
    """[(label, path, added text)]: for every commit in base..HEAD the lines it added (a secret added and removed again inside the branch is
    still in the history the PR carries), then the uncommitted changes and new files of the working tree. Binary files and oversize chunks
    are skipped."""
    head = _lib.head_of(ctx)
    common_args = ["-U0", "--no-ext-diff", "--no-textconv", "--no-renames", "--text"]
    chunks = []  # type: List[Tuple[str, str, str]]

    def parse(out: Optional[str], label_default: str) -> None:
        label, cur, lines = label_default, None, []  # type: str, Optional[str], List[str]

        def flush() -> None:
            if cur is not None and lines:
                if sum(len(x) for x in lines) < MAX_BYTES:
                    chunks.append((label, cur, "\n".join(lines)))
                else:
                    skipped.append(cur)
        for ln in (out or "").splitlines():
            if ln.startswith("\x01"):
                flush()
                label, cur, lines = "commit " + ln[1:11], None, []
            elif ln.startswith("+++ "):
                flush()
                cur, lines = (ln[6:] if ln.startswith("+++ b/") else None), []
            elif cur is not None and ln.startswith("+") and not ln.startswith("+++"):
                lines.append(ln[1:])
        flush()

    parse(_lib.git_ok(ctx, ["log", "-p", "--format=%x01%H"] + common_args + ["%s..%s" % (ctx.base_sha, head), "--"]), "commit")
    parse(_lib.git_ok(ctx, ["diff"] + common_args + ["HEAD", "--"]), "the working tree")
    untracked = (_lib.git_ok(ctx, ["ls-files", "--others", "--exclude-standard", "-z"]) or "").split("\0")
    for rel in [u for u in untracked if u and common.safe_rel_path(u)][:50]:
        full = os.path.join(ctx.worktree, rel)
        try:
            if os.path.islink(full):
                continue
            if os.path.getsize(full) > MAX_BYTES:
                skipped.append(rel)
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if "\x00" not in text[:4096]:
            chunks.append(("the working tree", rel, text))
    return chunks


def run(ctx: Any) -> GateResult:
    head = _lib.head_of(ctx)
    if head is None:
        return GateResult.errored("there is no head commit")
    ext = ctx.item.get("ext") if isinstance(ctx.item.get("ext"), dict) else {}
    g11 = ext.get("g11") if isinstance(ext.get("g11"), dict) else {}
    allow = [a for a in (g11.get("allow") or []) if isinstance(a, str) and common.safe_glob(a)]
    findings = []  # type: List[Dict[str, Any]]
    n_hits = 0

    def report(kind: str, where: str, line: int, path: str = "", sev: str = "error") -> None:
        nonlocal n_hits
        if path and _lib.match_any(allow, path):
            sev = "warn"
        findings.append(_lib.finding("g11." + kind, sev, "%s secret-shaped text (%s) at %s:%d; the value is not printed" % (
            "possible" if sev == "warn" else "a", kind, where, line), path=path or where))
        if sev == "error":
            n_hits += 1

    skipped = []  # type: List[str]
    for label, path, text in _added_chunks(ctx, skipped):
        for kind, line in scan(text)[:5]:
            report(kind, "%s (%s)" % (path, label), line, path)
    if skipped:
        findings.append(_lib.finding("g11.skipped-large", "warn", "%d added file(s) over %d MiB were not scanned by the built-in scan (%s): the external scanner or CI must cover them"
                                     % (len(skipped), MAX_BYTES >> 20, ", ".join(common.clean_text(p, 50) for p in skipped[:3]))))
    msgs = _lib.git_ok(ctx, ["log", "--format=%H%x00%B%x01", "%s..%s" % (ctx.base_sha, head)]) or ""
    for rec in msgs.split("\x01"):
        if "\x00" in rec:
            sha, body = rec.strip().split("\x00", 1)
            for kind, line in scan(body)[:3]:
                report(kind, "the message of commit %s" % sha[:10], line)
    # evidence: reports and verify logs on disk (uncommitted ones included)
    candidates = []  # type: List[str]
    rep_dir = ctx.paths.get("reports")
    if rep_dir and os.path.isdir(rep_dir):
        candidates += sorted(_glob.glob(os.path.join(rep_dir, "*.md")))[:40]
    rs = _lib.runstate_of(ctx)
    candidates += sorted(_glob.glob(os.path.join(rs.root, "verify", "%s-a*.log" % ctx.item_id)))[-3:]
    for path in candidates:
        try:
            if os.path.islink(path) or os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for kind, line in scan(text)[:3]:
            report(kind, "evidence file " + os.path.basename(path), line)
    # events: redacted when written, so a hit here is a redaction gap
    for path in sorted(_glob.glob(os.path.join(rs.events_dir, "*.jsonl")))[:20]:
        try:
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if size > 262144:
                    fh.seek(size - 262144)
                text = fh.read()
        except OSError:
            continue
        for kind, line in scan(text)[:2]:
            report(kind, "event file " + os.path.basename(path), line, sev="warn")
    cmd = _lib.command_set(ctx.cfg, "secrets_scan_cmd")
    scanner = "built-in"
    if cmd:
        scanner = "built-in + external"
        stage, text, _f = _lib.run_command(ctx, "secrets_scan", cmd, {"LOOPCTL_BASE": ctx.base_sha, "LOOPCTL_HEAD": head}, count_tests=False)
        if stage["status"] != "pass":
            findings.append(_lib.finding("g11.scanner", "error", "secrets_scan_cmd %s (%s): %s" % (stage["status"], stage.get("reason", ""), _lib.tail(text, 300))))
            n_hits += 1
    ev = {"summary": "%s scan: %d finding(s)" % (scanner, n_hits)}
    if n_hits:
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
