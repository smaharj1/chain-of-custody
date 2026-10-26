"""Path normalisation and the protected-path tables (report §6.1 H6, §7.1 guard-main-edit v2, §5.9).

A guard that compares strings loses to `./.claude/../.claude/settings.json`, to a symlink, to
`.CLAUDE/Settings.json` on a case-insensitive macOS volume, and to a worktree path that is not under
`$CLAUDE_PROJECT_DIR`. So every comparison here is made on two forms of the path, the lexical one
(`..` collapsed) and the physical one (`realpath`: symlinks of every existing prefix resolved), and on
case-folded, NFC-normalised text. If either form is protected, the path is protected.

Repository resolution never uses `$CLAUDE_PROJECT_DIR`: the root is the nearest ancestor of the
*target path* that holds a `.git` entry, which is also how a linked worktree resolves to itself (§7.1).

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import fnmatch
import glob as _glob
import os
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["fold", "absolute", "lexical", "physical", "forms", "find_root", "classify", "classify_forms",
           "rel_to_root", "match_rel", "expand_glob", "home_dir", "PROTECTED_DESCRIPTIONS", "ENGINEER_ONLY",
           "is_report_path", "safe_read", "within"]

MAX_READ = 512 * 1024


def fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def home_dir(env: Optional[Dict[str, str]] = None) -> str:
    h = (env or os.environ).get("HOME") or os.path.expanduser("~")
    return os.path.normpath(h) if h else "/"


def absolute(path: str, cwd: str, home: Optional[str] = None) -> str:
    """`path` as an absolute, tilde-expanded, un-normalised string."""
    if path.startswith("~"):
        h = home or home_dir()
        if path == "~" or path.startswith("~/"):
            path = h + path[1:]
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return path


def lexical(path: str, cwd: str, home: Optional[str] = None) -> str:
    return os.path.normpath(absolute(path, cwd, home))


def physical(path: str, cwd: str, home: Optional[str] = None) -> str:
    try:
        return os.path.realpath(absolute(path, cwd, home))
    except (OSError, ValueError):
        return lexical(path, cwd, home)


def forms(path: str, cwd: str, home: Optional[str] = None) -> List[str]:
    """The distinct absolute forms to test: lexical and physical."""
    a = lexical(path, cwd, home)
    b = physical(path, cwd, home)
    return [a] if a == b else [a, b]


def within(path: str, base: str) -> bool:
    p, b = fold(os.path.normpath(path)), fold(os.path.normpath(base))
    return p == b or p.startswith(b.rstrip("/") + "/")


def find_root(abs_path: str) -> Optional[str]:
    """Nearest ancestor (or the path itself) that has a `.git` entry."""
    p = abs_path
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    while True:
        if os.path.lexists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def rel_to_root(abs_path: str, root: Optional[str]) -> Optional[str]:
    if not root:
        return None
    r = os.path.relpath(abs_path, root)
    return None if r.startswith("..") else r.replace(os.sep, "/")


def match_rel(rel: str, patterns: Iterable[str]) -> bool:
    """fnmatch on case-folded repo-relative text; `**` matches any depth, `*` does not cross `/`."""
    r = fold(rel)
    for pat in patterns:
        if _glob_match(r, fold(pat)):
            return True
    return False


_RX_CACHE = {}  # type: Dict[str, "re.Pattern[str]"]


def _glob_match(rel: str, pat: str) -> bool:
    rx = _RX_CACHE.get(pat)
    if rx is None:
        out = []  # type: List[str]
        i = 0
        while i < len(pat):
            c = pat[i]
            if c == "*":
                if pat.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if pat.startswith("**", i):
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            elif c == "[":
                j = pat.find("]", i + 1)
                if j < 0:
                    out.append(re.escape(c))
                else:
                    out.append("[" + pat[i + 1:j].replace("\\", "\\\\") + "]")
                    i = j
            else:
                out.append(re.escape(c))
            i += 1
        rx = re.compile("^" + "".join(out) + "$")
        _RX_CACHE[pat] = rx
    return bool(rx.match(rel))


def expand_glob(word: str, cwd: str, home: Optional[str] = None) -> List[str]:
    """Filesystem glob for a word that contains wildcards; the literal word if nothing matches."""
    pat = absolute(word, cwd, home)
    try:
        hits = sorted(_glob.glob(pat))
    except Exception:
        hits = []
    return hits[:64] if hits else [word]


# ---------------------------------------------------------------------------------------------------------------
# protected paths

# `.claude`-anchored: matched against the part of the path after ANY `.claude` component, so the same rule
# covers `<repo>/.claude/...`, `~/.claude/...` and `<repo>/.claude/worktrees/x/.claude/...`.
CLAUDE_ANCHORED = [
    ("settings", ["settings.json", "settings.local.json", "settings.*.json", "settings*.json", "managed-settings.json"]),
    ("hooks", ["hooks", "hooks/**"]),
    ("scripts", ["scripts", "scripts/**"]),
    ("agents", ["agents", "agents/**"]),
    ("skills", ["skills", "skills/**"]),
    ("commands", ["commands", "commands/**"]),
    ("rules", ["rules", "rules/**"]),
    ("workflows", ["workflows", "workflows/**"]),
    ("loop-config", ["loop.config.md"]),
    ("claude-json", ["../.claude.json"]),
]

# anchored at the repository root
ROOT_ANCHORED = [
    ("mcp", [".mcp.json", ".mcp.*.json"]),
    ("plugin", [".claude-plugin", ".claude-plugin/**", "plugin", "plugin/**"]),
    ("ci", [".github/**", ".gitlab-ci.yml", ".circleci/**", "jenkinsfile", "azure-pipelines.yml", ".buildkite/**",
            "bitbucket-pipelines.yml", ".travis.yml"]),
    ("codeowners", ["codeowners", "docs/codeowners", ".gitlab/codeowners"]),
]

# anywhere in the path: git internals
GIT_COMPONENT = ".git"

# in the user's home directory
HOME_PROTECTED = [".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zshenv", ".zprofile", ".zlogin",
                  ".gitconfig", ".config/git", ".ssh", ".aws", ".config/gh", ".kube", ".docker", ".netrc", ".npmrc",
                  ".pypirc", ".git-credentials", ".config/gcloud", ".azure", ".claude.json", ".gnupg", ".terraformrc",
                  ".config/fish/config.fish", ".inputrc", ".bash_logout", "library/launchagents", "library/launchdaemons",
                  ".config/systemd", ".config/autostart"]

PROTECTED_DESCRIPTIONS = {
    "settings": "Claude Code settings (H6)",
    "hooks": "the hooks directory (H6)",
    "scripts": "kit scripts, which include the guards (H6)",
    "agents": "agent definitions (H6)",
    "skills": "skill definitions, whose frontmatter can run hooks (H6, RC-18)",
    "commands": "mode definitions (H6)",
    "rules": "path-scoped rules (H6)",
    "workflows": "Workflow scripts (H6)",
    "loop-config": "loop.config.md guard keys (H6)",
    "claude-json": "the user MCP and project config (H6)",
    "mcp": ".mcp.json, the MCP tool plane (H3, §5.6)",
    "plugin": "plugin definitions (H3, RC-18)",
    "ci": "CI workflows and pipeline config (H3)",
    "codeowners": "CODEOWNERS (H6)",
    "git": "git internals: hooks, config, info and the loopctl state (§5.9)",
    "home": "a credential or shell-startup file in the home directory (B2, §5.9)",
}

# Engineer-only (the report's guard-main-edit v2 row): not guarded for the conductor.
ENGINEER_ONLY = [
    ("gitattributes", [".gitattributes", "**/.gitattributes"]),
    ("husky", [".husky/**", "**/.husky/**"]),
    ("envrc", [".envrc", "**/.envrc"]),
    ("test-config", [".eslintrc", ".eslintrc.*", "eslint.config.*", "**/eslint.config.*", "**/.eslintrc*", ".prettierrc*",
                     "**/.prettierrc*", "prettier.config.*", ".stylelintrc*", "jest.config.*", "**/jest.config.*",
                     "vitest.config.*", "**/vitest.config.*", "vitest.workspace.*", "playwright.config.*",
                     "**/playwright.config.*", "cypress.config.*", "pytest.ini", "**/pytest.ini", "tox.ini",
                     "**/tox.ini", ".flake8", "ruff.toml", ".ruff.toml", "**/ruff.toml", "mypy.ini", "**/mypy.ini",
                     ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml", "karma.conf.*", ".coveragerc",
                     ".nycrc*", "codecov.yml", ".codecov.yml", "sonar-project.properties", "biome.json", "biome.jsonc",
                     "commitlint.config.*", ".commitlintrc*", "conftest.py"]),
]


def classify(abs_path: str, home: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """(category, why) if this absolute path is protected, else None."""
    p = os.path.normpath(abs_path)
    parts = [x for x in p.split(os.sep) if x]
    low = [fold(x) for x in parts]
    # git internals: any `.git` component
    if GIT_COMPONENT in low:
        return "git", "/".join(parts[low.index(GIT_COMPONENT):])
    # `.claude`-anchored
    for k, comp in enumerate(low):
        if comp != ".claude":
            continue
        rest = "/".join(low[k + 1:])
        if not rest:
            continue
        for cat, pats in CLAUDE_ANCHORED:
            if cat == "claude-json":
                continue
            for pat in pats:
                if _glob_match(rest, fold(pat)):
                    return cat, ".claude/" + "/".join(parts[k + 1:])
    # repo-root anchored
    root = find_root(p)
    rel = rel_to_root(p, root)
    if rel and rel != ".":
        for cat, pats in ROOT_ANCHORED:
            if match_rel(rel, pats):
                return cat, rel
    # home
    h = fold(home or home_dir())
    fp = fold(p)
    if fp == h + "/.claude.json" or fp.startswith(h + "/.claude.json"):
        return "claude-json", "~/.claude.json"
    if fp.startswith(h + "/"):
        sub = fp[len(h) + 1:]
        for pat in HOME_PROTECTED:
            pf = fold(pat)
            if sub == pf or sub.startswith(pf + "/"):
                return "home", "~/" + pat
    return None


def classify_forms(path: str, cwd: str, home: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """Test the lexical and physical form. Returns (category, why, the form that matched)."""
    for f in forms(path, cwd, home):
        hit = classify(f, home)
        if hit:
            return hit[0], hit[1], f
    return None


def engineer_only(abs_path: str) -> Optional[str]:
    root = find_root(os.path.normpath(abs_path))
    rel = rel_to_root(os.path.normpath(abs_path), root) or os.path.basename(abs_path)
    for cat, pats in ENGINEER_ONLY:
        if match_rel(rel, pats):
            return cat
    return None


REPORT_DIR_RE = re.compile(r"(^|/)docs/features/[^/]+/(reports|reviews)/[^/]+$")
REPORT_NAME_RE = re.compile(r"(review|critic|security|verdict|audit|critique)", re.I)


def is_report_path(abs_path: str) -> bool:
    """A critic may write only its own report: a new file under docs/features/<slug>/reports|reviews/, or an
    existing one whose name says it is a review."""
    p = os.path.normpath(abs_path)
    root = find_root(p)
    rel = rel_to_root(p, root)
    if not rel or not REPORT_DIR_RE.search(rel):
        return False
    return (not os.path.exists(p)) or bool(REPORT_NAME_RE.search(os.path.basename(p)))


def safe_read(path: str, limit: int = MAX_READ) -> Optional[str]:
    """Read a small regular file as text; None if it is missing, not regular, or too big."""
    try:
        st = os.stat(path)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None
