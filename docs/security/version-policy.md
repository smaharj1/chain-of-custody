# Claude Code version policy

The kit depends on specific Claude Code behaviours, and a CLI update can change them silently (report section 14). So the kit declares a version range, checks it at every session start, and pins the CLI in automation.

## The floor: v2.1.302

| Behaviour | Since |
|---|---|
| Resumable `maxTurns` partial output | v2.1.246 |
| `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | v2.1.257 |
| Per-invocation `effort` | v2.1.292 |
| `onFailure: block` on every blocking-capable hook | v2.1.295 |
| Fork mode on by default in the terminal | v2.1.232 |
| Fork mode on by default in `-p`; `-p` starts in Manual mode | v2.1.302 |

v2.1.302 is the floor because the last row changes how dispatch waits and how headless permissions start. A kit built for v2.1.295 would assume blocking dispatch and the wrong `-p` permission mode.

## The declared range

`.claude/KIT_VERSION` carries it:

```
2.0.0-rc.1 (2026-10-26)
claude_code_min: 2.1.302
claude_code_tested_max: none
```

`claude_code_tested_max` is the newest version the canary passed. It is `none` until a human runs the canary and records the result (H6, through review).

| Installed version | L0 to L2 | L3 and above |
|---|---|---|
| below `claude_code_min` | loud banner | **refused** |
| above `claude_code_tested_max` | warning | **refused** until the canary passes on that version |
| `tested_max` is `none` | note | **refused** |
| cannot be read | note | **refused** |

The doctor applies the same rule as a cap: unreadable caps the level at L1; below the minimum, above the tested maximum, or untested caps it at L2. `session_start.py` does the session-start half and refuses at L3 and above by not writing the canary nonce ([sandbox.md](sandbox.md)).

## Reading the version

Neither tool runs `claude` inside a Claude Code session. `session_start.py` is the hook the CLI starts, so it asks `claude --version` (once per binary, cached by path, size and mtime), or takes `KIT_CLAUDE_VERSION` from the environment. The doctor, run by you in a plain terminal, asks `claude --version` the same way; run inside a session, or by an agent, it reports the version as unreadable and the level stays at L1. Pass `--claude-version 2.1.310` to give it the number.

## Pin and canary (CO-10)

- **CI pins the CLI**: `npm i -g @anthropic-ai/claude-code@<tested_max>`. Unattended runners use the same pin. Turn off auto-update for the bot user.
- **A canary on every CLI release** (a scheduled CI job): a hook-input schema snapshot, the liveness canary (a guard must fire on a planted call), the bypass corpus, and the safety eval cases at threshold 1.0. When it passes, it opens a pull request that bumps `claude_code_tested_max`; a human merges it.
- **Schema drift fails closed**: a guard that sees an unknown hook-input shape denies, and the canary opens an issue.

The canary and its CI job belong to Phase 2 and later; recording the result is pending-human. Until a human has run it, `claude_code_tested_max` stays `none`, which independently keeps L3 and above off.
