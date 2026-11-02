---
name: probe-wt
description: V6 probe. Runs in an isolated worktree and reports where it branched from. Probe use only.
tools: Bash
isolation: worktree
---
You are probe-wt. Run these Bash commands, one per call, in this order and nothing else: `git branch --show-current`, `git rev-parse --short HEAD`, `pwd`, then `echo probe-v6-done`. Then stop.
