"""Secret redaction for events, evidence, reports and logs (report §5.4, G11 companion).

`redact(text)` replaces known secret shapes with `[REDACTED:<kind>]`. It is
idempotent and never raises on str input. Patterns are deliberately tuned for few
false negatives on the common shapes the kit meets (cloud keys, GitHub and Slack
tokens, private keys, bearer headers, `password=`); gitleaks in CI is the real
scanner, this is the last line of defence for text the kit itself writes.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

__all__ = ["redact", "redact_obj", "find_secrets", "REDACTED_PREFIX"]

REDACTED_PREFIX = "[REDACTED"

# A secret-looking IDENTIFIER, not a word (UN-2): `\b` around the key name misses every compound name, because `_` is a word character
# (`DB_PASSWORD=`, `STRIPE_SECRET_KEY=`, `API_SECRET=`, `db_password=`, `apiSecret=`). The name is any run of identifier parts, one of which is
# a keyword, followed by at most two secret-ish suffix parts. The suffix list is closed on purpose: `token_budget_per_item: 150000` and
# `input_tokens: 12` are not secrets, and a bare `auth` is not a keyword (`auth: required`); `auth_token` is covered by its `token` part.
# `pass` counts here only as a later part (`DB_PASS=`, `db_pass:`); a bare `pass` key has its own pattern below.
_KEYWORD = (r"(?:(?:password|passwd|passphrase|pwd|credential|api[_-]?key|access[_-]?key|private[_-]?key|auth[_-]?key|(?:signing|encryption|decryption|master|hmac)[_-]?key)s?|secret|token|(?<=[_-])pass)")
# a strong word may also be glued to a lowercase prefix (`clientsecret`, `dbpassword`, `secretkey`); the weak ones (`token`, `pwd`) may not (`maxtoken`)
_STRONG = r"(?:[A-Za-z0-9]*(?:password|passwd|passphrase|secret[_-]?key|secret|apikey|privatekey|accesskey)s?)"
_CAMEL = r"(?-i:Password|Passwd|Passphrase|Pwd|Credentials?|ApiKey|AccessKey|PrivateKey|Secret|Token)"
_SUFFIX = r"(?:[_-](?:key|id|value|secret|token|password|passwd|hash|salt|string|data|b64|base64|base)){0,2}"
_SECRET_KEY = (r"(?<![A-Za-z0-9])(?:(?:[A-Za-z0-9]+[_-]){0,6}" + _KEYWORD + r"|" + _STRONG + r"|[a-z][a-z0-9]*" + _CAMEL + r")" + _SUFFIX + r"(?![A-Za-z0-9])")

# a quoted value, escaped quotes included (`"a\"b"`); bounded, so an unclosed quote cannot make a long line quadratic
_DQ = r'"(?:(?:[^"\\\n]|\\.){0,4096}"|(?:[^"\\\n]|\\.){4096}[^\n]*)'
_SQ = r"'(?:(?:[^'\\\n]|\\.){0,4096}'|(?:[^'\\\n]|\\.){4096}[^\n]*)"
# A quoted value over the bound is not left in the clear (R5-B1): the second alternative takes the first 4096 characters and the rest of the line, so a
# very long password is redacted whole. Over-redaction is the safe side.

# (kind, compiled regex, group to replace or 0 for the whole match)
_PATTERNS = [
    ("private-key",
     re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[A-Z0-9]{16}\b"), 0),
    ("aws-secret-key", re.compile(r"(?i)\baws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})"), 1),
    ("aws-session-token", re.compile(r"(?i)\baws_?session_?token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=_-]{16,})"), 1),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"), 0),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"), 0),
    ("slack-token", re.compile(r"\bxox[abprso]-[A-Za-z0-9-]{10,}\b"), 0),
    ("slack-token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"), 0),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9/_-]{12,}"), 0),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    ("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}"), 0),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai-key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"), 0),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), 0),
    ("stripe-webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{16,}"), 0),
    ("azure-account-key", re.compile(r"(?i)\bAccountKey=([A-Za-z0-9+/]{20,}={0,2})"), 1),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), 0),
    # every repetition is bounded: an unbounded scheme or user part made a long hyphenated line quadratic (a 1 MB line took minutes)
    ("url-credentials", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s/:@]{1,200}:([^\s/@]{3,4096})@"), 1),
    ("basic-auth", re.compile(r"(?i)\bauthorization\s*:\s*(?:basic|digest)\s+(?!\[REDACTED)([A-Za-z0-9+/=._-]{8,})"), 1),
    ("bearer", re.compile(r"(?i)\b(?:bearer|token)\s+(?!\[REDACTED)([A-Za-z0-9._~+/=-]{16,})"), 1),
    ("secret-assignment",
     re.compile(r"(?i)[\"']?" + _SECRET_KEY + r"[\"']?\s*[:=]\s*(?!\[REDACTED)(" + _DQ + r"|" + _SQ + r"|[^\s,;\"'}\])]+)"), 1),
    # a bare `pass` is a key only where a config key stands: at the start of a line (`pass: v`, `export PASS=v`, `- pass: v`) or quoted (`"pass": "v"`);
    # in running text `PASS: g6 passed` is a test verdict
    ("secret-assignment",
     re.compile(r"(?im)(?:^[ \t]*(?:export[ \t]+|-[ \t]+)?pass|\"pass\"|'pass')[ \t]*[:=][ \t]*(?!\[REDACTED)(" + _DQ + r"|" + _SQ + r"|[^\s,;\"'}\])]+)"), 1),
    # `--password VALUE`, `--db-token VALUE`: the value is the next word (a variable or another flag is not a value)
    ("cli-secret-arg",
     re.compile(r"(?i)(?<![\w-])--(?:[a-z0-9]+-){0,3}(?:password|passwd|passphrase|pass|secret|token|api-?key|access-?key|private-?key|auth-?key)[ \t]+(?![-$<\[]|\[REDACTED)"
                r"(" + _DQ + r"|" + _SQ + r"|\S+)"), 1),
    ("cli-secret-arg", re.compile(r"\bsshpass[ \t]+-p[ \t]*(?![$]|\[REDACTED)(" + _DQ + r"|" + _SQ + r"|\S+)"), 1),
    ("cli-secret-arg", re.compile(r"\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n|;&]{0,200}?[ \t]-p(?![$]|\[REDACTED)(" + _DQ + r"|" + _SQ + r"|\S+)"), 1),
    # Dockerfile's old form `ENV DB_PASSWORD value` (no `=`)
    ("secret-assignment", re.compile(r"(?im)^[ \t]*ENV[ \t]+" + _SECRET_KEY + r"[ \t]+(?![=:\[]|\[REDACTED)(" + _DQ + r"|" + _SQ + r"|[^\s\"']+)"), 1),
    # XML: `<password>value</password>` and `<add key="DbPassword" value="value"/>`. The body is greedy up to the next `<`, so it has ONE way to parse (a lazy
    # body followed by a blank run was quadratic, and a bounded blank run missed a value padded with more blanks than the bound); `_span` cuts the blanks
    # before the closing tag off the match in code
    ("secret-element",
     re.compile(r"(?i)<(?:[A-Za-z0-9_.-]{1,40}:)?" + _SECRET_KEY + r"(?:[ \t\r\n][^<>]{0,200})?>[ \t\r\n]*(?:<!\[CDATA\[)?(?!\[REDACTED)([^<\s][^<]{0,4096})</"), 1),
    # the same for a value of one word, whatever the padding: the greedy body above reads at most 4096 characters, a value padded past that is still a value
    ("secret-element",
     re.compile(r"(?i)<(?:[A-Za-z0-9_.-]{1,40}:)?" + _SECRET_KEY + r"(?:[ \t\r\n][^<>]{0,200})?>[ \t\r\n]*(?:<!\[CDATA\[)?(?!\[REDACTED)([^<\s]{1,500})[ \t\r\n]*(?:\]\]>)?</"), 1),
    # a body that goes past the bound with no closing tag in reach (padding, a long sentence): the bound is the reader's, not a reason to leave the start of
    # it in the clear. Over-redaction is the safe side (R4-B5)
    ("secret-element",
     re.compile(r"(?i)<(?:[A-Za-z0-9_.-]{1,40}:)?" + _SECRET_KEY + r"(?:[ \t\r\n][^<>]{0,200})?>[ \t\r\n]*(?:<!\[CDATA\[)?(?!\[REDACTED)([^<\s][^<]{4096})"), 1),
    ("secret-attribute",
     re.compile(r"(?i)\b(?:key|name)[ \t]*=[ \t]*[\"']" + _SECRET_KEY + r"[\"'][ \t]+value[ \t]*=[ \t]*[\"'](?!\[REDACTED)([^\"'\n]+)[\"']"), 1),
]  # type: List[Tuple[str, Any, int]]

_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:password|passwd|passphrase|secret|token|api[_-]?key|apikey|authorization|credentials?|private[_-]?key|auth[_-]?key|(?:signing|encryption|decryption|master|hmac)[_-]?key)s?(?:$|[_-])|(?:^|[_-])pass$|"
    r"[a-z0-9](?-i:Password|Passwd|Passphrase|Secret|Token|ApiKey|AccessKey|PrivateKey|Credentials?|Authorization|SigningKey|EncryptionKey|MasterKey)(?-i:$|[A-Z_-])",
    re.I)
# keys whose value is a secret whatever its type (a numeric password is still a password); `token` is left out because `tokens: 1200` is a usage figure
_STRONG_KEY_RE = re.compile(r"(?:^|[_-])(?:password|passwd|passphrase|secret|api[_-]?key|apikey|private[_-]?key)s?$|"
                            r"[a-z0-9](?-i:Password|Passwd|Passphrase|Secret|ApiKey|PrivateKey)$", re.I)       # the key ENDS in the word: `password_min_length: 12` is not one


def _span(kind: str, m: Any, grp: int, text: str) -> Tuple[int, int]:
    """The span of group `grp` of match `m`. An XML element body ends before the blanks that precede the closing tag."""
    s, e = m.start(grp), m.end(grp)
    if kind == "secret-element":
        while e > s and text[e - 1] in " \t\r\n":
            e -= 1
        if e - s > 3 and text.startswith("]]>", e - 3):
            e -= 3                              # the end of a CDATA section is markup, not part of the value
    return s, e


def find_secrets(text: str) -> List[Tuple[str, int, int]]:
    """Return (kind, start, end) spans, in the order patterns are applied (may overlap). Text it cannot read (any exception, a RecursionError or a MemoryError
    included) is one `unreadable` span over the whole text, never an empty list: a reader that fails must not look like a clean result."""
    spans = []  # type: List[Tuple[str, int, int]]
    try:
        for kind, rx, grp in _PATTERNS:
            for m in rx.finditer(text):
                s, e = _span(kind, m, grp, text)
                spans.append((kind, s, e))
    except Exception:  # noqa: BLE001
        return [("unreadable", 0, len(text))]
    return spans


def redact(text: str) -> str:
    """Replace secret-looking substrings. Idempotent."""
    if not isinstance(text, str) or not text:
        return text
    try:
        for kind, rx, grp in _PATTERNS:
            text = _sub(rx, grp, kind, text)
    except Exception:  # noqa: BLE001 - text this module cannot read is not text to keep
        return "[REDACTED:unreadable]"
    return text


def _sub(rx, grp: int, kind: str, text: str) -> str:
    out = []  # type: List[str]
    pos = 0
    for m in rx.finditer(text):
        s, e = _span(kind, m, grp, text)
        if s < pos or text.startswith(REDACTED_PREFIX, s):
            continue
        out.append(text[pos:s])
        out.append("[REDACTED:%s]" % kind)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def redact_obj(obj: Any, _secret: int = 0) -> Any:
    """`_redact_obj` with the same rule as `redact`: a structure it cannot walk (nested past the recursion limit) becomes one redacted string."""
    try:
        return _redact_obj(obj, _secret)
    except Exception:  # noqa: BLE001
        return "[REDACTED:unreadable]"


def _redact_obj(obj: Any, _secret: int = 0) -> Any:
    """Redact every string in a JSON-like structure. Values under secret-looking keys (`password`, `token`, `authorization`, ...) are replaced
    wholesale: a string at once, and every string inside a list or dict (`{"password": ["x"]}` leaks nothing). Under a strong key (`password`,
    `secret`, `api_key`) numbers go too; a number under `token_*` or `tokens` is a usage figure and stays. (`_secret` is 0, 1 under a secret key,
    2 under a strong one.)"""
    if isinstance(obj, str):
        if _secret and obj and not obj.startswith(REDACTED_PREFIX):
            return "[REDACTED:secret-key]"
        return redact(obj)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return "[REDACTED:secret-key]" if _secret == 2 else obj
    if isinstance(obj, list):
        return [_redact_obj(v, _secret) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_obj(v, _secret) for v in obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            level = _secret
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                level = max(level, 2 if _STRONG_KEY_RE.search(k) else 1)
            out[k] = _redact_obj(v, level)
        return out
    return obj
