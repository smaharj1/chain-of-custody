# fix-b: gates that read untrusted output, secrets and supply chain (review fixes, package B)

## Changes

- **Acceptance verdicts (UN-1, GT-2, D8).** `loopctl/gates/_acparse.py`: `parse()` now always reads the text lines, and (when `allow_json`) EVERY JSON object that has a `results` key, then merges them per criterion with `merge_verdict`, so a failure on any channel wins. A JSON `pass` never upgrades a text or JUnit verdict; a JSON-only `pass` counts only with `allow_json_pass=True`, which `_acrun.run_visible` passes for the G3 visible (`green`) and held-out runs (and for G2's green-on-the-base run of a `characterization` item), never for G6, which calls `parse(..., allow_json=False)`. `Parsed.source` is `text|json|junit|mixed`. A bare `FAIL <id>` line is now a text failure. `g3_acceptance.run_heldout`: a held-out command that does not exit 0 fails with `g3.heldout-failed` (failure class `item`) whatever the output says, so the CI `heldout` job (`gates/_ci.py`) gets the same rule.
- **G5 (GT-5, GT-6, D8).** `_toml_tables` is now a small reader: header spacing and quoting are normalised (`[ tool . pytest . ini_options ]`, `[tool."pytest"]`), a table header is only a header at bracket depth 0 outside multi-line strings (a nested array line can no longer hide the rest of a table), `[tool]` table form and top-level `tool.*` / `tool = {...}` keys are tracked, and anything it cannot classify (unterminated string, unbalanced brackets, unreadable header) makes the whole file count as changed. New always-on lints on lines added to non-test, non-protected, non-doc files: `g5.harness-shaped-output` (error; `"results"` plus an `AC-<x>-<n>` id within a four-line window, committed or untracked) and `g5.hard-exit` (warn; `os._exit(`). New `g5.ignored-config` (error, item class): see `_lib.ignored_problems` below.
- **Ignored files (GT-6).** `gates/_lib.py`: `ignored_files(ctx)` (`git ls-files --others --ignored --exclude-standard`, ignored directories listed once and expanded unless they are dependency directories) and `ignored_problems(ctx)`: an ignored file that matches `CONFIG_GLOBS`, `**/conftest.py`, `*.pth`, `sitecustomize.py`, `usercustomize.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `.coveragerc`, or is executable, and a TRACKED file marked assume-unchanged or skip-worktree (the same hole from the other side). G5 reports `g5.ignored-config`, G11 `g11.ignored-config`.
- **Evidence and freshness (GT-9, evidence side).** `evidence_gate.py`: a recorded gate with no `head`, outside the pre-build gates (`g0`, `g2`), is `evidence.stale`.
- **G1 (GT-13).** `git grep -z`; the output is split on NUL, so a `:` in a test path no longer reads as an untested criterion.
- **`uncommitted_changes` (GT-14).** Only `docs/features/<slug>/reports/` and `.claude/pr/` are excepted, not the whole feature folder.
- **Secrets (UN-2, UN-3, UN-4).** `kitlib/redact.py`: key names are identifiers, not words: `DB_PASSWORD=`, `API_SECRET=`, `STRIPE_SECRET_KEY=`, `db_password=`, `apiSecret=`, `dbPassword:`, `clientsecret=`, `SECRETKEY=` all match; the closed suffix list (`_key _id _value _secret _token _password _hash _salt _string _data _b64`) keeps `token_budget_per_item: 150000`, `input_tokens: 12`, `max_tokens=4000` and `auth: required` unredacted. `redact_obj` handles camelCase keys. New token shapes: `sk-ant-…`, `sk-proj-/svcacct-/admin-…`, `glpat-…`, `whsec_…`, `pypi-AgEIcHlwaS5vcmc…`, `AccountKey=…` (`ghp_ gho_ ghs_ github_pat_ xox[abprs]- AKIA… AIza… sk_live_ rk_live_ npm_…` were already there). `gates/g11_secrets.py`: unquoted values count in `.env*`, yaml, ini, cfg, conf, properties, toml files and in `KEY=value` lines elsewhere; a placeholder excuses a value only as the WHOLE value; `_added_chunks` reads diffs with `core.quotePath=false`, decodes C-quoted `+++ "b/…"` headers, treats `+++ ` as a header only between `diff` and the first `@@` (an added line that reads `++ b/x` can no longer end a file's scan), reads merge commits with `--cc`, and more than 50 untracked files is the blocking `g11.untracked-overflow`.
- **G9 (UN-7, UN-8).** `gates/g9_supply.py`: a changed spec on an EXISTING dependency is `g9.source-changed` (error; warn when `ext.g9.allow` names it) when it moves to a git, tarball, `file:`, `link:`, `portal:`, `owner/repo`, PEP 508 direct-reference, `{ git|path|url|registry = … }` or Gemfile `git:/path:/source:` source; a plain version bump is unchanged. New `g9.index-changed` (error) for an added or changed `--index-url/-i`, `--extra-index-url`, `--find-links/-f`, `--trusted-host`, `--pre`, `--no-index` in a requirements file; `registry=` and `@scope:registry=` lines in `.npmrc`, `.yarnrc*`, `pip.conf`, `uv.toml`, `.cargo/config*`; `[tool.uv]` index keys, `[[tool.uv.index]]`, `tool.uv.sources`, `tool.poetry.source`, `tool.pdm.source`; Cargo `[patch|replace|source]`; Gemfile `source` lines; go.mod `replace`; npm `overrides`/`resolutions`/`pnpm.overrides` to a non-registry source.
- **GT-10 (verify.py).** No behaviour change; the reasoning is now a comment at the `verify.test-count-unknown` warning.
- Tests: `tests/loopctl/gates/test_review_fixes_b.py` (new, 42 tests), additions to `test_readers.py`, `test_g3_heldout.py`, `tests/kitlib/test_redact.py`.

## Upgrade impact

Install A and Install B behave the same after upgrade; nothing to migrate. Things an adopter can notice:

- A gate run can now FAIL where it passed before: an ignored `conftest.py` / `.pth` / executable in the item worktree, a committed `.env`-style or yaml file with an unquoted secret, a placeholder-looking word inside a real secret, a requirements file with `--extra-index-url`, an existing dependency moved to a git URL, a held-out command that exits non-zero, and (via the evidence gate) a recorded gate result that carries no head.
- `redact()` redacts more key names (`DB_PASSWORD=…`, `apiSecret=…`) in events, reports and logs.
- G6 no longer reads JSON in verify output at all; G3 visible and held-out still read a documented JSON harness (`examples/hello-loop/acceptance/run_checks.py --json`), but a JSON `pass` cannot override a text failure.
- The reference harness already exits 1 on a failing criterion; a custom held-out wrapper must too.

## New config keys

None.

## Pending-human actions

None.

## Docs impact

None made here (no doc file is in package B's ownership). Suggested text for the conductor, per file:

- `docs/security/supply-chain.md` (G9): the two new finding ids (`g9.source-changed`, `g9.index-changed`) and what they cover.
- `docs/security/secrets-policy.md` (G11, redact): whole-value placeholders only; unquoted values in config files; the token prefixes above; `g11.untracked-overflow`, `g11.ignored-config`.
- `.claude/spec/loopctl-gates.md` (package C owns it): G3 "held-out must exit 0"; G5 `g5.harness-shaped-output`, `g5.hard-exit`, `g5.ignored-config`; the `_acparse` merging rule (failure on any channel wins; JSON-only pass only in G3 visible and held-out, never G6).
- GT-10 (decided, no code change): say in the gates doc that `verify_tests` is H6 configuration read at the base sha, that exit 0 with an unreadable test count is a pass with a warning at the tests stage, and that G3 and G6 still require a parsed test line naming each criterion.

## Decisions made

- **Narrowed exemptions in the ignored-file rule.** The decision says "matches `CONFIG_GLOBS`". `CONFIG_GLOBS` contains `.env*` and `.claude/**`, which are present and ignored in nearly every developer checkout; flagging them would block every sequential run. Exempt: `.env`, `.env.*`, `.envrc`, `.claude/settings.local.json`, `.claude/worktrees/` (other worktrees hold whole checkouts). Dependency and cache directories are skipped: `node_modules .venv venv vendor target dist build` (named in the decision) plus `.tox .nox site-packages __pycache__ .git .next .nuxt .svelte-kit .angular .turbo .cache .parcel-cache .gradle .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov coverage`. A listing over 50000 paths is `g5.ignored-incomplete` / `g11.ignored-incomplete` (warn), not a block. A committed `.env*` is still a G5 protected path.
- **`allow_json_pass` is also true for `phase == "red"` when the item's class is `characterization`**, because G2 needs a green run on the base and a JSON-only harness would otherwise never get a pass there. The decision named only the visible and held-out phases; in this code the G3 visible phase is called `green`.
- **`Allowed` is not applied to JSON.** The reviewer asked for it, but a JSON verdict carries no test names to filter on. The stand-in is the merge rule plus G1 (the id must appear in a pinned visible file) plus the G5 lint.
- **`FAIL <id>` at the start of a line is a failure** (was: only `FAIL: `). Strictly more failures, never a new pass.
- **`g5.hard-exit` stays a warning** as decided; `g5.harness-shaped-output` is the error. The harness-shaped lint reads a window of four added lines so a literal split over lines is still seen, and also reads new untracked files from the worktree.
- **Redact key suffixes are a closed list** and bare `auth` / plural `tokens` are not keywords. This departs from the open `(?:[_-][A-Za-z0-9]+)*` suffix in the decision text, which would redact `token_budget_per_item: 150000` (an existing "must stay unchanged" test) and every `*_tokens` usage figure. Strong words (`password passwd passphrase secret apikey privatekey accesskey`) also match when glued to a prefix (`clientsecret`).
- **Quoted values still need a digit and a letter** (unchanged); the whole-value placeholder list is the decision's list plus `$(…)`, `placeholder`, and `example|sample|dummy|fake|test|placeholder|changeme` followed by lowercase words and at most two digits (`example-password-1`), so the repo's existing documentation placeholders keep passing. A scan of every tracked file in the repo with the old and the new `scan()` gives 4 new hits, all fixtures in test files.
- **Unquoted `KEY=value` lines outside config files** need no space around `=`, a value of at least eight characters with a digit and a letter, only value characters (no `(`, `$`, `[`), and not a whole-value placeholder.
- **Held-out exit status:** `exit_code != 0` (including a signal) is `g3.heldout-failed` with class `item`, returned before per-criterion parsing.
- **Extra coverage beyond the decision** (all additive, all errors only in the "can put an unreviewed source in" direction): tracked files marked assume-unchanged/skip-worktree; npm `overrides`/`resolutions`; Cargo patch tables; go.mod `replace`; `.yarnrc*`, `pip.conf`, `uv.toml`, `.cargo/config*`.
- **Mapping of findings:** UN-1 fixed; GT-2 fixed (same change); UN-2 fixed; UN-3 fixed; UN-4 fixed; UN-7 fixed; UN-8 fixed; GT-5 fixed; GT-6 fixed (limited as decided); GT-9 evidence side fixed (state side is package C); GT-10 documented, no code change; GT-13 fixed; GT-14 fixed.

Known gaps:

- No channel is unforgeable. A product module that prints a text runner line or an all-pass JSON object and then calls `os._exit(0)` can still produce a pass in the held-out and G3 visible runs when no real runner line contradicts it; the rule only guarantees it cannot hide a failure the runner printed, cannot spoof G6 and cannot pass held-out with a non-zero exit. The CI re-run on the merge-candidate tree is the backstop.
- A JSON `pass` for a criterion that has no text line still counts in G3 visible and held-out (the decision's documented-harness contract).
- The harness-shaped lint is a literal-shape check; building the keys or ids with string arithmetic evades it.
- Ignored files inside dependency directories, and ignored `.env*` files, are not scanned. `--index-url` options reached through `-r other.txt` are only seen when `other.txt` is itself a changed `requirements*.txt`.
- The mixed-source reader treats `[tool]` content as one bucket, so any edit inside `[tool]` table form is a protected change.

## Conductor: pointers to add

- `.claude/spec/loopctl-gates.md` (package C): the G3 held-out exit-0 rule, the `_acparse` merge rule, the new G5 finding ids, and the GT-10 note above.
- `loopctl/state.py` (package C, GT-9 state side): `advance` must treat a recorded gate result with no head as stale outside `g0`/`g2`. Test fixtures in `tests/loopctl/` that record gate results by hand without a `head` will then (like the real Evidence gate now) see them as stale.
- `loopctl/driver.py` (package C): the driver reads the gate results unchanged; no call-site change is needed for B.
- `docs/security/supply-chain.md`, `docs/security/secrets-policy.md`: see Docs impact.
