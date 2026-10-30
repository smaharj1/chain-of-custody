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
           "is_report_path", "safe_read", "within", "SANDBOX_DENY_WRITE", "SANDBOX_DENY_WRITE_FILES", "sandbox_edit_rule", "agent_authored", "trusted_bases"]

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
    if not hits:
        # a file that does not exist yet (`.g?t/loopctl/sessions/new.json`): the directory part of the pattern may still name a real directory
        head, tail = os.path.split(pat.rstrip("/"))
        if head and tail and not any(c in tail for c in "*?[") and any(c in head for c in "*?["):
            try:
                dirs = sorted(_glob.glob(head))
            except Exception:
                dirs = []
            hits = [os.path.join(d, tail) for d in dirs if os.path.isdir(d)]
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

# The sandbox `denyWrite` set (review fixes D1): the whole H6 write set above, plus the session registry, as paths relative to the project directory.
# One source for `templates/security/sandbox.settings.json`, the generated settings templates and the doctor's `sandbox.deny-paths`;
# tests/guards/test_sandbox_template.py proves every CLAUDE_ANCHORED and ROOT_ANCHORED pattern is covered. NOT listed on purpose:
# `.claude/context/**` (primers are editable on main, g_file.MAIN_ALLOW), `.claude/pr/`, and the rest of `.git/loopctl/` (run.json, lock, HALT, nonces: the
# conductor writes them legitimately). The session registry is `<git-common-dir>/loopctl/sessions`; in a linked worktree that directory is outside
# the project directory and its absolute path must be added too (`wiring.py --sandbox-snippet` prints the line, the doctor row `sandbox.registry-path` checks it).
SANDBOX_DENY_WRITE = (
    ".git/hooks", ".git/config", ".git/info", ".git/loopctl/sessions",
    ".claude/settings.json", ".claude/settings.local.json", ".claude/settings.v2.json", ".claude/settings*.json", ".claude/managed-settings.json",
    ".claude/hooks", ".claude/scripts", ".claude/agents", ".claude/skills", ".claude/commands", ".claude/rules", ".claude/workflows",
    ".claude/loop.config.md", ".claude/templates",
    ".claude-plugin", "plugin",
    ".mcp.json", ".mcp.*.json",
    ".github", ".gitlab-ci.yml", ".gitlab", ".circleci", "Jenkinsfile", "azure-pipelines.yml", ".buildkite", "bitbucket-pipelines.yml", ".travis.yml",
    "CODEOWNERS", "docs/CODEOWNERS",
)
# the entries that name a file or a glob (`Edit(<entry>)`); every other entry is a directory (`Edit(<entry>/**)`)
SANDBOX_DENY_WRITE_FILES = frozenset({
    ".git/config", ".claude/settings.json", ".claude/settings.local.json", ".claude/settings.v2.json", ".claude/settings*.json",
    ".claude/managed-settings.json", ".claude/loop.config.md", ".mcp.json", ".mcp.*.json", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml",
    "bitbucket-pipelines.yml", ".travis.yml", "CODEOWNERS", "docs/CODEOWNERS",
})


def sandbox_edit_rule(entry: str) -> str:
    """The `permissions.deny` rule that repeats a `denyWrite` entry (Claude Code derives sandbox filesystem limits from permission rules too)."""
    return "Edit(%s)" % entry if entry in SANDBOX_DENY_WRITE_FILES else "Edit(%s/**)" % entry


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
    # `.claude`-anchored: in the user's own ~/.claude, or inside a git working tree. A `.claude` in a scratch directory that is neither
    # (for example /tmp/fixture/.claude/settings.json) configures nothing until someone makes it a project.
    for k, comp in enumerate(low):
        if comp != ".claude":
            continue
        rest = "/".join(low[k + 1:])
        if not rest:
            continue
        base = os.sep + os.sep.join(parts[:k])
        if fold(base) != fold(home or home_dir()) and find_root(base) is None:
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
    if "\x00" in path:        # a path with a run-time-computed part: there is nothing to read
        return None
    try:
        st = os.stat(path)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


BASE_REFS = ("origin/HEAD", "main", "master")


def _git(root: str, *args: str) -> Optional["subprocess.CompletedProcess"]:
    import subprocess
    try:
        return subprocess.run(["git", "-C", root, "-c", "core.fsmonitor=false"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                              shell=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def trusted_bases(root: str) -> List[str]:
    """The commits a script must be unchanged from to count as reviewed: the active run's `baseSha` (run.json) when it is well-formed, and the
    merge-base of HEAD with the first of origin/HEAD, main, master that exists. A file must be unchanged from every one of them: run.json is writable
    from the sandbox on purpose, so a forged `baseSha` can only make the check stricter, never launder a script that changed since the merge-base.
    Empty when neither can be found (callers then fail closed)."""
    out = []   # type: List[str]
    try:
        from kitlib import runstate
        st = runstate.RunState.for_path(root)
        rec = st.read_run() if st is not None else None
    except Exception:
        rec = None
    sha = rec.get("baseSha") if isinstance(rec, dict) else None
    if isinstance(sha, str) and re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        out.append(sha)
    for ref in BASE_REFS:
        mb = _git(root, "merge-base", "HEAD", ref)
        if mb is not None and mb.returncode == 0 and mb.stdout.strip():
            out.append(mb.stdout.decode("utf-8", "replace").strip().split()[0])
            break
    return out


def agent_authored(path: str) -> bool:
    """True when a script file is not a reviewed one: untracked, modified, changed since a trusted base, or outside any repository.
    A tracked, clean file counts as reviewed only when it is also unchanged from the trusted bases (the run's `baseSha` and the merge-base with the
    protected branch): a conductor commit on a feat/* branch must not launder what an engineer wrote (GD-6). If no base can be determined the file
    is agent-authored (fail closed)."""
    p = os.path.realpath(path)
    root = find_root(p)
    if root is None:
        return True
    st = _git(root, "status", "--porcelain=v1", "--", p)
    if st is None or st.returncode != 0 or st.stdout.strip():
        return True
    tracked = _git(root, "ls-files", "--error-unmatch", "--", p)
    if tracked is None or tracked.returncode != 0:
        return True
    bases = trusted_bases(root)
    if not bases:
        return True
    for base in bases:
        diff = _git(root, "diff", "--quiet", base, "--", p)
        if diff is None or diff.returncode != 0:
            return True
    return False


def archive_members(path: str, limit: int = 3000, max_bytes: int = 64 * 1024 * 1024) -> Optional[List[str]]:
    """Member names of a tar or zip archive (no extraction), or None if it is not readable or too large. Used to see where an
    extraction would write before it happens."""
    try:
        if os.path.getsize(path) > max_bytes:
            return None
    except OSError:
        return None
    try:
        import tarfile
        if tarfile.is_tarfile(path):
            names = []   # type: List[str]
            with tarfile.open(path, "r:*") as tf:
                for m in tf:
                    names.append(m.name)
                    if len(names) >= limit:
                        break
            return names
    except Exception:
        pass
    try:
        import zipfile
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                return zf.namelist()[:limit]
    except Exception:
        pass
    return None


def tree_entries(directory: str, limit: int = 3000) -> List[str]:
    """Relative paths of the files and directories below `directory` (a bounded walk; symlinks are not followed)."""
    out = []   # type: List[str]
    base = os.path.normpath(directory)
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, base)
        for n in list(dirnames) + filenames:
            out.append(n if rel_dir == "." else os.path.join(rel_dir, n))
            if len(out) >= limit:
                return out
    return out
