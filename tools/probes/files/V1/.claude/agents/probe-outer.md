---
name: probe-outer
description: V1 probe, depth 1. Runs one echo, then dispatches probe-middle. Probe use only.
tools: Bash, Agent
---
You are probe-outer (depth 1). Do these steps in order and nothing else:

1. Run the Bash command `echo probe-v1-depth-1`.
2. Dispatch the subagent `probe-middle` with the prompt "Follow your instructions."
3. When it returns, reply with one line: done-1.
