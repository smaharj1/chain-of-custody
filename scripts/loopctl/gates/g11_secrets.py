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

import bisect
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
MAX_SLICES = 8                  # a large added file is read in slices of MAX_BYTES, this many of them (48 MiB); more than that is skipped and blocks
# A placeholder excuses a value only as a WHOLE value (UN-3): a substring match let `Zx81kQm2example` or `$` + any letter through.
_PLACEHOLDER = re.compile(r"(?i)^(?:\$[A-Za-z_][A-Za-z0-9_]*|%\([^)\n]*\)s|\$\(.*\)|<[^>]+>|\{\{.*\}\}|change-?me|example|your[-_ ][a-z-_ ]+|xxx+|\*+|todo|none|null|true|false|"
                          r"redacted|dummy|fake|sample|test|placeholder|(?:example|sample|dummy|fake|test|placeholder|change-?me)(?:[-_ ][a-z]+)+(?:[-_ ]?\d{1,2})?)$")
_KNOWN_EXAMPLES = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
# files where an unquoted `key = value` or `key: value` is a literal value: dotenv, yaml, ini and friends, Dockerfiles, JSON and XML (R-GATES-3: the
# set follows what `redact` already catches)
_CONFIG_NAME = re.compile(r"(?i)(?:^|/)\.env[^/]*$|\.env$|\.(?:ya?ml|ini|cfg|conf|config|properties|toml|json5?|xml|plist|tpl|tmpl)$|(?:^|/)(?:Docker|Container)file[^/]*$|\.(?:docker|container)file$")
# in these a value cannot start with a structure character, so one that does is syntax (a yaml tag, anchor, alias or block scalar, a json array)
_STRUCTURED_NAME = re.compile(r"(?i)\.(?:ya?ml|toml|json5?|xml|plist|tpl|tmpl)$")
# in these an unquoted value stays as the shared pattern cut it: toml, json and xml have no unquoted strings. Yaml does, and a plain scalar there runs to the
# end of the line: `POSTGRES_PASSWORD: Ab12,Cd34Ef56` and the compose list form `- POSTGRES_PASSWORD=Ab12;Cd34Ef56` carry `,` and `;` in the value
_NO_WHOLE_VALUE_NAME = re.compile(r"(?i)\.(?:toml|json5?|xml|plist|tpl|tmpl)$")
# source code: an unquoted right-hand side is an expression, not a literal, so the strict line-start `KEY=value` rule stays
_CODE_NAME = re.compile(r"(?i)\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|rb|php|cs|swift|scala|c|cc|cpp|h|hpp|lua|pl|r|dart|ex|exs|clj|vue|svelte)$")
_KEY_VALUE_LINE = re.compile(r"^\s*(?:export\s+|set\s+)?[A-Za-z_][\w.-]*=\S")
# the token that carries the value in a command line, Dockerfile or connection string: `KEY=`, `-e KEY=`, `--password=`, `ENV KEY=`, `;Password=`
_ASSIGN_TOKEN = re.compile(r"(?:^|[\s;\"'])-{0,2}[A-Za-z_][\w.-]*=$")
_SHELLISH_VALUE = re.compile(r"^[A-Za-z0-9_+/=.@%!~:-]{8,}$")
_CONN_TOKEN = re.compile(r";[ \t]*[\w ]{1,40}=$")                      # `Server=x;Password=…` in a connection string, in any kind of file
# source-code references, not literals: `args.password1`, `my_secret_name2`, `get_password(x)` (only judged outside config files)
_REFERENCE = re.compile(r"^[a-z_]+(?:\.[a-z_][a-z_0-9]*)+$|^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$|^[A-Za-z_]\w*\(")
# `${NAME}` is a placeholder; `${NAME:-literal}` carries a literal that is committed with the file, so only the literal decides (R2-GATES-2). The literal can hold
# another reference (`${A:-${B:-literal}}`, `${A:-${B}literal}`), so a reference is matched to its closing brace by counting `${` and `}` (R3-GATES-7).
_DEFAULT_HEAD = re.compile(r"\$\{[A-Za-z_][\w.]{0,128}:?[-=+]")      # `${NAME:-x}`, `${NAME-x}`, `${NAME:=x}`, `${NAME=x}`, `${NAME:+x}`; not `${NAME:?message}`
_NONWS = re.compile(r"\S{1,4096}")
_NEWLINE = re.compile("\n")
_BRACE_WINDOW, _BRACE_STEPS, _BRACE_DEPTH = 1100, 16, 4          # an unclosed `${` repeated on a long line must not be quadratic: each reference is read in a bounded window


def _braced_end(s: str, i: int) -> int:
    """The index just after the `}` that closes the `${` at s[i], counting nested `${ … }`; -1 when there is none within a bounded window, on the same line,
    or when it nests more than a few levels deep."""
    w = s[i:i + _BRACE_WINDOW]
    nl = w.find("\n")
    if nl >= 0:
        w = w[:nl]
    depth, pos = 0, 0
    for _ in range(_BRACE_STEPS):
        o, c = w.find("${", pos), w.find("}", pos)
        if c < 0:
            return -1
        if 0 <= o < c:
            depth += 1
            pos = o + 2
            if depth > _BRACE_DEPTH:
                return -1
        else:
            depth -= 1
            pos = c + 1
            if depth <= 0:
                return i + pos
    return -1


def _ref_default(ref: str) -> Optional[str]:
    """The literal default of a `${NAME:-default}` reference (nested references still in it), None for a bare `${NAME}` or `${NAME:?message}` (no literal in it)."""
    m = _DEFAULT_HEAD.match(ref)
    return ref[m.end():-1] if m and ref.endswith("}") else None


def _default_texts(default: str, depth: int = 0) -> List[str]:
    """The texts of a default to judge: its own literal text with the nested references taken out (`${B}Ab12Cd34Ef56` is `Ab12Cd34Ef56`), then the default of
    every nested reference."""
    lit, nested, i = [], [], 0  # type: List[str], List[str], int
    for _ in range(8):
        j = default.find("${", i)
        if j < 0:
            break
        end = _braced_end(default, j) if depth < _BRACE_DEPTH else -1
        lit.append(default[i:j])
        if end < 0:
            lit.append(default[j + 2:])             # unclosed, too long or nested too deep to read: the rest is judged as a literal (over-redaction is the safe side)
            i = len(default)
            break
        inner = _ref_default(default[j:end])
        if inner is not None:
            nested.extend(_default_texts(inner, depth + 1))
        i = end
    lit.append(default[i:])
    return ["".join(lit)] + nested


def _secret_defaults(default: str) -> List[str]:
    """The parts of the default of a `${NAME:-default}` that are a committed secret: each judged as a value of its own, so a placeholder word (`changeme`),
    another variable, a short value and a plain word with no digit (`postgres`, as in a dev compose file) are not."""
    out = []  # type: List[str]
    for text in _default_texts(default):
        d = text.strip().strip("\"'")
        if len(d) >= 8 and d[0] != "$" and not _PLACEHOLDER.match(d) and re.search(r"[0-9]", d) and re.search(r"[A-Za-z]", d):
            out.append(d)
    return out


# a bare variable: `$NAME`, `${NAME}`, either followed by a path (`$HOME/.secrets/x`): the only values that start with `$` and are not a literal
_CALL = re.compile(r"[A-Za-z_][\w.]{0,128}\(.{0,512}\)\Z")          # a function call spelled out: the word, `(`, the arguments, `)` at the very end
_PURE_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][\w.]{0,128}\})(/[\w./-]*)?\Z")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{8,}")


def _secret_like_run(run: str) -> bool:
    """A run of 8 or more letters and digits that reads as a committed secret and not as a file name: it has both, and either both cases of a letter or a digit
    followed by a letter (`Zx81kQm2Pw9L`, `kqm8wplzx`); `dbpass12` and `password` are names."""
    if not (re.search(r"[0-9]", run) and re.search(r"[A-Za-z]", run)):
        return False
    return bool(re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)) or bool(re.search(r"[0-9][A-Za-z]", run))


class _PureRef:
    """`$NAME` or `${NAME}`, alone or followed by a path (`$HOME/.secrets/x`). A path with a secret-looking run in it (`$X/Zx81kQm2Pw9L`) is a literal glued to
    a reference, so it is not pure (R5-B3). Same interface as a compiled pattern's `match`."""

    @staticmethod
    def match(s: str) -> bool:
        m = _PURE_REF_RE.match(s)
        if not m:
            return False
        return not (m.group(1) and any(_secret_like_run(r) for r in _ALNUM_RUN.findall(m.group(1))))


_PURE_REF = _PureRef()


def _braced_secret(raw: str) -> bool:
    """Whether `raw`, which starts with `$`, carries a committed secret: a literal in a `${NAME:-literal}` reference, or text glued to a reference
    (`${NAME}Ab12Cd34Ef56`). A bare `$NAME` or `${NAME}` carries none."""
    if not raw.startswith("$") or _PURE_REF.match(raw):
        return False
    return bool(_secret_defaults(raw))


def _is_placeholder(raw: str) -> bool:
    """A placeholder, as a WHOLE value (UN-3). A `${NAME}` reference is one, and `${NAME:-literal}` only when its literal is not a secret."""
    if raw.startswith("${") and _braced_end(raw, 0) == len(raw):
        return not _braced_secret(raw)
    return bool(_PLACEHOLDER.match(raw))


def _whole_value(value: str, line: str, col: int, path: str) -> str:
    """In a dotenv, ini, properties, yaml or Dockerfile an unquoted value is the whole run of non-blank characters: `,` `;` `"` `)` are ordinary password
    characters there, and the shared pattern stops at the first of them (`DB_PASSWORD=Ab12;Cd34Ef56` read as `Ab12`). Never past a blank: the rest of the line
    is other words (`ENV DB_PASSWORD=Ab12Cd34Ef56Gh78 API_URL=https://x`, `RUN DB_PASSWORD=test ./run-tests.sh`)."""
    if not path or not _CONFIG_NAME.search(path) or _NO_WHOLE_VALUE_NAME.search(path):
        return value
    m = _NONWS.match(line, col)
    return m.group(0) if m and m.group(0).startswith(value) else value


def _unquoted_values(value: str, line: str, col: int, path: str) -> List[str]:
    """The readings of an unquoted value to judge; one that passes `_unquoted_ok` is a secret. The value as the shared pattern cut it, and the whole run of
    non-blank characters (so a longer reading can only ADD a finding). A value that starts with `${` is read to its closing brace and the word after it: the
    default of `${NAME:-default}` and any literal glued to a reference are candidates (`${NAME}Ab12Cd34Ef56`); a reference that cannot be matched (unclosed, longer
    than the window, nested too deep) is read as a literal. A bare `$NAME` or `${NAME}` has none. Any other `$…` is a value like the rest (`$$Ab12Cd34Ef56`)."""
    if value.startswith("${"):
        end = _braced_end(line, col)
        word = _NONWS.match(line, col if end < 0 else end)
        token = (line[col:end] if end > 0 else "") + (word.group(0) if word else "")
        if _PURE_REF.match(token):
            return []
        return _secret_defaults(token)
    if value.startswith("$"):
        word = _NONWS.match(line, col)
        w = word.group(0) if word else value
        if _PURE_REF.match(w):
            return []
        head = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*", w)
        if head and any(_secret_like_run(r) for r in _ALNUM_RUN.findall(w[head.end():])):
            return [value, w[head.end():]]              # a literal glued to `$NAME` (`$X/Zx81kQm2Pw9L`): in shell and markdown a `$` value is otherwise skipped (R5-B3)
    whole = _whole_value(value, line, col, path)
    if _STRUCTURED_NAME.search(path) and _CALL.match(whole):
        return []                                       # `lookup(x)`, `get_secret(name)` in a yaml value is a call, not a password; `Ab12(Cd34Ef56` is a password
    return [value] if whole == value else [value, whole]


def _unquoted_ok(raw: str, prefix: str, line: str, path: str) -> bool:
    """Whether an unquoted `key = value` is a literal secret and not a variable or an expression. `raw` is the value, `prefix` the text of the line
    before it. Config files (`.env`, yaml, ini, properties, toml, json, xml, Dockerfile): always, whatever the value starts with, except a yaml,
    toml, json or xml structure character. Source code: only a `KEY=value` line. Anything else (shell, Makefile, markdown, text): the value follows
    a `KEY=` token (`-e KEY=v`, `--password=v`, `ENV KEY=v`). A `;Password=` in a connection string counts in any file. A value that starts with
    `$` is a variable."""
    raw = raw.strip()
    if len(raw) < 8 or _is_placeholder(raw) or _PURE_REF.match(raw) or "`" in raw or raw.startswith(("./", "../", "~/", "%(")):
        return False
    if "://" in raw and "@" not in raw:
        return False
    if re.match(r"^/[\w.-]+(?:/[\w.-]+)+/?$", raw):
        return False                                    # an absolute path with several segments
    if path and _CONFIG_NAME.search(path):
        # a yaml/toml/json value cannot start with a structure character, so one that does is syntax, not a password; in a dotenv, ini, properties
        # or Dockerfile `!`, `*`, `&`, `|`, `>`, `{`, `[`, `<` and `(` are ordinary characters (R-GATES-3: `DB_PASSWORD=!Xx7Qm9Zk2pq`)
        if _STRUCTURED_NAME.search(path) and raw[0] in "!*&|>{[<(%@":
            return False
        return True
    if "(" in raw or _REFERENCE.match(raw):
        return False
    if not (re.search(r"[0-9]", raw) and re.search(r"[A-Za-z]", raw) and _SHELLISH_VALUE.match(raw)):
        return False
    if _CONN_TOKEN.search(prefix):
        return True
    if path and _CODE_NAME.search(path):
        return bool(_KEY_VALUE_LINE.match(line))
    return bool(_ASSIGN_TOKEN.search(prefix)) or bool(_KEY_VALUE_LINE.match(line))


MAX_SPANS = 20000        # more secret-shaped matches than this in one text is not an honest file: one finding, and the rest is not judged one by one
_PREFIX_TAIL = 256       # the text before a value that `_unquoted_ok` looks at (`KEY=`, `-e KEY=`, `;Password=`) is a short token


def scan(text: str, path: str = "") -> List[Tuple[str, int]]:
    """[(kind, line number)] of secret-shaped text. Never returns a value. `path` (optional) says what kind of file the text is from. Text this reader cannot read
    (any exception, a RecursionError included) is one `unreadable` hit, never a clean result; more than MAX_SPANS matches is one `too-many-matches` hit."""
    try:
        return _scan(text, path)
    except Exception:  # noqa: BLE001
        return [("unreadable", 1)]


def _scan(text: str, path: str) -> List[Tuple[str, int]]:
    hits = []  # type: List[Tuple[str, int]]
    seen = set()
    spans = redact.find_secrets(text)
    if not spans:
        return hits
    if len(spans) > MAX_SPANS:
        return [("too-many-matches", 1)]
    # one pass: line starts are found once and every hit is placed by bisection; the text of a line is cut out once for all the hits on it (a 200 KB line with
    # 20 000 hits used to be copied 20 000 times)
    newlines = [m.start() for m in _NEWLINE.finditer(text)]
    cur_start, cur_end, line = -1, -1, ""
    for kind, start, end in spans:
        value = text[start:end]
        if value.strip("\"' ") in _KNOWN_EXAMPLES:            # the whole value, not a substring: `AKIAIOSFODNN7EXAMPLE` glued to a real secret is not the example
            continue
        k = bisect.bisect_left(newlines, start)
        line_start = newlines[k - 1] + 1 if k else 0
        if kind in ("secret-assignment", "bearer", "basic-auth", "url-credentials", "cli-secret-arg", "secret-element", "secret-attribute"):
            raw = value.strip("\"'")
            quoted = value.startswith(("\"", "'"))
            if kind == "secret-assignment" and not quoted:
                if line_start != cur_start:
                    k2 = bisect.bisect_left(newlines, end)
                    cur_start, cur_end = line_start, (newlines[k2] if k2 < len(newlines) else len(text))
                    line = text[cur_start:cur_end]
                col = start - line_start
                prefix = text[line_start:start] if col <= _PREFIX_TAIL else " " + text[start - _PREFIX_TAIL:start]
                if not any(_unquoted_ok(v, prefix, line, path) for v in _unquoted_values(value, line, col, path)):
                    continue
            elif len(raw) < (6 if kind == "cli-secret-arg" else 8) or _is_placeholder(raw) or (
                    kind != "secret-assignment" and raw.startswith("$") and not _braced_secret(raw)):
                continue                    # the VALUE only: a trailing comment saying "example" must not excuse a real one
            if kind in ("secret-assignment", "secret-element", "secret-attribute", "cli-secret-arg") and (quoted or kind != "secret-assignment") and not (
                    re.search(r"[0-9]", raw) and re.search(r"[A-Za-z]", raw)):
                continue                    # a quoted word with no digit is a name or a sentence, not a generated secret
            if kind == "bearer" and not re.search(r"[0-9]", raw):
                continue
            if kind == "url-credentials" and raw.lower() in ("password", "pass", "secret"):
                continue
        n = k + 1
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


def slices(text: str) -> Optional[List[str]]:
    """`text` cut into pieces of fewer than MAX_BYTES characters, at line ends where it can (a line longer than a piece is cut with a 4 KiB overlap so
    a secret on the cut survives in one piece). None when it would take more than MAX_SLICES pieces: the caller then skips the file and blocks (R-GATES-6:
    a large file used to be skipped with a warning, an easy way past the scan)."""
    if len(text) < MAX_BYTES:
        return [text]
    out = []  # type: List[str]
    cur, size = [], 0   # type: List[str], int
    for ln in text.split("\n"):
        while len(ln) >= MAX_BYTES:
            if cur:
                out.append("\n".join(cur))
                cur, size = [], 0
            out.append(ln[:MAX_BYTES - 1])
            ln = ln[MAX_BYTES - 4097:]
            if len(out) > MAX_SLICES:
                return None
        if size + len(ln) + 1 >= MAX_BYTES and cur:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
        if len(out) > MAX_SLICES:
            return None
    if cur:
        out.append("\n".join(cur))
    return out if len(out) <= MAX_SLICES else None


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
                parts = slices("\n".join(lines))
                if parts is None:
                    skipped.append(cur)
                    return
                for k, part in enumerate(parts):
                    chunks.append((label if len(parts) == 1 else "%s, part %d" % (label, k + 1), cur, part))
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
    # a git command that fails reads nothing, and "nothing found" must never look like "clean": it is recorded as skipped, which blocks
    for what, out in (("commit", _lib.git_ok(ctx, ["-c", "core.quotePath=false", "log", "-p", "--cc", "--format=%x01%H"] + common_args + ["%s..%s" % (ctx.base_sha, head), "--"])),
                      ("the working tree", _lib.git_ok(ctx, ["-c", "core.quotePath=false", "diff"] + common_args + ["HEAD", "--"]))):
        if out is None:
            skipped.append("<git failed reading %s>" % what)
        parse(out, what)
    untracked = [u for u in (_lib.git_ok(ctx, ["ls-files", "--others", "--exclude-standard", "-z"]) or "").split("\0") if u and common.safe_rel_path(u)]
    if len(untracked) > 50 and overflow is not None:
        overflow.append(str(len(untracked)))
    for rel in untracked[:50]:
        full = os.path.join(ctx.worktree, rel)
        try:
            if os.path.islink(full):
                continue
            if os.path.getsize(full) > MAX_BYTES * MAX_SLICES:
                skipped.append(rel)
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if "\x00" not in text[:4096]:
            parts = slices(text)
            if parts is None:
                skipped.append(rel)
                continue
            for k, part in enumerate(parts):
                chunks.append(("the working tree" if len(parts) == 1 else "the working tree, part %d" % (k + 1), rel, part))
    return chunks


def _run(ctx: Any) -> GateResult:
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
    if skipped:
        # what the built-in scan could not read blocks, unless the plan allows the path or the external scanner ran and passed (R-GATES-6): "skipped"
        # used to be a warning, and `delivery: local` has no CI re-run behind it
        covered = bool(cmd) and not any(f["id"] == "g11.scanner" for f in findings)
        open_ = [p for p in skipped if p.startswith("<") or not _lib.match_any(allow, p)]
        sev = "warn" if covered or not open_ else "error"
        findings.append(_lib.finding("g11.skipped-large", sev, "%d added file(s) the built-in scan could not read in full (over %d MiB in %d slices, or git failed) (%s)%s"
                                     % (len(skipped), MAX_BYTES >> 20, MAX_SLICES, ", ".join(common.clean_text(p, 50) for p in skipped[:3]),
                                        ": the external scanner covers them" if covered else ": set secrets_scan_cmd, split the file, or allow the path in the plan (ext.g11.allow)")))
        if sev == "error":
            n_hits += 1
    ev = {"summary": "%s scan: %d finding(s)" % (scanner, n_hits)}
    if n_hits:
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)


def run(ctx: Any) -> GateResult:
    """The secrets scan. Any exception while reading (a RecursionError, a MemoryError, a bug) is a blocking `g11.unreadable` finding: never a pass."""
    try:
        return _run(ctx)
    except Exception as exc:  # noqa: BLE001
        return GateResult.failed([_lib.finding("g11.unreadable", "error", "The secrets scan could not be read to the end (%s): a human reads the change" % (
            common.clean_text(type(exc).__name__, 40),))])
