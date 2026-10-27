"""G11: secrets (report section 5.4, section 9 G11, CO-2). No secret reaches a PR.

Two scans, both required to be clean:

1. **Built in** (always runs, offline, deterministic): the lines this item ADDED between the base and HEAD, every commit message in the range,
   the item's committed and uncommitted engineer and review reports, and the verify logs, are read with the shapes in `kitlib/redact.py`
   (cloud keys, GitHub, GitLab, Slack, Stripe, npm, PyPI, Anthropic and OpenAI project tokens, Azure `AccountKey=`, private keys, JWTs,
   credentials in URLs, bearer headers, quoted `password = "..."`, and UNQUOTED values in `.env*`, yaml, ini, cfg, conf, properties and toml files
   and in `KEY=value` lines anywhere). A placeholder (`changeme`, `example`, `<your-key>`, `${VAR}`, the documented AWS example key) excuses a
   value only when it is the WHOLE value, never a substring of it. A fixture that must hold a fake secret is allowed by the plan:
   `ext.g11.allow: ["tests/fixtures/**"]` (the plan is read at the base sha and reviewed by a human).
   Diffs are read with `core.quotePath=false`, quoted `+++ "b/…"` headers are decoded, merge-resolution lines are read (`--cc`), and more than
   50 untracked files is a blocking finding rather than a silent cap.
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
# A placeholder excuses a value only as a WHOLE value (UN-3): a substring match let `Zx81kQm2example` or `$` + any letter through.
_PLACEHOLDER = re.compile(r"(?i)^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(.*\)|<[^>]+>|\{\{.*\}\}|change-?me|example|your[-_ ][a-z-_ ]+|xxx+|\*+|todo|none|null|true|false|"
                          r"redacted|dummy|fake|sample|test|placeholder|(?:example|sample|dummy|fake|test|placeholder|change-?me)(?:[-_ ][a-z]+)+(?:[-_ ]?\d{1,2})?)$")
_KNOWN_EXAMPLES = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
_CONFIG_NAME = re.compile(r"(?i)(?:^|/)\.env[^/]*$|\.env$|\.(?:ya?ml|ini|cfg|conf|properties|toml)$")
_KEY_VALUE_LINE = re.compile(r"^\s*(?:export\s+|set\s+)?[A-Za-z_][\w.-]*=\S")
_SHELLISH_VALUE = re.compile(r"^[A-Za-z0-9_+/=.@%!~:-]{8,}$")


def _unquoted_ok(kind_value: str, line: str, config: bool) -> bool:
    """Whether an unquoted `key = value` is a literal secret and not a variable or an expression: always in config files (`.env`, yaml, ini,
    properties, toml), and in `KEY=value` lines (no spaces around `=`, nothing but value characters) anywhere else."""
    raw = kind_value.split("#", 1)[0].strip()
    if len(raw) < 8 or _PLACEHOLDER.match(raw):
        return False
    if raw[0] in "$!*&|>{[<(" or raw.startswith(("/", "./", "../", "~")) or "(" in raw or "://" in raw and "@" not in raw:
        return False
    if config:
        return True
    return bool(_KEY_VALUE_LINE.match(line)) and bool(_SHELLISH_VALUE.match(raw)) and bool(re.search(r"[0-9]", raw)) and bool(re.search(r"[A-Za-z]", raw))


def scan(text: str, path: str = "") -> List[Tuple[str, int]]:
    """[(kind, line number)] of secret-shaped text. Never returns a value. `path` (optional) says what kind of file the text is from."""
    hits = []  # type: List[Tuple[str, int]]
    seen = set()
    config = bool(path and _CONFIG_NAME.search(path))
    for kind, start, end in redact.find_secrets(text):
        value = text[start:end]
        if any(k in value for k in _KNOWN_EXAMPLES):
            continue
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        if kind in ("secret-assignment", "bearer", "basic-auth", "url-credentials"):
            raw = value.strip("\"'")
            quoted = value.startswith(("\"", "'"))
            if kind == "secret-assignment" and not quoted:
                if not _unquoted_ok(value, line, config):
                    continue
            elif len(raw) < 8 or _PLACEHOLDER.match(raw):          # the VALUE only: a trailing comment saying "example" must not excuse a real one
                continue
            if kind == "secret-assignment" and quoted and not (re.search(r"[0-9]", raw) and re.search(r"[A-Za-z]", raw)):
                continue                    # a quoted word with no digit is a name or a sentence, not a generated secret
            if kind == "bearer" and not re.search(r"[0-9]", raw):
                continue
            if kind == "url-credentials" and raw.lower() in ("password", "pass", "secret"):
                continue
        n = text.count("\n", 0, start) + 1
        if (kind, n) not in seen:
            seen.add((kind, n))
            hits.append((kind, n))
    return hits


_OCT = re.compile(r"\\([0-7]{3}|.)", re.S)
_ESC = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", '"': '"', "\\": "\\"}


def unquote_c(s: str) -> str:
    """Decode a C-quoted git path (`"b/caf\\303\\251.py"` without the quotes): octal bytes are UTF-8, a few single-letter escapes."""
    raw = bytearray()
    pos = 0
    for m in _OCT.finditer(s):
        raw += s[pos:m.start()].encode("utf-8", "replace")
        g = m.group(1)
        raw += bytes([int(g, 8) & 0xFF]) if len(g) == 3 and g.isdigit() else _ESC.get(g, g).encode("utf-8", "replace")
        pos = m.end()
    raw += s[pos:].encode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def diff_target(header: str) -> Optional[str]:
    """The path in a `+++ ` line (without the `+++ `): `b/x.py`, a trailing-TAB form for names with spaces, or a C-quoted `"b/caf\\303\\251.py"`.
    None for /dev/null. A header that cannot be read still yields a label, so the lines under it are scanned: a file is never dropped."""
    h = header.rstrip("\t")
    if h == "/dev/null":
        return None
    if h.startswith('"') and h.endswith('"') and len(h) >= 2:
        h = unquote_c(h[1:-1])
    if h.startswith("b/"):
        return h[2:]
    return h or "<unreadable path>"


def _added_chunks(ctx: Any, skipped: List[str], overflow: Optional[List[str]] = None) -> List[Tuple[str, str, str]]:
    """[(label, path, added text)]: for every commit in base..HEAD the lines it added (a secret added and removed again inside the branch is
    still in the history the PR carries; merge commits are read with --cc so a resolution that adds a secret shows), then the uncommitted
    changes and new files of the working tree. Binary files and oversize chunks are skipped; more than 50 untracked files go in `overflow`."""
    head = _lib.head_of(ctx)
    common_args = ["-U0", "--no-ext-diff", "--no-textconv", "--no-renames", "--text"]
    chunks = []  # type: List[Tuple[str, str, str]]

    def parse(out: Optional[str], label_default: str) -> None:
        label, cur, lines = label_default, None, []  # type: str, Optional[str], List[str]
        in_header, width = False, 1

        def flush() -> None:
            if cur is not None and lines:
                if sum(len(x) for x in lines) < MAX_BYTES:
                    chunks.append((label, cur, "\n".join(lines)))
                else:
                    skipped.append(cur)
        for ln in (out or "").splitlines():
            if ln.startswith("\x01"):
                flush()
                label, cur, lines, in_header = "commit " + ln[1:11], None, [], False
            elif ln.startswith("diff --git ") or ln.startswith("diff --cc ") or ln.startswith("diff --combined "):
                flush()
                cur, lines, in_header, width = "<unreadable path>", [], True, 1
            elif in_header and ln.startswith("+++ "):
                # only a header line, never an added line that happens to read `++ …`: header lines sit between `diff` and the first hunk
                cur = diff_target(ln[4:])
            elif ln.startswith("@@"):
                in_header = False
                width = max(1, len(ln) - len(ln.lstrip("@")) - 1)       # `@@ ` is one parent, `@@@ ` (--cc) is two
            elif not in_header and cur is not None and "+" in ln[:width] and not ln.startswith("\\"):
                lines.append(ln[width:])
        flush()

    # the log gets its own pass for every commit; --cc adds the combined diff of merges (plain `-p` prints nothing for a merge)
    parse(_lib.git_ok(ctx, ["-c", "core.quotePath=false", "log", "-p", "--cc", "--format=%x01%H"] + common_args + ["%s..%s" % (ctx.base_sha, head), "--"]), "commit")
    parse(_lib.git_ok(ctx, ["-c", "core.quotePath=false", "diff"] + common_args + ["HEAD", "--"]), "the working tree")
    untracked = [u for u in (_lib.git_ok(ctx, ["ls-files", "--others", "--exclude-standard", "-z"]) or "").split("\0") if u and common.safe_rel_path(u)]
    if len(untracked) > 50 and overflow is not None:
        overflow.append(str(len(untracked)))
    for rel in untracked[:50]:
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
    overflow = []  # type: List[str]
    for label, path, text in _added_chunks(ctx, skipped, overflow):
        for kind, line in scan(text, path)[:5]:
            report(kind, "%s (%s)" % (path, label), line, path)
    if overflow:
        findings.append(_lib.finding("g11.untracked-overflow", "error", "%s untracked files are in the worktree and the built-in scan reads 50: commit or delete them so the "
                                     "scan can cover them (it does not cap silently)" % overflow[0]))
        n_hits += 1
    for path, why in _lib.ignored_problems(ctx)[:20]:
        if path.startswith("<more than"):
            findings.append(_lib.finding("g11.ignored-incomplete", "warn", "%s: %s" % (path, why)))
            continue
        findings.append(_lib.finding("g11.ignored-config", "error", "%s: %s. No scan has read it" % (path, why), path=path))
        n_hits += 1
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
