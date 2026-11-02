# Probe results

Copy this file to `docs/design/probe-results.md` and fill it in. One row per probe **run** (a probe
with variants gets one row per variant and cell). Steps and expected observations:
`tools/probes/README.md`. What each assumption protects and what changes if the probe disagrees:
`docs/design/probe-assumptions.md`.

Fill the header once per sitting:

- Claude Code version (`claude --version`):
- Install method and model (if the probe used one):
- OS and version:
- Shell, `python3 --version`, `git --version`:
- Kit commit (`git rev-parse --short HEAD` in the kit checkout):
- Who ran it:

Verdict values: **confirms** (the build's assumption holds), **overturns** (it does not: follow the
"if the probe shows otherwise" note and fill "Follow-up"), **inconclusive** (say why and what would
settle it), **not run**. Quote the log facts (`analyze.py <V> "$LOG" --json` prints them) and any terminal
message verbatim, with secrets and paths you do not want public removed.

| V-item | Variant / cell | Date | Claude Code version | OS | Observation | Verdict | Follow-up |
|---|---|---|---|---|---|---|---|
| V1 | A nested subagents, interactive | | | | depth 1: agent_id ?, agent_type ?; depth 2: ...; depth 3: ... | | |
| V1 | B driver child (`-p --agent`) | | | | | | |
| V1 | C Workflow agent | | | | | | |
| V5 | 1 project settings only | | | | hooks ? allow ? sandbox ? env ? branch ? | | |
| V5 | 2 plus `--settings` | | | | | | |
| V5 | 3 `--setting-sources user` plus explicit flags | | | | | | |
| V5 | 4 linked worktree | | | | | | |
| V5 | 5 trusted directory | | | | | | |
| V9 | missing-interpreter / PreToolUse | | | | | | |
| V9 | missing-interpreter / PermissionRequest | | | | | | |
| V9 | missing-interpreter / ConfigChange | | | | | | |
| V9 | crash / PreToolUse | | | | | | |
| V9 | crash / PermissionRequest | | | | | | |
| V9 | crash / ConfigChange | | | | | | |
| V9 | timeout / PreToolUse | | | | | | |
| V9 | timeout / PermissionRequest | | | | | | |
| V9 | timeout / ConfigChange | | | | | | |
| V9 | control (no `onFailure`) | | | | | | |
| V18 | fork-on: interactive direct / nested | | | | parent_waited: ? / ? | | |
| V18 | fork-on: `-p` direct / nested | | | | | | |
| V18 | fork-on: driver child | | | | | | |
| V18 | fork-off: interactive direct / nested | | | | | | |
| V18 | fork-off: `-p` direct / nested | | | | | | |
| V18 | fork-off: driver child | | | | | | |
| V18 | fork-off-nobg: all five cells | | | | | | |
| V19 | macOS: healthy sandbox | | | | | | |
| V19 | macOS: sandbox unavailable | | | | | | |
| V19 | Linux: healthy sandbox | | | | | | |
| V19 | Linux: sandbox unavailable | | | | | | |
| V19 | devcontainer: sandbox unavailable | | | | | | |
| V19 | control (no `failIfUnavailable`) | | | | | | |
| V22 | 1 project agent | | | | | | |
| V22 | 2 inline `--agents` | | | | | | |
| V22 | 3 plugin-namespaced | | | | | | |
| V22 | 4 plugin, unprefixed | | | | | | |
| V22 | 5 inline next to `--plugin-dir` | | | | | | |
| V22 | 6 inline overriding a project agent | | | | | | |
| V24 | main / plain / loose / strict | | | | main ?, plain ?, loose ?, strict ? | | |
| V27 | Free private repo | | | | rulesets ? code-owner review ? push rulesets ? merge queue ? | | |
| V27 | Pro private repo | | | | | | |
| V27 | Team private repo | | | | | | |
| V3 | | | | | | | |
| V4 | | | | | | | |
| V6 | | | | | | | |
| V7 | | | | | | | |
| V8 | | | | | | | |
| V12 | | | | | | | |
| V13 | | | | | | | |
| V14 | | | | | | | |
| V15 | | | | | | | |
| V16 | | | | | | | |
| V17 | | | | | | | |
| V20 | | | | | | | |
| V21 | one row per override route | | | | | | |
| V23 | allow / subagent | | | | | | |
| V23 | allow / `-p` | | | | | | |
| V23 | noallow / subagent | | | | | | |
| V23 | noallow / `-p` | | | | | | |
| V25 | | | | | | | |
| V26 | | | | | | | |

Not in the table: V2, V10 and V11 are closed or moot (`tools/probes/README.md`).

After filling the table:

1. For every **overturns** row, make the change `docs/design/probe-assumptions.md` names and cite the row.
2. Record the Claude Code version you tested on as `claude_code_tested_max` in `.claude/KIT_VERSION`
   only after the canary passes (IMP-21); a probe run alone does not raise it.
3. Replace "UNVERIFIED" in the docs only for the items whose row says **confirms** or **overturns**.
