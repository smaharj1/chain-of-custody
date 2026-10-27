"""Helpers shared by the gate modules (Phase 4b). Not a gate: the leading underscore keeps `gates.discover()` from loading it.

What lives here: the plan-declared inputs the gates read (`ext.g3`, `ext.g5`), the path sets (test files, protected files), git
reads against the base and head shas, the AC id patterns, a command runner built on `verify.py`, and small finding builders.

Every input comes from the plan at the BASE sha or the config at the BASE sha (CO-11); the only things read from the branch under
judgement are the files being judged, and those are read from git objects at `head_sha`, not from the working tree.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kitlib import gitutil, redact, runstate

from .. import common
safe_rel_path = common.safe_rel_path

__all__ = ["finding", "g3_spec", "g5_allow", "TEST_GLOBS", "PROTECTED_GLOBS", "is_test_path", "is_protected", "match_any", "diff_status",
           "changed_paths", "blob", "tree_files", "head_of", "ac_pattern", "ac_ids_of", "runstate_of", "run_command", "tail", "worst_class",
           "command_set", "git_ok", "is_binary", "footprint", "ere_for", "quote", "same_code", "review_globs", "worktree_head_problem", "uncommitted_changes", "command_files", "CONFIG_GLOBS", "SNAPSHOT_GLOBS", "tree_entries"]


def finding(fid: str, severity: str, message: str, **extra: Any) -> Dict[str, Any]:
    d = {"id": fid, "severity": severity, "message": common.clean_text(redact.redact(message), 400)}  # type: Dict[str, Any]
    d.update(extra)
    return d


# -- paths ---------------------------------------------------------------------------------------------------------------------

# Test files and the places tests live, at any depth (report section 9 G5). New files count: an engineer adding a test is not the same as
# the acceptance-author's pinned tests, and the plan item's footprint says which new tests are expected.
TEST_GLOBS = ["**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**", "**/specs/**", "**/e2e/**", "**/acceptance/**", "**/cypress/**",
              "**/*.test.*", "**/*.spec.*", "**/test_*.py", "**/*_test.py", "**/*_test.go", "**/*_spec.rb", "**/*Test.java", "**/*Tests.java",
              "**/*Test.kt", "**/*_test.rs", "**/*.t.sol"]
# Files that decide what a test run means (config, environment, CI, lint): a change here weakens the gate without touching a test.
CONFIG_GLOBS = ["conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "package.json", "jest.config.*", "vitest.config.*",
                "vitest.workspace.*", "playwright.config.*", "karma.conf.*", ".mocharc*", "cypress.config.*", "babel.config.*", ".babelrc*",
                "tsconfig*.json", "noxfile.py", "Makefile", ".env*", ".envrc", ".github/workflows/**", ".github/actions/**", ".gitlab-ci.yml",
                ".circleci/**", "Jenkinsfile", "azure-pipelines.yml", ".eslintrc*", "eslint.config.*", ".prettierrc*", "ruff.toml", ".ruff.toml",
                ".flake8", ".pylintrc", "mypy.ini", ".pre-commit-config.yaml", ".husky/**", "pnpm-workspace.yaml", ".npmrc", ".gitattributes", ".coveragerc", "codecov.yml", ".claude/**", "plan/**", "CODEOWNERS", ".github/**"]
SNAPSHOT_GLOBS = ["**/__snapshots__/**", "**/snapshots/**", "**/*.snap", "**/*.snapshot"]
PROTECTED_GLOBS = TEST_GLOBS + CONFIG_GLOBS + SNAPSHOT_GLOBS

_rx_cache = {}  # type: Dict[str, Any]


def _rx(glob: str) -> Any:
    rx = _rx_cache.get(glob)
    if rx is None:
        import pr_prepare
        rx = pr_prepare.glob_to_regex(glob)
        _rx_cache[glob] = rx
    return rx


def match_any(globs: Iterable[str], path: str) -> bool:
    return any(_rx(g).match(path) for g in globs)


def is_test_path(path: str) -> bool:
    return match_any(TEST_GLOBS, path)


def is_protected(path: str) -> bool:
    return match_any(PROTECTED_GLOBS, path)


# -- the plan item's gate inputs (ext, read from the plan at the base sha) ---------------------------------------------------------------

_G3_KEYS = {"visible", "stubs", "cmd", "class", "heldout", "exempt", "junit"}


def g3_spec(item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """`ext.g3` of a plan item, normalised, with the problems found (a bad declaration is a finding, never silently ignored).

        visible    globs of the acceptance-author's visible tests (they ride in the plan PR, so they exist at the base sha)
        stubs      paths of the interface stubs the acceptance-author commits first (G2)
        cmd        command that runs the visible tests (default: the item's verify.tests)
        class      behaviour (default) | characterization (a refactor: green on both sides)
        heldout    true when the plan says held-out tests exist for this item
        exempt     a reason: the item has no executable acceptance tests (docs, infra); G2 and G3 pass with a human flag
        junit      repo-relative JUnit XML the command writes (optional)
    """
    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    raw = ext.get("g3") if isinstance(ext.get("g3"), dict) else {}
    problems = []  # type: List[Dict[str, Any]]
    spec = {"visible": [], "stubs": [], "cmd": None, "class": "behaviour", "heldout": False, "exempt": None, "junit": None, "declared": bool(raw)}
    for k in raw:
        if k not in _G3_KEYS:
            problems.append(finding("g3.ext-unknown-key", "error", "plan item %s ext.g3 has an unknown key %r" % (item.get("id"), k)))
    for k in ("visible", "stubs"):
        v = raw.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append(finding("g3.ext-type", "error", "ext.g3.%s must be a list of paths or globs" % k))
            continue
        good = [x for x in v if common.safe_glob(x)]
        if len(good) != len(v):
            problems.append(finding("g3.ext-unsafe-path", "error", "ext.g3.%s names a path that is not a safe repo-relative glob" % k))
        spec[k] = good
    cmd = raw.get("cmd")
    if cmd is None:
        v = item.get("verify") if isinstance(item.get("verify"), dict) else {}
        cmd = v.get("tests")
    spec["cmd"] = cmd.strip() if isinstance(cmd, str) and cmd.strip() else None
    cls = raw.get("class", "behaviour")
    if cls not in ("behaviour", "characterization"):
        problems.append(finding("g3.ext-class", "error", "ext.g3.class must be behaviour or characterization"))
        cls = "behaviour"
    spec["class"] = cls
    spec["heldout"] = raw.get("heldout") is True
    ex = raw.get("exempt")
    spec["exempt"] = ex.strip() if isinstance(ex, str) and ex.strip() else None
    if ex is not None and spec["exempt"] is None:
        problems.append(finding("g3.ext-exempt", "error", "ext.g3.exempt must be a non-empty reason"))
    j = raw.get("junit")
    if j is not None:
        if common.safe_rel_path(j):
            spec["junit"] = j
        else:
            problems.append(finding("g3.ext-unsafe-path", "error", "ext.g3.junit is not a safe repo-relative path"))
    return spec, problems


def g5_allow(item: Dict[str, Any]) -> List[str]:
    """Globs the plan lets this item edit even though they are test or config files (`ext.g5.allow`, reviewed in the plan PR)."""
    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    g5 = ext.get("g5") if isinstance(ext.get("g5"), dict) else {}
    allow = g5.get("allow")
    return [a for a in allow if isinstance(a, str) and common.safe_glob(a)] if isinstance(allow, list) else []


def footprint(item: Dict[str, Any]) -> List[str]:
    t = item.get("touches")
    if isinstance(t, dict) and isinstance(t.get("paths"), list):
        return [g for g in t["paths"] if isinstance(g, str) and common.safe_glob(g)]
    return []


# -- git reads ---------------------------------------------------------------------------------------------------------------------

def _memo(ctx: Any, key: Any, fn: Any) -> Any:
    """Per-run memo on the GateContext (one is built for each gate run, so nothing outlives the run): the gates ask the same git questions."""
    cache = ctx.__dict__.setdefault("_gate_memo", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def head_of(ctx: Any) -> Optional[str]:
    h = ctx.head_sha
    return h if isinstance(h, str) and gitutil.valid_oid(h) else None


def git_ok(ctx: Any, args: Sequence[str], **kw: Any) -> Optional[str]:
    """stdout of a git command in the worktree, or None when it failed."""
    proc = ctx.git(list(args), **kw)
    return proc.stdout if proc.returncode == 0 else None


def diff_status(ctx: Any, renames: bool = False, untracked: bool = True) -> List[Tuple[str, str, Optional[str]]]:
    """[(status, path, old_path)] for base..head. status is A, M, D, R, T. Untracked files in the worktree count as added: a new test
    file that was never committed is still a new test file."""
    return list(_memo(ctx, ("diff", renames, untracked), lambda: _diff_status(ctx, renames, untracked)))


def _diff_status(ctx: Any, renames: bool, untracked: bool) -> List[Tuple[str, str, Optional[str]]]:
    head = head_of(ctx)
    if head is None:
        raise ValueError("there is no head commit")
    args = ["diff", "--name-status", "-z", "--no-ext-diff", "--no-textconv", "-M" if renames else "--no-renames", ctx.base_sha, head, "--"]
    out = git_ok(ctx, args)
    if out is None:
        raise ValueError("git diff base..head failed")
    toks = [t for t in out.split("\0")]
    rows = []  # type: List[Tuple[str, str, Optional[str]]]
    i = 0
    while i < len(toks) and toks[i]:
        st = toks[i]
        if st[0] in "RC":
            if i + 2 >= len(toks):
                break
            rows.append(("R", toks[i + 2], toks[i + 1]))
            i += 3
        else:
            if i + 1 >= len(toks):
                break
            rows.append((st[0], toks[i + 1], None))
            i += 2
    if untracked and ctx.worktree:
        extra = git_ok(ctx, ["ls-files", "--others", "--exclude-standard", "-z"])
        seen = {p for _s, p, _o in rows}
        for p in (extra or "").split("\0"):
            if p and p not in seen and common.safe_rel_path(p):
                rows.append(("A", p, None))
    return rows


def changed_paths(ctx: Any) -> List[str]:
    return [p for _s, p, _o in diff_status(ctx)]


def blob(ctx: Any, rev: Optional[str], path: str, limit: int = 1 << 20) -> Optional[str]:
    """File content at a revision (None when absent or when the path is not a safe relative path). Capped at `limit` bytes."""
    if not rev or not common.safe_rel_path(path):
        return None
    text = _memo(ctx, ("blob", rev, path), lambda: gitutil.show_file(rev, path, ctx.worktree, isolated=True))
    if text is None:
        return None
    return text[:limit]


_COMMAND_KEYS = ("verify_build", "verify_lint", "verify_typecheck", "verify_tests", "verify_dev", "verify_run", "env_check_cmd", "migrate_check_cmd",
                 "dep_check_cmd", "secrets_scan_cmd", "mutation_cmd", "heldout_cmd", "db_ephemeral", "db_migrate_down")


def command_files(ctx: Any) -> List[str]:
    """Files at the base that the configured commands run (`scripts/test.sh`, `python3 scripts/verify_run.py`, `python -m pkg.runner`). Editing
    one changes what a gate means without touching a test, so G5 treats them as test configuration."""
    cmds = []  # type: List[str]
    for k in _COMMAND_KEYS:
        v = ctx.cfg.get(k)
        if isinstance(v, str) and v.strip() and v.strip() != "<command>":
            cmds.append(v)
    v = ctx.item.get("verify")
    if isinstance(v, dict) and isinstance(v.get("tests"), str):
        cmds.append(v["tests"])
    spec = g3_spec(ctx.item)[0]
    if spec["cmd"]:
        cmds.append(spec["cmd"])
    tree = tree_entries(ctx, ctx.base_sha)
    found = []  # type: List[str]
    for cmd in cmds:
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        prev = ""
        for tok in toks:
            cand = [tok] if safe_rel_path(tok) else []
            if prev == "-m" and re.match(r"^[A-Za-z_][\w.]*$", tok):
                base = tok.replace(".", "/")
                cand += [base + ".py", base + "/__init__.py", base + "/__main__.py"]
            for c in cand:
                if c in tree and c not in found:
                    found.append(c)
            prev = tok
    return found


def worktree_head_problem(ctx: Any) -> Optional[str]:
    """Why a gate that RUNS code cannot vouch for `head_sha`: the worktree is at another commit. (The gates that read git objects do not care.)"""
    head = head_of(ctx)
    real = gitutil.rev_parse("HEAD^{commit}", ctx.worktree, isolated=True)
    if head is None or real is None or head != real:
        return "the worktree is at %s but the gate was asked to judge %s: a run there says nothing about that commit" % (
            (real or "no commit")[:12], (head or "no commit")[:12])
    return None


def uncommitted_changes(ctx: Any) -> List[str]:
    """Tracked files that differ from HEAD in the worktree. A gate that runs code would be judging them, and HEAD does not hold them. Saved
    reports under the item's reports directory are excepted; untracked files are G5's and G11's business."""
    out = git_ok(ctx, ["status", "--porcelain=v1", "-z", "--untracked-files=no"])
    if out is None:
        return ["<git status failed>"]
    prefixes = ("docs/features/%s/" % ctx.item.get("slug", ""), ".claude/pr/")
    return [rec[3:] for rec in out.split("\0") if len(rec) > 3 and not rec[3:].startswith(prefixes)]


def review_globs(ctx: Any) -> List[str]:
    """The saved reviews of the item. They are pinned from the start of the item (G0 and G7 add them to locked_paths.json) so that the
    engineer, who can write its own report in the same directory, cannot write the reviewer's verdict."""
    prefix = "docs/features/%s/reports/" % ctx.item.get("slug", "")
    return [prefix + n for n in ("code-review.md", "security-review.md", "design-review.md")]


def same_code(ctx: Any, old: Optional[str], new: Optional[str]) -> bool:
    """True when `new` differs from `old` only in the item's reports directory (saving a review or an engineer report after a gate ran is not a
    code change, so a pass recorded for `old` still speaks for `new`)."""
    if not old or not new:
        return False
    if old == new:
        return True
    if not gitutil.valid_oid(old) or not gitutil.valid_oid(new):
        return False
    out = git_ok(ctx, ["diff", "--name-only", "-z", "--no-renames", old, new, "--"])
    if out is None:
        return False
    prefix = "docs/features/%s/reports/" % ctx.item.get("slug", "")
    return all(p.startswith(prefix) for p in out.split("\0") if p)


def is_binary(text: str) -> bool:
    return "\x00" in text[:4096]


def tree_entries(ctx: Any, rev: Optional[str]) -> Dict[str, str]:
    """{path: blob id} for every file at a revision."""
    return dict(_memo(ctx, ("tree", rev), lambda: _tree_entries(ctx, rev)))


def _tree_entries(ctx: Any, rev: Optional[str]) -> Dict[str, str]:
    if not rev:
        return {}
    out = git_ok(ctx, ["ls-tree", "-r", "-z", rev]) or ""
    res = {}  # type: Dict[str, str]
    for rec in out.split("\0"):
        meta, _tab, path = rec.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[1] == "blob":
            res[path] = parts[2]
    return res


def tree_files(ctx: Any, rev: Optional[str]) -> List[str]:
    return list(tree_entries(ctx, rev))


# -- acceptance criteria ids ----------------------------------------------------------------------------------------------------------

def ac_ids_of(item: Dict[str, Any]) -> List[str]:
    a = item.get("acceptance")
    return [x["id"] for x in a if isinstance(x, dict) and isinstance(x.get("id"), str)] if isinstance(a, list) else []


def ac_pattern(ac_id: str) -> Any:
    """A regex finding `AC-F2-1` however a test runner spells it: `AC-F2-1`, `ac_f2_1`, `AC F2 1`. Separators are required, so `AC-F2-1`
    never matches `AC-F2-10` or `AC-F21-1`. A `Test` prefix is allowed (`TestAC_F2_1`, a Go test name or a class)."""
    m = common.AC_RE.match(ac_id)
    if not m:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?:(?<![A-Za-z0-9])|(?<=Test)|(?<=test))ac[-_ ]%s[-_ ]%s(?![0-9])" % (re.escape(m.group(1)), m.group(2)), re.I)


def ere_for(ac_id: str) -> str:
    m = common.AC_RE.match(ac_id)
    return "(^|[^A-Za-z0-9])ac[-_ ]%s[-_ ]%s([^0-9]|$)" % (m.group(1), m.group(2)) if m else "(^)(?!x)x"


# -- running commands (the process machinery is verify.py's) ----------------------------------------------------------------------------

def _verify() -> Any:
    import verify
    return verify


def run_command(ctx: Any, name: str, command: str, extra_env: Optional[Dict[str, str]] = None, timeout_s: Optional[int] = None,
                count_tests: bool = True, cwd: Optional[str] = None) -> Tuple[Dict[str, Any], str, List[Dict[str, str]]]:
    """Run one configured command the way verify.py runs a stage: its own process group, an allowlisted environment (no credential
    ever reaches test code), killed on timeout, leaks reported. Returns (stage, full output text, verify findings)."""
    v = _verify()
    d = v.Definition()
    d.repo = cwd or ctx.worktree
    d.item = ctx.item_id
    try:
        d.timeout_s = int(timeout_s or ctx.cfg.get_int("verify_timeout_s"))
    except Exception:  # noqa: BLE001
        d.timeout_s = 600
    d.env_allow = [str(n) for n in (ctx.cfg.get_list("verify_env_allow") or [])]
    d.extra_env = dict(extra_env or {})
    env = v.build_env(d)
    findings = []  # type: List[Dict[str, str]]
    stage, text = v.run_command_stage(d, name, command, env, count_tests, findings)
    return stage, text, findings


def tail(text: str, n: int = 600) -> str:
    return common.clean_text(redact.redact(text[-n:]), n)


def worst_class(classes: Iterable[str]) -> str:
    """One failure class for a result built from several problems: env beats platform beats transient beats item."""
    order = ("env", "platform", "transient", "item")
    cs = set(classes)
    for c in order:
        if c in cs:
            return c
    return "item"


def runstate_of(ctx: Any) -> Any:
    """The RunState for the repository (`<git-common-dir>/loopctl`)."""
    root = ctx.paths["runstate"]
    return runstate.RunState(os.path.dirname(root))


def command_set(cfg: Any, key: str) -> Optional[str]:
    """A configured command, or None when it is empty or still the `<command>` placeholder."""
    v = cfg.get(key)
    if not isinstance(v, str) or not v.strip() or v.strip() == "<command>":
        return None
    return v.strip()


def quote(s: str) -> str:
    return shlex.quote(s)
