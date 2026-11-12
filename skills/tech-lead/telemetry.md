# Tech Lead: telemetry

Reference file of the `tech-lead` skill ([SKILL.md](SKILL.md)). Read it when you wrap a feature or a quick task.

## C. Telemetry — `.claude/metrics.jsonl` (facts only)

After every wrapped feature or quick task, append **one JSON line** to `.claude/metrics.jsonl` (gitignored; create it if absent) with only what you actually know:

```json
{"ts": "<ISO time>", "mode": "full", "slug": "<feature-name>", "engineers": ["database", "backend"], "iterations": {"database": 1, "backend": 2}, "escalations": [], "brief_gaps": [{"engineer": "backend", "gap": "<what the brief left out>"}], "duration_min_approx": 18}
```

(`mode` is `full` or `quick`.) **Do not record the model used or a tool-call count.** Neither can be read reliably from inside a session, and a typed-in value looks like evidence when it is a guess; one shipped example said `opus` while the profile said otherwise. Hook-based telemetry records both once it exists; until then the only persistent feedback signals are the iteration counts and brief gaps above. The user reviews this file periodically to tune briefs.

---
