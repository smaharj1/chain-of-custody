#!/usr/bin/env bash
# PreToolUse(Edit|Write|NotebookEdit) guard: block app-source writes while on main/master.
#
# Engineers cannot create branches (engineer-protocol §8), so without this hook
# interactive-lane engineers would edit directly on main with no backstop. The
# allowlist covers paths the conductor and modes legitimately write while on
# main (kit config, plan files, docs). Allow-on-uncertainty, same policy as
# guard-git.sh: parse failure, missing python3, or no git repo -> allow.
#
# Wired via .claude/settings.json PreToolUse matcher "Edit|Write|NotebookEdit".
# Exit 0 = allow; exit 2 = block (stderr shown to the model).

command -v python3 >/dev/null 2>&1 || exit 0
branch="$(git -C "${CLAUDE_PROJECT_DIR:-.}" branch --show-current 2>/dev/null)" || exit 0
[ "$branch" = "main" ] || [ "$branch" = "master" ] || exit 0
GUARD_INPUT="$(cat)" python3 <<'PY'
import os, json, sys
try:
    fp = json.loads(os.environ.get("GUARD_INPUT") or "{}").get("tool_input", {}).get("file_path", "") or ""
except Exception:
    sys.exit(0)
if not fp:
    sys.exit(0)
root = os.environ.get("CLAUDE_PROJECT_DIR", "")
rel = os.path.relpath(fp, root) if root and os.path.isabs(fp) else fp
allowed = (".claude/", "docs/", "build-plan.json", "BUILD_PLAN.md", "CHANGELOG.md",
           "README.md", "GLOSSARY.md", "LICENSE", ".gitignore")
if rel.startswith(allowed) or rel.startswith(".."):
    sys.exit(0)
sys.stderr.write("BLOCKED by guard-main-edit: editing %s while on a shared branch. "
                 "Create a feature branch first (ask the user / Tech Lead) — do not "
                 "run `git checkout -b` yourself.\n" % rel)
sys.exit(2)
PY
