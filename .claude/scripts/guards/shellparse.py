"""A small, conservative shell parser for the guards (report §7.1, CS-6, CO-3).

It turns a Bash command string into a flat list of simple commands (`Cmd`) so the guards can
judge every command that could run, however it was chained, quoted or nested. It is a
*guardrail* parser, not a shell: where the real shell would do something the parser cannot
follow (a variable it never saw assigned, `eval "$(curl ...)"`, a glob in command position),
the affected word is marked `dynamic` and the guards decide what that means for the caller.

What it follows
  * separators and grouping: `;` `&` `&&` `||` `|` `|&`, newlines, `( )`, `{ }`, `if/while/for/case`
  * quoting: single, double, `$'...'`, backslash, line continuation
  * expansion: `$(...)`, backticks, `$((...))`, `${...}` (default, substring, #, %, / operators),
    `$VAR` for variables assigned earlier in the same command, `$IFS` word splitting, `~`,
    brace expansion, process substitution. A command substitution built only from pure producers
    (`echo`, `printf`, `base64 -d`, `rev`, `tr`, `xxd -r -p`) is evaluated, so
    `$(echo dGVycmFmb3Jt | base64 -d) apply` is seen as `terraform apply`.
  * redirections (`>`, `>>`, `<`, `<<`, `<<<`, `&>`, fd prefixes) and heredoc bodies, including
    command substitutions inside an unquoted heredoc
  * `alias` assigned in the same command, `NAME=value` prefixes, standalone assignments
  * function definitions (the body is parsed as ordinary commands)

Limits that are real (documented in docs/security/guards.md): arbitrary computed strings, code in
files the parser is not pointed at, and anything the shell decides at run time.

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import base64
import fnmatch
import re
import binascii
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = ["Word", "Redir", "Cmd", "Script", "parse", "static_eval", "NUL", "ParseLimit", "MAX_INPUT"]

NUL = "\x00"            # placeholder for "something the parser could not resolve"
FIELD = "\x02"          # internal: word-splitting boundary
MAX_INPUT = 512 * 1024
MAX_CMDS = 1500
MAX_DEPTH = 8
MAX_BRACE = 64

_SENT = {"{": "", "}": "", ",": ""}
_SENT_BACK = {v: k for k, v in _SENT.items()}

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=")
SAFE_ENV_KEYS = ("HOME", "PWD", "OLDPWD", "TMPDIR", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "TERM",
                 "EDITOR", "HOSTNAME", "TMP", "TEMP")


class ParseLimit(Exception):
    """The input is too large or too deeply nested to analyse: callers fail closed."""


class Word(object):
    __slots__ = ("text", "dynamic", "raw", "bare", "glob", "maybe_empty", "subst")

    def __init__(self, text: str, dynamic: bool = False, raw: str = "", bare: bool = False, glob: bool = False,
                 maybe_empty: bool = False, subst: bool = False) -> None:
        self.text = text
        self.dynamic = dynamic
        self.raw = raw
        self.bare = bare          # unquoted literal source text: eligible to be a shell keyword
        self.glob = glob
        self.maybe_empty = maybe_empty
        self.subst = subst        # contained a command substitution

    def __repr__(self) -> str:
        return "Word(%r%s)" % (self.text.replace(NUL, "?"), "*" if self.dynamic else "")


class Redir(object):
    __slots__ = ("op", "fd", "target", "body", "quoted_delim")

    def __init__(self, op: str, fd: Optional[str], target: Optional[Word], body: Optional[str] = None,
                 quoted_delim: bool = False) -> None:
        self.op = op
        self.fd = fd
        self.target = target
        self.body = body
        self.quoted_delim = quoted_delim

    @property
    def writes(self) -> bool:
        if self.op in (">", ">>", ">|", "<>", "&>", "&>>"):
            return True
        if self.op == ">&":
            t = self.target.text if self.target is not None else ""
            return not (t.isdigit() or t == "-" or t.endswith("-") and t[:-1].isdigit())
        return False

    def __repr__(self) -> str:
        return "Redir(%s%s %r)" % (self.fd or "", self.op, self.target)


class Cmd(object):
    __slots__ = ("words", "assigns", "redirs", "pipe_prev", "pipe_next", "origin", "bg", "index", "kind", "depth",
                 "stdin_text")

    def __init__(self, origin: str = "top", depth: int = 0) -> None:
        self.words = []          # type: List[Word]
        self.assigns = []        # type: List[Tuple[str, str, bool]]   # name, value, dynamic (prefix NAME=value)
        self.redirs = []         # type: List[Redir]
        self.pipe_prev = None    # type: Optional[int]
        self.pipe_next = False
        self.origin = origin
        self.bg = False
        self.index = -1
        self.kind = "simple"
        self.depth = depth
        self.stdin_text = None   # type: Optional[str]

    def argv(self) -> List[str]:
        return [w.text for w in self.words]

    def __repr__(self) -> str:
        return "Cmd(%s %r%s)" % (self.origin, self.words, " |" if self.pipe_next else "")


class Script(object):
    def __init__(self) -> None:
        self.cmds = []        # type: List[Cmd]
        self.notes = []       # type: List[str]
        self.ifs_changed = False

    def __repr__(self) -> str:
        return "Script(%d cmds)" % len(self.cmds)


# ---------------------------------------------------------------------------------------------------------------
# helpers

def _ansi_c(s: str) -> str:
    out = []  # type: List[str]
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = s[i + 1]
        simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b", "E": "\x1b",
                  "\\": "\\", "'": "'", '"': '"', "?": "?"}
        if d in simple:
            out.append(simple[d])
            i += 2
        elif d == "x":
            m = re.match(r"[0-9a-fA-F]{1,2}", s[i + 2:])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
            else:
                out.append("\\x")
                i += 2
        elif d in "01234567":
            m = re.match(r"[0-7]{1,3}", s[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
        elif d in "uU":
            m = re.match(r"[0-9a-fA-F]{1,%d}" % (4 if d == "u" else 8), s[i + 2:])
            if m:
                try:
                    out.append(chr(int(m.group(0), 16)))
                except (ValueError, OverflowError):
                    out.append("?")
                i += 2 + len(m.group(0))
            else:
                out.append("\\" + d)
                i += 2
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(s[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + d)
            i += 2
    return "".join(out)


def _find_close_paren(text: str, i: int) -> int:
    """Index of the `)` matching an already-consumed `(` at text[i-1]. -1 if unbalanced."""
    depth = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text[i] == "$" and i + 1 < n and text[i + 1] == "(":
                    j = _find_close_paren(text, i + 2)
                    if j < 0:
                        return -1
                    i = j
                i += 1
            i += 1
            continue
        if c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 1
                j += 1
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            j = _find_close_paren(text, i + 2)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_close_brace(text: str, i: int) -> int:
    """Index of the `}` matching an already-consumed `${` (i points just after it). -1 if unbalanced."""
    depth = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            j = _find_close_paren(text, i + 2)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _brace_expand(s: str, limit: int = MAX_BRACE) -> List[str]:
    """Bash brace expansion on a string whose inert braces were already replaced by sentinels."""
    i = s.find("{")
    while i >= 0:
        depth, j, commas = 0, i, []
        while j < len(s):
            c = s[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            elif c == "," and depth == 1:
                commas.append(j)
            j += 1
        if j >= len(s):
            return [s]
        body = s[i + 1:j]
        pre, post = s[:i], s[j + 1:]
        alts = None  # type: Optional[List[str]]
        if commas:
            parts, last = [], i + 1
            for cpos in commas:
                parts.append(s[last:cpos])
                last = cpos + 1
            parts.append(s[last:j])
            alts = parts
        else:
            m = re.match(r"^(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?$", body)
            m2 = re.match(r"^([A-Za-z])\.\.([A-Za-z])$", body)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                step = abs(int(m.group(3))) if m.group(3) and int(m.group(3)) else 1
                seq = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
                alts = [str(x) for x in seq][:limit]
            elif m2:
                a, b = ord(m2.group(1)), ord(m2.group(2))
                seq = range(a, b + 1) if a <= b else range(a, b - 1, -1)
                alts = [chr(x) for x in seq][:limit]
        if alts is None:
            i = s.find("{", i + 1)
            continue
        out = []  # type: List[str]
        for a in alts:
            for e in _brace_expand(pre + a + post, limit):
                out.append(e)
                if len(out) >= limit:
                    return out
        return out
    return [s]


def _restore(s: str) -> str:
    for k, v in _SENT_BACK.items():
        s = s.replace(k, v)
    return s


def _sentinelize(s: str) -> str:
    for k, v in _SENT.items():
        s = s.replace(k, v)
    return s


def _pattern_remove(value: str, pattern: str, prefix: bool, longest: bool) -> str:
    n = len(value)
    rng = range(n + 1)
    if prefix:
        order = reversed(rng) if longest else rng
        for k in order:
            if fnmatch.fnmatchcase(value[:k], pattern):
                return value[k:]
    else:
        order = rng if longest else reversed(rng)
        for k in order:
            if fnmatch.fnmatchcase(value[k:], pattern):
                return value[:k]
    return value


# ---------------------------------------------------------------------------------------------------------------
# static evaluation of pure producer pipelines

def _echo_text(args: Sequence[str]) -> str:
    interpret = False
    newline = True
    i = 0
    while i < len(args) and re.match(r"^-[neE]+$", args[i]):
        flags = args[i][1:]
        if "n" in flags:
            newline = False
        if "e" in flags:
            interpret = True
        if "E" in flags:
            interpret = False
        i += 1
    text = " ".join(args[i:])
    if interpret:
        text = _ansi_c(text)
    return text + ("\n" if newline else "")


def _printf_text(args: Sequence[str]) -> Optional[str]:
    if not args:
        return ""
    fmt, rest = args[0], list(args[1:])
    out = []  # type: List[str]
    first = True
    while first or rest:
        first = False
        i, consumed = 0, 0
        buf = []  # type: List[str]
        while i < len(fmt):
            c = fmt[i]
            if c == "%" and i + 1 < len(fmt):
                d = fmt[i + 1]
                if d == "%":
                    buf.append("%")
                    i += 2
                    continue
                m = re.match(r"%[-+ #0]*\d*(?:\.\d+)?([sbdiqxXoucf])", fmt[i:])
                if not m:
                    return None
                arg = rest.pop(0) if rest else ""
                consumed += 1
                conv = m.group(1)
                if conv == "s":
                    buf.append(arg)
                elif conv == "b":
                    buf.append(_ansi_c(arg))
                elif conv in "di":
                    try:
                        buf.append(str(int(arg or "0", 0)))
                    except ValueError:
                        return None
                elif conv == "c":
                    buf.append(arg[:1])
                elif conv in "xXo":
                    try:
                        buf.append(format(int(arg or "0", 0), conv))
                    except ValueError:
                        return None
                else:
                    return None
                i += len(m.group(0))
            elif c == "\\":
                m2 = re.match(r"\\(?:x[0-9a-fA-F]{1,2}|[0-7]{1,3}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", fmt[i:], re.S)
                if m2:
                    buf.append(_ansi_c(m2.group(0)))
                    i += len(m2.group(0))
                else:
                    buf.append("\\")
                    i += 1
            else:
                buf.append(c)
                i += 1
        out.append("".join(buf))
        if consumed == 0:
            break
    return "".join(out)


def _tr_expand(spec: str) -> str:
    out, i = [], 0
    spec = _ansi_c(spec)
    while i < len(spec):
        if i + 2 < len(spec) and spec[i + 1] == "-":
            a, b = ord(spec[i]), ord(spec[i + 2])
            out.extend(chr(x) for x in range(a, b + 1))
            i += 3
        else:
            out.append(spec[i])
            i += 1
    return "".join(out)


def _stage(name: str, args: Sequence[str], data: Optional[str]) -> Optional[str]:
    """One pure pipeline stage. None = not a pure stage we can evaluate."""
    name = name.lower()
    if name == "echo":
        return _echo_text(args)
    if name == "printf":
        return _printf_text(args)
    if data is None:
        return None
    if name == "cat" and not args:
        return data
    if name in ("base64", "openssl", "b64decode"):
        a = list(args)
        if name == "openssl":
            if a[:1] != ["base64"]:
                return None
            a = a[1:]
        if not any(x in ("-d", "-D", "--decode", "-decode") for x in a):
            return None
        try:
            raw = base64.b64decode("".join(data.split()) + "=" * (-len("".join(data.split())) % 4), validate=False)
        except (binascii.Error, ValueError):
            return None
        return raw.decode("utf-8", "replace")
    if name == "xxd" and "-r" in args and ("-p" in args or "-ps" in args or "-r" in args):
        try:
            return bytes.fromhex("".join(data.split())).decode("utf-8", "replace")
        except ValueError:
            return None
    if name == "rev":
        return "\n".join(line[::-1] for line in data.split("\n"))
    if name == "tr":
        a = [x for x in args if x != "--"]
        delete = "-d" in a
        a = [x for x in a if not x.startswith("-") or x == "-"]
        if delete and len(a) == 1:
            drop = set(_tr_expand(a[0]))
            return "".join(ch for ch in data if ch not in drop)
        if len(a) == 2 and not delete:
            s1, s2 = _tr_expand(a[0]), _tr_expand(a[1])
            if not s2:
                return None
            s2 = s2 + s2[-1] * (len(s1) - len(s2))
            table = {ord(c): s2[k] for k, c in enumerate(s1)}
            return data.translate(table)
        return None
    if name in ("tee",) and not args:
        return data
    return None


def static_eval(cmds: Sequence["Cmd"], allow_final_redirect: bool = False) -> Optional[str]:
    """Evaluate a command list made only of pure producers, or None. Trailing newlines are kept (the caller strips
    them for `$(...)`)."""
    top = [c for c in cmds if c.words or c.stdin_text is not None]
    if not top:
        return None
    # a single pipeline: each command's pipe_prev is the previous one
    chain = [top[0]]
    for c in top[1:]:
        if c.pipe_prev is not None and chain[-1].index == c.pipe_prev:
            chain.append(c)
        else:
            return None
    data = None  # type: Optional[str]
    for pos, c in enumerate(chain):
        if any(r.writes for r in c.redirs) and not (allow_final_redirect and pos == len(chain) - 1):
            return None
        if any(w.dynamic for w in c.words):
            return None
        if c.assigns:
            return None
        if not c.words:
            return None
        name = c.words[0].text.rsplit("/", 1)[-1]
        stdin = c.stdin_text if c.stdin_text is not None else data
        out = _stage(name, [w.text for w in c.words[1:]], stdin if pos > 0 or c.stdin_text is not None else None)
        if out is None:
            return None
        data = out
    return data


# ---------------------------------------------------------------------------------------------------------------
# the parser

_KW_SKIP = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "esac", "!", "time", "{", "}",
            "coproc", "in"}
_OP_CHARS = ";&|()<>"
_REDIR_OPS = ["&>>", "<<<", "<<-", ">>", "<<", "&>", ">&", "<&", ">|", "<>", ">", "<"]


class _Shared(object):
    def __init__(self, base_env: Mapping[str, str]) -> None:
        self.cmds = []                # type: List[Cmd]
        self.notes = []               # type: List[str]
        self.base_env = {k: v for k, v in base_env.items() if k in SAFE_ENV_KEYS}
        self.aliases = {}             # type: Dict[str, str]
        self.ifs_changed = False
        self.total = 0


class _Parser(object):
    def __init__(self, text: str, shared: _Shared, vars_: Dict[str, Tuple[str, bool]], depth: int, origin: str) -> None:
        if depth > MAX_DEPTH:
            raise ParseLimit("nesting deeper than %d" % MAX_DEPTH)
        self.s = text
        self.i = 0
        self.n = len(text)
        self.sh = shared
        self.vars = vars_
        self.depth = depth
        self.origin = origin
        self.pending_heredocs = []    # type: List[Tuple[Redir, str, bool, bool]]
        self.prev_idx = None          # type: Optional[int]

    # -- character level ------------------------------------------------------------------------------
    def _skip_blank(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r":
                self.i += 1
            elif c == "\\" and self.i + 1 < n and s[self.i + 1] == "\n":
                self.i += 2
            elif c == "#" and (self.i == 0 or s[self.i - 1] in " \t\n;&|()"):
                while self.i < n and s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def _read_heredoc_bodies(self) -> None:
        """Called right after consuming a newline: read each pending heredoc body."""
        for redir, delim, strip_tabs, quoted in self.pending_heredocs:
            lines = []  # type: List[str]
            while self.i < self.n:
                j = self.s.find("\n", self.i)
                line = self.s[self.i:] if j < 0 else self.s[self.i:j]
                self.i = self.n if j < 0 else j + 1
                cmp_line = line.lstrip("\t") if strip_tabs else line
                if cmp_line.rstrip("\r") == delim:
                    break
                lines.append(line.lstrip("\t") if strip_tabs else line)
            redir.body = "\n".join(lines) + ("\n" if lines else "")
            if not quoted and redir.body:
                self._scan_body_substitutions(redir.body)
        self.pending_heredocs = []

    def _scan_body_substitutions(self, body: str) -> None:
        """Commands inside `$(...)`/backticks of an unquoted heredoc body still run."""
        i, n = 0, len(body)
        while i < n:
            c = body[i]
            if c == "\\":
                i += 2
                continue
            if c == "$" and i + 1 < n and body[i + 1] == "(" and not body.startswith("$((", i):
                j = _find_close_paren(body, i + 2)
                if j > 0:
                    self._sub(body[i + 2:j], "subst")
                    i = j + 1
                    continue
            if c == "`":
                j = i + 1
                while j < n and body[j] != "`":
                    j += 2 if body[j] == "\\" else 1
                self._sub(self._unbacktick(body[i + 1:j]), "subst")
                i = j + 1
                continue
            i += 1

    @staticmethod
    def _unbacktick(code: str) -> str:
        return re.sub(r"\\([`\\$])", r"\1", code)

    # -- substitutions ---------------------------------------------------------------------------------
    def _sub(self, code: str, origin: str) -> Tuple[List[Cmd], bool]:
        """Parse a nested script. Its commands join the flat list. Returns (its commands, ok)."""
        if self.sh.total > MAX_CMDS:
            raise ParseLimit("more than %d commands" % MAX_CMDS)
        p = _Parser(code, self.sh, dict(self.vars), self.depth + 1, origin)
        start = len(self.sh.cmds)
        p.parse_list(set(), set())
        return self.sh.cmds[start:], True

    # -- words -----------------------------------------------------------------------------------------
    def _lookup(self, name: str) -> Optional[Tuple[str, bool]]:
        if name == "IFS":
            return (" \t\n", False) if not self.sh.ifs_changed else None
        if name in self.vars:
            return self.vars[name]
        if name in self.sh.base_env:
            return (self.sh.base_env[name], False)
        return None

    def _param(self, body: str, quoted: bool) -> Tuple[str, bool]:
        """Resolve the inside of `${...}`. Returns (value, dynamic)."""
        b = body
        if b.startswith("#") and _NAME_RE.fullmatch(b[1:] or "x"):
            v = self._lookup(b[1:])
            return (str(len(v[0])), False) if v is not None and not v[1] else (NUL, True)
        if b.startswith("!"):
            return NUL, True
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])(.*)$", b, re.S)
        if not m:
            return NUL, True
        name, op = m.group(1), m.group(2)
        v = self._lookup(name)
        known = v is not None and not v[1]
        val = v[0] if known else ""
        if not op:
            return (val, False) if known else (NUL, True)
        if op.startswith((":-", "-", ":=", "=")):
            colon = op.startswith(":")
            word = op[2:] if colon else op[1:]
            default_val, _ = self._expand_inline(word)
            if known and (val != "" or not colon):
                return val, False
            if known:
                return default_val, False
            return default_val, True
        if op.startswith((":+", "+")):
            colon = op.startswith(":")
            word = op[2:] if colon else op[1:]
            if known:
                return ((self._expand_inline(word)[0], False) if (val != "" or not colon) else ("", False))
            return NUL, True
        if not known:
            return NUL, True
        if op.startswith(":"):
            m2 = re.match(r"^:\s*(-?\d+)\s*(?::\s*(-?\d+))?\s*$", op)
            if m2:
                a = int(m2.group(1))
                if a < 0:
                    a = max(len(val) + a, 0)
                if m2.group(2) is None:
                    return val[a:], False
                ln = int(m2.group(2))
                end = a + ln if ln >= 0 else len(val) + ln
                return val[a:end], False
            return NUL, True
        if op.startswith(("##", "#")):
            longest = op.startswith("##")
            pat = self._expand_inline(op[2 if longest else 1:])[0]
            return _pattern_remove(val, pat, True, longest), False
        if op.startswith(("%%", "%")):
            longest = op.startswith("%%")
            pat = self._expand_inline(op[2 if longest else 1:])[0]
            return _pattern_remove(val, pat, False, longest), False
        if op.startswith(("//", "/")):
            all_ = op.startswith("//")
            rest = op[2 if all_ else 1:]
            pat, _, rep = rest.partition("/")
            pat, rep = self._expand_inline(pat)[0], self._expand_inline(rep)[0]
            if pat.startswith("#"):
                return (rep + val[len(pat) - 1:]) if val.startswith(pat[1:]) else val, False
            if pat.startswith("%"):
                return (val[:len(val) - len(pat) + 1] + rep) if val.endswith(pat[1:]) else val, False
            if "*" in pat or "?" in pat or "[" in pat:
                return NUL, True
            return (val.replace(pat, rep) if all_ else val.replace(pat, rep, 1)), False
        if op in ("^^", "^"):
            return val.upper() if op == "^^" else val[:1].upper() + val[1:], False
        if op in (",,", ","):
            return val.lower() if op == ",," else val[:1].lower() + val[1:], False
        return NUL, True

    def _expand_inline(self, word: str) -> Tuple[str, bool]:
        """Expand a small word (the default in `${X:-word}`): quotes removed, $VAR resolved."""
        try:
            p = _Parser(word, self.sh, dict(self.vars), self.depth + 1, "inline")
            segs, flags = p._scan_word_segments(stop_at=set())
            text = "".join(t for t, _ in segs)
            return text, NUL in text
        except ParseLimit:
            raise
        except Exception:
            return NUL, True

    def _scan_dq(self, segs: List[Tuple[str, str]], flags: Dict[str, bool]) -> None:
        """Inside double quotes; self.i is just after the opening quote."""
        s, n = self.s, self.n
        buf = []  # type: List[str]

        def flush() -> None:
            if buf:
                segs.append(("".join(buf), "Q"))
                del buf[:]
        while self.i < n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                flush()
                return
            if c == "\\" and self.i + 1 < n:
                d = s[self.i + 1]
                if d in '$`"\\':
                    buf.append(d)
                elif d == "\n":
                    pass
                else:
                    buf.append("\\" + d)
                self.i += 2
                continue
            if c == "$":
                if self.i + 1 < n and s[self.i + 1] == "(":
                    flush()
                    self._scan_dollar_paren(segs, flags, quoted=True)
                    continue
                if self.i + 1 < n and s[self.i + 1] == "{":
                    j = _find_close_brace(s, self.i + 2)
                    if j < 0:
                        raise ParseLimit("unbalanced ${")
                    val, dyn = self._param(s[self.i + 2:j], True)
                    flush()
                    segs.append((val, "QD" if dyn else "Q"))
                    if dyn:
                        flags["dynamic"] = True
                    self.i = j + 1
                    continue
                m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])", s[self.i:])
                if m:
                    flush()
                    name = m.group(1)
                    v = self._lookup(name) if _NAME_RE.fullmatch(name) else None
                    if v is not None and not v[1]:
                        segs.append((v[0], "Q"))
                    else:
                        segs.append((NUL, "QD"))
                        flags["dynamic"] = True
                    self.i += len(m.group(0))
                    continue
            if c == "`":
                flush()
                self._scan_backtick(segs, flags, quoted=True)
                continue
            buf.append(c)
            self.i += 1
        raise ParseLimit("unterminated double quote")

    def _scan_dollar_paren(self, segs: List[Tuple[str, str]], flags: Dict[str, bool], quoted: bool) -> None:
        s = self.s
        if s.startswith("$((", self.i):
            j = _find_close_paren(s, self.i + 2)
            if j > 0 and s[j - 1] == ")":
                segs.append((NUL, "QD" if quoted else "D"))
                flags["dynamic"] = True
                self.i = j + 1
                return
        j = _find_close_paren(s, self.i + 2)
        if j < 0:
            raise ParseLimit("unbalanced $(")
        code = s[self.i + 2:j]
        self.i = j + 1
        self._finish_subst(code, segs, flags, quoted)

    def _scan_backtick(self, segs: List[Tuple[str, str]], flags: Dict[str, bool], quoted: bool) -> None:
        s, n = self.s, self.n
        j = self.i + 1
        while j < n and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        if j >= n:
            raise ParseLimit("unterminated backtick")
        code = self._unbacktick(s[self.i + 1:j])
        self.i = j + 1
        self._finish_subst(code, segs, flags, quoted)

    def _finish_subst(self, code: str, segs: List[Tuple[str, str]], flags: Dict[str, bool], quoted: bool) -> None:
        flags["subst"] = True
        cmds, _ = self._sub(code, "subst")
        value = static_eval(cmds)
        if value is not None:
            value = value.rstrip("\n")
            segs.append((value, "Q" if quoted else "X"))
        else:
            segs.append((NUL, "QD" if quoted else "D"))
            flags["dynamic"] = True

    def _scan_word_segments(self, stop_at: Optional[Set[str]] = None) -> Tuple[List[Tuple[str, str]], Dict[str, bool]]:
        """Scan one word starting at self.i. Returns (segments, flags)."""
        s, n = self.s, self.n
        segs = []  # type: List[Tuple[str, str]]
        flags = {"dynamic": False, "subst": False, "quote": False, "glob": False}
        lit = []  # type: List[str]

        def flush() -> None:
            if lit:
                segs.append(("".join(lit), "L"))
                del lit[:]
        start = self.i
        while self.i < n:
            c = s[self.i]
            if c in " \t\n\r":
                break
            if c in ";&|()" or (c in "<>" and not (self.i + 1 < n and s[self.i + 1] == "(")):
                if stop_at is not None and not stop_at:
                    pass
                break
            if c == "\\":
                if self.i + 1 < n:
                    if s[self.i + 1] == "\n":
                        self.i += 2
                        continue
                    flush()
                    segs.append((s[self.i + 1], "Q"))
                    flags["quote"] = True
                    self.i += 2
                else:
                    self.i += 1
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseLimit("unterminated single quote")
                flush()
                segs.append((s[self.i + 1:j], "Q"))
                flags["quote"] = True
                self.i = j + 1
                continue
            if c == '"':
                flush()
                flags["quote"] = True
                self.i += 1
                before = len(segs)
                self._scan_dq(segs, flags)
                if len(segs) == before:
                    segs.append(("", "Q"))
                continue
            if c == "$":
                nxt = s[self.i + 1] if self.i + 1 < n else ""
                if nxt == "'":
                    j = self.i + 2
                    while j < n and s[j] != "'":
                        j += 2 if s[j] == "\\" else 1
                    if j >= n:
                        raise ParseLimit("unterminated $'")
                    flush()
                    segs.append((_ansi_c(s[self.i + 2:j]), "Q"))
                    flags["quote"] = True
                    self.i = j + 1
                    continue
                if nxt == '"':
                    flush()
                    flags["quote"] = True
                    self.i += 2
                    self._scan_dq(segs, flags)
                    continue
                if nxt == "(":
                    flush()
                    self._scan_dollar_paren(segs, flags, quoted=False)
                    continue
                if nxt == "{":
                    j = _find_close_brace(s, self.i + 2)
                    if j < 0:
                        raise ParseLimit("unbalanced ${")
                    flush()
                    inner = s[self.i + 2:j]
                    val, dyn = self._param(inner, False)
                    segs.append((val, "D" if dyn and val == NUL else ("XD" if dyn else "X")))
                    if dyn:
                        flags["dynamic"] = True
                    self.i = j + 1
                    continue
                m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])", s[self.i:])
                if m:
                    flush()
                    name = m.group(1)
                    v = self._lookup(name) if _NAME_RE.fullmatch(name) else None
                    if v is not None and not v[1]:
                        segs.append((v[0], "X"))
                    else:
                        segs.append((NUL, "D"))
                        flags["dynamic"] = True
                    self.i += len(m.group(0))
                    continue
                lit.append(c)
                self.i += 1
                continue
            if c == "`":
                flush()
                self._scan_backtick(segs, flags, quoted=False)
                continue
            if c in "<>" and self.i + 1 < n and s[self.i + 1] == "(":
                j = _find_close_paren(s, self.i + 2)
                if j < 0:
                    raise ParseLimit("unbalanced process substitution")
                flush()
                code = s[self.i + 2:j]
                cmds, _ = self._sub(code, "procsub")
                value = static_eval(cmds) if c == "<" else None
                if value is not None:
                    segs.append(("\x01PROCSUB:" + value, "Q"))
                    flags["procsub"] = True
                else:
                    segs.append((NUL, "D"))
                    flags["dynamic"] = True
                self.i = j + 1
                continue
            if c == "~" and not segs and not lit and (self.i == start):
                m = re.match(r"~([A-Za-z0-9_.-]*)(?=/|$|\s|[;&|()<>])", s[self.i:])
                if m and not m.group(1):
                    home = self.sh.base_env.get("HOME")
                    if home:
                        segs.append((home, "Q"))
                        self.i += 1
                        continue
                elif m:
                    segs.append((NUL, "D"))
                    flags["dynamic"] = True
                    self.i += len(m.group(0))
                    continue
            if c in "*?[":
                flags["glob"] = True
            lit.append(c)
            self.i += 1
        flush()
        return segs, flags

    def _build_words(self, segs: List[Tuple[str, str]], flags: Dict[str, bool], raw: str) -> List[Word]:
        """Brace expansion, word splitting and quote removal over scanned segments."""
        if not segs:
            return []
        has_q = any(m in ("Q", "QD") for _, m in segs)
        all_literal = all(m == "L" for _, m in segs)
        parts = []  # type: List[str]
        for text, mode in segs:
            if mode == "L":
                parts.append(text)
            elif mode in ("Q", "QD"):
                parts.append(_sentinelize(text))
            elif mode in ("X", "XD"):
                t = _sentinelize(text)
                t = re.sub(r"[ \t\n]+", FIELD, t)
                parts.append(t)
            else:  # D
                parts.append(NUL)
        joined = "".join(parts)
        variants = _brace_expand(joined) if "{" in joined else [joined]
        words = []  # type: List[Word]
        for v in variants:
            fields = v.split(FIELD) if FIELD in v else [v]
            for k, f in enumerate(fields):
                f = _restore(f)
                if f == "" and not (has_q and len(fields) == 1):
                    continue
                dyn = NUL in f
                words.append(Word(f, dynamic=dyn, raw=raw, bare=all_literal and len(variants) == 1 and FIELD not in v,
                                  glob=flags.get("glob", False) and not has_q or flags.get("glob", False) and any(
                                      m == "L" and any(ch in t for ch in "*?[") for t, m in segs),
                                  maybe_empty=dyn and f.replace(NUL, "") == "", subst=flags.get("subst", False)))
        if not words and flags.get("dynamic"):
            words.append(Word(NUL, True, raw, False, False, True, flags.get("subst", False)))
        return words

    def _read_word(self) -> List[Word]:
        raw_start = self.i
        segs, flags = self._scan_word_segments()
        raw = self.s[raw_start:self.i]
        return self._build_words(segs, flags, raw)

    # -- redirections ----------------------------------------------------------------------------------
    def _try_redir_op(self) -> Optional[Tuple[str, Optional[str]]]:
        """If a redirection operator starts at self.i, consume it and return (op, fd)."""
        s, n = self.s, self.n
        j = self.i
        fd = None
        m = re.match(r"(\d+|\{[A-Za-z_][A-Za-z0-9_]*\})(?=[<>&])", s[j:])
        if m:
            fd = m.group(1)
            j += len(m.group(1))
        for op in _REDIR_OPS:
            if s.startswith(op, j):
                if op in ("<", ">") and j + 1 < n and s[j + 1] == "(":
                    return None
                if fd is None and op in ("<&", ">&", "&>", "&>>"):
                    pass
                self.i = j + len(op)
                return op, fd
        return None

    # -- the command loop ------------------------------------------------------------------------------
    def parse_list(self, stop_ops: Set[str], stop_words: Set[str]) -> Optional[str]:
        """Parse commands until one of `stop_ops` (operators) or `stop_words` (keywords at command start).
        Returns the stopper, or None at end of input."""
        while True:
            self._skip_blank()
            if self.i >= self.n:
                return None
            c = self.s[self.i]
            if c == "\n":
                self.i += 1
                self._read_heredoc_bodies()
                self.prev_idx = None
                continue
            r = self._parse_command(stop_ops, stop_words)
            if r is not None and r[0] == "stop":
                return r[1]

    def _finish(self, cmd: Cmd) -> None:
        if not cmd.words and not cmd.assigns and not cmd.redirs:
            return
        self.sh.total += 1
        if self.sh.total > MAX_CMDS:
            raise ParseLimit("more than %d commands" % MAX_CMDS)
        # alias expansion in command position
        if cmd.words and cmd.words[0].bare and cmd.words[0].text in self.sh.aliases and cmd.kind == "simple":
            val = self.sh.aliases[cmd.words[0].text]
            try:
                p = _Parser(val, self.sh, dict(self.vars), self.depth + 1, "alias")
                pre = []  # type: List[Word]
                while True:
                    p._skip_blank()
                    if p.i >= p.n:
                        break
                    pre.extend(p._read_word())
                cmd.words = pre + cmd.words[1:]
            except ParseLimit:
                pass
        if cmd.words:
            name = cmd.words[0].text
            args = [w.text for w in cmd.words[1:]]
            low = name.rsplit("/", 1)[-1]
            if low == "alias":
                for a in args:
                    mm = re.match(r"^([A-Za-z0-9_.:+-]+)=(.*)$", a, re.S)
                    if mm:
                        self.sh.aliases[mm.group(1)] = mm.group(2)
            elif low in ("export", "declare", "typeset", "readonly", "local", "set"):
                for w in cmd.words[1:]:
                    m = _ASSIGN_RE.match(w.text)
                    if m:
                        self._assign(m.group(1), w.text[m.end():], w.dynamic, m.group(2) == "+")
            elif low in ("read", "mapfile", "readarray", "getopts", "unset"):
                for a in args:
                    if _NAME_RE.fullmatch(a):
                        self.vars.pop(a, None)
                        if a == "IFS":
                            self.sh.ifs_changed = True
        elif cmd.assigns:
            for name, value, dyn in cmd.assigns:
                self._assign(name, value, dyn, False)
        cmd.index = len(self.sh.cmds)
        cmd.depth = self.depth
        self.sh.cmds.append(cmd)

    def _assign(self, name: str, value: str, dyn: bool, append: bool) -> None:
        if name == "IFS" and value != " \t\n":
            self.sh.ifs_changed = True
        if append:
            old = self.vars.get(name)
            if old is None or old[1] or dyn:
                self.vars[name] = (NUL, True)
                return
            value = old[0] + value
        self.vars[name] = (value, dyn or NUL in value)

    def _parse_command(self, stop_ops: Set[str], stop_words: Set[str]) -> Optional[Tuple[str, Optional[str]]]:
        s = self.s
        cmd = Cmd(self.origin, self.depth)
        pending = None   # type: Optional[Redir]
        pend_heredoc = None  # type: Optional[Tuple[Redir, bool, bool]]
        while True:
            self._skip_blank()
            if self.i >= self.n:
                break
            c = s[self.i]
            if c == "\n":
                break
            # operators
            if c in ";&|()":
                if c == "(":
                    if not cmd.words and not cmd.assigns:
                        if s.startswith("((", self.i):
                            j = _find_close_paren(s, self.i + 2)
                            if j > 0 and j + 1 <= self.n and s[j - 1] == ")":
                                self.i = j + 1
                                cmd.kind = "arith"
                                continue
                        self.i += 1
                        self.parse_list({")"}, set())
                        cmd.kind = "subshell"
                        continue
                    if cmd.words and len(cmd.words) == 1 and re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", cmd.words[0].text):
                        # name ( ) { ... }: a function definition
                        k = self.i + 1
                        while k < self.n and s[k] in " \t":
                            k += 1
                        if k < self.n and s[k] == ")":
                            self.i = k + 1
                            cmd.words = []
                            cmd.kind = "funcdef"
                            continue
                    self.i += 1
                    continue
                if c == ")":
                    if ")" in stop_ops:
                        self.i += 1
                        self._end_command(cmd, pending, pend_heredoc)
                        self._push(cmd)
                        return ("stop", ")")
                    self.i += 1
                    continue
                if s.startswith(";;&", self.i):
                    op = ";;&"
                elif s.startswith(";;", self.i):
                    op = ";;"
                elif s.startswith(";&", self.i):
                    op = ";&"
                elif s.startswith("&&", self.i):
                    op = "&&"
                elif s.startswith("||", self.i):
                    op = "||"
                elif s.startswith("|&", self.i):
                    op = "|&"
                else:
                    op = c
                if op == "&" and s.startswith("&>", self.i):
                    pass  # redirect, handled below
                else:
                    self.i += len(op)
                    self._end_command(cmd, pending, pend_heredoc)
                    cmd_has = bool(cmd.words or cmd.assigns or cmd.redirs)
                    if op in ("|", "|&"):
                        cmd.pipe_next = True
                    if op == "&":
                        cmd.bg = True
                    idx_before = len(self.sh.cmds)
                    self._push(cmd)
                    if op in ("|", "|&") and cmd_has:
                        self.prev_idx = cmd.index
                    elif cmd_has or True:
                        self.prev_idx = None
                    del idx_before
                    if op in stop_ops:
                        return ("stop", op)
                    return None
            # redirections
            r = self._try_redir_op()
            if r is not None:
                op, fd = r
                rd = Redir(op, fd, None)
                cmd.redirs.append(rd)
                if op in ("<<", "<<-"):
                    pend_heredoc = (rd, op == "<<-", False)
                    pending = rd
                else:
                    pending = rd
                continue
            if c == "&" or c == "|":
                self.i += 1
                continue
            words = self._read_word()
            if not words:
                if self.i < self.n and self.s[self.i] in _OP_CHARS and self.s[self.i] not in "<>":
                    continue
                self.i += 1
                continue
            if pending is not None:
                w = words[0]
                if pend_heredoc is not None and pending is pend_heredoc[0]:
                    quoted = not w.bare
                    delim = w.text
                    self.pending_heredocs.append((pending, delim, pend_heredoc[1], quoted))
                    pending.quoted_delim = quoted
                    pending.target = w
                    pend_heredoc = None
                else:
                    pending.target = w
                    if pending.op == "<<<":
                        pending.body = w.text + "\n"
                        if not w.dynamic:
                            pass
                pending = None
                for extra in words[1:]:
                    cmd.words.append(extra)
                continue
            # keywords and assignments at command start
            w = words[0]
            if not cmd.words:
                if w.bare and len(words) == 1:
                    kw = w.text
                    if kw in stop_words:
                        self._end_command(cmd, pending, pend_heredoc)
                        self._push(cmd)
                        return ("stop", kw)
                    if kw in _KW_SKIP and not cmd.assigns:
                        continue
                    if kw == "case":
                        self._parse_case()
                        cmd.kind = "case"
                        continue
                    if kw in ("for", "select"):
                        cmd.kind = "for"
                        self._parse_for()
                        continue
                    if kw == "function":
                        cmd.kind = "funcdef"
                        self._skip_blank()
                        self._read_word()
                        self._skip_blank()
                        if self.s.startswith("()", self.i):
                            self.i += 2
                        continue
                    if kw == "[[":
                        self._skip_dbracket()
                        cmd.kind = "test"
                        continue
                m = _ASSIGN_RE.match(w.raw) if w.raw else None
                if m and not (cmd.words):
                    name = m.group(1)
                    rest_raw = w.raw[m.end():]
                    if rest_raw.startswith("(") or (not rest_raw and self.i < self.n and self.s[self.i] == "("):
                        # array assignment: NAME=( ... )
                        if not rest_raw:
                            self.i += 1
                        j = _find_close_paren(self.s, self.i if not rest_raw else self.i)
                        if rest_raw:
                            j = _find_close_paren(self.s, self.i - len(rest_raw) + 1) if False else j
                        self.i = (j + 1) if j > 0 else self.n
                        cmd.assigns.append((name, NUL, True))
                        continue
                    val = w.text[len(name) + len(m.group(2)) + 1:]
                    # re-expand only the value part so quoting is honoured (w.text already is quote-removed)
                    cmd.assigns.append((name + ("+" if m.group(2) else ""), val, w.dynamic))
                    if m.group(2):
                        cmd.assigns[-1] = (name, val, w.dynamic)
                    continue
            cmd.words.extend(words)
        self._end_command(cmd, pending, pend_heredoc)
        if cmd.words or cmd.assigns or cmd.redirs:
            self._push(cmd)
        return None

    def _end_command(self, cmd: Cmd, pending: Optional[Redir], pend_heredoc: Any) -> None:
        for rd in cmd.redirs:
            if rd.op == "<<<" and rd.body is not None and not rd.target.dynamic if rd.target is not None else False:
                cmd.stdin_text = rd.body

    def _push(self, cmd: Cmd) -> None:
        if not (cmd.words or cmd.assigns or cmd.redirs):
            return
        cmd.pipe_prev = self.prev_idx if self.prev_idx is not None else None
        self._finish(cmd)

    def _skip_dbracket(self) -> None:
        s = self.s
        depth = 1
        while self.i < self.n:
            self._skip_blank()
            if self.i >= self.n:
                break
            if s[self.i] == "\n":
                self.i += 1
                continue
            if s[self.i] in ";&|()<>" and s[self.i] not in "":
                # operators inside [[ ]] belong to the expression
                self.i += 1
                continue
            for w in self._read_word():
                if w.text == "]]" and w.bare:
                    return
            if self.i < self.n and s[self.i] not in " \t\n":
                self.i += 1

    def _parse_for(self) -> None:
        """`for x in a b c; do` : read to the `;`/newline/`do`. Only the substitutions matter."""
        while self.i < self.n:
            self._skip_blank()
            if self.i >= self.n or self.s[self.i] in ";\n":
                break
            ws = self._read_word()
            if not ws:
                self.i += 1
                continue
            if ws[0].bare and ws[0].text == "do":
                break
            if ws[0].bare and ws[0].text == "((":
                break

    def _parse_case(self) -> None:
        self._skip_blank()
        self._read_word()                 # subject
        self._skip_blank()
        ws = self._read_word()            # `in`
        while True:
            self._skip_blank()
            while self.i < self.n and self.s[self.i] == "\n":
                self.i += 1
                self._read_heredoc_bodies()
                self._skip_blank()
            if self.i >= self.n:
                return
            if self.s[self.i] == "(":
                self.i += 1
            # patterns up to `)`
            peek = self.i
            ws = self._read_word()
            if ws and ws[0].bare and ws[0].text == "esac":
                return
            if not ws and self.i == peek:
                self.i += 1
                continue
            while self.i < self.n and self.s[self.i] not in ")":
                self._skip_blank()
                if self.i < self.n and self.s[self.i] == "|":
                    self.i += 1
                    continue
                if self.i < self.n and self.s[self.i] != ")":
                    before = self.i
                    self._read_word()
                    if self.i == before:
                        self.i += 1
            if self.i < self.n and self.s[self.i] == ")":
                self.i += 1
            stopper = self.parse_list({";;", ";&", ";;&"}, {"esac"})
            if stopper == "esac" or stopper is None:
                return


def parse(text: str, base_env: Optional[Mapping[str, str]] = None) -> Script:
    """Parse a Bash command string. Raises `ParseLimit` for input too large or nested to analyse."""
    if len(text) > MAX_INPUT:
        raise ParseLimit("command longer than %d bytes" % MAX_INPUT)
    shared = _Shared(base_env or {})
    p = _Parser(text, shared, {}, 0, "top")
    p.parse_list(set(), set())
    sc = Script()
    sc.cmds = shared.cmds
    sc.notes = shared.notes
    sc.ifs_changed = shared.ifs_changed
    # a here-string/heredoc is the stdin of its command
    for c in sc.cmds:
        for r in c.redirs:
            if r.op in ("<<", "<<-", "<<<") and r.body is not None and not (r.target is not None and r.target.dynamic and r.op == "<<<"):
                c.stdin_text = r.body
    return sc
