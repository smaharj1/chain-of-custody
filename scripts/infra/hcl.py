"""A careful HCL2 scanner for the source policy (report §8.1 Layer 5, RC-3; IMP-25).

It is not an evaluator. It reads Terraform and OpenTofu source far enough to answer "which
blocks, labels and literal attributes does this file contain?" without running terraform, so the
check can happen BEFORE `init` and `plan` execute anything.

What it handles: `#`, `//` and `/* */` comments; quoted strings with escapes (`\\"`, `\\uXXXX`) and
`${}` / `%{}` interpolation, including nested strings and braces inside an interpolation;
heredocs (`<<EOT` and `<<-EOT`); block labels as strings or bare identifiers; one-line blocks;
nested blocks; CR, CRLF and the other Unicode line breaks. The JSON syntax (`*.tf.json`) is
converted to the same tree.

Fail-closed rules: anything this reader is unsure about raises `HclError`, and the caller reports
"unparsed: manual review". Specifically it refuses: an unterminated string, comment or heredoc;
a comment or heredoc opener inside an interpolation; an unexpected character; a block label with
interpolation; two statements on one line; invalid UTF-8; duplicate keys in JSON syntax; nesting
deeper than `MAX_DEPTH`. Where the scanner and Terraform could disagree, the scanner takes the
reading in which MORE text is code: a bare CR ends a comment; a heredoc ends at any line whose
trimmed text is the marker (Terraform may require column 0, which would end it no earlier).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["HclError", "Tok", "Attr", "Block", "tokenize", "parse_native", "parse_json", "parse_file_text",
           "literal_of", "object_of", "walk_blocks", "names_in", "MAX_DEPTH", "MAX_BYTES"]

MAX_DEPTH = 48
MAX_BYTES = 2 * 1024 * 1024
NL_CHARS = "\n\r\x0b\x0c\x85  "
_ESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
_NUM_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_HEREDOC_RE = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_-]*)[ \t]*(?:\r\n|[\n\r\x0b\x0c\x85  ])")
_MULTI_PUNCT = ("...", "==", "!=", "<=", ">=", "&&", "||", "=>")
_SINGLE_PUNCT = "{}[]()=,.:?+-*/%!<>"


class HclError(ValueError):
    def __init__(self, message: str, line: Optional[int] = None) -> None:
        self.message = message
        self.line = line
        ValueError.__init__(self, ("line %d: %s" % (line, message)) if line else message)


class Tok(object):
    """kind: ID, STR, HERE, NUM, P (punctuation), NL, EOF. `val` is the identifier, the decoded
    literal of a string or heredoc (None when it has interpolation), or the punctuation."""
    __slots__ = ("kind", "val", "line", "text")

    def __init__(self, kind: str, val: Optional[str], line: int, text: str = "") -> None:
        self.kind, self.val, self.line, self.text = kind, val, line, text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Tok(%s,%r,%d)" % (self.kind, self.val, self.line)


class Attr(object):
    """`name = expression`. `toks` is the expression's tokens (native syntax); `value` is the
    Python value (JSON syntax). Use `literal_of` and `object_of` to read either."""
    __slots__ = ("name", "toks", "value", "line", "from_json")

    def __init__(self, name: str, toks: Optional[List[Tok]], line: int, value: Any = None, from_json: bool = False) -> None:
        self.name, self.toks, self.line, self.value, self.from_json = name, toks, line, value, from_json


class Block(object):
    __slots__ = ("type", "labels", "body", "line")

    def __init__(self, type_: str, labels: List[str], body: List[Any], line: int) -> None:
        self.type, self.labels, self.body, self.line = type_, labels, body, line

    def attrs(self, name: str) -> List[Attr]:
        return [i for i in self.body if isinstance(i, Attr) and i.name == name]

    def blocks(self, type_: Optional[str] = None) -> List["Block"]:
        return [i for i in self.body if isinstance(i, Block) and (type_ is None or i.type == type_)]


# --- tokenizer -------------------------------------------------------------------------------------------

def _count_breaks(text: str) -> int:
    n = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in NL_CHARS:
            if c == "\r" and text[i + 1:i + 2] == "\n":
                i += 1
            n += 1
        i += 1
    return n


def _scan_string(src: str, i: int, depth: int) -> Tuple[Optional[str], int]:
    """src[i] is the opening quote. Returns (literal or None if it has interpolation, index after the closing quote)."""
    if depth > MAX_DEPTH:
        raise HclError("strings nested too deeply in interpolation")
    j = i + 1
    n = len(src)
    out = []  # type: List[str]
    interp = False
    while True:
        if j >= n:
            raise HclError("unterminated string")
        c = src[j]
        if c == '"':
            j += 1
            break
        if c in NL_CHARS:
            raise HclError("unterminated string (line break inside quotes)")
        if c == "\\":
            d = src[j + 1:j + 2]
            if d in _ESC:
                out.append(_ESC[d])
                j += 2
            elif d in ("u", "U"):
                width = 4 if d == "u" else 8
                hexs = src[j + 2:j + 2 + width]
                if len(hexs) != width or not re.match(r"^[0-9A-Fa-f]+$", hexs):
                    raise HclError("invalid unicode escape in string")
                try:
                    out.append(chr(int(hexs, 16)))
                except (ValueError, OverflowError):
                    raise HclError("invalid unicode escape in string")
                j += 2 + width
            else:
                raise HclError("invalid escape in string")
            continue
        if c == "$" and src.startswith("$${", j):
            out.append("${")
            j += 3
            continue
        if c == "%" and src.startswith("%%{", j):
            out.append("%{")
            j += 3
            continue
        if (c == "$" or c == "%") and src.startswith("{", j + 1):
            interp = True
            j = _skip_interp(src, j + 2, depth + 1)
            continue
        out.append(c)
        j += 1
    value = "".join(out)
    if interp or "${" in value or "%{" in value:
        return None, j
    return value, j


def _skip_interp(src: str, j: int, depth: int) -> int:
    """j is just after `${` or `%{`. Returns the index after the matching `}`."""
    n = len(src)
    level = 1
    while j < n:
        c = src[j]
        if c == '"':
            _, j = _scan_string(src, j, depth)
            continue
        if c == "{":
            level += 1
        elif c == "}":
            level -= 1
            if level == 0:
                return j + 1
        elif c == "#" or src.startswith("//", j) or src.startswith("/*", j):
            raise HclError("comment inside an interpolation: manual review")
        elif src.startswith("<<", j):
            raise HclError("heredoc inside an interpolation: manual review")
        j += 1
    raise HclError("unterminated interpolation")


def tokenize(src: str) -> List[Tok]:
    if src.startswith("﻿"):
        src = src[1:]
    toks = []  # type: List[Tok]
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c in " \t":
            i += 1
            continue
        if c in NL_CHARS:
            if c == "\r" and src[i + 1:i + 2] == "\n":
                i += 1
            i += 1
            toks.append(Tok("NL", None, line))
            line += 1
            continue
        if c == "#" or src.startswith("//", i):
            while i < n and src[i] not in NL_CHARS:
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise HclError("unterminated block comment", line)
            breaks = _count_breaks(src[i:j + 2])
            if breaks:
                toks.append(Tok("NL", None, line))
            line += breaks
            i = j + 2
            continue
        if c == '"':
            try:
                val, j = _scan_string(src, i, 0)
            except HclError as exc:
                raise HclError(exc.message, line)
            raw = src[i:j]
            toks.append(Tok("STR", val, line, raw))
            line += _count_breaks(raw)
            i = j
            continue
        if src.startswith("<<", i):
            m = _HEREDOC_RE.match(src, i)
            if not m:
                raise HclError("malformed heredoc opener", line)
            delim = m.group(2)
            pos = m.end()
            start_line = line
            line += 1
            body_lines = []  # type: List[str]
            while True:
                if pos >= n:
                    raise HclError("unterminated heredoc <<%s" % delim, start_line)
                k = pos
                while k < n and src[k] not in NL_CHARS:
                    k += 1
                text = src[pos:k]
                nxt = k
                if nxt < n:
                    if src[nxt] == "\r" and src[nxt + 1:nxt + 2] == "\n":
                        nxt += 1
                    nxt += 1
                if text.strip() == delim:
                    pos = nxt
                    ended_with_break = nxt > k
                    break
                body_lines.append(text)
                line += 1
                pos = nxt
            body = "\n".join(body_lines)
            val = None if ("${" in body or "%{" in body) else body
            toks.append(Tok("HERE", val, start_line, src[i:pos]))
            if ended_with_break:
                toks.append(Tok("NL", None, line))
                line += 1
            i = pos
            continue
        if c.isdigit():
            m = _NUM_RE.match(src, i)
            toks.append(Tok("NUM", m.group(0), line, m.group(0)))
            i = m.end()
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_-"):
                j += 1
            toks.append(Tok("ID", src[i:j], line, src[i:j]))
            i = j
            continue
        for p in _MULTI_PUNCT:
            if src.startswith(p, i):
                toks.append(Tok("P", p, line, p))
                i += len(p)
                break
        else:
            if c in _SINGLE_PUNCT:
                toks.append(Tok("P", c, line, c))
                i += 1
            else:
                raise HclError("unexpected character %r" % c, line)
    toks.append(Tok("EOF", None, line))
    return toks


# --- parser (native syntax) ------------------------------------------------------------------------------

class _P(object):
    def __init__(self, toks: List[Tok]) -> None:
        self.toks = toks
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def next(self) -> Tok:
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t


def _is_p(t: Tok, val: str) -> bool:
    return t.kind == "P" and t.val == val


def _parse_body(p: _P, closing: bool, depth: int) -> List[Any]:
    if depth > MAX_DEPTH:
        raise HclError("blocks nested deeper than %d" % MAX_DEPTH, p.peek().line)
    items = []  # type: List[Any]
    while True:
        t = p.peek()
        if t.kind == "NL":
            p.next()
            continue
        if t.kind == "EOF":
            if closing:
                raise HclError("end of file inside a block: missing '}'", t.line)
            return items
        if _is_p(t, "}"):
            if closing:
                p.next()
                return items
            raise HclError("unexpected '}'", t.line)
        if t.kind != "ID":
            raise HclError("expected an attribute or block name, found %r" % (t.text or t.val), t.line)
        p.next()
        t2 = p.peek()
        if _is_p(t2, "="):
            p.next()
            items.append(Attr(t.val or "", _collect_expr(p, closing), t.line))
            continue
        labels = []  # type: List[str]
        while True:
            t2 = p.peek()
            if t2.kind == "STR":
                if t2.val is None:
                    raise HclError("block label with interpolation", t2.line)
                labels.append(t2.val)
                p.next()
            elif t2.kind == "ID":
                labels.append(t2.val or "")
                p.next()
            else:
                break
        if not _is_p(t2, "{"):
            raise HclError("expected '{' or '=' after %r" % t.val, t2.line)
        p.next()
        body = _parse_body(p, True, depth + 1)
        items.append(Block(t.val or "", labels, body, t.line))
        t3 = p.peek()
        if not (t3.kind in ("NL", "EOF") or (closing and _is_p(t3, "}"))):
            raise HclError("expected a line break after the closing '}' of block %r" % t.val, t3.line)


def _collect_expr(p: _P, closing: bool) -> List[Tok]:
    toks = []  # type: List[Tok]
    depth = 0
    while True:
        t = p.peek()
        if t.kind == "EOF":
            if depth:
                raise HclError("end of file inside an expression", t.line)
            return toks
        if t.kind == "NL":
            if depth == 0:
                return toks
            toks.append(t)          # kept inside brackets: object literals use line breaks as separators
            p.next()
            continue
        if t.kind == "P":
            if t.val in ("(", "[", "{"):
                depth += 1
            elif t.val in (")", "]", "}"):
                if depth == 0:
                    if t.val == "}" and closing:
                        return toks
                    raise HclError("unbalanced %r in an expression" % t.val, t.line)
                depth -= 1
        toks.append(t)
        p.next()


def parse_native(src: str) -> List[Any]:
    p = _P(tokenize(src))
    return _parse_body(p, False, 0)


# --- JSON syntax -----------------------------------------------------------------------------------------

# Block types that carry labels in the JSON syntax, and how many: top-level ones, and nested ones.
_JSON_TOP_LABELS = {"resource": 2, "data": 2, "module": 1, "provider": 1, "variable": 1, "output": 1}
_JSON_NESTED_LABELS = {"backend": 1, "provisioner": 1, "dynamic": 1}
_JSON_LITERAL_BAD = ("${", "%{")


def _no_dups(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    d = {}  # type: Dict[str, Any]
    for k, v in pairs:
        if k in d:
            raise HclError("duplicate key %r in JSON syntax" % k)
        d[k] = v
    return d


def _json_body(obj: Dict[str, Any], depth: int) -> List[Any]:
    if depth > MAX_DEPTH:
        raise HclError("JSON nested deeper than %d" % MAX_DEPTH)
    items = []  # type: List[Any]
    labels = _JSON_TOP_LABELS if depth == 0 else _JSON_NESTED_LABELS
    for key, val in obj.items():
        if key == "//":
            continue
        dicts = _as_dicts(val)
        if dicts is None and isinstance(val, list) and any(isinstance(x, (dict, list)) for x in val):
            raise HclError("JSON syntax: %r is a list that mixes objects with other values: manual review" % key)
        if dicts is not None:
            for d in dicts:
                items.extend(_json_labelled(key, d, labels.get(key, 0), depth))
        # also keep it as an attribute, so literal reads (`source`, `version`, `required_providers` entries) work
        items.append(Attr(key, None, 0, val, True))
    return items


def _as_dicts(val: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
        return list(val)
    return None


def _json_labelled(key: str, d: Dict[str, Any], n_labels: int, depth: int) -> List[Any]:
    """Expand `{"resource": {"aws_x": {"name": {...}}}}` into Block('resource', ['aws_x','name'], ...)."""
    if n_labels == 0:
        return [Block(key, [], _json_body(d, depth + 1), 0)]
    out = []  # type: List[Any]

    def descend(cur: Dict[str, Any], labels: List[str]) -> None:
        if len(labels) == n_labels:
            out.append(Block(key, list(labels), _json_body(cur, depth + 1), 0))
            return
        for k, v in cur.items():
            dicts = _as_dicts(v)
            if dicts is None:
                if v is None or v == {} or v == []:
                    dicts = [{}]
                else:
                    raise HclError("JSON block %r has an unexpected shape" % key)
            for sub in dicts:
                descend(sub, labels + [k])
    descend(d, [])
    return out


def parse_json(text: str) -> List[Any]:
    try:
        data = json.loads(text, object_pairs_hook=_no_dups)
    except HclError:
        raise
    except (ValueError, RecursionError) as exc:
        raise HclError("invalid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise HclError("JSON syntax: the top level is not an object")
    return _json_body(data, 0)


def parse_file_text(text: str, path: str) -> List[Any]:
    low = path.lower()
    if low.endswith(".json"):
        return parse_json(text)
    return parse_native(text)


# --- reading values --------------------------------------------------------------------------------------

def literal_of(attr: Attr) -> Optional[str]:
    """The attribute's value if it is exactly one string literal, else None (an expression)."""
    if attr.from_json:
        v = attr.value
        if isinstance(v, str) and not any(b in v for b in _JSON_LITERAL_BAD):
            return v
        return None
    toks = attr.toks or []
    if len(toks) == 1 and toks[0].kind in ("STR", "HERE"):
        return toks[0].val
    return None


def object_of(attr: Attr) -> Optional[Dict[str, Any]]:
    """An object-constructor attribute (`{ source = "x", version = "1" }`) as a dict of key ->
    literal string, None (an expression) or nested dict. None if the value is not an object."""
    if attr.from_json:
        if isinstance(attr.value, dict):
            return _obj_from_json(attr.value)
        return None
    toks = attr.toks or []
    if len(toks) < 2 or not _is_p(toks[0], "{") or not _is_p(toks[-1], "}"):
        return None
    try:
        obj, end = _obj_from_toks(toks, 0)
    except HclError:
        return None
    return obj if end == len(toks) else None


def _obj_from_json(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _obj_from_json(v)
        elif isinstance(v, str) and not any(b in v for b in _JSON_LITERAL_BAD):
            out[k] = v
        else:
            out[k] = None
    return out


def _obj_from_toks(toks: List[Tok], i: int) -> Tuple[Dict[str, Any], int]:
    """toks[i] is '{'. Returns (dict, index after the matching '}')."""
    out = {}  # type: Dict[str, Any]
    i += 1
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "NL" or _is_p(t, ","):
            i += 1
            continue
        if _is_p(t, "}"):
            return out, i + 1
        if t.kind in ("ID", "STR") and i + 1 < n and (_is_p(toks[i + 1], "=") or _is_p(toks[i + 1], ":")):
            key = t.val if t.val is not None else None
            if key is None:
                raise HclError("object key with interpolation", t.line)
            if key in out:
                raise HclError("duplicate object key %r" % key, t.line)
            i += 2
            if i < n and _is_p(toks[i], "{"):
                sub, i = _obj_from_toks(toks, i)
                out[key] = sub
                continue
            # a scalar or an expression: take tokens up to the next top-level comma / newline / closing brace
            depth = 0
            start = i
            while i < n:
                u = toks[i]
                if u.kind == "P" and u.val in ("(", "[", "{"):
                    depth += 1
                elif u.kind == "P" and u.val in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and (u.kind == "NL" or _is_p(u, ",")):
                    break
                i += 1
            span = toks[start:i]
            out[key] = span[0].val if len(span) == 1 and span[0].kind in ("STR", "HERE") else None
            continue
        raise HclError("unexpected token in an object", t.line)
    raise HclError("unterminated object")


def walk_blocks(items: Sequence[Any], depth: int = 0):
    """Yield every Block in the tree, depth first."""
    for it in items:
        if isinstance(it, Block):
            yield it
            for sub in walk_blocks(it.body, depth + 1):
                yield sub


def names_in(block: Block) -> List[Tuple[str, int]]:
    """Every attribute name, nested block type and object-constructor key inside a block, with line numbers."""
    out = []  # type: List[Tuple[str, int]]
    for it in block.body:
        if isinstance(it, Block):
            out.append((it.type, it.line))
            out.extend(names_in(it))
        else:
            out.append((it.name, it.line))
            if it.from_json:
                out.extend(_json_keys(it.value, it.line))
            else:
                toks = it.toks or []
                for k, t in enumerate(toks[:-1]):
                    if t.kind in ("ID", "STR") and t.val is not None and (_is_p(toks[k + 1], "=") or _is_p(toks[k + 1], ":")):
                        out.append((t.val, t.line))
    return out


def _json_keys(v: Any, line: int, depth: int = 0) -> List[Tuple[str, int]]:
    out = []  # type: List[Tuple[str, int]]
    if depth > MAX_DEPTH:
        return out
    if isinstance(v, dict):
        for k, x in v.items():
            out.append((k, line))
            out.extend(_json_keys(x, line, depth + 1))
    elif isinstance(v, list):
        for x in v:
            out.extend(_json_keys(x, line, depth + 1))
    return out
