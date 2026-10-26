#!/usr/bin/env python3
"""bootstrap_detect: read-only facts about a repository, for /bootstrap (Phase 11a; report 16.1 row 11, Q6, F-15, H5).

    python3 .claude/scripts/bootstrap_detect.py [--repo DIR] [--json]

It answers, without changing anything: is this a greenfield or an existing repository, which stack do the manifests show, which
verify commands would be reasonable (as suggestions: nothing here writes config), which deploy hosts and workflows are present, whether
release automation (release-please, changesets, semantic-release, goreleaser) is installed, and what state the kit and an earlier
bootstrap receipt are in. Output schema `bootstrap_detect.v1`.

Rules it keeps, because a repository can be hostile:
  * it reads files and runs a few read-only git commands (rev-parse, symbolic-ref, config --get-regexp, ls-files) with the user's and
    system git config ignored and hooks pinned off; it never runs a package manager, a test or a project script;
  * manifests are read as data with a size cap, only if they are regular files (a symlink, pipe or device is skipped);
  * nothing read from a manifest reaches a command suggestion except through a fixed table: a package.json script NAME is looked up
    ("test", "build", ...), its body is never read; a dependency name is matched against a fixed list;
  * a remote URL is parsed into host and OWNER/NAME; the raw URL (which can hold a token) is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio  # noqa: E402
from kitlib import config as config_mod  # noqa: E402
from kitlib import gitutil, semver  # noqa: E402

SCHEMA = "bootstrap_detect.v1"
MAX_MANIFEST = 1 << 20
MAX_FILES = 20000
KIT_PREFIXES = (".claude/", ".github/", "docs/", ".git/", "node_modules/", "vendor/", ".venv/", "venv/", "dist/", "build/", "target/")
SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")

LANG_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
            ".tsx": "typescript", ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java", ".kt": "kotlin", ".swift": "swift",
            ".php": "php", ".cs": "csharp", ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".sh": "shell", ".vue": "vue", ".svelte": "svelte"}

NPM_KNOWN = {"next": "Next.js", "react": "React", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "astro": "Astro",
             "express": "Express", "fastify": "Fastify", "@nestjs/core": "NestJS", "hono": "Hono", "vite": "Vite", "tailwindcss": "Tailwind CSS",
             "prisma": "Prisma", "@prisma/client": "Prisma", "drizzle-orm": "Drizzle", "typeorm": "TypeORM", "zod": "Zod",
             "@clerk/nextjs": "Clerk", "stripe": "Stripe", "expo": "Expo", "react-native": "React Native", "vitest": "Vitest", "jest": "Jest",
             "@playwright/test": "Playwright", "cypress": "Cypress", "typescript": "TypeScript"}
PY_KNOWN = ("fastapi", "django", "flask", "sqlalchemy", "pytest", "ruff", "mypy", "pydantic", "alembic")
NODE_SCRIPTS = ("test", "build", "lint", "typecheck", "type-check", "dev", "start", "format")

HOST_FILES = ("vercel.json", "netlify.toml", "amplify.yml", "fly.toml", "wrangler.toml", "wrangler.json", "render.yaml", "railway.json",
              "Procfile", "app.yaml", "serverless.yml", "serverless.yaml", "sst.config.ts", "cdk.json", "Dockerfile")

RELEASE_FILES = (
    ("release-please", ("release-please-config.json", ".release-please-manifest.json")),
    ("changesets", (".changeset/config.json",)),
    ("semantic-release", (".releaserc", ".releaserc.json", ".releaserc.yml", ".releaserc.yaml", ".releaserc.js", ".releaserc.cjs",
                          "release.config.js", "release.config.cjs", "release.config.mjs")),
    ("goreleaser", (".goreleaser.yml", ".goreleaser.yaml")),
)
RELEASE_WORKFLOW = (("release-please", re.compile(r"googleapis/release-please-action|release-please", re.I)),
                    ("changesets", re.compile(r"changesets/action|changeset (?:publish|version)", re.I)),
                    ("semantic-release", re.compile(r"semantic-release", re.I)),
                    ("goreleaser", re.compile(r"goreleaser/goreleaser-action|goreleaser (?:release|build)", re.I)))
RELEASE_DEPS = {"@changesets/cli": "changesets", "semantic-release": "semantic-release", "release-please": "release-please"}
RELEASE_PATHS = {
    "release-please": ["release-please-config.json", ".release-please-manifest.json", "CHANGELOG.md"],
    "changesets": [".changeset/**"],
    "semantic-release": [".releaserc*", "release.config.*"],
    "goreleaser": [".goreleaser.y*ml"],
}
OPS_TEMPLATES = ("intake", "dependency-check", "smoke", "verify", "claude-review", "label-guard")


def _is_regular(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_json(path: str) -> Any:
    if not _is_regular(path):
        return None
    data, _ = safeio.read_bytes(path, MAX_MANIFEST)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None


def _read_text(path: str, limit: int = MAX_MANIFEST) -> str:
    if not _is_regular(path):
        return ""
    text, _ = safeio.read_text_status(path, limit)
    return text or ""


def resolve_root(repo: str) -> str:
    repo = os.path.abspath(repo)
    top = gitutil.toplevel(repo, isolated=True, env=_GIT_HARDEN) if os.path.isdir(repo) else None
    return os.path.realpath(top) if top else repo


# A repository's own config can name programs git runs on its behalf (core.fsmonitor runs on `ls-files`, which refreshes the index).
# GIT_CONFIG_COUNT entries sit at command-line scope, above the repository's config, so they win.
_GIT_HARDEN = {"GIT_CONFIG_COUNT": "3",
               "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
               "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": os.devnull,
               "GIT_CONFIG_KEY_2": "core.untrackedCache", "GIT_CONFIG_VALUE_2": "false"}


def _git(root: str, *args: str) -> Optional[str]:
    try:
        p = gitutil.run_git(list(args), cwd=root, isolated=True, pin_hooks=True, timeout=20, env=_GIT_HARDEN)
    except gitutil.GitError:
        return None
    return p.stdout if p.returncode == 0 else None


def parse_remote(url: str) -> Dict[str, Optional[str]]:
    """{"host", "slug"} from a remote URL. Credentials in the URL are dropped, never returned."""
    url = url.strip()
    host, path = None, None
    m = re.match(r"^(?:https?|ssh|git)://(?:[^/@]*@)?([A-Za-z0-9.-]+)(?::\d+)?/(.+)$", url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^(?:[^/@:]+@)?([A-Za-z0-9.-]+):(?!//)(.+)$", url)
        if m:
            host, path = m.group(1), m.group(2)
    if host is None or path is None:
        return {"host": None, "slug": None}
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    slug = path if SLUG.match(path) and not any(p in (".", "..") for p in path.split("/")) else None
    return {"host": host.lower(), "slug": slug}


def git_facts(root: str) -> Dict[str, Any]:
    out = {"is_repo": False, "has_commits": False, "branch": None, "remotes": []}  # type: Dict[str, Any]
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return out
    out["is_repo"] = True
    out["has_commits"] = _git(root, "rev-parse", "--verify", "-q", "HEAD") is not None
    branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    out["branch"] = branch.strip() if branch and re.match(r"^[A-Za-z0-9._/-]{1,100}\Z", branch.strip()) else None
    raw = _git(root, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    for line in raw.split("\n"):
        m = re.match(r"^remote\.([A-Za-z0-9._-]{1,50})\.url (.+)$", line.strip())
        if m:
            info = parse_remote(m.group(2))
            out["remotes"].append({"name": m.group(1), "host": info["host"], "slug": info["slug"]})
    return out


def list_files(root: str, tracked: bool) -> List[str]:
    files = []  # type: List[str]
    if tracked:
        raw = _git(root, "ls-files", "-z")
        if raw is not None:
            files = [p for p in raw.split("\0") if p][:MAX_FILES]
            return files
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if (d + "/") not in (".git/", "node_modules/", ".venv/", "venv/", "dist/", "build/", "target/", ".claude/")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            files.append(fn if rel == "." else os.path.join(rel, fn))
            if len(files) >= MAX_FILES:
                return files
    return files


def language_counts(files: Sequence[str]) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    for f in files:
        if f.startswith(KIT_PREFIXES) or "/node_modules/" in f:
            continue
        lang = LANG_EXT.get(os.path.splitext(f)[1].lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items()))


def node_facts(root: str) -> Optional[Dict[str, Any]]:
    doc = _read_json(os.path.join(root, "package.json"))
    if not isinstance(doc, dict):
        return None
    deps = {}  # type: Dict[str, Any]
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(doc.get(k), dict):
            deps.update(doc[k])
    scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
    pm = "npm"
    for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"), ("package-lock.json", "npm")):
        if _is_regular(os.path.join(root, lock)):
            pm = name
            break
    return {"package_manager": pm,
            "frameworks": sorted({NPM_KNOWN[d] for d in deps if d in NPM_KNOWN}),
            "scripts": sorted(s for s in scripts if s in NODE_SCRIPTS),
            "release_deps": sorted({RELEASE_DEPS[d] for d in list(deps) + (["semantic-release"] if "release" in doc and isinstance(doc.get("release"), dict) else [])
                                    if d in RELEASE_DEPS})}


def python_facts(root: str) -> Optional[Dict[str, Any]]:
    text = ""
    present = []
    for fn in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile"):
        t = _read_text(os.path.join(root, fn))
        if t or _is_regular(os.path.join(root, fn)):
            present.append(fn)
            text += "\n" + t.lower()
    if not present:
        return None
    tokens = set(re.findall(r"[a-z][a-z0-9_-]*", text))
    return {"files": present, "packages": sorted(p for p in PY_KNOWN if p in tokens)}


def suggest_verify(root: str, node: Optional[Dict[str, Any]], py: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    if node:
        pm = node["package_manager"]
        run = {"npm": "npm run ", "pnpm": "pnpm run ", "yarn": "yarn ", "bun": "bun run "}[pm]
        s = set(node["scripts"])
        cfg = {}  # type: Dict[str, str]
        if "test" in s:
            cfg["verify_tests"] = {"npm": "npm test", "pnpm": "pnpm test", "yarn": "yarn test", "bun": "bun run test"}[pm]
        if "build" in s:
            cfg["verify_build"] = run + "build"
        if "lint" in s:
            cfg["verify_lint"] = run + "lint"
        for name in ("typecheck", "type-check"):
            if name in s:
                cfg["verify_typecheck"] = run + name
                break
        if "dev" in s:
            cfg["verify_dev"] = run + "dev"
        out.append({"ecosystem": "node", "basis": "package.json script names (%s) and the %s lockfile" % (", ".join(sorted(s)) or "none", pm), "config": cfg})
    if py:
        cfg = {}
        pk = set(py["packages"])
        if "pytest" in pk:
            cfg["verify_tests"] = "python3 -m pytest"
        if "ruff" in pk:
            cfg["verify_lint"] = "ruff check ."
        if "mypy" in pk:
            cfg["verify_typecheck"] = "mypy ."
        out.append({"ecosystem": "python", "basis": "packages named in %s" % ", ".join(py["files"]), "config": cfg})
    if _is_regular(os.path.join(root, "go.mod")):
        out.append({"ecosystem": "go", "basis": "go.mod", "config": {"verify_tests": "go test ./...", "verify_build": "go build ./...", "verify_lint": "go vet ./..."}})
    if _is_regular(os.path.join(root, "Cargo.toml")):
        out.append({"ecosystem": "rust", "basis": "Cargo.toml", "config": {"verify_tests": "cargo test", "verify_build": "cargo build", "verify_lint": "cargo clippy"}})
    if _is_regular(os.path.join(root, "Gemfile")):
        spec = os.path.isdir(os.path.join(root, "spec"))
        out.append({"ecosystem": "ruby", "basis": "Gemfile" + (" and spec/" if spec else ""), "config": {"verify_tests": "bundle exec rspec" if spec else "bundle exec rake test"}})
    return out


def release_facts(root: str, files: Sequence[str], node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    found = {}  # type: Dict[str, List[str]]
    for tool, names in RELEASE_FILES:
        for n in names:
            if _is_regular(os.path.join(root, n)):
                found.setdefault(tool, []).append(n)
    if os.path.isdir(os.path.join(root, ".changeset")) and not os.path.islink(os.path.join(root, ".changeset")):
        found.setdefault("changesets", []).append(".changeset/")
    for tool in (node or {}).get("release_deps", []):
        found.setdefault(tool, []).append("package.json (dependency)")
    wf_dir = os.path.join(root, ".github", "workflows")
    workflows = []  # type: List[str]
    try:
        names = sorted(os.listdir(wf_dir))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith((".yml", ".yaml")) or not re.match(r"^[A-Za-z0-9._-]{1,100}\Z", fn):
            continue
        workflows.append(fn)
        text = _read_text(os.path.join(wf_dir, fn))
        for tool, rx in RELEASE_WORKFLOW:
            if rx.search(text):
                found.setdefault(tool, []).append(".github/workflows/" + fn)
    tools = sorted(found)
    paths = []  # type: List[str]
    for t in tools:
        paths += RELEASE_PATHS.get(t, [])
    return {"tools": tools, "evidence": {t: sorted(set(found[t])) for t in tools}, "h5": bool(tools),
            "workflows": workflows,
            "suggested_always_human_paths": sorted(set(paths + ([".github/workflows/" + w for w in workflows if re.search(r"release|publish|deploy", w, re.I)] if tools else []))),
            "note": ("Release pull requests from %s are H5: a human merges them, never auto-merge, and the release workflow belongs in deploy_triggers "
                     "(docs/release-automation.md)." % ", ".join(tools)) if tools else "No release automation found."}


def kit_facts(root: str) -> Dict[str, Any]:
    kit = {"installed": os.path.isdir(os.path.join(root, ".claude", "scripts")), "version": None, "claude_md": "missing", "loop_config": "missing"}  # type: Dict[str, Any]
    text = _read_text(os.path.join(root, ".claude", "KIT_VERSION"), 8192)
    if text:
        try:
            kit["version"] = semver.parse_kit_version_file(text).get("version")
        except ValueError:
            pass
    md = _read_text(os.path.join(root, ".claude", "CLAUDE.md"), 1 << 18)
    if md:
        kit["claude_md"] = "template" if ("PROJECT_NAME" in md or "<!-- e.g." in md or "Your Project Name" in md) else "filled"
    cfg = _read_text(os.path.join(root, ".claude", "loop.config.md"), 1 << 18)
    if cfg:
        try:
            c = config_mod.load_text(cfg)
            kit["loop_config"] = "default" if c.get("verify_tests") in ("<command>", "", None) else "customised"
        except config_mod.ConfigError:
            kit["loop_config"] = "invalid"
    return kit


def receipt_facts(root: str) -> Dict[str, Any]:
    import bootstrap_receipt
    return bootstrap_receipt.status(root)


def detect(repo: str) -> Dict[str, Any]:
    root = resolve_root(repo)
    git = git_facts(root)
    files = list_files(root, git["is_repo"] and git["has_commits"])
    langs = language_counts(files)
    node, py = node_facts(root), python_facts(root)
    hosts = sorted(h for h in HOST_FILES if _is_regular(os.path.join(root, h)))
    release = release_facts(root, files, node)
    installed = {n: _is_regular(os.path.join(root, ".github", "workflows", n + ".yml")) for n in OPS_TEMPLATES}
    source_files = sum(langs.values())
    others = [m for m in ("go.mod", "Cargo.toml", "Gemfile", "pom.xml", "build.gradle", "composer.json", "Package.swift") if _is_regular(os.path.join(root, m))]
    has_code = source_files > 0 or bool(node) or bool(py) or bool(others)
    mode = "existing" if git["has_commits"] and has_code else "greenfield"
    return {"schema": SCHEMA, "mode_hint": mode, "git": git,
            "stack": {"languages": langs, "source_files": source_files, "node": node, "python": py,
                      "other_manifests": sorted(others)},
            "suggested_verify": suggest_verify(root, node, py),
            "deploy_hosts": hosts, "release_automation": release, "ops_workflows_installed": installed,
            "kit": kit_facts(root), "receipt": receipt_facts(root)}


def render(d: Dict[str, Any]) -> str:
    g, s, r = d["git"], d["stack"], d["release_automation"]
    lines = ["bootstrap detect (%s)" % d["schema"],
             "  mode hint:     %s" % d["mode_hint"],
             "  git:           %s" % ("repo, %s commits, branch %s" % ("has" if g["has_commits"] else "no", g["branch"]) if g["is_repo"] else "not a git repository"),
             "  remotes:       %s" % (", ".join("%s=%s/%s" % (x["name"], x["host"], x["slug"]) for x in g["remotes"]) or "none"),
             "  languages:     %s" % (", ".join("%s %d" % kv for kv in s["languages"].items()) or "none"),
             "  deploy hosts:  %s" % (", ".join(d["deploy_hosts"]) or "none"),
             "  release tool:  %s" % (", ".join(r["tools"]) or "none"),
             "  kit:           installed=%s version=%s CLAUDE.md=%s loop.config=%s" % (d["kit"]["installed"], d["kit"]["version"], d["kit"]["claude_md"], d["kit"]["loop_config"]),
             "  receipt:       %s" % d["receipt"]["state"]]
    for sv in d["suggested_verify"]:
        lines.append("  suggest (%s): %s" % (sv["ecosystem"], ", ".join("%s=%s" % kv for kv in sorted(sv["config"].items())) or "nothing"))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    d = detect(args.repo)
    print(json.dumps(d, indent=2, sort_keys=True) if args.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
