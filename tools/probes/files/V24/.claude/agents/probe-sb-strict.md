---
name: probe-sb-strict
description: V24 probe (frontmatter tries to deny reading README.md). Probe use only.
tools: Bash
sandbox:
  filesystem:
    denyRead:
      - README.md
---
You are probe-sb-strict. Run the Bash command `sh .claude/probe/read-heldout.sh probe-v24-strict README.md`, then reply with one line: done. Do nothing else.
