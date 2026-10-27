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
_KEYWORD = (r"(?:(?:password|passwd|passphrase|pwd|credential|api[_-]?key|access[_-]?key|private[_-]?key)s?|secret|token)")
# a strong word may also be glued to a lowercase prefix (`clientsecret`, `dbpassword`, `secretkey`); the weak ones (`token`, `pwd`) may not (`maxtoken`)
_STRONG = r"(?:[A-Za-z0-9]*(?:password|passwd|passphrase|secret[_-]?key|secret|apikey|privatekey|accesskey)s?)"
_CAMEL = r"(?-i:Password|Passwd|Passphrase|Pwd|Credentials?|ApiKey|AccessKey|PrivateKey|Secret|Token)"
_SUFFIX = r"(?:[_-](?:key|id|value|secret|token|password|passwd|hash|salt|string|data|b64|base64)){0,2}"
_SECRET_KEY = (r"(?<![A-Za-z0-9])(?:(?:[A-Za-z0-9]+[_-]){0,6}" + _KEYWORD + r"|" + _STRONG + r"|[a-z][a-z0-9]*" + _CAMEL + r")" + _SUFFIX + r"(?![A-Za-z0-9])")

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
    ("url-credentials", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:([^\s/@]{3,})@"), 1),
    ("basic-auth", re.compile(r"(?i)\bauthorization\s*:\s*(?:basic|digest)\s+(?!\[REDACTED)([A-Za-z0-9+/=._-]{8,})"), 1),
    ("bearer", re.compile(r"(?i)\b(?:bearer|token)\s+(?!\[REDACTED)([A-Za-z0-9._~+/=-]{16,})"), 1),
    ("secret-assignment",
     re.compile(r"(?i)[\"']?" + _SECRET_KEY + r"[\"']?\s*[:=]\s*(?!\[REDACTED)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\"'}\])]+)"), 1),
]  # type: List[Tuple[str, Any, int]]

_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:password|passwd|passphrase|secret|token|api[_-]?key|apikey|authorization|credentials?|private[_-]?key)s?(?:$|[_-])|"
    r"[a-z0-9](?-i:Password|Passwd|Passphrase|Secret|Token|ApiKey|AccessKey|PrivateKey|Credentials?|Authorization)(?-i:$|[A-Z_-])",
    re.I)


def find_secrets(text: str) -> List[Tuple[str, int, int]]:
    """Return (kind, start, end) spans, in the order patterns are applied (may overlap)."""
    spans = []  # type: List[Tuple[str, int, int]]
    for kind, rx, grp in _PATTERNS:
        for m in rx.finditer(text):
            spans.append((kind, m.start(grp), m.end(grp)))
    return spans


def redact(text: str) -> str:
    """Replace secret-looking substrings. Idempotent."""
    if not isinstance(text, str) or not text:
        return text
    for kind, rx, grp in _PATTERNS:
        text = _sub(rx, grp, kind, text)
    return text


def _sub(rx, grp: int, kind: str, text: str) -> str:
    out = []  # type: List[str]
    pos = 0
    for m in rx.finditer(text):
        s, e = m.start(grp), m.end(grp)
        if s < pos or text.startswith(REDACTED_PREFIX, s):
            continue
        out.append(text[pos:s])
        out.append("[REDACTED:%s]" % kind)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def redact_obj(obj: Any) -> Any:
    """Redact every string in a JSON-like structure. Values under secret-looking
    keys (`password`, `token`, `authorization`, ...) are replaced wholesale."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and isinstance(v, str) and v \
                    and not v.startswith(REDACTED_PREFIX):
                out[k] = "[REDACTED:secret-key]"
            else:
                out[k] = redact_obj(v)
        return out
    return obj
