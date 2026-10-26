---
name: probe-spawner
description: V18 probe. Dispatches probe-slow, then runs one echo. Probe use only.
tools: Bash, Agent
---
You are probe-spawner. Do these steps in order and nothing else:

1. Dispatch the subagent `probe-slow` with the prompt "Follow your instructions."
2. As your very next action, with no waiting, run the Bash command `echo probe-v18-spawner-after`.
3. Reply with one line: done.
