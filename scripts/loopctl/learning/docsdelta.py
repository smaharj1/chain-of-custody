"""The Docs Delta check, as pure functions over a diff and the engineers' `## Doc Delta` sections (report 19.7 F-30, 16.1 row 10).

"The engineer report gains `## Doc Delta`: README, API reference, env vars, runbook. ... The gate in Ph 10 checks that the delta matches the diff."

`gates/docs_delta.py` reads the diff and the reports from git and calls `evaluate`. The rules, and what each one is there to stop:

1. TRIGGERS. A new environment variable read, a new route or request path, a new command-line flag, in the added lines of source files (not tests, not
   docs, not the kit's own files), and not in the removed lines (a moved line is not new). These are the things a person or a deploy needs to know about.
2. DOCUMENTED. A trigger is documented only when its own name appears in an ADDED line of a documentation file (prose with at least three words, or a
   `.env.example` style file). A blank line, trailing whitespace, a reflow, a punctuation-only edit, an edit that only deletes, an edit in
   `docs/features/**` (the feature's own scratch folder, where the report lives) or in a test, adds no word and documents nothing. So a documentation
   change cannot satisfy the gate by existing: it has to say the thing.
3. DECLARED. The alternative the protocol allows ("otherwise report it") is the engineer's `## Doc Delta` naming the trigger in at least
   `docs_delta_min_words` words of its own. That passes with a warning, because someone still has to write the doc.
4. Otherwise `docs.undeclared`: the diff adds something to document and neither the docs nor the report say so. A `## Doc Delta` that is missing,
   whitespace only, or `NONE` with no `checked:` line is no answer when there is a trigger.

The check is a heuristic over three families of trigger. It does not read meaning: the code-reviewer still blocks on an undocumented endpoint
(engineer protocol section 12), and the gate says `report` by default until its false-positive rate is known.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import common
from ..gates import _lib

__all__ = ["parse_diff", "is_doc_path", "is_code_path", "words", "net_new_words", "extract_triggers", "documented_in", "parse_delta", "evaluate", "MENTION_MIN_WORDS"]

MENTION_MIN_WORDS = 3
MAX_DIFF_BYTES = 4 << 20
DOC_GLOBS = ["**/*.md", "**/*.mdx", "**/*.rst", "**/*.adoc", "docs/**", "README*", "**/README*", "CHANGELOG*", "**/CHANGELOG*", "**/openapi*.json",
             "**/openapi*.yaml", "**/openapi*.yml", "**/swagger*.json", "**/swagger*.yaml", "**/swagger*.yml"]
ENV_EXAMPLE_GLOBS = ["**/.env.example", ".env.example", "**/.env.sample", ".env.sample", "**/.env.template", ".env.template", "**/*.env.example"]
# A change here documents nothing and is not source either: the feature's scratch folder (the report is in it), the plan, the kit, vendored code.
NOT_DOCS = ["docs/features/**", "plan/**", ".claude/**", "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**", "**/.git/**", "docs/baseline/**"]
NOT_CODE = NOT_DOCS + ["**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock", "**/*.snap", "**/*.min.js", "**/*.map", "**/*.json", "**/*.svg",
                       "**/*.png", "**/*.jpg", "**/*.ico", "**/*.csv", "**/*.sql", "**/*.yaml", "**/*.yml", "**/*.toml", "**/*.md"]
_EXEMPT_ENV = {"HOME", "PATH", "PWD", "USER", "TERM", "CI", "TZ", "LANG", "LC_ALL", "SHELL", "TMPDIR", "TEMP", "TMP", "NODE_ENV", "PYTHONPATH", "DEBUG", "HOSTNAME",
               "LOGNAME", "EDITOR", "PAGER", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
_NAME = r"([A-Za-z][A-Za-z0-9_]{2,63})"
_ENV_RES = [re.compile(x) for x in (
    r"process\.env\.([A-Z][A-Z0-9_]{2,63})", r"process\.env\[\s*['\"]" + _NAME + r"['\"]\s*\]", r"import\.meta\.env\.([A-Z][A-Z0-9_]{2,63})",
    r"os\.environ(?:\.get)?\s*[\[(]\s*['\"]" + _NAME + r"['\"]", r"os\.getenv\(\s*['\"]" + _NAME + r"['\"]", r"(?<![A-Za-z_.])getenv\(\s*['\"]" + _NAME + r"['\"]",
    r"System\.getenv\(\s*\"" + _NAME + r"\"", r"Deno\.env\.get\(\s*['\"]" + _NAME + r"['\"]", r"\bENV\[\s*['\"]" + _NAME + r"['\"]\s*\]",
    r"env::var\(\s*\"" + _NAME + r"\"")]
_ROUTE_RES = [re.compile(x) for x in (
    r"@(?:\w+\.)?(?:route|get|post|put|patch|delete)\(\s*['\"](/[^'\"\s]*)['\"]",
    r"\b(?:app|router|api|server|r|e|g|mux)\.(?:get|post|put|patch|delete|all|route|use|GET|POST|PUT|PATCH|DELETE|Handle|HandleFunc)\(\s*['\"`](/[^'\"`\s]*)",
    r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*)?['\"](/[^'\"\s]*)", r"\bself\.path\s*(?:==|\.startswith\()\s*['\"](/[^'\"\s]*)")]
_FLAG_RES = [re.compile(x) for x in (
    r"add_argument\(\s*(?:['\"]-[a-z]['\"]\s*,\s*)?['\"](--[a-z][a-z0-9-]{1,40})['\"]", r"\.option\(\s*['\"](?:-[a-z],\s*)?(--[a-z][a-z0-9-]{1,40})",
    r"@click\.option\(\s*['\"](?:-[a-z]['\"]\s*,\s*['\"])?(--[a-z][a-z0-9-]{1,40})")]
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def is_doc_path(path: str) -> bool:
    return bool(common.safe_rel_path(path)) and (_lib.match_any(DOC_GLOBS + ENV_EXAMPLE_GLOBS, path) and not _lib.match_any(NOT_DOCS, path)
                                                 and not _lib.is_test_path(path))


def is_env_example(path: str) -> bool:
    return _lib.match_any(ENV_EXAMPLE_GLOBS, path)


def is_code_path(path: str) -> bool:
    return bool(common.safe_rel_path(path)) and not _lib.match_any(NOT_CODE, path) and not _lib.is_test_path(path) and not is_doc_path(path) \
        and not _lib.match_any(_lib.SNAPSHOT_GLOBS, path)


def words(line: str) -> Dict[str, int]:
    out = {}  # type: Dict[str, int]
    for w in _WORD_RE.findall(line or ""):
        w = w.lower()
        out[w] = out.get(w, 0) + 1
    return out


def net_new_words(added: Iterable[str], removed: Iterable[str]) -> int:
    """Words added minus words removed (as multisets, per file): whitespace, a reflow, a case change, punctuation and a deletion add none."""
    a, r = {}, {}  # type: Dict[str, int], Dict[str, int]
    for ln in added:
        for w, n in words(ln).items():
            a[w] = a.get(w, 0) + n
    for ln in removed:
        for w, n in words(ln).items():
            r[w] = r.get(w, 0) + n
    return sum(max(0, n - r.get(w, 0)) for w, n in a.items())


def parse_diff(text: str) -> Dict[str, Dict[str, List[str]]]:
    """`git diff --unified=0` output -> {path: {added: [...], removed: [...]}}. A binary file, a mode change and a rename without content have no lines."""
    out = {}  # type: Dict[str, Dict[str, List[str]]]
    cur = None  # type: Optional[Dict[str, List[str]]]
    for ln in (text or "")[:MAX_DIFF_BYTES].split("\n"):
        if ln.startswith("diff --git "):
            cur = None
        elif ln.startswith("+++ "):
            p = ln[4:].strip()
            if p == "/dev/null":
                cur = None
            else:
                p = p[2:] if p.startswith("b/") else p
                p = p.strip('"')
                cur = out.setdefault(p, {"added": [], "removed": []})
        elif ln.startswith("--- "):
            continue
        elif cur is not None and ln.startswith("+"):
            cur["added"].append(ln[1:])
        elif cur is not None and ln.startswith("-"):
            cur["removed"].append(ln[1:])
    return out


def _tokens(res: Sequence[Any], lines: Iterable[str]) -> List[str]:
    out = []  # type: List[str]
    for ln in lines:
        if len(ln) > 2000:                               # a minified bundle: nothing to read, and no pattern should be run over it
            continue
        for rx in res:
            for m in rx.finditer(ln):
                out.append(m.group(1))
    return out


def _route_key(path: str) -> str:
    """`/api/tasks/:id` -> `/api/tasks`: the literal prefix before the first parameter, without a trailing slash."""
    for mark in (":", "<", "{", "[", "(", "?", "*", "$"):
        i = path.find(mark)
        if i != -1:
            path = path[:i]
    return path.rstrip("/") if len(path.rstrip("/")) >= 2 else path


def extract_triggers(code: Dict[str, Dict[str, List[str]]]) -> List[Dict[str, str]]:
    """New things worth documenting in the source changes: [{kind: env|route|flag, name, path}], first sighting only."""
    seen = set()
    out = []  # type: List[Dict[str, str]]
    for path in sorted(code):
        if not is_code_path(path):                       # tests, docs, the kit's files and lockfiles never raise a trigger, whoever passes them in
            continue
        ch = code[path]
        for kind, res, norm in (("env", _ENV_RES, lambda x: x), ("route", _ROUTE_RES, _route_key), ("flag", _FLAG_RES, lambda x: x)):
            was = {norm(t) for t in _tokens(res, ch["removed"])}
            for t in _tokens(res, ch["added"]):
                t = norm(t)
                if kind == "env" and (t in _EXEMPT_ENV or t.upper() != t):
                    continue
                if kind == "route" and len(t) < 2:
                    continue
                if t in was or (kind, t) in seen:
                    continue
                seen.add((kind, t))
                out.append({"kind": kind, "name": t, "path": path})
    return out[:40]


def documented_in(name: str, docs: Dict[str, Dict[str, List[str]]], kind: str = "env") -> Optional[str]:
    """The doc path whose ADDED lines state `name` (prose with at least MENTION_MIN_WORDS words; an env example file needs only the name), or None."""
    low = name.lower()
    for path in sorted(docs):
        for ln in docs[path]["added"]:
            if low not in ln.lower():
                continue
            if is_env_example(path) and kind == "env":
                return path
            if sum(words(ln).values()) >= MENTION_MIN_WORDS:
                return path
    return None


def parse_delta(report_text: str) -> Dict[str, Any]:
    """{present, text, declared, checked} for a report's `## Doc Delta`. `present` is False when the section is missing or only whitespace."""
    m = re.search(r"(?ms)^##\s*Doc Delta\s*\n(.*?)(?=^##\s|\Z)", report_text or "")
    body = m.group(1).strip() if m else ""
    if not body:
        return {"present": False, "text": "", "declared": False, "checked": False}
    first = body.split(None, 1)[0].strip(".:,;-").upper()
    return {"present": True, "text": body, "declared": first != "NONE", "checked": bool(re.search(r"(?im)^\s*checked:\s*\S", body))}


def evaluate(changes: Dict[str, Dict[str, List[str]]], deltas: Sequence[Dict[str, Any]], min_words: int = 5) -> Dict[str, Any]:
    """The gate's judgement. `changes` is parse_diff of base..head; `deltas` is [{name, present, text, declared, checked}] per engineer report.
    Returns {findings: [...], triggers: [...], documented: n, summary}. A finding has id, severity (error|warn|info), message, and for a trigger its name."""
    min_words = max(1, int(min_words))
    docs = {p: c for p, c in changes.items() if is_doc_path(p)}
    code = {p: c for p, c in changes.items() if is_code_path(p)}
    trig = extract_triggers(code)
    findings = []  # type: List[Dict[str, Any]]
    declared_text = " ".join(d["text"] for d in deltas if d.get("declared") and len(words(d["text"])) >= min_words).lower()
    n_doc = 0
    for t in trig:
        where = documented_in(t["name"], docs, t["kind"])
        if where:
            n_doc += 1
            findings.append({"id": "docs.documented", "severity": "info", "name": t["name"], "message": "%s %s is documented in %s" % (t["kind"], t["name"], where)})
        elif t["name"].lower() in declared_text:
            findings.append({"id": "docs.reported-not-done", "severity": "warn", "name": t["name"],
                             "message": "%s %s (added in %s) is named in a Doc Delta but not yet in any documentation file: someone still has to write it" % (t["kind"], t["name"], t["path"])})
        else:
            findings.append({"id": "docs.undeclared", "severity": "error", "name": t["name"], "reason": "doc_delta_missing",
                             "message": "%s %s is added in %s and neither a documentation file nor a Doc Delta says so" % (t["kind"], t["name"], t["path"])})
    if trig:
        if not deltas:
            findings.append({"id": "docs.no-report", "severity": "error", "reason": "doc_delta_missing",
                             "message": "the diff adds %d thing(s) to document and there is no engineer report with a Doc Delta" % len(trig)})
        for d in deltas:
            sev = "error" if trig else "warn"
            if not d.get("present"):
                findings.append({"id": "docs.no-delta", "severity": sev, "reason": "doc_delta_missing",
                                 "message": "report %s has no Doc Delta section, or it is only whitespace: that is not NONE" % common.clean_text(d.get("name"), 40)})
            elif not d.get("declared") and not d.get("checked"):
                findings.append({"id": "docs.unchecked", "severity": sev, "reason": "doc_delta_missing",
                                 "message": "report %s says NONE with no `checked:` line, and the diff adds something to document" % common.clean_text(d.get("name"), 40)})
    for p in sorted(docs):
        n = net_new_words(docs[p]["added"], docs[p]["removed"])
        if n < min_words and not is_env_example(p):
            findings.append({"id": "docs.insubstantial", "severity": "warn", "message": "%s changed but adds %d new word(s) (needs %d): a blank line, whitespace, a reflow, "
                                                                                      "punctuation or a deletion documents nothing and is ignored" % (p, n, min_words)})
    if not trig and any(d.get("declared") for d in deltas) and sum(net_new_words(c["added"], c["removed"]) for c in docs.values()) < min_words:
        findings.append({"id": "docs.declared-not-done", "severity": "warn",
                         "message": "a Doc Delta lists docs to change and the diff changes none of them substantively (fewer than %d new words): reported for follow-up" % min_words})
    errs = sum(1 for f in findings if f["severity"] == "error")
    summary = "%d trigger(s), %d documented, %d error(s)" % (len(trig), n_doc, errs)
    return {"findings": findings, "triggers": trig, "documented": n_doc, "summary": summary}
