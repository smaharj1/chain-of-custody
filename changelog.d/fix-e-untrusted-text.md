# fix-e-untrusted-text: notify command allow-list, intake rejection codes, audit escaping and Manifest line, promotion origin, intake.yml fence

## Changes

- **UN-5 `notify.py parse_command`.** The program may not be a shell, interpreter, launcher or transfer tool (sh, bash, zsh, python*, node, perl, ruby, php, lua, awk, sed, env, xargs, curl, wget, nc, ssh, scp, rsync, sudo, osascript, nohup, timeout, find, busybox and the like, matched on the name given AND on the resolved real path, version suffixes stripped: `python3.12`, `perl5.34.0`). The check runs before the "is it executable" lookup, so a missing `wget` still gets the clear reason. No argument may start with `@` or contain a piece (after `=`, `:` or `,`) that starts with `@`, and no argument may name a path (absolute, `~/...`, `file://...`, or relative to HOME, which is the child's directory) inside the repository roots or `~/.aws`, `~/.ssh`, `~/.config/gh`, `~/.config/gcloud`, `~/.azure`, `~/.kube`, `~/.docker`, `~/.netrc`, `~/.git-credentials`, `~/.gnupg`. The credential-directory comparison is case-insensitive (a case-insensitive volume reaches `~/.AWS`). `parse_command(..., home=None)` gains an optional `home`; `send()` passes `env["HOME"]`.
- **UN-6 `intake_validate.py`.** A rejection reason never echoes a name the stranger chose: "N field(s) are not in the schema (the first is field K)". All other reasons were already fixed strings or schema-owned field names.
- **UN-10 `loopctl/intake.py`.** `draft_item` `riskClass` is the fixed `"medium"`, never derived from the stranger's severity.
- **UN-11 `loopctl/audit.py`.** Primer Delta and Doc Delta (including the `checked:` lines) go through `md_text`, so `<` and `>` cannot open an HTML comment or tag in the PR body. Same fix for the Design critic cell (`md_cell`), which also took reviewer text unescaped.
- **Phase 4a carry-over, `loopctl/audit.py`: the `Manifest:` line.** The PR Notes carry `Manifest: <hash> (seal and repo, kit, config, plan, toolchain, lockfiles, verify, evidence re-read from git ...)` for the reproducibility manifest `loopctl manifest write ITEM --head <sha>` stored for this item and head, or "none written for this head". `manifest.verify` (reused, not copied) is run against git. `check_body`: a stored manifest that does not verify, or a body whose `Manifest:` hash is not the stored one (the body's hash is passed as `--expect-hash`), is the error `audit.manifest-broken`; no manifest at all is not an error (it is opt-in); a stored manifest the body does not name is the warning `audit.manifest-absent`; a hash this machine has no manifest for is the warning `audit.manifest-unknown`. This is the trusted channel `manifest verify --expect-hash` wanted (changelog 11b). The task text called this "the KIT_MANIFEST verification result"; `.claude/KIT_MANIFEST` (the install file list that `migrate.py` uses) is a different thing and is untouched, so the line is about the reproducibility manifest.
- **UN-12 `templates/github/workflows/intake.yml`.** A new step makes a random nonce AFTER the issue is fetched (and fails if the issue text contains it); the triage step builds its stdin as the issue JSON between `UNTRUSTED-ISSUE-BEGIN-<nonce>` and `UNTRUSTED-ISSUE-END-<nonce>` lines, the nonce reaches the step by `env`, and the prompt names the markers. `--allowedTools ""` is added next to `--disallowedTools` and `dontAsk`, labelled `[probe]` in a comment because the empty-list semantics are an assumption. `.claude/agents/intake-triage.md` is unchanged. `ci_lint` shows only the four existing floating-tag warnings.
- **UN-13 `loopctl/promotion.py`.** `parse_facts(raw, origin="file")` sets `origin` (loader-set: `file:<name>` when the caller passes it) and moves the file's own `source` to `claimed_source`; no key of the document can set either (unknown keys are refused). `evaluate` still refuses a claim other than `github`/`ci` but reports `origin` and `claimed_source` in `stats`, so a report that says ELIGIBLE also says which file it read and that the source was only a claim.

## Upgrade impact

- A `notify_command` that names a shell, an interpreter, `env`, `curl` or similar, or passes an argument starting with `@` or naming a credential directory or a path in the repository, is now refused (exit 3 from `notify.py`, `unattended check` reports it). Fix: point `notify_command` at a small program of your own and keep the arguments plain. `bash -c '...'`-style commands were already refused for shell syntax; `sh /path/script.sh` and `python3 /path/notify.py` are the new refusals.
- The wording of the intake rejection reason for an unknown field changes (a count and a position instead of names). The `result.json` schema is the same.
- `loopctl unattended promotion --json` shows `origin` and `claimed_source` under `stats`.

## New config keys

None.

## Pending-human actions

- Install A and Install B owners: if `notify_command` uses an interpreter or `curl`, wrap it in a script outside the repository (for example `~/bin/notify-me`) and point the config at that.
- Confirm once, against the pinned Claude Code version used by `intake.yml`, that `--allowedTools ""` is accepted (the deny list and `dontAsk` work without it). If the CLI rejects the empty argument, delete that one line.

## Docs impact

- `docs/unattended.md` (not owned here): the `notify_command` rules paragraph should add the new refusals (shell, interpreter, launcher and transfer-tool programs; `@` arguments; arguments naming the repository or a credential directory) and the promotion section should say the facts file's `source` is a claim and `origin` is what loopctl read. See the pointers below.
- `docs/security/` has no page for notify; `notify.py`'s own docstring carries the rules.

## Decisions made

- UN-5 denylist: the task's list plus `csh tcsh ash busybox netcat socat mawk tclsh expect nohup nice timeout setsid stdbuf command exec find xterm open nodejs`; names are matched exactly, with version suffix stripped, and by `python*`/`pypy*` prefix. Matching is on the basename of both the name the human wrote and the realpath it resolves to (a link named `notify` to `/bin/sh` is refused). Candidates for "is this argument a path" are the whole argument and each piece after `=`, `:`, `,`, with a leading `<` stripped and `///x` collapsed to `/x`; relative candidates are resolved against HOME, because that is the directory `send()` runs the program in.
- UN-6: a position is shown, never a name, because an unknown field by definition is not in the schema's own field list (the only names that may be shown).
- UN-13: the task text says "ledger, state, file:<name>"; in this module the only loader is the facts-file reader, so `origin` is `file:<name>` and the default is the plain `file`. The `loopctl/cli.py` call still uses the default until package C passes the name (pointer below); the field is present and tested either way.
- Manifest line: the head the manifest is looked up for is the item branch tip (the same `head` the audit block shows); a manifest written with the default `--head HEAD` from another checkout will not match and is reported as "none written for this head".
- Test file placement: the notify tests live in `tests/loopctl/unattended/test_notify.py` (the existing home); there is no `tests/test_notify*.py`.

Known gaps:

- UN-5: a renamed copy of a shell or interpreter (for example a copy of `bash` named `notify-me`) is not detected: the check is on names and resolved names, not on file contents or shebangs. The program must still live outside the repository. `~user` expansion and environment-variable expansion do not exist without a shell, so they are not treated as paths.
- UN-5: a program that itself reads files named in its own config (outside the arguments) is the human's own program; not in scope.
- UN-13: `origin` is only as good as the caller; loopctl still cannot verify that a facts file came from GitHub or CI. The `--facts` path is also required to be inside the repository (`cli.py`, unchanged), so an agent that can write the repository can write a facts file; promotion stays advice for a human.
- UN-12: the nonce fence and the empty allow list are defence in depth around a model that is already tool-less; the model's answer is still untrusted and still goes through `intake_validate.py`. The `--allowedTools ""` semantics are `[probe]`.
- Manifest line: it is opt-in and advisory like the manifest itself (a person who controls the repository can write a manifest that is true about a repository they made up).
- UN-11: other free text in the PR body that comes from plan items (title, intent) already used `md_text`; no other unescaped agent-written value was found in `audit.py` after the Design critic cell.

Findings: UN-5 fixed; UN-6 fixed; UN-10 fixed; UN-11 fixed (plus the Design critic cell); UN-12 fixed (empty-allow flag is a `[probe]`); UN-13 fixed in `promotion.py` (cli wiring pointer); Phase 4a `Manifest:` carry-over done.

## Conductor: pointers to add

- `docs/unattended.md`: in the notify section add: "`notify_command` may not name a shell, an interpreter, `env`, `xargs`, `curl`, `wget`, `ssh`, `sudo` or a similar launcher or transfer tool; no argument may start with `@` or name a path in the repository or a credential directory (`~/.aws`, `~/.ssh`, ...). Write a small program of your own and name that." In the promotion section add: "The facts file's `source` is its own claim; the report shows `origin` (the file loopctl read) and `claimed_source`; nothing is verified against GitHub."
- `docs/getting-started.md` / `docs/faq.md` if they show a `notify_command` example with `curl` or `python3`: change to a script path.
- `loopctl/cli.py` (package C), `unattended promotion`: call `promotion.parse_facts(raw, origin="file:" + os.path.basename(a.facts))`. Optional: print `origin` in the text report.
- `docs/packaging.md` / `KIT_MANIFEST`: nothing new is shipped by this package; the generated `plugin/`, `dist/` and `.claude/KIT_MANIFEST` need regenerating after merge because `notify.py`, `intake_validate.py`, `loopctl/{audit,intake,promotion}.py` and `intake.yml` changed.
- PR-body docs (`docs/delivery.md`, owned by C) can mention the new `Manifest:` Notes line and the finding ids `audit.manifest-broken`, `audit.manifest-absent`, `audit.manifest-unknown`.
- `tests/doctor/test_extras.py` (lines ~28, 41, 60) and `tests/doctor/test_level_caps.py` (line ~45) use `sys.executable` (a Python interpreter) as `notify_command` and are now refused by UN-5 (12 test failures: `test_extras` 3, `test_level_caps` 9). Package D/A or the conductor: replace `sys.executable` with a non-interpreter program that exists everywhere, e.g. `/bin/echo` (`test_extras` line 60 already uses it). Nothing else in those tests changes. Not edited here because those files are not package E's.
