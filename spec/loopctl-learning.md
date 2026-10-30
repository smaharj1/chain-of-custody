# loopctl learning loop: retro, gaps, calibration, report conformance, Docs Delta, harness review

**Status**: built, FULL in warn mode; `report_trailers: block` is a person's switch. Implements report sections 9 ("Critic calibration", G7 owner metric), 16.1 row 10, 16.2 (the pruning rule) and 19.7 (L-32 Primer Delta, L-33 brief gaps, L-34 blocker definition, F-29 learning loop, F-30 Doc Delta), DEC-28 and DEC-29, under H6. Operator guide: `${CLAUDE_PLUGIN_ROOT}/docs/learning.md`. The release procedure: `${CLAUDE_PLUGIN_ROOT}/docs/harness-review.md`. The mode: `${CLAUDE_PLUGIN_ROOT}/skills/retro/SKILL.md`.

Code: `${CLAUDE_PLUGIN_ROOT}/scripts/loopctl/learning/` (`data`, `retro`, `trailers`, `gaps`, `calibration`, `harness`, `docsdelta`, `commands`), `loopctl/report.py` (the hard and soft problems), `loopctl/gates/docs_delta.py`, `tools/evals/ablate.py`.

## The rules the package keeps

1. **It reads, and it drafts. It does not act.** The only files it writes are drafts (`<git-common-dir>/loopctl/gaps/<id>/`) and the calibration log (`<git-common-dir>/loopctl/calibration.jsonl`). It never edits a rule, primer, agent, hook, setting, `loop.config` or `docs/baseline/evidence.json`; never commits, pushes, switches a branch or opens a pull request; starts no process (a test holds `gaps.py` to that). The git and gh commands are printed, quoted for a shell, for a person.
2. **The data is untrusted.** Event fields, report sections, review files and gate messages came from agents that read untrusted content. Counts and closed-vocabulary tokens (`learning.token`) are the only log-derived values in a retro, a rule, a file name or a path suggestion. Text that must reach a person is quoted by `learning.fence`: a fence longer than any run of `~` in it, a label that says UNTRUSTED DATA, control and bidi characters replaced, length capped.
3. **A draft has one shape and one place.** The only repository path a draft may propose is `.claude/rules/<name>.md` (`gaps.RULE_TARGET_RE`), checked again against the protected list (`gaps.PROTECTED`); every write is exclusive-create, refuses a symlink on the way and checks its real path stays under the draft directory.
4. **Config comes from the base sha** (CO-11). `report_trailers`, `docs_delta_min_words` and `gates.docs` cannot be weakened by the branch under judgement.

## Report conformance (`report.py`)

`ReportResult.issues` is `[{kind, severity: hard|soft, message}]`; `problems` still holds the same messages. `blocking(mode)` is what fails the item (everything under `block`; hard only under `warn`; an unknown mode is `block`); `warnings(mode)` is what is recorded (the soft ones under `warn`, as a `report_warning` event `{item, role, mode, kinds, count}`).

| Hard (both modes) | Soft (`warn` records, `block` fails) |
|---|---|
| `bad-path`, `no-report`, `unsafe-report` (symlink, not a regular file, over 1 MiB) | `no-trailer` (none, in a fence, extra key, wrong type, prose after it) |
| `no-status`, `bad-status` | `missing-section` (any but Status) |
| `trailer-mismatch` (status, path, iteration, or the path the brief named) | `none-without-checked` |
| `trailer-ambiguous` (duplicate keys, NaN, deep nesting, oversize on a trailer-shaped last line) | |

## Events the learning commands read, and add

Read: `begin`, `fail` (`failure_class`, `reason`), `park` (`code`), `gate` (`gate`, `status`, `mode`, `blocking`, and since Phase 10 `ids`: the finding ids of an error or warn finding), `engineer_report`, `report_warning`, `merged`, the lock events, Phase 8's `admit` ... `lease_reaped`, `notify.jsonl` `kind`s, `unattended.json`, `header.demotion` and `header.cascades`, approvals of scope `unblock|env|breaker|cost|demotion`. Added: `report_warning`; `ids` on `gate`.

## Schemas

`loopctl.retro.v1` (`retro --json`), `loopctl.gaps.v1` (`gaps --json`, `MANIFEST.json`), `loopctl.calibration.v1` and `.scan.v1`, `loopctl.trailers.v1` (`report scan --json`), `loopctl.harness.v1`, `kit.ablation.v1` (`tools/evals/ablate.py`, read by `harness --ablation`, a regular file inside the repository only).

## Classification (the exec guard reads the same table)

Reads, open to any caller: `retro`, `harness`, `gaps` (without `--write`), `calibration report|scan` (without `--write`), `report scan`. Writes (the registered conductor session or a human; denied to other agents): `gaps --write`, `calibration scan --write`, `calibration record`; an unknown `calibration` action counts as a write. `tests/loopctl/test_guard_alignment.py` compares `cli.classification()` with `g_exec`; rows in `tests/guards/corpus/loopctl.json` (`loopctl-144` onward).

## The Docs Delta gate

`NAME = "docs"`, `DEFAULT_MODE = "report"`, `REQUIRED_FOR = (verifying -> reviewing)`, run with the verify gates after G11. Pure judgement in `learning/docsdelta.py`; `${CLAUDE_PLUGIN_ROOT}/docs/learning.md` section 5 lists what satisfies it and what cannot.

## Not built here

Automatic prompt or rule edits (DEC-28 forbids them). A rule's merge is a person's. Comparing a rule PR's effect on a live metric (`retro.compare` computes the difference between two retro documents; the live runs that make it meaningful are pending-human). A verifier agent for Docs Delta (the code-reviewer is the semantic check). Calibration of the design-critic's rounds beyond logging (`max_design_rounds` is 0b's).
