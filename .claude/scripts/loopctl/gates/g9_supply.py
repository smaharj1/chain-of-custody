"""G9: supply chain (report section 5.5, section 9 G9, CO-13, CO-28).

What it does without the network: reads the dependency manifests at the base and at HEAD (`package.json`, `requirements*.txt`,
`pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`), and for every NEW top-level dependency checks

* the plan declared it (`touches.deps_change: true`; a dependency the plan did not announce is the classic way an agent adds a package);
* it comes from a registry by name, not from a git URL, a tarball URL or a local path (which skip every other check);
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
_LIFECYCLE = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack")
_LOCKS = {"npm": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
          "pypi": ("poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock"), "go": ("go.sum",), "cargo": ("Cargo.lock",), "ruby": ("Gemfile.lock",)}
_NON_REGISTRY = re.compile(r"^(?:git\+|git://|github:|gitlab:|bitbucket:|https?://|file:|link:|npm:|patch:|workspace:\*?$|\.{1,2}/|/|ssh://)", re.I)

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
    if re.match(r"^(?:requirements|constraints)[\w.-]*\.txt$", base) or base == "pyproject.toml":
        return "pypi"
    return {"go.mod": "go", "Cargo.toml": "cargo", "Gemfile": "ruby"}.get(base)


def _toml_tables(text: str) -> List[Tuple[str, str]]:
    """[(table name, body)] for a TOML file, good enough for dependency lists (no inline-table or array-of-tables semantics)."""
    out = []  # type: List[Tuple[str, str]]
    name, buf = "", []  # type: str, List[str]
    for ln in text.splitlines():
        m = re.match(r"^\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$", ln)
        if m:
            out.append((name, "\n".join(buf)))
            name, buf = m.group(1), []
        else:
            buf.append(ln)
    out.append((name, "\n".join(buf)))
    return out


def _toml_list(text: str, start: int) -> List[str]:
    """The strings of the array whose `[` is at text[start]; brackets inside quotes (extras such as `pkg[extra]`) do not count."""
    items = []  # type: List[str]
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = text.find(c, i + 1)
            if j < 0:
                break
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
                break
        i += 1
    return items


def _add_pep508(out: Dict[str, str], specs: List[str]) -> None:
    for s in specs:
        m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$", s)
        if m:
            out[m.group(1)] = m.group(2).split(";")[0].strip() or "*"


def parse_manifest(path: str, text: Optional[str]) -> Dict[str, str]:
    """{name: spec} of top-level dependencies. Unparsable text gives {} (the caller flags nothing it cannot read)."""
    if not text:
        return {}
    base = os.path.basename(path)
    out = {}  # type: Dict[str, str]
    if base == "package.json":
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            return {}
        for sect in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            v = doc.get(sect) if isinstance(doc, dict) else None
            if isinstance(v, dict):
                out.update({str(k): str(s) for k, s in v.items()})
    elif base.endswith(".txt"):
        for ln in text.splitlines():
            ln = ln.split("#")[0].strip()
            if not ln or ln.startswith(("-r", "-c", "--")):
                continue
            if ln.startswith("-e"):
                out[ln] = ln
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$", ln)
            if m:
                out[m.group(1)] = m.group(2).split(";")[0].strip() or "*"
            elif _NON_REGISTRY.match(ln):
                out[ln] = ln
    elif base == "pyproject.toml":
        for table, body in _toml_tables(text):
            if table == "project":
                for m in re.finditer(r"(?m)^dependencies\s*=\s*\[", body):
                    _add_pep508(out, _toml_list(body, m.end() - 1))
            elif table in ("project.optional-dependencies", "dependency-groups"):
                for m in re.finditer(r"(?m)^[\w.-]+\s*=\s*\[", body):
                    _add_pep508(out, _toml_list(body, m.end() - 1))
            elif table in ("tool.poetry.dependencies", "tool.poetry.dev-dependencies") or re.match(r"tool\.poetry\.group\.[\w-]+\.dependencies$", table):
                for m in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.+)$", body):
                    if m.group(1).lower() != "python":
                        out[m.group(1)] = m.group(2).strip()
    elif base == "go.mod":
        for blk in re.findall(r"(?ms)^require\s*\((.*?)^\)", text):
            for m in re.finditer(r"(?m)^\s*(\S+)\s+(v\S+)", blk):
                out[m.group(1)] = m.group(2)
        for m in re.finditer(r"(?m)^require\s+(\S+)\s+(v\S+)", text):
            out[m.group(1)] = m.group(2)
    elif base == "Cargo.toml":
        for tbl in re.findall(r"(?ms)^\[(?:dev-|build-)?dependencies\]\s*\n(.*?)(?=^\[|\Z)", text):
            for m in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9_-]*)\s*=\s*(.+)$", tbl):
                out[m.group(1)] = m.group(2).strip()
    elif base == "Gemfile":
        for m in re.finditer(r"(?m)^\s*gem\s+[\"']([^\"']+)[\"'](?:\s*,\s*(.*))?", text):
            out[m.group(1)] = (m.group(2) or "*").strip()
    return out


def _lifecycle(text: Optional[str]) -> List[str]:
    try:
        sc = json.loads(text or "{}").get("scripts")
    except (ValueError, AttributeError, RecursionError):
        return []
    return [k for k in _LIFECYCLE if isinstance(sc, dict) and k in sc]


def _dep_check(ctx: Any, cmd: str, eco: str, name: str) -> Optional[Dict[str, Any]]:
    stage, text, _f = _lib.run_command(ctx, "dep_check", cmd, {"LOOPCTL_DEP_ECOSYSTEM": eco, "LOOPCTL_DEP_NAME": name}, timeout_s=120, count_tests=False)
    if stage["status"] != "pass":
        return None
    try:
        doc = common.strict_loads(text.strip().splitlines()[-1] if text.strip() else "", max_depth=3, max_bytes=8192)
    except (ValueError, RecursionError, IndexError):
        return None
    return doc if isinstance(doc, dict) else None


def run(ctx: Any) -> GateResult:
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
    manifests = [(s, p) for s, p, _o in rows if manifest_kind(p) and s != "D"]
    for status, path in manifests:
        eco = manifest_kind(path) or ""
        before = parse_manifest(path, _lib.blob(ctx, ctx.base_sha, path)) if status != "A" else {}
        after = parse_manifest(path, _lib.blob(ctx, head, path))
        fresh = sorted(set(after) - set(before))
        if os.path.basename(path) == "package.json":
            old_t = _lib.blob(ctx, ctx.base_sha, path) if status != "A" else None
            add = [k for k in _lifecycle(_lib.blob(ctx, head, path)) if k not in _lifecycle(old_t)]
            if add:
                findings.append(_lib.finding("g9.install-script-added", "error", "%s adds the lifecycle script(s) %s: they run code on every install (use --ignore-scripts and "
                                             "an allowlist)" % (path, ", ".join(add)), path=path))
        if not fresh:
            continue
        d = os.path.dirname(path)
        locks = [os.path.join(d, n) if d else n for n in _LOCKS.get(eco, ())]
        pinned_txt = os.path.basename(path).endswith(".txt")
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
            if _NON_REGISTRY.match(spec) or _NON_REGISTRY.match(name):
                sev = "warn" if name in allow else "error"
                findings.append(_lib.finding("g9.non-registry-source", sev, "new dependency %s comes from %s, not from a registry: none of the registry checks apply to it"
                                             % (rec["name"], common.clean_text(spec, 50)), dependency=rec["name"]))
            if pinned_txt and not re.search(r"==\s*[0-9]", spec) and not _NON_REGISTRY.match(spec):
                findings.append(_lib.finding("g9.unpinned", "error", "new requirement %s is not pinned with == (frozen installs need an exact version)" % rec["name"]))
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
        if status == "D" or not (path.startswith((".github/workflows/", ".github/actions/")) or os.path.basename(path) in ("Makefile", "Dockerfile", ".npmrc")
                                 or path.endswith((".sh", ".yml", ".yaml"))):
            continue
        text = _lib.blob(ctx, head, path) or ""
        old = (_lib.blob(ctx, ctx.base_sha, path) or "") if status != "A" else ""
        if os.path.basename(path) == ".npmrc" and re.search(r"(?mi)^\s*ignore-scripts\s*=\s*false", text) and not re.search(r"(?mi)^\s*ignore-scripts\s*=\s*false", old):
            findings.append(_lib.finding("g9.scripts-reenabled", "error", ".npmrc re-enables install scripts (ignore-scripts=false)", path=path))
            continue
        bad_re = re.compile(r"(?m)^[^#\n]*\b(?:npm (?:install|i)\b|yarn install\b|pnpm (?:install|i)\b)(?![^\n]*(?:--ignore-scripts|--frozen-lockfile|--immutable))[^\n]*$")
        if len(bad_re.findall(text)) > len(bad_re.findall(old)):
            findings.append(_lib.finding("g9.install-hygiene", "warn", "%s installs dependencies without --ignore-scripts or a frozen lockfile" % path, path=path))
    ev = {"summary": "%d new dependenc%s%s" % (len(new_deps), "y" if len(new_deps) == 1 else "ies", " (unchecked)" if new_deps and not cmd else ""), "new_dependencies": new_deps}
    if any(f["severity"] == "error" for f in findings):
        return GateResult.failed(findings, **ev)
    return GateResult("pass", findings=findings, evidence=ev)
