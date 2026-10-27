<!-- PATH-SCOPED RULE TEMPLATE. Copy to .claude/rules/<topic>.md in YOUR project; delete this comment block. -->
<!-- One invariant per file. Loaded automatically when Claude works on files matching `paths`, including inside subagents. -->
<!-- Keep it to a few lines. Anything longer belongs in a primer (.claude/context/), and the rule points at it. -->
---
paths:
  - "<glob of the files this applies to, e.g. packages/api/src/routes/**>"
---

# <The invariant, as an imperative sentence>

<!-- Example title: Validation failures return 422, never 400 -->

- <What to do, in one or two lines.>
- <The one exception, if there is one, and how to recognise it.>
- Why: <one line: the incident or the contract clause behind it.> See `.claude/context/<primer>.md` for the pattern.
