"""G9: supply chain (report section 5.5, section 9 G9, CO-13, CO-28).

What it does without the network: reads the dependency manifests at the base and at HEAD (`package.json`, `requirements*.txt`,
`pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`), and for every NEW top-level dependency checks

* the plan declared it (`touches.deps_change: true`; a dependency the plan did not announce is the classic way an agent adds a package);
* it comes from a registry by name, not from a git URL, a tarball URL or a local path (which skip every other check) -- and the same holds
  when an EXISTING dependency's spec changes to such a source (a name that was fine yesterday is not fine as a tarball today);
* the diff adds no package-index override: `--index-url`, `--extra-index-url`, `--find-links`, `--trusted-host`, `--pre` in a requirements
  file, a `registry=` or `@scope:registry=` line in `.npmrc` and its cousins, a `[tool.uv]`, `[tool.poetry.source]` or `[tool.pdm.source]` change
  (`g9.index-changed`: a dependency name only means something relative to where it is resolved);
* its name is not within edit distance one or two of a very popular package without being one (slopsquat and typosquat), unless the plan
  allows it (`ext.g9.allow: ["name"]`);
* the lockfile moved with the manifest (frozen lockfile), and an exact pin for `requirements.txt`;
* `package.json` gained no lifecycle script (`preinstall`, `install`, `postinstall`, `prepare`), `.npmrc` does not re-enable install scripts,
  and no changed workflow or script installs without `--ignore-scripts` or a frozen lockfile.

What needs the network is delegated: `dep_check_cmd` (yours; the kit never reaches a registry) is run once per new dependency and prints
`{"exists": bool, "age_days": N, "downloads": N, "license": "MIT"}`. A package that does not exist is a failure (a hallucinated name); a package
younger than 30 days or with few downloads is a warning, and an error when it is also a look-alike; a licence outside `license_allowlist`
is a failure. Without that command the new dependencies are listed as UNCHECKED for the human, never treated as safe. At L3+ every new
dependency needs a human anyway (`touches.deps_change` already makes the item human-gated at merge).

Owner metric: new dependencies per item; blocked dependencies.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import common
from . import GateResult
from . import _lib

NAME = "g9"
OWNER_METRIC = "new dependencies per item; dependencies blocked"
REQUIRED_FOR = (("verifying", "reviewing"),)

MAX_CHECKED = 20
_NAME_OK = re.compile(r"^[@A-Za-z0-9][A-Za-z0-9._/@+~-]{0,213}\Z")        # what may reach dep_check_cmd's environment
_LIFECYCLE = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack")
_LOCKS = {"npm": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
          "pypi": ("poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock"), "go": ("go.sum",), "cargo": ("Cargo.lock",), "ruby": ("Gemfile.lock",)}
_NON_REGISTRY = re.compile(r"^(?:git\+|git://|git@|github:|gitlab:|bitbucket:|gist:|https?://|file:|link:|portal:|exec:|npm:|patch:|workspace:|\.{1,2}/|~/|/|ssh:|@\s*\S|"
                           r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#\S*)?$)", re.I)       # the last two: a PEP 508 direct reference (`pkg @ https://…`) and npm's `owner/repo` GitHub shorthand
_STRUCT_SOURCE = re.compile(r"\b(?:git|path|url|source|registry|github|branch|rev)\s*[=:]", re.I)      # poetry/cargo inline tables, Gemfile options
_INDEX_OPT = re.compile(r"(?:^|\s)(--index-url|-i|--extra-index-url|--find-links|-f|--trusted-host|--pre|--no-index)(?:[=\s]\s*(\S*))?", re.I)
_REGISTRY_FILES = (".npmrc", ".yarnrc", ".yarnrc.yml", "pip.conf", "pip.ini", "uv.toml", "config.toml", "config", ".pypirc")
# no `\s*` in front: `[^#;\n]*` already takes the blanks, and the two together split a line of N blanks N ways (quadratic)
_REGISTRY_LINE = re.compile(r"(?i)^[^#;\n]*(?:registry|index-url|index_url|extra-index-url|find-links|trusted-host|replace-with|npmRegistryServer|repository|"
                            r"yarnPath|yarn-path|script-shell|node-options|enableScripts|enableStrictSsl|strict-ssl|cafile|unsafeHttpWhitelist|httpProxy|httpsProxy|caFilePath)\b[^\n]*$")        # the last row: what runs on install, and what the install trusts
# keys that must START the line (`ca`, `key` and `cert` would match half the words of a registry file anywhere in it): the proxy and certificate settings of .npmrc, pip.conf
# and the like decide who sees the traffic and what it trusts
_RC_KEY_LINE = re.compile(r"(?i)^[ \t]*(?:proxy|https-proxy|http-proxy|noproxy|ca(?:\[\])?|cafile|cert|certfile|key|keyfile|client-cert|prefix|userconfig|globalconfig|onload-script)"
                          r"[ \t]*(?:[=:]|[ \t]\S)")

_POPULAR = {
    "npm": """react react-dom vue angular lodash express axios moment chalk commander debug request async underscore bluebird webpack babel-core typescript
        jquery uuid dotenv eslint prettier jest mocha chai sinon rxjs redux next nuxt svelte tailwindcss postcss sass socket.io mongoose mysql pg
        sequelize passport cors body-parser cookie-parser helmet morgan nodemon yargs glob rimraf minimist semver fs-extra bcrypt jsonwebtoken
        winston puppeteer cheerio ws zod yup joi handlebars ejs pug classnames prop-types react-router vite esbuild rollup gulp grunt""".split(),
    "pypi": """requests numpy pandas flask django scipy matplotlib pytest setuptools pip wheel six urllib3 certifi idna chardet boto3 botocore
        sqlalchemy pyyaml jinja2 click pillow cryptography beautifulsoup4 lxml scikit-learn tensorflow torch fastapi uvicorn pydantic aiohttp
        httpx celery redis psycopg2 pymysql gunicorn attrs packaging typing-extensions python-dateutil pytz tqdm rich black flake8 mypy
        coverage tox virtualenv paramiko docker werkzeug markupsafe itsdangerous openpyxl selenium""".split(),
    "cargo": """serde serde_json tokio rand clap regex log reqwest anyhow thiserror syn quote hyper futures chrono itertools lazy_static once_cell bytes""".split(),
    "go": [], "ruby": """rails rake rspec bundler puma nokogiri devise sidekiq pg redis rubocop""".split()}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def _distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def lookalike(name: str, eco: str) -> Optional[str]:
    """The popular package `name` imitates, or None. An exact (normalised) match is the package itself."""
    n = _norm(name.split("/")[-1])
    pop = [_norm(p) for p in _POPULAR.get(eco, [])]
    if n in pop:
        return None
    for p in pop:
        if len(n) >= 4 and (_distance(n, p) == 1 or (len(n) >= 8 and _distance(n, p) == 2)):
            return p
    return None


# -- manifests -----------------------------------------------------------------------------------------------------------------------

def manifest_kind(path: str) -> Optional[str]:
    base = os.path.basename(path)
    if base == "package.json":
        return "npm"
    if re.match(r"^[\w.-]*(?:requirements|constraints)[\w.-]*\.(?:txt|in)$", base) or base == "pyproject.toml":        # also `dev-requirements.txt`, `requirements-ci.txt`
        return "pypi"
    # `requirements/base.txt`, `deps/requirements/dev/x.txt`: every .txt (or pip-tools .in) inside a requirements or constraints directory is a requirements file
    if base.endswith((".txt", ".in")) and any(re.match(r"^(?:requirements|constraints)[\w.-]*$", part) for part in path.split("/")[:-1]):
        return "pypi"
    return {"go.mod": "go", "Cargo.toml": "cargo", "Gemfile": "ruby"}.get(base)


_NOT_BRACKET_OR_QUOTE = re.compile(r"""[^\]"']*""")


def _find_close(s: str, i: int) -> int:
    """The index of the first `]` at or after s[i] that is not inside a quoted part of a table name, -1 when there is none (or a quote is never closed)."""
    n = len(s)
    while True:
        i = _NOT_BRACKET_OR_QUOTE.match(s, i).end()
        if i >= n:
            return -1
        if s[i] == "]":
            return i
        q = (_TOML_BASIC if s[i] == '"' else _TOML_LITERAL).match(s, i)
        if q is None:
            return -1
        i = q.end()


def _toml_header(ln: str) -> Optional[str]:
    """The table name of a TOML header line (`[a.b]`, `[[a.b]]`, indented, a trailing comment allowed), None when the line is not one. String methods, no regex
    over the name: a name of any length costs one pass, and a header longer than a bounded pattern allows is still a header."""
    s = ln.strip()
    if not s.startswith("["):
        return None
    start = 2 if s.startswith("[[") else 1
    close = _find_close(s, start)
    if close < 0:
        return None
    name = s[start:close].strip()
    rest = s[close + 1:]
    if rest.startswith("]"):
        rest = rest[1:]
    rest = rest.strip()
    if not name or "[" in name or (rest and not rest.startswith("#")):
        return None
    return name


# What a TOML line can hold that changes the reader's state: a quote, a comment, a bracket or a brace. Everything between two of them is skipped in C.
_TOML_SPECIAL = re.compile(r"[\"'#\[\]{}]")
_TOML_BASIC = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')          # a one-line basic string: escapes honoured, no ambiguity, so linear
_TOML_LITERAL = re.compile(r"'[^']*'")
_TOML_ML_BASIC = re.compile(r'\\|"""')
_TOML_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_advance(line: str, ml: Optional[str], depth: int) -> Optional[Tuple[Optional[str], int]]:
    """Advance the multi-line string and the bracket/brace depth over one physical line: (ml, depth) after it, None when the line cannot be read (an
    unterminated one-line string). A basic multi-line string honours backslash escapes (`\\\"\"\"` does not end it); a closing delimiter may be followed by
    up to two more quotes that belong to the string. The same rules as G5's reader."""
    i, n = 0, len(line)
    while i < n:
        if ml:
            k = -1
            if ml == "'''":
                k = line.find(ml, i)
            else:
                j = i
                while j <= n:
                    m = _TOML_ML_BASIC.search(line, j)
                    if m is None:
                        break
                    if m.group(0) == "\\":
                        j = m.end() + 1
                        continue
                    k = m.start()
                    break
            if k < 0:
                return ml, depth
            i, ml = k + 3, None
            while i < n and line[i] == line[k] and i < k + 5:
                i += 1
            continue
        m = _TOML_SPECIAL.search(line, i)
        if m is None:
            break
        i = m.start()
        ch = line[i]
        if ch == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml, i = line[i:i + 3], i + 3
        elif ch in "\"'":
            s = (_TOML_BASIC if ch == '"' else _TOML_LITERAL).match(line, i)
            if s is None:
                return None
            i = s.end()
        else:
            depth += 1 if ch in "[{" else -1
            i += 1
    return ml, depth


def _toml_tables(text: str, unparsed: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """[(table name, body)] for a TOML file, good enough for dependency lists (no inline-table or array-of-tables semantics). A header is only a header at
    bracket depth 0 and outside a multi-line string: a `["a"]` line inside an array or a `[x]` line inside `\"\"\"…\"\"\"` belongs to the table above it. What
    the reader cannot place goes to `unparsed` when given, and the caller must not trust the tables it read: a line that starts with `[` at depth 0 and is
    not a header it understands, a line it cannot read, and a string or array still open at the end of the file (`<…>` entries)."""
    out = []  # type: List[Tuple[str, str]]
    name, buf = "", []  # type: str, List[str]
    ml, depth = None, 0  # type: Optional[str], int
    for ln in text.splitlines():
        if ml is None and depth == 0:
            header = _toml_header(ln)
            if header is not None:
                out.append((name, "\n".join(buf)))
                name, buf = header, []
                if unparsed is not None and _split_key(header) is None:
                    unparsed.append("<a table name this reader cannot decode: %s>" % header[:100])
                continue
            if unparsed is not None and ln.lstrip().startswith("["):
                unparsed.append(ln.strip()[:120])
        buf.append(ln)
        st = _toml_advance(ln, ml, depth)
        if st is None or st[1] < 0:
            if unparsed is not None:
                unparsed.append("<a line this reader cannot parse: %s>" % ln.strip()[:100])
            ml, depth = None, 0
        else:
            ml, depth = st
    if unparsed is not None and (ml is not None or depth != 0):
        unparsed.append("<a string or array that is never closed>")
    out.append((name, "\n".join(buf)))
    return out


_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\", "e": "\x1b"}
_HEX_WIDTH = {"u": 4, "U": 8, "x": 2}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _unescape_basic(raw: str) -> Optional[str]:
    """The text inside a TOML basic string: `\\u0065` is `e`. None for an escape TOML does not define (the caller then fails closed)."""
    if "\\" not in raw:
        return raw
    out = []  # type: List[str]
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        e = raw[i]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 1
        elif e in _HEX_WIDTH:
            w = _HEX_WIDTH[e]
            digits = raw[i + 1:i + 1 + w]
            if len(digits) != w or not all(d in _HEX_DIGITS for d in digits):
                return None
            try:
                out.append(chr(int(digits, 16)))
            except (ValueError, OverflowError):
                return None
            i += 1 + w
        else:
            return None
    return "".join(out)


def _scan_key(s: str, i: int = 0) -> Optional[Tuple[List[str], int]]:
    """The dotted key that starts at s[i]: ([parts, quotes removed and escapes decoded], the index after it). Bare, quoted and dotted keys, blanks around the dots.
    None when it is not a key, or has an escape this reader cannot decode. The ONE reader of key and table names (`evil.git`, `"evil"`, `["dep\\u0065ndencies"]`)."""
    parts = []  # type: List[str]
    n = len(s)
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            return None
        if s[i] in "\"'":
            q = (_TOML_BASIC if s[i] == '"' else _TOML_LITERAL).match(s, i)
            if q is None:
                return None
            part = _unescape_basic(s[i + 1:q.end() - 1]) if s[i] == '"' else s[i + 1:q.end() - 1]
            if part is None:
                return None
            parts.append(part)
            i = q.end()
        else:
            q = _TOML_BARE.match(s, i)
            if q is None:
                return None
            parts.append(q.group(0))
            i = q.end()
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ".":
            i += 1
            continue
        return parts, i


def _split_key(raw: str) -> Optional[List[str]]:
    """The parts of a whole dotted name (the inside of a table header), None when it is not exactly one key."""
    got = _scan_key(raw)
    return got[0] if got is not None and got[1] == len(raw) else None


def _toml_keyline(s: str) -> Optional[Tuple[List[str], str]]:
    """([key parts], value text) of a `key = value` line: bare, quoted and dotted keys (`evil.git = …`, `"evil" = …`, `a."b c".d = …`). None when it is not one."""
    got = _scan_key(s)
    if got is None:
        return None
    parts, i = got
    if i < len(s) and s[i] == "=":
        return parts, s[i + 1:].strip()
    return None


def _toml_entries(body: str, bad: Optional[List[str]] = None) -> List[Tuple[List[str], str]]:
    """[(key parts, value)] of the `key = value` statements of a table body. A value that goes on over several lines (an array, an inline table, a
    multi-line string) is read whole, its lines joined with newlines; a line inside one is never read as a key of its own. A statement that is not
    `key = value` (a stray `]`, an unquoted non-ASCII key) is appended to `bad` when given: the caller must not trust that it saw every key."""
    out = []  # type: List[Tuple[List[str], List[str]]]
    cur = None  # type: Optional[List[str]]
    ml, depth = None, 0  # type: Optional[str], int
    for ln in body.splitlines():
        s = ln.strip()
        if ml is None and depth == 0:
            cur = None
            if not s or s.startswith("#"):
                continue
            kv = _toml_keyline(s)
            if kv is not None:
                cur = [kv[1]]
                out.append((kv[0], cur))
            elif bad is not None:
                bad.append(s[:80])
        elif cur is not None and not s.startswith("#"):
            cur.append(s)
        st = _toml_advance(ln, ml, depth)
        if st is None or st[1] < 0:
            ml, depth, cur = None, 0, None
        else:
            ml, depth = st
    return [(parts, "\n".join(vals)) for parts, vals in out]


def _dep_entries(body: str) -> Dict[str, str]:
    """{name: spec} for the entries of a poetry or Cargo dependency table. The name is the FIRST part of the key (`evil.git = "…"` and `"evil" = {…}` are
    dependency `evil`); a dotted key makes the rest of the key part of the spec (`git = "…"`), several of them are joined."""
    out = {}  # type: Dict[str, str]
    for parts, value in _toml_entries(body):
        name = parts[0]
        if not name or not value:
            continue
        spec = " ".join(value.split())
        if len(parts) > 1:
            spec = "%s = %s" % (".".join(parts[1:]), spec)
            if name in out:
                spec = "%s, %s" % (out[name], spec)
        out[name] = spec
    return out


def _toml_list(text: str, start: int) -> Tuple[List[str], int]:
    """(the strings of the array whose `[` is at text[start], the index where the scan stopped); brackets inside quotes (extras such as `pkg[extra]`) do not
    count. The caller skips what was scanned, so an array that is never closed is read once, not once per `[` after it."""
    items = []  # type: List[str]
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = text.find(c, i + 1)
            if j < 0:
                return items, n
            if depth == 1:
                items.append(text[i + 1:j])
            i = j + 1
            continue
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return items, i + 1
        i += 1
    return items, n


def _put(out: Dict[str, str], name: str, spec: str) -> None:
    """out[name] = spec, but a name that comes twice keeps both specs, one per line: `requests @ https://…` followed by `requests>=2` is still a URL source."""
    if name not in out:
        out[name] = spec
    elif spec not in out[name].split("\n"):
        out[name] = out[name] + "\n" + spec             # a spec already kept is not added again, and never replaces the others (R5-B2)


def _add_pep508(out: Dict[str, str], specs: List[str]) -> None:
    for s in specs:
        m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$", s)
        if m:
            _put(out, m.group(1), m.group(2).split(";")[0].strip() or "*")


_POETRY_DEPS = r"tool\.poetry\.(?:dev-dependencies|dependencies|group\.[\w-]+\.dependencies)"
_CARGO_DEPS = r"(?:workspace\.|target\.[^\n]{1,200}\.)?(?:dev-|build-)?dependencies"


_DEP_TABLE_KEYS = ("dependencies", "dev-dependencies", "build-dependencies")


def _dep_key(parts: List[str]) -> bool:
    """Whether a (dotted, possibly quoted) key names a dependency list: a part that contains `dependenc` or is `requires`."""
    return any("dependenc" in p.lower() or p.lower() == "requires" for p in parts)


def _tname(table: str) -> str:
    """A TOML table name without quotes and without spaces around the dots (`tool . "poetry".dependencies` is `tool.poetry.dependencies`)."""
    parts = _split_key(table)          # quotes removed, escapes decoded (`"dependencies"` is `dependencies`)
    if parts is not None:
        return ".".join(parts)
    return ".".join(part.strip() for part in table.replace('"', "").replace("'", "").split("."))        # not a regex: `\s*\.\s*` is quadratic on a long run of blanks


def _json_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """json.loads hook: a key that comes twice keeps both string values (one per line) instead of the last one winning (`{"evil": "git+…", "evil": "1.0.0"}`)."""
    out = {}  # type: Dict[str, Any]
    for k, v in pairs:
        if k in out and isinstance(out[k], str) and isinstance(v, str) and out[k] != v:
            out[k] = out[k] + "\n" + v
        else:
            out[k] = v
    return out


def _load_json(text: str) -> Any:
    """The JSON document of a package.json: a leading byte-order mark is skipped, duplicate string keys are all kept. ValueError when it is not JSON; a
    RecursionError (nested deeper than Python reads) is NOT caught here: it reaches the gate's `unreadable` finding."""
    return json.loads(text.lstrip("﻿"), object_pairs_hook=_json_pairs)


_URL_OPTS = ("-r", "-c", "--requirement", "--constraint")


def _requirement_url(ln: str) -> Optional[str]:
    """The URL of a `-r https://…`, `--requirement=https://…`, `-c …` line (an include or constraints file fetched from a server), else None."""
    m = re.match(r"(-r|-c|--requirement|--constraint)(?:[ \t]*=[ \t]*|[ \t]+|(?=https?:))(\S+)", ln)
    return m.group(2) if m and "://" in m.group(2) else None


UNREADABLE_ENCODING = "<unreadable encoding>"


def unreadable_encoding(text: Optional[str]) -> bool:
    """Whether text read as UTF-8 is really something else: a NUL (UTF-16 and UTF-32 put one beside every ASCII character) or the two replacement characters a
    UTF-16 byte-order mark becomes. This reader cannot see a dependency in such a file, so it is a finding, not an empty manifest (R5-B4)."""
    return bool(text) and ("\x00" in text or text.startswith("\ufffd\ufffd"))


def parse_manifest(path: str, text: Optional[str]) -> Dict[str, str]:
    """{name: spec} of top-level dependencies. Unparsable text gives {} (the caller flags nothing it cannot read); text in another encoding gives one
    `<unreadable encoding>` entry. A name that comes twice keeps every spec."""
    if not text:
        return {}
    if unreadable_encoding(text):
        return {UNREADABLE_ENCODING: UNREADABLE_ENCODING}
    text = text.lstrip("﻿")
    base = os.path.basename(path)
    out = {}  # type: Dict[str, str]
    if base == "package.json":
        try:
            doc = _load_json(text)
        except ValueError:
            return {}
        for sect in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            v = doc.get(sect) if isinstance(doc, dict) else None
            if isinstance(v, dict):
                for k, s in v.items():
                    _put(out, str(k), str(s))
    elif base.endswith((".txt", ".in")):
        for ln in text.splitlines():
            ln = ln.split("#")[0].strip()
            if not ln:
                continue
            if ln.startswith(("-r", "-c", "--")):
                url = _requirement_url(ln)
                if url:
                    out[ln] = ln                    # a requirements or constraints file fetched over the network is a source nobody checked
                continue
            if ln.startswith("-e"):
                out[ln] = ln
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$", ln)
            if m:
                _put(out, m.group(1), m.group(2).split(";")[0].strip() or "*")
            elif _NON_REGISTRY.match(ln):
                out[ln] = ln
    elif base == "pyproject.toml":
        for table, body in _toml_tables(text):
            tname = _tname(table)
            poetry = re.match(_POETRY_DEPS + r"$", tname)
            sub = re.match(_POETRY_DEPS + r"\.([A-Za-z0-9][A-Za-z0-9._-]*)$", tname)
            if poetry:
                for name, spec in _dep_entries(body).items():          # bare, quoted and dotted keys, an inline table that goes on over several lines
                    if name.lower() != "python":
                        _put(out, name, spec)
            elif sub:                                   # `[tool.poetry.dependencies.requests]` with `git = "…"` below it
                if sub.group(1).lower() != "python":
                    _put(out, sub.group(1), " ".join(body.split()) or "*")
            else:
                # every array under a key that names dependencies or build requirements, in any table and also as a dotted or quoted key:
                # `[project] dependencies`, `"dependencies" = [`, `project.dependencies = [`, `optional-dependencies.x`, `[build-system] requires`,
                # `[dependency-groups]`, `[tool.pdm.dev-dependencies]`, `[tool.uv] dev-dependencies`, `[tool.hatch.envs.x] dependencies`
                listy = "dependenc" in tname          # a table of named groups: any `group = [` below it is a dependency list
                for parts, value in _toml_entries(body):          # one pass over the statements: an array is read once, however many `[` follow it
                    if value.startswith("[") and (listy or _dep_key(parts)):
                        _add_pep508(out, _toml_list(value, 0)[0])
    elif base == "go.mod":
        in_block = False        # line by line: a lazy `(.*?)^\)` from every `require (` line read to the end of the file each time
        for ln in text.splitlines():
            s = ln.strip()                      # go.mod is usually gofmt'ed, but nothing says it is: indentation never hides a line
            if in_block:
                if s.startswith(")"):
                    in_block = False
                    continue
                m = re.match(r"(\S+)[ \t]+(v\S+)", s)
            else:
                opener = re.match(r"require[ \t]*\((.*)$", s)
                if opener:
                    rest = opener.group(1).split("//")[0].strip()
                    if rest.endswith(")"):
                        rest = rest[:-1].strip()        # `require (a v1)` on one line
                    else:
                        in_block = True
                    m = re.match(r"(\S+)[ \t]+(v\S+)", rest) if rest else None
                else:
                    m = re.match(r"require[ \t]+(\S+)[ \t]+(v\S+)", s)
            if m:
                _put(out, m.group(1), m.group(2))
    elif base == "Cargo.toml":
        found = {}  # type: Dict[str, str]
        for table, body in _toml_tables(text):
            tname = _tname(table)
            plain = re.match(_CARGO_DEPS + r"$", tname)
            sub = re.match(_CARGO_DEPS + r"\.([A-Za-z0-9][A-Za-z0-9_-]*)$", tname)
            if plain:
                found.update(_dep_entries(body))                  # `evil.git = "…"` and `"evil" = {…}` are dependency `evil`
            elif sub:                                   # `[dependencies.serde]` with `git = "…"` below it
                found[sub.group(1)] = " ".join(body.split()) or "*"
        out.update(found)
        for spec in list(found.values()):
            pkg = re.search(r"\bpackage[ \t]*=[ \t]*[\"']([^\"'\n]+)[\"']", spec)
            if pkg:
                _put(out, pkg.group(1), spec)       # `serde = { package = "evil-serde" }` installs the crate `evil-serde`
    elif base == "Gemfile":
        # `gem "a"`, `gem("a")`, several statements on a line (`gem "a"; gem "b", git: "…"`); the options may continue on the next line
        for m in re.finditer(r"(?m)(?:^|;)[ \t]*gem[ \t]*\(?[ \t]*[\"']([^\"'\n]+)[\"'](?:\s*,\s*([^\n;]*))?", text):
            _put(out, m.group(1), (m.group(2) or "*").strip())
    return out


def unread_tables(path: str, text: Optional[str]) -> List[str]:
    """`table: body` of the dependency-looking tables of a pyproject or Cargo.toml that `parse_manifest` does not read (an unknown tool's `dependencies`
    table, a Cargo dependency table with a dotted name). The caller compares base and head and sends a change to a human: a dependency that this
    reader cannot see is not a dependency that was checked (fail closed)."""
    if not text or os.path.basename(path) not in ("pyproject.toml", "Cargo.toml"):
        return []
    text = text.lstrip("\ufeff")
    out = []  # type: List[str]
    cargo = os.path.basename(path) == "Cargo.toml"
    unparsed = []  # type: List[str]
    for table, body in _toml_tables(text, unparsed):
        tname = _tname(table)
        if "dependenc" not in tname:
            # a dependency table written as a dotted key in a table that is not one (`dependencies.evil.git = "…"` at the top of a Cargo.toml, `[workspace]`
            # or `[tool.poetry]`): nothing here reads it, unless it is an array (read as a list) or a `{ file = … }` (setuptools reads requirements from a file)
            for parts, value in _toml_entries(body):
                if any(p in _DEP_TABLE_KEYS for p in parts) and not value.startswith("[") and not re.match(r"\{\s*file\s*=", value):
                    out.append("%s: %s = %s" % (tname or "<top level>", ".".join(parts), " ".join(value.split())))
            continue
        if not any(ln.strip() and not ln.strip().startswith("#") for ln in body.splitlines()):
            continue
        bad = []  # type: List[str]
        entries = _toml_entries(body, bad)
        if cargo:
            known = re.match(_CARGO_DEPS + r"(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)?$", tname)
        else:
            known = re.match(_POETRY_DEPS + r"(?:\.[A-Za-z0-9][A-Za-z0-9._-]*)?$", tname) or (entries and all(v.startswith("[") for _p, v in entries))
        if not known or bad:            # a statement it could not read in a table it does read may be a dependency (`évil = {…}`, a stray `]` before one)
            out.append("%s: %s" % (tname, " ".join(ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#"))))
    # a line that starts with `[` and is not a header this reader understands may be a table header of a kind it does not know: whatever dependencies sit
    # under it were joined to the table above and never seen, so the line itself is the unread table (fail closed). The same for a line it cannot read and
    # a string or array that is never closed: every table after that point is in doubt
    out.extend(("unparsed table header: %s" % ln) if not ln.startswith("<") else ("unreadable toml: %s" % ln) for ln in unparsed)
    return out


def _nonregistry(spec: str) -> bool:
    """Whether a dependency spec names a source other than the default registry: a git, tarball, file or path source, or an inline table that
    says so (`{ git = "..." }`, `{ path = "..." }`). A name that came twice has one spec per line: any of them is enough."""
    return any(_NON_REGISTRY.match(one.strip()) or _STRUCT_SOURCE.search(one) for one in spec.split("\n"))


def _npm_overrides(text: str) -> List[str]:
    """`name=spec` for every `overrides`, `resolutions` and `pnpm.overrides` entry whose spec is not a registry version: these pin TRANSITIVE
    packages, so a git or tarball source there skips the checks as well as a direct one. Walked with a stack at any depth (the depth Python's JSON reader
    allows is the only limit, and a document past it is a RecursionError, which is the gate's `unreadable` finding)."""
    try:
        doc = _load_json(text)
    except ValueError:
        return []
    out = []  # type: List[str]
    stack = []  # type: List[Tuple[str, Any]]
    if isinstance(doc, dict):
        pn = doc.get("pnpm")
        roots = [("overrides", doc.get("overrides")), ("resolutions", doc.get("resolutions"))]
        if isinstance(pn, dict):
            roots.append(("pnpm.overrides", pn.get("overrides")))
        stack.extend(reversed(roots))
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            stack.extend(reversed([("%s>%s" % (path, k), v) for k, v in node.items()]))
        elif isinstance(node, str) and _nonregistry(node):
            out.append("%s=%s" % (path, node))
    return out


# The dotted paths (table name + key) that choose where packages come from, by file. A table whose name matches is taken whole; a KEY whose full path matches, in
# any table or at the top (`patch.crates-io.serde.git = …`, `sources.requests.git = …` under `[tool.uv]`), is taken on its own.
_INDEX_PATHS = {
    "pyproject.toml": re.compile(r"tool\.(?:uv|poetry|pdm)\.(?:index|sources|source|pip|index-url|extra-index-url|find-links|no-index|trusted-host)\b"),
    "Cargo.toml": re.compile(r"(?:workspace\.)?(?:patch|replace|source|registries|http|net)\b"),
    "uv.toml": re.compile(r"(?:index|sources|pip|index-url|extra-index-url|find-links|no-index|trusted-host)\b"),
    "cargo-config": re.compile(r"(?:patch|replace|source|registries|registry|http|net|credential-alias)\b"),
}


def _toml_index_overrides(text: str, kind: str) -> List[str]:
    out = []  # type: List[str]
    rx = _INDEX_PATHS[kind]
    for table, body in _toml_tables(re.sub(r"(?m)^[ \t]*\[\[(.*?)\]\]", r"[\1]", text)):        # an array of tables is read as a table
        tname = _tname(table)
        if tname and rx.match(tname):
            out.append("[%s] %s" % (table, " ".join(body.split())))
            continue
        for parts, value in _toml_entries(body):
            full = ".".join(([tname] if tname else []) + parts)
            if rx.match(full):
                out.append("%s = %s" % (full, " ".join(value.split())))
    return out


def _index_overrides(path: str, text: Optional[str]) -> List[str]:
    """Normalised lines of a manifest or config file that choose WHERE packages are resolved (UN-8): the pip options in a requirements file, the
    registry lines of `.npmrc` and its cousins, the index and source settings of a pyproject, uv.toml and `.cargo/config.toml`, Cargo `patch` tables, extra
    `source` lines of a Gemfile."""
    out = []  # type: List[str]
    if not text:
        return out
    if unreadable_encoding(text):
        return [UNREADABLE_ENCODING]
    text = text.lstrip("﻿")
    base = os.path.basename(path)
    if manifest_kind(path) == "pypi" and base.endswith((".txt", ".in")):
        for ln in text.splitlines():
            ln = ln.split(" #")[0].strip()
            if ln.startswith("-"):
                if _requirement_url(ln):
                    out.append(ln)              # `-r https://…`: an include fetched from a server
                for m in _INDEX_OPT.finditer(ln):
                    out.append(" ".join(x for x in (m.group(1).lower(), m.group(2)) if x))
    elif base == "pyproject.toml":
        for m in re.finditer(r"(?m)^[ \t]*(index-url|extra-index-url|find-links|index|trusted-host|no-index)[ \t]*=[ \t]*(.*)$", text):
            out.append("%s = %s" % (m.group(1), m.group(2).strip()))
        out.extend(_toml_index_overrides(text, "pyproject.toml"))
    elif base == "Cargo.toml":
        out.extend(_toml_index_overrides(text, "Cargo.toml"))
    elif base == "package.json":
        out.extend(_npm_overrides(text))
    elif base == "Gemfile":
        out.extend(m.group(0).strip() for m in re.finditer(r"(?m)^[ \t]*source[ \t]+[\"'][^\"'\n]+[\"']", text))
    elif base == "go.mod":
        # `replace a => b` and every line of a `replace ( … )` block, each as `replace <line>`: a line added to a block that already exists is a change
        in_block = False
        for ln in text.splitlines():
            s = ln.strip()
            if in_block:
                if s.startswith(")"):
                    in_block = False
                elif s and not s.startswith("//"):
                    out.append("replace " + s)
            elif re.match(r"replace[ \t]*\([ \t]*(?://.*)?$", s):
                in_block = True
            elif re.match(r"replace[ \t]+\S", s):
                out.append(s)
    elif base in _REGISTRY_FILES:
        out.extend(ln.strip() for ln in text.splitlines() if _REGISTRY_LINE.match(ln) or _RC_KEY_LINE.match(ln))
        if base == "uv.toml":
            out.extend(_toml_index_overrides(text, "uv.toml"))
        elif base in ("config", "config.toml") and path.replace("\\", "/").endswith((".cargo/config", ".cargo/config.toml")):
            out.extend(_toml_index_overrides(text, "cargo-config"))
    return out


def _lifecycle(text: Optional[str]) -> List[str]:
    try:
        sc = _load_json(text or "{}").get("scripts")
    except (ValueError, AttributeError):
        return []
    return [k for k in _LIFECYCLE if isinstance(sc, dict) and k in sc]


MAX_MANIFEST = 1 << 20          # characters of a manifest or registry file that are read; the rest is not, and that is a finding


class _Unread(list):
    """The paths whose text was cut (the list itself) and the paths in another encoding (`.encoding`): neither is a read manifest."""

    def __init__(self) -> None:
        super().__init__()
        self.encoding = []  # type: List[str]


def _blob(ctx: Any, rev: Optional[str], path: str, over: "_Unread") -> Optional[str]:
    """The text of a manifest at a revision, at most MAX_MANIFEST characters. A longer one is cut AND recorded in `over`: a cut manifest is not a read manifest.
    One in another encoding (UTF-16, UTF-32) is recorded in `over.encoding` and read as absent."""
    text = _lib.blob(ctx, rev, path, MAX_MANIFEST + 1)
    if unreadable_encoding(text):
        if path not in over.encoding:
            over.encoding.append(path)
        return None
    if text is not None and len(text) > MAX_MANIFEST:
        if path not in over:
            over.append(path)
        return text[:MAX_MANIFEST]
    return text


def _dep_check(ctx: Any, cmd: str, eco: str, name: str) -> Optional[Dict[str, Any]]:
    stage, text, _f = _lib.run_command(ctx, "dep_check", cmd, {"LOOPCTL_DEP_ECOSYSTEM": eco, "LOOPCTL_DEP_NAME": name}, timeout_s=120, count_tests=False)
    if stage["status"] != "pass":
        return None
    try:
        doc = common.strict_loads(text.strip().splitlines()[-1] if text.strip() else "", max_depth=3, max_bytes=8192)
    except (ValueError, RecursionError, IndexError):
        return None
    return doc if isinstance(doc, dict) else None


def _run(ctx: Any) -> GateResult:
    try:
        rows = _lib.diff_status(ctx)
    except ValueError as exc:
        return GateResult.errored("cannot read the diff: %s" % common.clean_text(exc, 120))
    head = _lib.head_of(ctx)
    findings = []  # type: List[Dict[str, Any]]
    new_deps = []  # type: List[Dict[str, Any]]
    declared = isinstance(ctx.item.get("touches"), dict) and ctx.item["touches"].get("deps_change") is True
    ext = ctx.item.get("ext") if isinstance(ctx.item.get("ext"), dict) else {}
    allow = set(str(x) for x in ((ext.get("g9") or {}).get("allow") or [])) if isinstance(ext.get("g9"), dict) else set()
    changed = {p for s, p, _o in rows if s != "D"}
    over = _Unread()
    manifests = [(s, p) for s, p, _o in rows if manifest_kind(p) and s != "D"]
    for status, path in manifests:
        eco = manifest_kind(path) or ""
        before = parse_manifest(path, _blob(ctx, ctx.base_sha, path, over)) if status != "A" else {}
        after = parse_manifest(path, _blob(ctx, head, path, over))
        fresh = sorted(set(after) - set(before))
        old_txt = _blob(ctx, ctx.base_sha, path, over) if status != "A" else None
        new_txt = _blob(ctx, head, path, over)
        # an existing dependency whose SPEC moved to a git, tarball, file or path source skips every registry check just as a new one would
        for name in sorted(set(after) & set(before)):
            if before[name] != after[name] and _nonregistry(after[name]):
                sev = "warn" if name in allow else "error"
                findings.append(_lib.finding("g9.source-changed", sev, "dependency %s changed from %s to %s, which is not a registry source: none of the registry checks apply to it"
                                             % (common.clean_text(name, 60), common.clean_text(before[name], 40), common.clean_text(after[name], 50)),
                                             dependency=common.clean_text(name, 60), path=path))
        for tbl in sorted(set(unread_tables(path, new_txt)) - set(unread_tables(path, old_txt)))[:3]:
            findings.append(_lib.finding("g9.dependency-table-unread", "error", "%s changes a dependency table this gate cannot read (%s): a dependency in it was not checked. "
                                         "A human reads it" % (path, common.clean_text(tbl, 80)), path=path))
        added_idx = sorted(set(_index_overrides(path, new_txt)) - set(_index_overrides(path, old_txt)))
        for opt in added_idx[:5]:
            findings.append(_lib.finding("g9.index-changed", "error", "%s adds or changes a package-index setting (%s): where a name resolves decides what it installs"
                                         % (path, common.clean_text(opt, 80)), path=path))
        if os.path.basename(path) == "package.json":
            old_t = _blob(ctx, ctx.base_sha, path, over) if status != "A" else None
            add = [k for k in _lifecycle(_blob(ctx, head, path, over)) if k not in _lifecycle(old_t)]
            if add:
                findings.append(_lib.finding("g9.install-script-added", "error", "%s adds the lifecycle script(s) %s: they run code on every install (use --ignore-scripts and "
                                             "an allowlist)" % (path, ", ".join(add)), path=path))
        if not fresh:
            continue
        d = os.path.dirname(path)
        locks = [os.path.join(d, n) if d else n for n in _LOCKS.get(eco, ())]
        pinned_txt = os.path.basename(path).endswith(".txt")
        if path.endswith(".in"):
            # a pip-tools input: the lock is the compiled requirements file beside it (`requirements.in` -> `requirements.txt`), so that moving counts
            locks.extend(c for c in changed if os.path.dirname(c) == d and c.endswith(".txt") and manifest_kind(c) == "pypi")
        if not pinned_txt and eco in _LOCKS and not any(l in changed for l in locks) and not any(
                os.path.basename(c) in _LOCKS[eco] for c in changed):
            findings.append(_lib.finding("g9.lockfile-not-updated", "error", "%s gained %d dependenc%s but no lockfile changed: installs must use a frozen "
                                         "lockfile that moves with the manifest" % (path, len(fresh), "y" if len(fresh) == 1 else "ies"), path=path))
        for name in fresh:
            spec = after[name]
            rec = {"ecosystem": eco, "name": common.clean_text(name, 80), "spec": common.clean_text(spec, 60), "manifest": path}
            new_deps.append(rec)
            if not name.isascii():
                findings.append(_lib.finding("g9.non-ascii-name", "error", "new dependency %s has non-ASCII characters in its name: a homoglyph look-alike of a real package"
                                             % ascii(name)[:60], dependency=rec["name"]))
            if _nonregistry(spec) or _NON_REGISTRY.match(name):
                sev = "warn" if name in allow else "error"
                findings.append(_lib.finding("g9.non-registry-source", sev, "new dependency %s comes from %s, not from a registry: none of the registry checks apply to it"
                                             % (rec["name"], common.clean_text(spec, 50)), dependency=rec["name"]))
            if pinned_txt and not re.search(r"==\s*[0-9]", spec) and not _nonregistry(spec):
                findings.append(_lib.finding("g9.unpinned", "error", "new requirement %s is not pinned with == (frozen installs need an exact version)" % rec["name"]))
    for status, path, _o in rows:
        if status == "D" or os.path.basename(path) not in _REGISTRY_FILES or manifest_kind(path):
            continue
        if os.path.basename(path) in ("config", "config.toml") and not path.endswith((".cargo/config", ".cargo/config.toml")):
            continue
        old_t = (_blob(ctx, ctx.base_sha, path, over) if status != "A" else None)
        for opt in sorted(set(_index_overrides(path, _blob(ctx, head, path, over))) - set(_index_overrides(path, old_t)))[:5]:
            findings.append(_lib.finding("g9.index-changed", "error", "%s adds or changes a package-index setting (%s): where a name resolves decides what it installs"
                                         % (path, common.clean_text(opt, 80)), path=path))
    for p in over[:5]:
        findings.append(_lib.finding("g9.manifest-over-limit", "error", "%s is larger than %d KiB and this check read only the first %d KiB of it: a human reads the rest" % (
            common.clean_text(p, 80), MAX_MANIFEST >> 10, MAX_MANIFEST >> 10), path=p))
    for p in over.encoding[:5]:
        findings.append(_lib.finding("g9.manifest-unreadable", "error", "%s is not UTF-8 text (it holds a NUL or a UTF-16/UTF-32 byte-order mark): this check cannot read the dependencies "
                                     "in it, so a human reads it" % common.clean_text(p, 80), path=p))
    manifest_dirs = {os.path.dirname(p) for _s, p in manifests}
    lock_names = {n for names in _LOCKS.values() for n in names}
    for _s, p, _o in rows:
        if os.path.basename(p) in lock_names and os.path.dirname(p) not in manifest_dirs and not declared:
            findings.append(_lib.finding("g9.lockfile-only", "warn", "%s changed with no manifest change beside it: resolved versions moved (including transitive ones) outside "
                                         "any declared dependency change" % p, path=p))
    if new_deps and not declared:
        findings.append(_lib.finding("g9.undeclared-dependency", "error", "the diff adds %d new dependenc%s (%s) but the plan item does not declare touches.deps_change: true. "
                                     "A dependency change goes through the plan PR" % (len(new_deps), "y" if len(new_deps) == 1 else "ies",
                                                                                     ", ".join(d["name"] for d in new_deps[:4]))))
    cmd = _lib.command_set(ctx.cfg, "dep_check_cmd")
    allowed_lic = [str(x).lower() for x in (ctx.cfg.get("license_allowlist") or [])]
    for rec in new_deps[:MAX_CHECKED]:
        eco, name = rec["ecosystem"], rec["name"]
        if not _NAME_OK.match(name):
            findings.append(_lib.finding("g9.bad-name", "error", "new dependency %s is not a plausible package name; it was not passed to dep_check_cmd" % ascii(name)[:60], dependency=name))
            continue
        look = lookalike(name, eco)
        info = _dep_check(ctx, cmd, eco, name) if cmd else None
        verified = bool(info and info.get("exists") is True)
        if info is not None and info.get("exists") is False:
            findings.append(_lib.finding("g9.not-in-registry", "error", "new dependency %s does not exist in the %s registry: a hallucinated or squatted name" % (name, eco), dependency=name))
            continue
        young = bool(info) and isinstance(info.get("age_days"), (int, float)) and info["age_days"] < 30
        rare = bool(info) and isinstance(info.get("downloads"), (int, float)) and info["downloads"] < 100
        if look:
            sev = "warn" if (name in allow or (verified and not young and not rare)) else "error"
            findings.append(_lib.finding("g9.lookalike", sev, "new dependency %s looks like the popular %s (typosquat check)%s" % (
                name, look, "" if sev == "error" else "; allowed or verified old and widely used"), dependency=name))
        elif young or rare:
            findings.append(_lib.finding("g9.young-or-rare", "warn", "new dependency %s is %s" % (name, "younger than 30 days" if young else "rarely downloaded"), dependency=name))
        lic = info.get("license") if info else None
        if allowed_lic:
            if not isinstance(lic, str) or not lic.strip():
                findings.append(_lib.finding("g9.license-unknown", "error", "the licence of %s is unknown and license_allowlist is set" % name, dependency=name))
            elif lic.strip().lower() not in allowed_lic:
                findings.append(_lib.finding("g9.license-not-allowed", "error", "%s is licensed %s, which is not in license_allowlist" % (name, common.clean_text(lic, 40)), dependency=name))
        rec["checked"] = bool(info)
    if len(new_deps) > MAX_CHECKED:
        findings.append(_lib.finding("g9.too-many", "error", "%d new dependencies is more than one item should add; only %d were checked" % (len(new_deps), MAX_CHECKED)))
    if new_deps and not cmd:
        findings.append(_lib.finding("g9.registry-unchecked", "warn", "dep_check_cmd is not set: existence, age and licence of %d new dependenc%s were NOT checked. Listed for the human" % (
            len(new_deps), "y" if len(new_deps) == 1 else "ies")))
    # install hygiene in what this diff changed
    for status, path, _o in rows:
        if status == "D" or not (path.startswith((".github/workflows/", ".github/actions/")) or os.path.basename(path) in ("Makefile", "Dockerfile", "Containerfile", ".npmrc")
                                 or path.endswith((".sh", ".yml", ".yaml"))):
            continue
        text = _lib.blob(ctx, head, path) or ""
        old = (_lib.blob(ctx, ctx.base_sha, path) or "") if status != "A" else ""
        if os.path.basename(path) == ".npmrc" and re.search(r"(?mi)^[ \t]*ignore-scripts[ \t]*=[ \t]*false", text) and not re.search(r"(?mi)^[ \t]*ignore-scripts[ \t]*=[ \t]*false", old):
            findings.append(_lib.finding("g9.scripts-reenabled", "error", ".npmrc re-enables install scripts (ignore-scripts=false)", path=path))
            continue
        bad_re = re.compile(r"(?m)^[^#\n]*\b(?:npm (?:install|i)\b|yarn install\b|pnpm (?:install|i)\b)(?![^\n]*(?:--ignore-scripts|--frozen-lockfile|--immutable))[^\n]*$")
        if len(bad_re.findall(text)) > len(bad_re.findall(old)):
            findings.append(_lib.finding("g9.install-hygiene", "warn", "%s installs dependencies without --ignore-scripts or a frozen lockfile" % path, path=path))
    ev = {"summary": "%d new dependenc%s%s" % (len(new_deps), "y" if len(new_deps) == 1 else "ies", " (unchecked)" if new_deps and not cmd else ""), "new_dependencies": new_deps}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)


def run(ctx: Any) -> GateResult:
    """The supply-chain check. Any exception while reading (a RecursionError, a MemoryError, a bug) is a blocking `g9.unreadable` finding: never a pass."""
    try:
        return _run(ctx)
    except Exception as exc:  # noqa: BLE001
        return GateResult.failed([_lib.finding("g9.unreadable", "error", "The supply-chain check could not be read to the end (%s): a human reads the change" % (
            common.clean_text(type(exc).__name__, 40),))])
