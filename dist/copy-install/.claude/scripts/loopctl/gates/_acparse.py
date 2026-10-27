"""Per-acceptance-criterion results out of a test run (G1, G2, G3, G6).

A test run proves a criterion only if a test that names the criterion's id (`AC-F2-1`) ran and passed. Three sources are read:

1. a JSON object printed by the harness: `{"results": {"AC-F2-1": "pass"}}` or `{"results": {"F2": {"AC-F2-1": "pass"}}}`
   (the shape `examples/hello-loop/acceptance/run_checks.py --json` prints);
2. a JUnit XML file (`<testcase name=... classname=...>` with `<failure>`, `<error>` or `<skipped>` children);
3. verbose runner output, one line per test: unittest `-v`, pytest `-v`/`-rA`, go test `-v`, TAP, cargo test, jest/vitest verbose.

A verdict is `pass`, `fail` (an assertion failed: the test is RED), `error` (the test could not run: an import or collection error, which
is INVALID rather than red, report section 9 G3) or `skipped` (skip, xfail, todo: never a pass). A criterion with no test line is simply
absent from the result, which every caller treats as "not executed".

Text parsing is a heuristic, and it can be spoofed by code that prints a runner-looking line. That is why a pass here is never the only
evidence: the criterion's test must also exist in a pinned file (G1), and CI re-runs the tests on the merge-candidate tree (Evidence).
A parse that misses a line produces a false FAIL, never a false pass.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from . import _lib

__all__ = ["Parsed", "Allowed", "parse", "parse_junit", "parse_json_results", "VERDICTS", "merge_verdict"]

VERDICTS = ("pass", "fail", "error", "skipped")
_MAX_TEXT = 2 * 1024 * 1024

_INVALID_RE = re.compile(r"ERROR collecting|ImportError while importing|ModuleNotFoundError|Cannot find module|^\s*SyntaxError|"
                         r"error\[E\d+\]|cannot find package|build failed|undefined: |NameError|No module named|^ImportError\b|_FailedTest|cannot import name", re.M)
_ASSERT_RE = re.compile(r"AssertionError|^E\s+assert |^FAIL: |\bExpected\b.*\bReceived\b|expected .* (?:got|but)|assertion failed|--- FAIL", re.M | re.I)

_SKIP_WORDS = re.compile(r"\.\.\. (?:skipped|expected failure)|\bSKIPPED\b|\bXFAIL\b|\bXPASS\b|\bSKIP\b|#\s*(?:SKIP|TODO)|\bignored$|^\s*[○↓]\s|\(skipped\)", re.I)


class Parsed(object):
    """`results`: {ac_id: {"verdict", "tests": [names]}}; `invalid`: the output shows import or collection errors."""

    def __init__(self) -> None:
        self.results = {}  # type: Dict[str, Dict[str, Any]]
        self.invalid = False
        self.source = "text"

    def verdict(self, ac: str) -> Optional[str]:
        r = self.results.get(ac)
        return r["verdict"] if r else None

    def tests(self, ac: str) -> List[str]:
        r = self.results.get(ac)
        return list(r["tests"]) if r else []

    def add(self, ac: str, verdict: str, test: str = "") -> None:
        r = self.results.setdefault(ac, {"verdict": verdict, "tests": []})
        r["verdict"] = merge_verdict(r["verdict"], verdict)
        if test and test not in r["tests"] and len(r["tests"]) < 8:
            r["tests"].append(test)


_RANK = {"pass": 1, "skipped": 0, "error": 3, "fail": 2}


def merge_verdict(a: str, b: str) -> str:
    """Several tests name one criterion: any error beats any fail beats a pass; a pass beats only a skip."""
    if a == b:
        return a
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def _norm_verdict(v: Any) -> Optional[str]:
    if isinstance(v, bool):
        return "pass" if v else "fail"
    if isinstance(v, dict):
        v = v.get("result", v.get("verdict", v.get("status")))
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if s in ("pass", "passed", "ok", "green", "success"):
        return "pass"
    if s in ("fail", "failed", "red", "failure"):
        return "fail"
    if s in ("error", "errored", "invalid"):
        return "error"
    if s.startswith("skip") or s in ("xfail", "todo", "pending", "ignored"):
        return "skipped"
    if s.startswith("error"):
        return "error"
    return "fail" if s else None        # a harness that prints a failure message instead of a verdict word is saying it failed: only the exact pass words pass


class Allowed(object):
    """Which test lines may speak for a criterion: the tests the pinned acceptance files DEFINE (`names`) in the modules or files they live in
    (`modules`). A test the engineer added elsewhere that merely repeats a criterion id in its name or docstring is not the acceptance test."""

    def __init__(self, names: Iterable[str], modules: Iterable[str]) -> None:
        self.names = sorted({n for n in names if n})
        self.modules = sorted({m for m in modules if m})

    def __bool__(self) -> bool:
        return bool(self.names)

    def admits(self, line: str) -> bool:
        if not self.names or not any(n in line for n in self.names):
            return False
        names_a_module = re.search(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b|[\w./-]+\.(?:py|js|ts|jsx|tsx|go|rs|rb)\b", line)
        if names_a_module and self.modules:
            return any(m in line for m in self.modules)
        return True


def parse_json_results(text: str, ac_ids: Iterable[str]) -> Optional[Dict[str, str]]:
    """The first JSON object in the output that carries `results`. None when there is none."""
    wanted = set(ac_ids)
    dec = json.JSONDecoder()
    for m in re.finditer(r"(?m)^\s*\{", text):
        try:
            obj, _end = dec.raw_decode(text[m.end() - 1:])
        except (ValueError, RecursionError):
            continue
        res = obj.get("results") if isinstance(obj, dict) else None
        if not isinstance(res, dict):
            continue
        flat = {}  # type: Dict[str, str]
        for k, v in res.items():
            if isinstance(v, dict) and not {"result", "verdict", "status"} & set(v):
                for k2, v2 in v.items():
                    nv = _norm_verdict(v2)
                    if nv and k2 in wanted:
                        flat[k2] = nv
            else:
                nv = _norm_verdict(v)
                if nv and k in wanted:
                    flat[k] = nv
        return flat
    return None


def parse_junit(xml_text: str, ac_ids: Iterable[str], allowed: Optional["Allowed"] = None) -> Optional[Parsed]:
    """JUnit XML. Refuses a DOCTYPE (entity expansion) and anything over 2 MiB; None when it does not parse."""
    if len(xml_text) > _MAX_TEXT or "<!DOCTYPE" in xml_text[:2000].upper() or "<!ENTITY" in xml_text.upper():
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001 - ParseError and friends
        return None
    pats = {a: _lib.ac_pattern(a) for a in ac_ids}
    out = Parsed()
    out.source = "junit"
    for tc in root.iter("testcase"):
        name = "%s::%s" % (tc.get("classname") or "", tc.get("name") or "")
        verdict = "pass"
        if tc.find("error") is not None:
            verdict = "error"
        elif tc.find("failure") is not None:
            msg = " ".join(((tc.find("failure").get("type") or ""), (tc.find("failure").get("message") or "")))
            verdict = "fail" if re.search(r"assert|expect", msg, re.I) or not msg.strip() else "error"
        elif tc.find("skipped") is not None:
            verdict = "skipped"
        if allowed and not allowed.admits(name):
            continue
        for ac, rx in pats.items():
            if rx.search(name):
                out.add(ac, verdict, name.strip(":"))
    return out


# -- verbose text ---------------------------------------------------------------------------------------------------------------------

def _line_verdict(line: str) -> Optional[str]:
    s = line.rstrip()
    if _SKIP_WORDS.search(s):
        return "skipped"
    if re.search(r"\.\.\. FAIL\s*$|\.\.\. unexpected success|^FAIL: |^--- FAIL|\bFAILED\b|^not ok\b|\.\.\. FAILED$|^\s*[✕×✗]", s):
        return "fail"
    if re.search(r"\.\.\. ERROR\s*$|^ERROR: |^ERROR\b|\bERROR\b\s*$", s):
        return "error"
    if re.search(r"\.\.\. ok\s*$|\bPASSED\b|^--- PASS|^ok \d+|^\s*[✓√✔]|\.\.\. ok\b", s):
        return "pass"
    return None


def _join_headers(lines: List[str]) -> List[str]:
    """unittest prints `test_x (mod.T)` and then `docstring ... ok` on the next line: one logical line."""
    out = []  # type: List[str]
    i = 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\S+ \([\w.]+\)\s*$", ln) and i + 1 < len(lines) and "..." in lines[i + 1]:
            out.append(ln + " :: " + lines[i + 1])
            i += 2
        else:
            out.append(ln)
            i += 1
    return out


_RED_EXC = ("NotImplementedError", "AssertionError", "assert ", "AssertionError:")


def _red_names(text: str) -> set:
    """Names of unittest tests whose ERROR or FAIL block ends in a RED exception: an assertion, or NotImplementedError (the stub's own
    signal, which is what a raising stub is for). Any other exception (ImportError, AttributeError on a missing stub, TypeError from a
    wrong signature) means the test could not run: invalid, not red."""
    names = set()
    for block in re.split(r"(?m)^={20,}\s*$", text):
        m = re.match(r"\s*(?:ERROR|FAIL): (\S+) \(", block)
        if not m:
            continue
        body = re.sub(r"(?s)\n-{20,}\s*\nRan \d+ tests?.*\Z", "", block)      # the run summary after the last block
        # the last line of the traceback is the exception
        lines = [ln for ln in body.strip().splitlines() if ln.strip()]
        last = lines[-1].strip() if lines else ""
        if last.startswith(_RED_EXC) or last.endswith(("NotImplementedError", "AssertionError")):
            names.add(m.group(1))
    return names


def parse(text: str, ac_ids: Iterable[str], allowed: Optional["Allowed"] = None) -> Parsed:
    """Results for the criteria named in `ac_ids`, from a JSON verdict object when the output has one (the harness vouches for its own
    verdicts), else from verbose lines, restricted to the tests `allowed` admits when it is given."""
    ids = list(ac_ids)
    out = Parsed()
    text = text[-_MAX_TEXT:] if isinstance(text, str) else ""
    js = parse_json_results(text, ids)
    if js is not None:
        out.source = "json"
        for ac, v in js.items():
            out.add(ac, v, "harness")
        return out
    out.invalid = bool(_INVALID_RE.search(text))
    pats = [(a, _lib.ac_pattern(a)) for a in ids]
    red = _red_names(text)
    for ln in _join_headers(text.splitlines()):
        if len(ln) > 2000:
            ln = ln[:2000]
        hits = [a for a, rx in pats if rx.search(ln)]
        if not hits:
            continue
        if allowed and not allowed.admits(ln):
            continue
        v = _line_verdict(ln)
        if v is None:
            continue
        if v == "fail" and re.search(r"\bFAILED\b", ln):
            # pytest: `FAILED path::test - ImportError: ...` is an error, `- AssertionError`/`- assert ...`/`- NotImplementedError` is a red test
            m = re.search(r" - (.*)$", ln)
            if m and not re.match(r"(?:Assertion|assert\b|Failed:|AssertionError|NotImplementedError)", m.group(1)):
                v = "error"
        if v == "error":
            nm = re.match(r"^\s*(?:(?:ERROR|FAIL): )?(\S+) \(", ln)
            if nm and nm.group(1) in red:
                v = "fail"
        for a in hits:
            out.add(a, v, re.sub(r"\s+", " ", ln.strip())[:120])
    if out.invalid and not _ASSERT_RE.search(text):
        for r in out.results.values():          # an import error and no assertion failure anywhere: nothing here is a red test
            if r["verdict"] == "fail":
                r["verdict"] = "error"
    return out
