# Developing Chain of Custody

This repository is the kit, and its root is the Claude Code plugin `chain-of-custody`. This file is for sessions that work on the kit itself. A project that installs the kit gets its own `CLAUDE.md` from `seed/.claude/CLAUDE.md` through `/chain-of-custody:init`.

## Layout

| Path | What it is |
|---|---|
| `.claude-plugin/` | `plugin.json` and `marketplace.json` (source `./`) |
| `agents/`, `skills/`, `hooks/hooks.json` | what the plugin loads |
| `scripts/` | the kit's Python (standard library, Python 3.9); `scripts/kitlib/kitpaths.py` finds kit files |
| `context/`, `spec/`, `templates/` | kit prose and templates the agents read |
| `seed/.claude/` | the starter files `/init` copies into a project |
| `migrate/`, `KIT_VERSION` | the v1.2.0 base archive for `/migrate`, and the version range |
| `adapters/` | Codex and Cursor setup (`scripts/adapt.py`) |
| `docs/`, `examples/`, `evals/` | human docs, filled-in examples, eval cases |
| `tests/`, `tools/` | the test suite and the maintainer tools; neither ships to a project |

## Paths in prose

- Kit prose (agents, skills, context, spec, templates) names kit files as `${CLAUDE_PLUGIN_ROOT}/...` and project files as `.claude/...`.
- Human docs (`docs/`, `README.md`, `GLOSSARY.md`, `examples/`) name commands a person runs as `python3 "$KIT/scripts/<x>"` and other kit files by their repository path.
- Commands the kit prints use absolute paths (`kitpaths.script_cmd`).

## Checks

```
sh tools/test.sh
python3 tools/check-refs.py
python3 tools/gen-config.py --check
python3 tools/validate-plugin.py --strict
```

`tools/validate-plugin-real.sh` runs Claude Code's own validator; it starts the real `claude`, so a person runs it.

## Rules

- `.claude/settings.json` here turns the plugin off, so the kit's guards do not block edits to the kit. Try the plugin with `claude --plugin-dir .` from a plain terminal.
- No agent pushes `main`, merges a pull request or grants a human-only approval.
- Each change adds a fragment to `changelog.d/` (format in `changelog.d/README.md`).
- The two real installs are named only "Install A" and "Install B".
- `docs/design/review-*.md` are records; do not edit them.
