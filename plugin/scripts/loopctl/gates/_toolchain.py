"""Toolchain pins against the installed tools (G0): `.nvmrc`, `.node-version`, `.python-version`, `.tool-versions`, `package.json` engines,
`pyproject.toml` requires-python. The Node-v25 class of failure: the repo pins one runtime, the machine has another, and every test result
means something different (report section 9 G0).

Pins are read from the BASE sha. Installed versions come from `<tool> --version` with a fixed argv and the environment the gate was given.
A pin this module cannot parse is skipped with a note, never guessed.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["Pin", "read_pins", "installed_version", "satisfies", "check"]

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_TOOLS = {"node": (["node", "--version"], "node"), "python": (["python3", "--version"], "python"), "ruby": (["ruby", "--version"], "ruby"),
          "go": (["go", "version"], "go")}
_ALIASES = {"nodejs": "node", "node": "node", "python": "python", "python3": "python", "ruby": "ruby", "golang": "go", "go": "go"}


class Pin(object):
    def __init__(self, tool: str, spec: str, source: str) -> None:
        self.tool, self.spec, self.source = tool, spec, source

    def __repr__(self) -> str:
        return "Pin(%s %s from %s)" % (self.tool, self.spec, self.source)


def _vt(text: str) -> Optional[Tuple[int, int, int]]:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def _parts(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"\d+", text.split("-")[0])][:3]


def satisfies(installed: str, spec: str) -> Optional[bool]:
    """Does `installed` meet `spec`? `spec` is a prefix pin (`20`, `20.11`, `v20.11.0`) or a semver-ish range (`>=3.9`, `^20.1`, `~1.2.3`,
    `>=18 <23`, `20.x || 22.x`). None when the spec is not understood."""
    spec = spec.strip()
    inst = _vt(installed)
    if inst is None or not spec:
        return None
    if "||" in spec:
        results = [satisfies(installed, s) for s in spec.split("||")]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    toks = spec.replace(",", " ").split()
    if len(toks) > 1 and all(re.match(r"^(?:[<>]=?|=|\^|~)", t) or re.match(r"^v?\d", t) for t in toks):
        # `>= 3.9` written with a space, or two comparators
        merged = []  # type: List[str]
        for t in toks:
            if merged and re.match(r"^(?:[<>]=?|=|\^|~)$", merged[-1]):
                merged[-1] += t
            else:
                merged.append(t)
        results = [satisfies(installed, t) for t in merged]
        if any(r is None for r in results):
            return None
        return all(results)
    m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*v?(\d+(?:\.(?:\d+|x|\*)){0,2}|\d+)\s*$", spec, re.I)
    if not m:
        return None
    op, ver = m.group(1) or "", m.group(2)
    want = _parts(ver.replace("x", "0").replace("*", "0"))
    while len(want) < 3:
        want.append(0)
    have = list(inst)
    nparts = len([p for p in ver.split(".") if p not in ("x", "*")])
    if op == "":
        return have[:nparts] == want[:nparts]
    if op == "=":
        return have[:nparts] == want[:nparts]
    if op == ">=":
        return have >= want
    if op == ">":
        return have > want
    if op == "<=":
        return have <= want
    if op == "<":
        return have < want
    if op == "^":
        if want[0] > 0:
            return have >= want and have[0] == want[0]
        if want[1] > 0:
            return have >= want and have[0] == 0 and have[1] == want[1]
        return have == want
    if op == "~":
        return have >= want and have[0] == want[0] and have[1] == want[1]
    return None


def read_pins(read: Callable[[str], Optional[str]]) -> List[Pin]:
    """Pins found in the repo. `read(path)` returns a file's text at the base sha, or None."""
    pins = []  # type: List[Pin]
    for fn, tool in ((".nvmrc", "node"), (".node-version", "node"), (".python-version", "python"), (".ruby-version", "ruby")):
        text = read(fn)
        if text:
            first = text.strip().splitlines()[0].strip() if text.strip() else ""
            if re.match(r"^v?\d", first):
                pins.append(Pin(tool, first.lstrip("v"), fn))
    tv = read(".tool-versions")
    if tv:
        for ln in tv.splitlines():
            parts = ln.split("#")[0].split()
            if len(parts) >= 2 and parts[0] in _ALIASES and re.match(r"^v?\d", parts[1]):
                pins.append(Pin(_ALIASES[parts[0]], parts[1].lstrip("v"), ".tool-versions"))
    pj = read("package.json")
    if pj:
        try:
            eng = json.loads(pj).get("engines")
        except (ValueError, AttributeError, RecursionError):
            eng = None
        if isinstance(eng, dict) and isinstance(eng.get("node"), str):
            pins.append(Pin("node", eng["node"], "package.json engines.node"))
    pp = read("pyproject.toml")
    if pp:
        m = re.search(r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']", pp)
        if m:
            pins.append(Pin("python", m.group(1), "pyproject.toml requires-python"))
    return pins


def installed_version(tool: str, env: Dict[str, str], run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    if tool not in _TOOLS:
        return None
    argv = _TOOLS[tool][0]
    try:
        proc = run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, timeout=15, env=dict(env), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()[:200]


def check(pins: List[Pin], env: Dict[str, str], run: Callable[..., Any] = subprocess.run) -> List[Dict[str, str]]:
    """[{"level": "error"|"info", "pin", "message"}] for every pin: a mismatch or a missing tool is an error."""
    out = []  # type: List[Dict[str, str]]
    cache = {}  # type: Dict[str, Optional[str]]
    for p in pins:
        if p.tool not in cache:
            cache[p.tool] = installed_version(p.tool, env, run)
        have = cache[p.tool]
        label = "%s %s (%s)" % (p.tool, p.spec, p.source)
        if have is None:
            out.append({"level": "error", "pin": label, "message": "%s is pinned but the tool is not installed (or not on PATH)" % p.tool})
            continue
        ok = satisfies(have, p.spec)
        if ok is None:
            out.append({"level": "info", "pin": label, "message": "the pin %r is not a form this check understands; it was not compared" % p.spec})
        elif not ok:
            out.append({"level": "error", "pin": label, "message": "pinned %s %s but the installed version is %s" % (p.tool, p.spec, have.splitlines()[0][:60])})
    return out
