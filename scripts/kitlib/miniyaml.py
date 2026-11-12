"""Restricted YAML subset parser (IMP-11, report §10.3 / §19.7).

The kit reads `loop.config.md`, plan items and CI workflows without a third-party
YAML library. This parser accepts a deliberately small subset and rejects
everything else with an error that carries a line number.

Accepted:
  * block mappings (`key: value`, `key:` + indented block)
  * block sequences of scalars or of simple mappings (`- a`, `- k: v` + more keys)
  * a sequence may sit at the same indent as its parent key
  * scalars: null (`null`, `~`, empty), `true`/`false`, integers, floats,
    'single' and "double" quoted strings, and plain strings
  * flow lists of scalars `[a, b, "c"]`, plus the empty `[]` and `{}`
  * `#` comments (full-line, or after whitespace)
  * `|`, `|-` and `|+` literal blocks

Rejected (with the line number): tabs for indentation, anchors/aliases/tags
(`& * !`), folded blocks (`>`), nested or non-empty flow collections, multiple
documents (`---`), duplicate keys, a plain scalar that contains `: `, merge keys
and anything else outside the list above.

Deliberate differences from YAML 1.1: `on`, `off`, `yes` and `no` are plain
strings, not booleans (gate toggles are `on|report|off`). Only `true` and `false`
(any of `true True TRUE`) are booleans.

Python 3.9, stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["YamlError", "parse", "parse_with_lines", "parse_scalar", "dump_scalar"]


class YamlError(ValueError):
    """A parse error. `lineno` is 1-based and already includes any line offset."""

    def __init__(self, message: str, lineno: Optional[int] = None) -> None:
        self.message = message
        self.lineno = lineno
        text = "line %d: %s" % (lineno, message) if lineno else message
        ValueError.__init__(self, text)


_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$|^[-+]?[0-9]+[eE][-+]?[0-9]+$")
_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}
_NULL = {"null", "Null", "NULL", "~", ""}
_BAD_START = "&*!%@`"
_LITERAL_RE = re.compile(r"^\|([-+]?)$")


def _strip_comment(text: str, lineno: int) -> str:
    """Remove a trailing ` # comment`, respecting quoted scalars. Also rstrips."""
    in_q = None  # type: Optional[str]
    prev_sig = ""  # previous non-space character outside quotes
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_q:
            if in_q == '"' and c == "\\":
                i += 2
                continue
            if c == in_q:
                if in_q == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_q = None
                prev_sig = c
            i += 1
            continue
        if c in "'\"" and prev_sig in ("", ":", "-", "[", ","):
            in_q = c
            i += 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        if c not in " \t":
            prev_sig = c
        i += 1
    if in_q:
        raise YamlError("unterminated quoted string", lineno)
    return text.rstrip()


def _unquote_double(body: str, lineno: int) -> str:
    out = []  # type: List[str]
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 1
            if i >= n:
                raise YamlError("dangling backslash in double-quoted string", lineno)
            e = body[i]
            table = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/"}
            if e in table:
                out.append(table[e])
            elif e == "x" and re.match(r"^[0-9a-fA-F]{2}$", body[i + 1:i + 3]):
                out.append(chr(int(body[i + 1:i + 3], 16)))
                i += 2
            elif e == "u" and re.match(r"^[0-9a-fA-F]{4}$", body[i + 1:i + 5]):
                out.append(chr(int(body[i + 1:i + 5], 16)))
                i += 4
            else:
                raise YamlError("unsupported escape \\%s in double-quoted string" % e, lineno)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _parse_quoted(text: str, lineno: int) -> str:
    """`text` starts with a quote and must be exactly one quoted scalar."""
    q = text[0]
    i = 1
    n = len(text)
    while i < n:
        c = text[i]
        if q == '"' and c == "\\":
            i += 2
            continue
        if c == q:
            if q == "'" and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            break
        i += 1
    else:
        raise YamlError("unterminated quoted string", lineno)
    if text[i + 1:].strip():
        raise YamlError("unexpected text after closing quote", lineno)
    body = text[1:i]
    if q == "'":
        return body.replace("''", "'")
    return _unquote_double(body, lineno)


def _resolve_plain(text: str) -> Any:
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _split_flow(inner: str, lineno: int) -> List[str]:
    items = []  # type: List[str]
    cur = []  # type: List[str]
    in_q = None  # type: Optional[str]
    i = 0
    n = len(inner)
    while i < n:
        c = inner[i]
        if in_q:
            cur.append(c)
            if in_q == '"' and c == "\\" and i + 1 < n:
                cur.append(inner[i + 1])
                i += 2
                continue
            if c == in_q:
                in_q = None
            i += 1
            continue
        if c in "'\"" and not "".join(cur).strip():
            in_q = c
            cur.append(c)
        elif c == ",":
            items.append("".join(cur).strip())
            cur = []
        elif c in "[]{}":
            raise YamlError("nested flow collections are not supported", lineno)
        else:
            cur.append(c)
        i += 1
    if in_q:
        raise YamlError("unterminated quoted string in flow list", lineno)
    last = "".join(cur).strip()
    if last or items:
        items.append(last)
    # allow one trailing comma: "[a, b,]"
    if items and items[-1] == "" and len(items) > 1:
        items.pop()
    return items


def parse_scalar(text: str, lineno: int = 0) -> Any:
    """Parse one scalar or flow value (already comment-stripped)."""
    text = text.strip()
    if not text:
        return None
    c = text[0]
    if c in "'\"":
        return _parse_quoted(text, lineno)
    if c == "[":
        if not text.endswith("]"):
            raise YamlError("unterminated flow list (flow lists must fit on one line)", lineno)
        items = _split_flow(text[1:-1], lineno)
        out = []  # type: List[Any]
        for it in items:
            if it == "":
                raise YamlError("empty item in flow list", lineno)
            out.append(parse_scalar(it, lineno))
        return out
    if c == "{":
        if text == "{}":
            return {}
        raise YamlError("flow mappings are not supported (only an empty {})", lineno)
    if c in _BAD_START:
        raise YamlError("unsupported YAML feature starting with %r (anchors, aliases, tags, directives)" % c, lineno)
    if c == ">":
        raise YamlError("folded blocks (>) are not supported; use | or a quoted string", lineno)
    if c == "|":
        raise YamlError("literal block indicator in an unexpected place", lineno)
    if c == "-" and (len(text) == 1 or text[1] == " "):
        raise YamlError("a sequence entry is not allowed here", lineno)
    if c in "?" and (len(text) == 1 or text[1] == " "):
        raise YamlError("complex keys (?) are not supported", lineno)
    if ": " in text or text.endswith(":"):
        raise YamlError("plain value contains ': ' (quote the value, or fix the indentation)", lineno)
    return _resolve_plain(text)


def _split_key(content: str, lineno: int) -> Optional[Tuple[str, str]]:
    """Split `key: rest`. Returns None if the content is not a mapping entry."""
    if not content:
        return None
    if content[0] in "'\"":
        q = content[0]
        i = 1
        n = len(content)
        while i < n:
            c = content[i]
            if q == '"' and c == "\\":
                i += 2
                continue
            if c == q:
                if q == "'" and i + 1 < n and content[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            return None
        after = content[i + 1:]
        if after.startswith(":") and (len(after) == 1 or after[1] == " "):
            return _parse_quoted(content[:i + 1], lineno), after[1:].strip()
        return None
    if content[0] in "[{":
        return None
    idx = content.find(": ")
    if idx == -1:
        if content.endswith(":"):
            idx = len(content) - 1
        else:
            return None
    key = content[:idx].strip()
    if not key:
        raise YamlError("empty key", lineno)
    if key[0] in _BAD_START or key[0] in "|>?":
        raise YamlError("unsupported YAML feature in key %r" % key, lineno)
    return key, content[idx + 1:].strip()


class _Parser:
    def __init__(self, text: str, line_offset: int = 0) -> None:
        self.raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.off = line_offset
        self.i = 0
        self.override = {}  # type: Dict[int, Tuple[int, str]]
        self.keylines = {}  # type: Dict[Tuple[str, ...], int]
        self.cache = {}  # type: Dict[int, Optional[Tuple[int, str]]]

    # -- line access -------------------------------------------------------
    def ln(self, i: int) -> int:
        return i + 1 + self.off

    def info(self, i: int) -> Optional[Tuple[int, str]]:
        """(indent, comment-stripped content) or None for blank/comment lines."""
        if i in self.override:
            return self.override[i]
        if i in self.cache:
            return self.cache[i]
        raw = self.raw[i]
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if stripped.strip() == "":
            self.cache[i] = None
            return None
        if stripped.startswith("\t"):
            raise YamlError("tabs are not allowed for indentation", self.ln(i))
        if stripped.startswith("#"):
            self.cache[i] = None
            return None
        if indent == 0 and stripped.rstrip() in ("---", "..."):
            raise YamlError("multiple documents / document markers are not supported", self.ln(i))
        content = _strip_comment(stripped, self.ln(i))
        res = (indent, content)
        self.cache[i] = res
        return res

    def peek(self) -> Optional[Tuple[int, str]]:
        while self.i < len(self.raw):
            inf = self.info(self.i)
            if inf is not None:
                return inf
            self.i += 1
        return None

    # -- grammar -----------------------------------------------------------
    def parse_document(self) -> Any:
        first = self.peek()
        if first is None:
            return None
        value = self.parse_block(first[0], ())
        nxt = self.peek()
        if nxt is not None:
            raise YamlError("unexpected content (bad indentation?)", self.ln(self.i))
        return value

    def parse_block(self, indent: int, path: Tuple[str, ...]) -> Any:
        inf = self.peek()
        assert inf is not None
        _, content = inf
        if content == "-" or content.startswith("- "):
            return self.parse_sequence(indent, path)
        if _split_key(content, self.ln(self.i)) is not None:
            return self.parse_mapping(indent, path)
        # a lone scalar document / value
        self.i += 1
        return parse_scalar(content, self.ln(self.i - 1))

    def parse_mapping(self, indent: int, path: Tuple[str, ...]) -> Dict[str, Any]:
        out = {}  # type: Dict[str, Any]
        while True:
            inf = self.peek()
            if inf is None:
                break
            ind, content = inf
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("unexpected indentation", self.ln(self.i))
            if content == "-" or content.startswith("- "):
                break  # handled by caller (sequence at the parent's indent) or error
            lineno = self.ln(self.i)
            kv = _split_key(content, lineno)
            if kv is None:
                raise YamlError("expected 'key: value'", lineno)
            key, rest = kv
            if key in out:
                raise YamlError("duplicate key %r" % key, lineno)
            kpath = path + (key,)
            self.keylines[kpath] = lineno
            key_i = self.i
            self.i += 1
            out[key] = self.parse_value(rest, indent, kpath, key_i, in_mapping=True)
        return out

    def parse_value(self, rest: str, indent: int, path: Tuple[str, ...], line_i: int, in_mapping: bool) -> Any:
        lineno = self.ln(line_i)
        m = _LITERAL_RE.match(rest)
        if m:
            return self.parse_literal(m.group(1), indent, line_i)
        if rest.startswith("|"):
            raise YamlError("unsupported literal block header %r (only |, |- and |+)" % rest, lineno)
        if rest != "":
            return parse_scalar(rest, lineno)
        # nothing on the line: nested block, sequence at same indent, or null
        nxt = self.peek()
        if nxt is None:
            return None
        nind, ncontent = nxt
        if nind > indent:
            return self.parse_block(nind, path)
        if nind == indent and in_mapping and (ncontent == "-" or ncontent.startswith("- ")):
            return self.parse_sequence(indent, path)
        return None

    def parse_sequence(self, indent: int, path: Tuple[str, ...]) -> List[Any]:
        out = []  # type: List[Any]
        while True:
            inf = self.peek()
            if inf is None:
                break
            ind, content = inf
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("unexpected indentation", self.ln(self.i))
            if not (content == "-" or content.startswith("- ")):
                break
            lineno = self.ln(self.i)
            line_i = self.i
            rest = content[1:].strip()
            if rest == "":
                self.i += 1
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent:
                    out.append(self.parse_block(nxt[0], path))
                else:
                    out.append(None)
                continue
            if rest == "-" or rest.startswith("- "):
                raise YamlError("nested sequences (- - x) are not supported", lineno)
            lit = _LITERAL_RE.match(rest)
            if lit:
                self.i += 1
                out.append(self.parse_literal(lit.group(1), indent, line_i))
                continue
            kv = _split_key(rest, lineno)
            if kv is not None:
                # re-seat the line as a mapping entry at the column of the key
                col = indent + (len(content) - len(rest))
                self.override[line_i] = (col, rest)
                out.append(self.parse_mapping(col, path))
                continue
            self.i += 1
            out.append(parse_scalar(rest, lineno))
        return out

    def parse_literal(self, chomp: str, parent_indent: int, line_i: int) -> str:
        j = line_i + 1
        lines = []  # type: List[str]
        content_indent = None  # type: Optional[int]
        while j < len(self.raw):
            raw = self.raw[j]
            if raw.strip() == "":
                lines.append("")
                j += 1
                continue
            stripped = raw.lstrip(" ")
            ind = len(raw) - len(stripped)
            if raw[ind:ind + 1] == "\t":
                raise YamlError("tabs are not allowed for indentation", self.ln(j))
            if ind <= parent_indent:
                break
            if content_indent is None:
                content_indent = ind
            if ind < content_indent:
                raise YamlError("literal block line is indented less than its first line", self.ln(j))
            lines.append(raw[content_indent:].rstrip("\r"))
            j += 1
        # trailing blank lines belong to chomping, not content
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        self.i = j
        if not lines:
            return "" if chomp != "+" else "\n" * trailing
        text = "\n".join(lines)
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"


def parse(text: str, line_offset: int = 0) -> Any:
    """Parse `text`. `line_offset` is added to every reported line number."""
    return _Parser(text, line_offset).parse_document()


def parse_with_lines(text: str, line_offset: int = 0) -> Tuple[Any, Dict[Tuple[str, ...], int]]:
    """Like `parse`, plus a map of mapping-key path -> 1-based line number."""
    p = _Parser(text, line_offset)
    data = p.parse_document()
    return data, p.keylines


_SAFE_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./<>$=+@~()-][^#]*$")


def dump_scalar(value: Any) -> str:
    """Render a scalar or list of scalars so that `parse_scalar` returns it unchanged."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(dump_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        if value:
            raise ValueError("dump_scalar cannot render a non-empty mapping")
        return "{}"
    s = str(value)
    if "\n" in s:
        raise ValueError("dump_scalar cannot render a multi-line string")
    if s and s == s.strip() and _SAFE_PLAIN_RE.match(s) and " #" not in s and ": " not in s and not s.endswith(":"):
        try:
            if parse_scalar(s) == s:
                return s
        except YamlError:
            pass
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'
