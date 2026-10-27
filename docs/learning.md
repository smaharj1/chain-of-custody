# The learning loop

The loop leaves data behind: events, item state, engineers' reports, reviewers' output, the spend ledger. Phase 10 turns it into things a person can act on, and **never acts on them itself**. A rule, a primer, an agent file or a setting changes only through a pull request a person reviews (H6, DEC-28). loopctl reads, counts, drafts and prints commands. It does not commit, push, switch a branch or open a pull request.

There are five parts. All of them work offline on the run data; none starts a model.

| Part | Command | Report line it answers |
|---|---|---|
| Report conformance | `report_trailers: warn \| block`, `loopctl report scan` | L-32, DEC-29: the Primer Delta channel was dead; make the report form hold |
| Retro | `loopctl retro`, the `/retro` mode | F-29: "no learning loop" |
| Gaps to drafts | `loopctl gaps [--write]` | L-33: brief gaps never became rules |
| Critic calibration | `loopctl calibration scan\|record\|report` | L-34: critics were uncalibrated |
| Docs Delta gate | `gates.docs` (`loopctl/gates/docs_delta.py`) | F-30: no doc gate |
| Harness review | `loopctl harness`, `tools/evals/ablate.py`, `docs/harness-review.md` | 16.2: prune what no longer pays |

## 1. Report trailers: warn, then block

An engineer ends its final message with a JSON trailer and writes its own report (`.claude/context/engineer-protocol.md` section 7). `report_trailers` says how strictly the driver reads them:

- **hard** problems fail the item in both modes (`malformed_report`): the report is missing, a symlink, over 1 MiB or outside `docs/features/<slug>/reports/`; `## Status` is missing or not in the enum; a **well-formed trailer that disagrees with the report** (status, path, iteration); a last line that looks like a trailer but a strict parser refuses (duplicate keys, `NaN`, deep nesting). A trailer someone forged, or half-forged, is never tolerated.
- **soft** problems are the form: no trailer, a trailer with an extra key or in a code fence, a missing section other than Status, `NONE` with no `checked:` line. `block` fails the item on them. `warn` (the default) lets the item go on and records a `report_warning` event with the kinds.

`loopctl report scan` reads those events and the reports on disk and says how many accepted reports in a row were clean (ten are needed). When it says `block` is ready, a person changes the key in a reviewed commit (it is H6). The scan is advice from a diagnostic log, so it never switches anything on.

**`block` is not evidence-gated.** The evidence gate (IMP-1) refuses the features that let the loop run with less supervision: unattended, parallel, mutation testing. `block` is the stricter reading of the same reports, it is what 4a and 4c did before Phase 10, and refusing it until the evidence file exists would make the safer setting the unavailable one. Its risk is availability (an item fails and is retried because an engineer got the form wrong), not safety, and the clean streak is the readiness signal for that.

## 2. The retro

`loopctl retro [--milestone M]` prints one document (`--json` for `loopctl.retro.v1`): flow, failures by class and reason, rescues, gate runs and the finding ids that repeat, spend, report form and brief gaps by domain, critic precision, unattended stops, parallel events and cascades, and the empty-Primer-Delta streak. Every number is a count over a closed vocabulary; **no log text appears in it**. `/retro` (`.claude/commands/retro.md`) is the mode that walks a person through it.

The numbers to read first:

- **rescues** (approvals of kind `unblock`, `env`, `breaker`, `cost`, `demotion`, and lock takeovers): more than three per run is the bar the report set for needing a loop at all (16.2);
- **the `env` share** of failures: it should fall as the doctor and the toolchain pins improve (G0's owner metric);
- **the empty-delta streak**: merged items that added a new top-level directory or changed a dependency manifest and reported no Primer Delta. Three in a row means the primers are probably stale (L-32).

## 3. Gaps to drafts

`loopctl gaps` finds what repeats in at least `gaps_min_occurrences` items (default 2; a primer delta is always shown):

| Gap | Seen as | Drafted as |
|---|---|---|
| `brief-gap` | the same domain's engineers write under `Open Questions / Brief Gaps` | a rule skeleton and a proposal |
| `primer-delta` | an engineer reports that a primer disagrees with the code | a proposal naming the primer (`.claude/context/<domain>.md`) |
| `gate-repeat` | one gate finding id (`g5.protected-path`) blocks different items | a rule skeleton and a proposal |
| `failure-repeat` | one item-class reason (`tests_failed`, `ac_unclear`, ...) recurs | a rule skeleton and a proposal |
| `env-failure` | `env` failures recur | a proposal: the doctor, `local-dev.md`, the pins (never a rule) |
| `trailer-form` | report form warnings recur | a proposal |
| `critic-precision` | a critic's blockers are mostly not fixed | a proposal for its rubric (an agent file: H6) |

`gaps` alone only shows them. `gaps --write` creates `<git-common-dir>/loopctl/gaps/gaps-<hash>/` (the same findings name the same directory, so it never drafts twice):

```
README.md        the gaps, the drafts, and the evidence, quoted
COMMANDS.txt     the git and gh commands, for you to run
PR_BODY.md       a pull request description with a checklist (when there is a rule)
MANIFEST.json    every file and its sha256
files/.claude/rules/<name>.md   rule skeletons, from .claude/templates/rule.md
proposals/NN-<kind>-<key>.md    notes: a primer, a rubric, the doctor
```

The commands are printed and not run: `git switch -c learning/<id> <integration>`, `cp -R <draft>/files/. .`, `git add`, `git commit`, `git push -u origin`, `gh pr create --draft --body-file`. You read the draft, fill every `<fill>` in the copied rule, and run them when you agree.

**What a draft cannot do.** It writes nowhere but its own directory, and the only file it may ever propose into the repository is `.claude/rules/<name>.md`; hooks, settings, agents, commands, skills, `.claude/scripts`, `loop.config`, workflows, CODEOWNERS and `docs/baseline/evidence.json` are refused twice over (a shape check and a protected list), with no symlink followed on the way. **What a draft cannot contain.** A rule's words are template text and `<fill>` marks. Its file name comes from a domain, a gate finding id or a failure reason that passed a closed-vocabulary check, and its `paths` suggestion from the approved plan. The quoted evidence (report sections, gate messages) is in the README and the proposals only, inside a fence longer than any run of `~` in the text and labelled UNTRUSTED DATA, with control and bidi characters replaced.

## 4. Critic calibration

`loopctl calibration scan --write` reads the review files (`docs/features/<slug>/reviews/*.md`, `reports/*review*.md`), logs each `[BLOCKER]` (or Critical or High security finding) once under an id that does not change between rounds, and marks `fixed` any that a later round no longer lists (derived, and a person can overrule it). `calibration record --blocker <id> --outcome fixed|overruled|false_positive --engineer-hit yes|no|unknown --by <name>` is a person's call. `calibration report` prints each critic's precision, `fixed / (fixed + overruled + false_positive)`, and says `pending` until `calibration_min_samples` (5) have an outcome. The log is `<git-common-dir>/loopctl/calibration.jsonl`, append-only and redacted. A low-precision critic gets its rubric examples sharpened by a person; nothing changes a prompt by itself.

## 5. The Docs Delta gate

`gates.docs` (default `report`: it runs, records and never blocks; set `on` when its false-positive rate is known). It reads `base..head` and the item's reports from git at the head commit. A **trigger** is an environment variable read, a route or a command-line flag added in source (not tests, docs or the kit's files, and not on a line that was merely moved). A trigger is **documented** when its name is in an added line of a documentation file (prose of at least three words, or a `.env.example`); **declared** when the report's `## Doc Delta` names it in at least `docs_delta_min_words` (5) words, which passes with a warning; otherwise `docs.undeclared` fails the item (`doc_delta_missing`).

An empty or whitespace-only documentation change cannot satisfy it: a blank line, trailing whitespace, a reflow, punctuation, a case change or a deletion adds no new word, an edit under `docs/features/**` (where the report lives) or a test is not documentation, an uncommitted edit is not read, and a Doc Delta that is missing, blank, `NONE` with no `checked:` line or too short is no answer when the diff adds something to document. It is a heuristic over three families of trigger, not a reader of meaning; the code-reviewer still blocks an undocumented endpoint.

## Keys

All owner `10-learning` (`.claude/loop.config.md`, section "Learning loop"): `report_trailers` (`warn`, H6), `docs_delta_min_words` (5, H6), `gaps_min_occurrences` (2), `calibration_min_samples` (5), `harness_min_item_runs` (10). The gate toggle is `gates.docs`.

## What needs live runs

The offline half is tested with fixture data. Whether a retro's rule PR changes a real metric, and whether the clean streak and the Docs Delta heuristics hold on a real project, need several real milestones (`changelog.d/10-learning.md`, "Pending-human actions").
