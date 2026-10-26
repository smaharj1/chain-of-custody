# 00-foundations: shared substrate for every v2 phase (IMP-10, IMP-11)

## Changes

- `.claude/scripts/kitlib/`, stdlib-only, Python 3.9+, imported by adding `.claude/scripts` to `sys.path` (no install step):
  - `miniyaml.py`: the restricted YAML subset of IMP-11 (block mappings and sequences, scalars, `[a, b]`, `|` literals, comments). Everything else is rejected with `line N: ...`. `on`/`off`/`yes`/`no` stay strings so gate toggles are not coerced to booleans.
  - `registry.py` and `schema/*.json`: the `loop.config` key registry. `schema/v1.json` holds all 16 v1 keys; `schema/foundations.json` holds the `gates.*` toggle family. `schema/README.md` is the field reference.
  - `config.py`: loads the single fenced yaml block of `loop.config.md`, validates against the registry (unknown key with a "did you mean", type errors, file line numbers), typed accessors, `<command>` placeholder test, `gate(name)` for `gates.<name>`, `load_at_sha(repo, sha)` via `git show` (the base-sha rule of report §9), `h6_changes(base, head)`, evidence-gated value detection, deprecated-key handling.
  - `configgen.py` and `tools/gen-config.py --write|--check`: render both config templates from the registry plus the templates in `schema/templates/`.
  - `gitutil.py`: toplevel, git-dir, common-dir (worktree-aware, works for a path that does not exist yet), branch, rev-parse, `show_file`, `update_ref_cas`, `delete_ref_cas`, `push_cas` (`--force-with-lease=<ref>:<old>`, or `<ref>:` for create-only), `ls_remote_ref`. Fixed argv, `shell=False`, refs and object ids pattern-checked, repo hooks pinned off, `GIT_DIR`-style variables stripped.
  - `jsonio.py`: atomic write (temp, fsync, rename, fsync dir), exclusive create (temp plus `os.link`, so a reader never sees an empty lock; `O_EXCL` fallback), strict and optional JSON reads.
  - `redact.py`: AWS keys and secrets, GitHub tokens (`ghp_ gho_ ghs_ ghu_ ghr_ github_pat_`), Slack tokens and webhooks, private key blocks, bearer/basic headers, JWTs, Stripe, Google and npm keys, URL credentials, `password=`-style assignments. Idempotent. `redact_obj` walks JSON-like data and blanks values under secret-looking keys.
  - `semver.py`: SemVer 2.0 with prerelease (`2.0.0-rc.1`), `compare`, `in_range`, `parse_leading` for `claude --version` text, `range_status` (unknown, below_min, untested, above_tested, ok) and `parse_kit_version_file` for the v1 and v2 `KIT_VERSION` shapes.
  - `evidence.py`: `passed(repo)` and `check(repo)` read `docs/baseline/evidence.json` (`evidence.v1`). Absent, unparsable, placeholder-bearing or incomplete means False with a reason.
  - `runstate.py`: the `<git-common-dir>/loopctl/` layout as an API: `lock` (exclusive create, heartbeat, release, steal-by-rename, staleness rule), `HALT`, `run.json`, `sessions/<id>.json` registry, `nonces/<id>`, `events/<name>.jsonl` (redacted), `spend.jsonl`, `heldout/`. The module docstring is the contract between guards (Phase 2) and loopctl (Phase 4). Ids from hook input are validated before they become path components.
  - `caller.py`: the report §5.7 classifier as a pure function (`conductor`, `engineer`, `critic`, `unknown-subagent`, `unknown`).
  - `levels.py`: `L0`..`L5`, `effective_level(configured, caps)` (min with reasons, doctor.v1 JSON shape), and the two caps IMP-21 states exactly: `evidence_cap`, `version_cap`.
- `tests/helpers/`: `TempRepo` and bare-remote factories (pinned identity and dates, no global git config), `FakeHome` (plants fake AWS config, credentials and SSO cache, gh `hosts.yml`, `.git-credentials`, ssh agent socket placeholder, ssh key, kube, gcloud, azure, docker) with `clean_env()`, a scriptable fake `claude` (`succeed`, `crash`, `stall`, `malformed`, `overloaded`, `empty`; `--version`; `total_cost_usd`; report and file writing; call log), a fake `gh` (argv log plus recorded-JSON replay), `ProcessTracker` (own session, `killpg` in tearDown) and `BaseTestCase`.
- `tools/test.sh`, `.github/workflows/kit-tests.yml` (Python 3.9 and 3.13, `contents: read`, no secrets), `tools/check-refs.py` with `tools/check-refs.allow`.
- Tests: `test_py39_syntax`, `test_no_project_names` (hash denylist at `tests/data/leak-denylist.sha256`, committed with this topic because it was untracked), `test_config_template`, `test_changelog_fragments`, `test_helpers`, `test_tools`, and `tests/kitlib/test_*` for every module. About 220 tests, about 9 s.
- `.claude/loop.config.md` regenerated from the registry. `examples/loop.config.md` is byte-identical to before.
- `.gitignore`: two lines at the top (`__pycache__/`, `*.pyc`).

## Upgrade impact

None for an installed v1.2.0 copy. The scripts are new files under `.claude/scripts/` and nothing in `.claude/settings.json`, hooks or commands changed. `.claude/loop.config.md` differs from v1.2.0 in four whitespace-only lines: the continuation comments under `autonomy` and `max_consecutive_failures` were indented to column 34 while every other continuation uses column 33; the generator normalises them to 33. Keys, values and comment text are unchanged (`tests/data/v1/` holds the frozen v1 files and the round-trip test proves it).

## New config keys

- `gates.<name>`: enum `on | report | off`, default `on`, H6-protected, not evidence-gated. A wildcard family declared in `schema/foundations.json`; phases that add gates may add exact `gates.<name>` keys.
- The 16 v1 keys are now registry entries (owner phase `v1`). Marked H6-protected: every key except `db_local` and `git_strategy`. Marked evidence-gated: `autonomy` for the value `unattended` only.
- Keys named in IMP-11 with fixed defaults are not registered here, because their owner phases register them (a duplicate name is a load error): `delivery` (local) and `git_autonomy` (none) in Phase 3; `runner` (sequential) and `model_profile` (quality) in Phases 4c/0b; `parallel_mode` (off, evidence-gated for `worktree`) in Phase 8; `merge_policy` (human) in Phase 11b; `report_trailers` (warn) in Phase 10.

## Pending-human actions

- Pin the two actions in `.github/workflows/kit-tests.yml` to commit SHAs (each line carries `# pin to SHA`). An agent has no network.
- First run of `kit-tests` on GitHub-hosted runners. The suite was run locally on Python 3.9.6, 3.13 and 3.14 only.

## Docs impact

- README.md: a short Development section naming `tools/test.sh`, `tools/check-refs.py`, `tools/gen-config.py --check|--write` and saying `.claude/loop.config.md` and `examples/loop.config.md` are generated from `.claude/scripts/kitlib/schema/*.json` (kit developers edit the registry; users of a copied kit edit the values in their own file).
- `docs/design/probe-assumptions.md` and the migration guide can cite `kitlib.caller` as the implementation of the IMP-7 V1 rule.
- The release stage should regenerate the config templates after all phases add keys, and add `example` entries for any v2 key that belongs in `examples/loop.config.md`.

## Decisions made

- **One yaml fence per config file.** Zero or several fenced `yaml` blocks is an error, never "use the first". An ambiguous config is not guessed at.
- **Registry file shape.** Each `schema/<phase>.json` carries its own `sections` and `keys`. Sections render in registry order (`v1.json` first, then alphabetical). Generated-file comments come from `doc` (list of lines), `after_doc` and per-key `example` overrides, so comments are data, not template text. Dotted names nest (`gates.g1`); a trailing `.*` declares a family and is not rendered; `deprecated_by` moves the old key's value to the new one with a warning.
- **`evidence_gated`** is `false`, `true` (any non-default value) or a list of gated values, because IMP-1 gates `parallel_mode: worktree` and `autonomy: unattended`, not the key itself.
- **`autonomy: unattended` is evidence-gated** (it is the v1 spelling of L4, "no discretionary pauses"). Flagged for review: if the maintainer reads v1 `unattended` differently, change `evidence_gated` in `schema/v1.json` and regenerate.
- **H6 flags on v1 keys.** Caps, budgets, verify and ephemeral-DB commands, `security_review`, `autonomy`, `infra_gated_behind_local` and `enforce_engineer_git_hook` are H6-protected (report §6.1: caps, gate toggles, guard wiring, verify definitions). `db_local` and `git_strategy` (only one legal value) are not.
- **Evidence schema `evidence.v1`:** `fixture_runs` (at least 3 objects with a unique `id` and `clean: true`), `real_feature` (`id`, `completed: true`), `attested_by` (non-empty). Extra fields are allowed; any `<placeholder>`, `TODO`, `CHANGEME` or `TBD` string makes it incomplete. Phase 0a's `docs/baseline/evidence.template.json` must produce a file in this shape once filled in.
- **`unknown-subagent`** in `caller.py` means `agent_id` present but `agent_type` missing, empty or not a string. A string `agent_type` that is not on the roster is an `engineer` (IMP-7 V1). Registered session with an off-roster role, and unregistered sessions, are `unknown` (least privilege).
- **Level JSON form** is the strings `"L0"`..`"L5"` in `levels.EffectiveLevel.to_dict()`, matching the `doctor.v1` shape of IMP-22.
- **Lock creation** uses temp file plus `os.link` rather than a bare `O_EXCL` open, so the lock's full content is visible from the first instant. The `O_EXCL` form is the fallback for filesystems without hard links. A corrupt lock is reported as held.
- **`check-refs`** scans inline-code tokens that start with a repo path prefix and every relative markdown link, skips fenced blocks and patterns (`<slug>`, `*`, `{a,b}`, `$VAR`), and excludes `changelog.d/*.md` as well as the files the brief named, because fragments name paths on their own branch. The allowlist is ten lines: v1 runtime files, user-project paths, one moved file in the CHANGELOG.
- **Fake `claude` output shape** (`type`, `subtype`, `is_error`, `result`, `session_id`, `total_cost_usd`) follows the report's assumptions and is synthetic. It makes no claim about the real CLI's output.
- **Secret-shaped test values** are assembled at runtime from fragments so that no literal token sits in the repo for a scanner or push protection to flag.
- **`.gitignore`** gets `__pycache__/` and `*.pyc` at the top of the file (not the end) to avoid an append-at-EOF conflict with Phase 1, which owns `.gitignore`.
- `tools/gen-config.py` takes `--repo` so tests can exercise `--check` and `--write` on a scratch tree; the registry always comes from this repo.

## Conductor: pointers to add

- `.gitignore`: this topic added two lines at the very top (`__pycache__/`, `*.pyc`); Phase 1 owns the file, so expect a trivial textual merge if it also edited the top.
- `README.md`: add the Development section described under "Docs impact".
- `docs/baseline/evidence.template.json` (Phase 0a): check it against the `evidence.v1` fields above before merging 0a; `python3 -c "import sys; sys.path.insert(0,'.claude/scripts'); from kitlib import evidence; print(evidence.check('.'))"` shows the reason a file is rejected.
