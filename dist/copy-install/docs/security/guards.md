# The guard suite (Guards v2)

Everything on this page is a **guardrail** (report section 7). It catches honest mistakes quickly, with a reason, and it produces telemetry. It is not the boundary: the boundary is B1 to B5 ([README.md](README.md)). A guard stops an agent that is trying to do its job; it does not stop code that is trying to get around it. Where a heuristic cannot see something, this page says so ([What the guards cannot see](#what-the-guards-cannot-see)).

The suite lives in `.claude/scripts/guards/` (Python 3.9, standard library only). It is **on by default**: `.claude/settings.json` wires `run.py` for every blocking event (`.claude/settings.v2.json` is the generated copy of the same wiring). The v1 shell hooks `guard-git.sh` and `guard-main-edit.sh` were removed in 2.0.0 (IMP-13).

## One entry point, one manifest

```
.claude/scripts/guards/
  run.py               the only hook command; dispatches by event and tool
  hooks.manifest.json  the single source of the wiring
  wiring.py            manifest -> .claude/settings.v2.json, the settings templates, the plugin hooks.json
  engine.py            manifest entry -> guards -> one decision
  shellparse.py        the shell parser (quotes, expansions, substitutions, heredocs, redirects)
  invoke.py            wrapper unwrapping and script/runner expansion
  paths.py             path normalisation and the protected-path tables
  lib.py               hook input, caller, repo, config and state resolution
  g_halt.py g_file.py g_git.py g_infra.py g_exec.py g_critic.py g_config.py g_taint.py
  telemetry.py selftest.py cli.py
```

`python3 .claude/scripts/guards/wiring.py --check` fails when `settings.v2.json` or a template no longer matches the manifest. `--print` shows the hooks block, `--plugin` the plugin form (`${CLAUDE_PLUGIN_ROOT}`), `--write` regenerates (a human runs it).

Every blocking entry (`PreToolUse`, `ConfigChange`) is wired with `onFailure: block` **and** fails closed by itself: any exception, malformed input or missing manifest exits 2 with a one-line reason. The wired shell command exits 2 when `python3` is missing or too old (a plain exit 127 would be a non-blocking error), and at SessionStart prints a banner saying why. The field name and placement of `onFailure` are unverified (V9, IMP-16); the self-test and the liveness canary are what detect a hook that never loads.

## Who is calling: the caller classification (section 5.7)

`kitlib.caller.classify` runs on every call, from the hook input and the session registry in `<git-common-dir>/loopctl/sessions/`:

| Hook input | Caller kind |
|---|---|
| `agent_id` present, `agent_type` on the roster | `engineer` or `critic` (a `<plugin>:` prefix is stripped first) |
| `agent_id` present, `agent_type` off the roster | `engineer` (agent-blind: the restrictive default, IMP-7 V1) |
| `agent_id` present, no usable `agent_type` | `unknown-subagent`: treated as an engineer |
| no `agent_id`, `session_id` registered as `conductor` | `conductor` |
| no `agent_id`, `session_id` registered with a roster role | that role (a driver child) |
| no `agent_id`, `session_id` not registered | `unknown`: least privilege, whatever `agent_type` says (a human's `--agent` session lands here) |

The roster is the kit's eight agent names plus the `roster` key of `loop.config.md`. The built-in names match `.claude/agents/`.

**What each kind may do with git.**

| Kind | Git |
|---|---|
| engineer, critic, unknown-subagent | read only: `status diff log show rev-parse blame ls-files grep ...`, branch/tag/remote/stash/config in their list forms |
| unknown (the human's own session), `git_autonomy: none` (default) | read only; run the write yourself with `! git ...` |
| unknown, `git_autonomy: branch` | + add, restore, stash, branch create, checkout/switch, fetch |
| unknown, `git_autonomy: branch+commit` | + commit, cherry-pick, revert, merge **into a non-protected branch** |
| conductor (registered session) | + merge to the local protected branch under `delivery: local`, branch delete (`-D` only for `feat/*`, `dead/*`, `blocked/*`), tags (no `-f`), `reset --hard` and `clean` **only in a loop worktree or on a loop branch**. **No push**: nobody who is an agent pushes (below) |
| tainted conductor at L2 and above | demoted to `unknown` (section 5.3) |

Nobody gets: `--no-verify` (or `-n` on commit and merge), `-c core.*` or `-c alias.*` (every other `-c` key outside a short harmless list is also refused), `git config` writes, `GIT_*` assignments other than author/committer identity and `GIT_TERMINAL_PROMPT`, `update-ref`, `filter-branch`, `replace`, `rebase`, `tag -f`, `push --force|--mirror|--delete|--tags`, a `gh api` method other than GET, `gh release|secret|workflow|repo edit|ruleset|auth`, or `gh pr merge`.

**Registering a session.** `loopctl begin` (and the driver, for its children) registers the conductor and the roles it starts. A human who runs the prose loop without `loopctl` and wants it to commit and merge from the session registers it by hand:

```
! python3 .claude/scripts/guards/run.py register-session --session <session_id> --role conductor
```

The session id is in the canary line SessionStart prints. The same file has `unregister-session`, `set-halt --reason ...` and `clear-halt`. These are human commands: they refuse to run inside a Claude Code session, and guard-exec denies them to agents. Without registration the session works at `git_autonomy` level. The registry is a plain directory of small JSON files, so the authority behind the `conductor` role is the *write access* to it: the sandbox denies sandboxed commands that path (`sandbox.registry-path`), and the guards refuse agent-authored code that writes it. Nothing else authenticates a registry record.

## The guards and their rule ids

Every deny names its rule id on stderr (`BLOCKED by guard-git:no-verify: ...`), the corpus rows use the same ids, and telemetry records them (IMP-22). The order inside a Bash call is halt, file, git, infra, exec, critic; the first deny wins. An `ask` (MCP in a main session) is only a prompt.

### guard-halt

Checked first. While `<git-common-dir>/loopctl/HALT` exists (it is shared by every worktree), every mutating tool is denied; read-only shell commands (`ls`, `cat`, `grep`, `git status`, `loopctl status`) stay available. `guard-halt:halted`. The `factory:halt` label is checked by the driver, which has the network.

### guard-file and guard-main-edit (self-modification, section 6.1 H6)

No agent writes the guardrails, on any branch, by Edit, Write, MultiEdit, NotebookEdit or an MCP filesystem tool, or from Bash. Protected (rule id `guard-file:<category>`):

| Category | Paths |
|---|---|
| `settings` | `.claude/settings*.json` (project, local, user, managed) |
| `hooks`, `scripts` | `.claude/hooks/**`, `.claude/scripts/**` (the guards, the doctor, loopctl) |
| `agents`, `skills`, `commands`, `rules`, `workflows` | the same-named directories under `.claude/` |
| `loop-config` | `.claude/loop.config.md`: allowed only if **no H6-protected key changes value** (`guard-file:h6-config-key`); from Bash it is always refused |
| `mcp`, `plugin` | `.mcp.json`, `.claude-plugin/**`, `plugin/**` |
| `ci`, `codeowners` | `.github/**` and other CI config, `CODEOWNERS` |
| `git` | anything under a `.git` directory (hooks, config, info, the loopctl state) |
| `home`, `claude-json` | shell startup files, `~/.gitconfig`, `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.kube`, LaunchAgents and the like; `~/.claude.json` |

`.claude/context/**` (primers) is deliberately **not** protected.

Engineers and unknown subagents additionally cannot edit `.gitattributes` (`guard-file:engineer-gitattributes`), `.husky/**` (`engineer-husky`), `.envrc` (`engineer-envrc`), test, lint and CI configuration (`engineer-test-config`), the `scripts` of `package.json` or a new `package.json` with lifecycle scripts (`package-scripts`), or paths listed in `<git-common-dir>/loopctl/locked_paths.json` (`engineer-locked`; loopctl writes that file, shape `{"globs": ["tests/acceptance/**"], "sources": {...}}`; the guards read `globs`). The sources are the plan (`plan/**`), the footprints of the other unmerged items, and the gates that pin files: G3 pins the acceptance tests (`g3`) and G7 pins the committed review files under `docs/features/<slug>/reports/` (`g7`).

Paths are compared lexically and physically (symlinks resolved), case-folded, with the repository root taken from the **target path**, never from `$CLAUDE_PROJECT_DIR`. A worktree's `.claude/` is protected like the main checkout's.

**guard-main-edit v2** (`guard-main-edit:shared-branch`): on `main`, `master`, the `integration_branch` or a `protected_branches` entry, only `docs/**`, plan files (`plan/**`, `build-plan.json`, `BUILD_PLAN.md`), `README.md`, `CHANGELOG.md`, `GLOSSARY.md`, `LICENSE`, `.gitignore`, `CLAUDE.md` and `.claude/context/**` may be edited, plus the project's `main_edit_allow` globs. There is no allow-list for the rest of `.claude/`. The branch is the branch of the **file's** repository: editing the main checkout from inside a feature worktree is refused, editing the worktree from a cwd on `main` is allowed (D-2).

From Bash the guard looks at: redirections (`>`, `>>`, `&>`, `>|`, fd redirects, redirect-only commands, `exec > file`), `tee`, `cp`, `mv`, `install`, `rsync`, `ln` (symlink and hard link), `rm`, `rmdir`, `chmod`, `chown`, `truncate`, `touch`, `mkdir`, `dd of=`, `sed -i` and `sed ... w file`, `perl -i`, `awk -i inplace`, `yq -i`, `sponge`, editors in ex mode, `curl -o|-O`, `wget`, `tar x`, `unzip`, `patch`, `git apply|am` (the patch is read), `git restore|checkout --|rm|mv|clean`, `git diff --output`, `git archive -o`, `find -delete|-exec rm`. It follows `cd` (also into a directory computed at run time: then a relative guardrail name is refused, `guard-file:dynamic-target`), `ln -s` made earlier in the same command (`guard-file:link-protected`), and scripts the same command writes and then runs. Inline interpreter code that names a protected path and a write call is `guard-file:script-writes-protected`.

### guard-git

See the table above. Rule ids: `push`, `push-delivery`, `push-protected`, `push-refspec`, `push-force`, `push-delete`, `push-mirror`, `push-tags`, `push-url`, `push-tainted`, `readonly-caller`, `no-verify`, `config-core-alias`, `config-override`, `config-write`, `git-env`, `exec-option`, `exec-path`, `pager-exec`, `update-ref`, `filter-branch`, `replace`, `rebase`, `credential`, `tag-force`, `branch-protected`, `branch-delete-protected`, `branch-force-delete`, `merge-protected`, `fetch-protected`, `amend-protected`, `reset-hard-shared`, `reset-hard-outside-loop`, `clean-outside-loop`, `reflog-expire`, `gc`, `bisect-run`, `submodule-foreach`, `update-index-hide`, `hook`, `dynamic-args`, `unknown-subcommand`, `alias`, `never-run` (git gui, daemon, difftool, mergetool, send-email, svn, p4 and the other external-program and server subcommands), and for GitHub `gh-api-write`, `gh-pr-merge`, `gh-release`, `gh-secret`, `gh-workflow`, `gh-repo`, `gh-ruleset`, `gh-auth`, `gh-mutation`. All are `guard-git:<name>`.

**No agent pushes, the conductor included** (D3). `git push` is denied to every Bash caller, in every form and every `delivery` mode (`guard-git:push`; a more specific rule id such as `push-protected`, `push-force`, `push-tags` or `push-delivery` is only a more helpful message for the same deny, and `no-verify` keeps precedence). The push at delivery is made by a human, or by the Keeper inside the human-started `loopctl run`, which holds the bot credential and does not go through this hook. `delivery: push-branch|draft-pr|pr-automerge` keeps its meaning: `pr_prepare.py` *prints* the exact commands for the human to run. `loopctl remote-lock acquire|renew|release|steal` and `loopctl claim take|release` also push refs and are human-only (below). Nothing under `refs/loopctl/` is pushed by an agent either.

Aliases are resolved from git config: `git pp` where `alias.pp = !git push` is judged as `git push`. An unknown subcommand, a dynamic one, or an alias the guard cannot analyse is denied.

`gh` is allowed for reads (`pr|issue|run|workflow|repo|release|ruleset|label view|list|diff|checks|status`, `gh api` GET, `gh auth status`). Everything else is denied with `gh-mutation`. `curl` and `wget` to `api.github.com` with a non-GET method are `gh-api-write`.

### guard-infra (a typo guard, section 8.2)

A verb classifier over the infrastructure tools, with an **allow list** of verbs per tool; any other verb of a known tool is denied (`unlisted-verb`). Allowed locally: `terraform|tofu|terragrunt fmt|validate|version|graph|providers|init -backend=false`; `cdk ls|doctor|version|init`, and `cdk synth` only when `agent_isolation` or `sandbox_verified` is set in loop.config; `helm lint|template|package|create`; `kubectl kustomize|version --client`; `aws` verbs that start `describe-|list-|get-` (not the secret-bearing ones), `sts get-caller-identity`; `gcloud|az|gsutil|bq` read verbs; `sam validate|build|init|local`, `serverless print|package|offline`; version and help for deploy-shaped CLIs. Rule ids: `mutating-verb` (apply, deploy, destroy, up, install, delete, ...), `local-plan` (plan, synth, preview, diff: they execute provider or program code, CS-4), `unlisted-verb`, `tunnel` (`ssh -L|-R|-D`, `kubectl port-forward|exec`, `aws ssm start-session`, `socat`, `nc -l`, `ngrok`, `cloudflared`, CS-12), `remote-shell` (`ssh|scp|rsync host:` for subagents), `publish` (`npm|pnpm|cargo|twine publish`, `docker push|login`), `db-prod-migrate` and `db-remote` (migrations and clients aimed at a non-local database or `prod`/`staging`), `credential-env` (assigning `AWS_*`, `GITHUB_TOKEN`, `KUBECONFIG` and the like; `test`/`fake`/`localstack` values are allowed), `mcp`.

`npm|pnpm|yarn|bun run <script>`, `npm test|start`, `make <target>`, `just <recipe>`, `turbo|nx run` are **expanded**: the script body is read from `package.json`, the Makefile or the justfile and judged as commands (with `pre*`/`post*` hooks and the install lifecycle scripts), so `npm run deploy` is judged by what `deploy` runs. `yarn cdk deploy` (a binary, not a script) is judged as `cdk deploy`. Tool images in `docker run hashicorp/terraform apply` are judged as the tool.

**MCP** (`PreToolUse` on `mcp__.*`): `mcp__*__run_script` and `mcp__*deploy*` are denied for every caller in a loop session. For other tools the words of the name are classified: a tool is a read only when its first word is a read verb and **no later word is a mutating one** (`check_and_apply` and `get_or_create_table` are not reads; `list_open_issues` is); a subagent may call reads only; a main session is **asked** for a mutating tool and for one that runs a command line it is handed (`call_aws`, a shell or eval tool: the verb is in an argument the guard cannot read). The settings deny block carries the same deploy-class patterns (`mcp__*apply*`, `*destroy*`, `*terminate*`, `*provision*`, `*rollout*`, `*change_set*`, stack create/update/delete) as `.claude/templates/infra/mcp-deploy-deny.settings.json`, and `templates/security/mcp-lockdown.settings.json` is the same list. Any path-like argument of an MCP tool is also judged as an Edit target.

### guard-exec

Denied for every caller: `claude`, `npx @anthropic-ai/claude-code`, the Agent SDK in a command, a script file or inline code (`guard-exec:claude`); `import anthropic` inline (`sdk-inline`); cloud SDKs inline (`sdk-inline`); `sudo|su|doas` (`sudo`); `docker run --privileged`, added capabilities, host namespaces, devices, the Docker socket, `/`, `$HOME` or credential directories mounted (`docker-privileged`, `docker-mount`); pipes into a shell whose content is computed at run time, `curl | sh` (`pipe-to-shell`); `PATH` with a relative or temporary component, `LD_*`, `DYLD_*`, `BASH_ENV`, `NODE_OPTIONS --require` (`path-hijack`, `env-hijack`); inline code that decodes and executes (`obfuscated-code`); inline code that runs a denied command (`inline-command`); `crontab`, `launchctl load`, `systemctl enable` (`persistence`); `npm config set` and friends (`pkg-config-write`); commands naming credential files such as `~/.aws/credentials`, `~/.ssh/id_*`, `.env.production`, or held-out tests (`credential-file`); `dangerouslyDisableSandbox` (`sandbox-escape`); `python -m guards.run`, `run.py register-session|set-halt|clear-halt`, `wiring.py --write` (`guards-cli`); the apply step of `migrate.py` and `init_project.py` (`--apply`, `--resolved`, `--take-kit`), which rewrite the kit's own instructions, scripts and settings (`human-apply`; both scripts also refuse inside a Claude Code session); the kit's own libraries or run state from inline code (`kit-internals`); `awk` with `system()` (`awk-system`); a command with a NUL byte (`nul-byte`); a command it cannot parse (`parse-limit`, fail closed).

`loopctl` has three classes, decided by one table (`kitlib/loopctl_table.py`, which `loopctl/cli.py` also imports; `tests/loopctl/test_guard_alignment.py` derives the table's coverage from the argparse parsers). The command is the first word that does not start with `-`, and its action is the next word only if it does not start with `-`: an option's value is never the action (`loopctl lock --session status steal` is a write, not a read of `status`). `loopctl` accepts no option before the command except `--help` and `--version`; anything else there is a write. Any write flag (`--apply`, `--write`, `--force`, or an abbreviation of one of three or more characters such as `--wri`) makes any command a write, even next to a read flag; `loopctl`'s parsers do not abbreviate. A word the guard cannot read (`$x`, `$(...)`) in the command position, or anywhere in `approve`, `reconcile`, `parallel`, `migrate-plan`, `remote-lock`, `claim` or `resume`, makes the call a human step.

- **read**: open to every caller: `validate status render doctor audit report help version retro harness gaps merge-class` (so `report scan`, `retro` and `harness` are reads), and a command with its read action or flag (`lock status`, `remote-lock status`, `claim list|show`, `manifest show|verify`, `unattended check|status|promotion|intake`, `parallel check|status|leases|--dry-run`, `reconcile --dry-run`, `migrate-plan --check|--dry-run`, `cost --projection`, `evidence --check`, `run --dry-run`, `abort --dry-run`, `calibration report|scan`).
- **write** (`begin advance fail clarify park gate spend run abort halt ...`; an unknown subcommand counts as a write): denied to everyone except the registered conductor session (`loopctl-write`, RC-13). This includes the writing actions of the later commands: `unattended watch|notify-test` and `unattended intake --write`, `gaps --write`, `calibration scan --write` and `calibration record`, `parallel` (everything except `check|status|leases|--dry-run`), `manifest write` and `lock steal`.
- **human**: denied to **every** caller, the registered conductor included (`loopctl-human-only`). These grant authority, lift a stop, or write to a remote. The conductor prints the exact command (`HUMAN STEP: ...`) and the human runs it in a plain terminal or with `!` in the prompt. Text in the session cannot approve.

<!-- loopctl-table:begin (generated by `python3 -m tests.guards.test_guards_doc --print`; tests/guards/test_guards_doc.py compares it) -->

| Human only (denied to every agent, the conductor included: rule `loopctl-human-only`) | Why |
|---|---|
| `loopctl claim release, take` | pushes a ref from this session (D3); `status`, `list`, `show` stay reads |
| `loopctl remote-lock acquire, release, renew, steal` | pushes a ref from this session (D3); `status`, `list`, `show` stay reads |
| `loopctl resume` | lifts a stop |
| `loopctl migrate-plan ... --force` | acts on other items or replaces a human-written plan |
| `loopctl parallel ... --apply` | acts on other items or replaces a human-written plan |
| `loopctl reconcile ... --live` | reads GitHub with the human's credential |
| `loopctl reconcile ... --apply` with `--prs` or `--rule-suites` | forces items to merged from a fact file the agent could have written |
| `loopctl approve <scope>` for any scope except `checkpoint`, `item`, `unblock` | `plan`, `env`, `breaker`, `cost`, `demotion`: the grant for the whole loop, or a lifted stop |
| `loopctl approve item ...` with any other choice (the default is `approve`) | the merge approval and the un-park of a parked item |
| `loopctl approve unblock ...` with any other choice (the default is `approve`) | resets the retry counters |

| Relayable (denied to engineers, critics, drivers and unknown callers; the registered conductor may run it after the human said so in the chat: rule `loopctl-write`) | Why |
|---|---|
| `loopctl approve checkpoint ...` (any choice) | continues inside the approved plan |
| `loopctl approve item ... --choice reject or stop` | narrows or acknowledges |
| `loopctl approve unblock ... --choice keep or replan` | narrows or acknowledges |
| `loopctl reconcile --apply` (no fact file), `loopctl migrate-plan --write` (no `--force`) | git facts only; creates new files only |

<!-- loopctl-table:end -->

An approval is only a message the human typed in this chat; `--by` is recorded, not authenticated. While HALT is set only the read commands pass, and `resume` is not one of them: HALT is released by a human. **The guard is the boundary for these commands.** The CLI cannot tell a human from the conductor, and a CLI refusal under `CLAUDECODE` would lock the human out of the relay-by-`!` path (a `!` command may carry the marker [probe]), so `loopctl approve` has no marker check, while `resume`, `reconcile --live`, `remote-lock` and `claim take|release` do refuse under either session marker. A PreToolUse hook does not see a `!` command. Python code that imports `loopctl` and calls into it (`main`, `Loop`, `approve`, ...) is refused as `kit-internals`, inline or in a script file the agent wrote, because the guard cannot classify a call that never goes through the command line.

Human-run apply steps: `migrate.py` and `init_project.py` (`--apply`, `--resolved`, `--take-kit`), `bootstrap_config.py apply`, `github/labels.py --apply`, `github/boundary.py --apply|--run`, `apply-boundary.sh`, `apply-labels.sh` (`--apply`), `verify-boundary.sh --run` and a generated `.claude/bootstrap/setup.sh --apply` are denied to every caller (`human-apply`), also through `env -u CLAUDECODE ...`, `python -m`, and a symlink whose real file is one of them, and also by an unambiguous abbreviation of the flag (`--ap`). A project's own `setup.sh` or `labels.py` is not caught: the generic names need the kit's directory in their real path. All of them also refuse inside a Claude Code session when either `CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` is present, even empty; that is a tripwire, because a process can unset both, and the guard above is what stops an agent that does.

**Scripts the agent wrote.** A script file that is untracked, modified, or changed since the trusted base is read like inline code. A tracked, clean file is "reviewed" only when it is also unchanged from every trusted base: the active run's `baseSha` (`run.json`, which the sandbox leaves writable, so a forged value can only make the check stricter) and the merge-base of `HEAD` with `origin/HEAD`, `main` or `master`; with none, every script is agent-authored (fail closed), so a conductor's commit on a `feat/*` branch does not turn an engineer's script into a reviewed one. For a script file the rule `kit-internals` names only the writers of run state (`register_session`, `set_halt`, `acquire_lock`, `write_nonce`, `write_run`, `loopctl/sessions|run.json|HALT|lock|nonces`); the kit's own tests legitimately `import kitlib`. An agent-authored Python script's imports from its own directory tree are followed (transitively, at most 10 files of 256 KB) with the same check. This is best effort: it does not see a module found through `sys.path` tricks or a computed name. **The real stop for the session registry is the sandbox `denyWrite` on `<git-common-dir>/loopctl/sessions` and the doctor row `sandbox.registry-path` ([sandbox.md](sandbox.md)); the guard is a typo guard.**

For subagents only: inline interpreters `python -c`, `node -e`, `ruby -e`, `perl -e`, `php -r`, `deno eval`, `bun -e`, and heredocs to an interpreter (`inline-interpreter`) unless the code is trivial (a few pure standard-library imports and no file, process or network access, or `require('./x.json')`); JavaScript package installs without `--ignore-scripts` (`install-scripts`, section 5.5); `npx` of a package that is not a known local dev tool (`npx-remote`); a command whose name is computed at run time (`opaque-command`; for everyone when its arguments carry a deploy-class verb).

### guard-critic-write

Critics (`code-reviewer`, `design-critic`, `security-reviewer`, plus any role the roster marks as a critic) cannot write. Edit, Write and friends are denied except for the critic's own report: a **new** file under `docs/features/<slug>/reports/` or `/reviews/`, or an existing one whose name contains review, critic, security, verdict, audit or critique (`guard-critic-write:edit`). From Bash: redirects to files (except `/dev/null` and the report path), `tee`, `sed -i`, `cp`, `mv`, `rm`, `touch`, `mkdir`, `ln`, `chmod`, `dd of=`, `curl -o`, `find -delete` (`bash-write`) and package installs (`install`) are denied; scanners and test runners stay available.

### ConfigChange blocker

While a run is active (`loopctl/lock` or `run.json` exists), a change to project settings, local settings or skills is blocked (exit 2, `guard-config-change:settings-during-run`). Policy settings cannot be blocked by a hook (P32).

Outside a run, the settings files are protected by the Edit/Write guards (`guard-file:settings`, H6), by the `Edit(...)` denies of the settings templates, and by the sandbox `denyWrite` of the whole H6 write set ([sandbox.md](sandbox.md)). The ConfigChange hook only covers a change that arrives from outside the tool path (an editor, another process) while a run is active; do not read its "during a run" scope as "settings are unprotected otherwise".

### Injection flag and taint (section 5.3)

A `PostToolUse` hook on `WebFetch`, `WebSearch`, `mcp__*`, `Read` of dependency directories (`node_modules`, `vendor`, `site-packages`, ...) and `Bash` that fetches (`gh issue|pr|run|api`, `curl`, `wget`, `npm view`, `git fetch`) marks the session, or the subagent, **tainted** (`<git-common-dir>/loopctl/taint/`). The effect: no push, and a tainted conductor at L2 and above falls back to `unknown`, so it loses commit, merge and loopctl rights until a new session starts. The same hook scans the output for text aimed at an agent ("ignore previous instructions", a fake `system:` role, "print your environment variables", a decode-and-run, hidden zero-width text) and, on a match, adds a notice to the context and logs `injection_suspected`. Detection never blocks and can be evaded by anyone who tries; the taint effect is the part that holds.

### Telemetry (section 7.3)

`<git-common-dir>/loopctl/events/<name>.jsonl`, one file per `agent_id` (`a-<agent_id>`) or per session (`s-<session_id>`): `subagent_start` (type, model if exposed, parent session and its kind), `subagent_stop` (hash of the last message, the report path from its JSON trailer), `tool` (name, ok, duration; async), `permission_denied`, `guard` (rule id of every deny), `injection_suspected`. Diagnostic only: no hash chain, no command text, secrets redacted at write time. A `PostToolUse` of the conductor session also re-stamps the lock heartbeat. Nothing here blocks, ever; an unwritable directory is ignored.

### Self-test and the doctor

SessionStart runs the self-test: 15 payloads (11 that must be denied, 4 that must be allowed, so a guard that denies everything is caught too). The result is recorded in `<git-common-dir>/loopctl/selftest.json`. `python3 .claude/scripts/doctor.py` reads it and the settings: `guards.manifest`, `guards.wiring` (active, partial, staged, v1), `guards.on-failure`, `guards.selftest`, `guards.deny-drift` (the effective deny rules against the suggested deny block). A failed self-test caps the effective level at **L0**; unwired guards cap it at L1.

Two further hooks come from `loopctl`. SessionStart with the `compact` matcher (`loopctl/compact_hook.py`) gives a driver child or the registered conductor the thread of the run back after `/compact` (the item, its stage, what waits on a human); it reads state, writes nothing and never blocks. And the driver is the only process that starts `claude`: every agent is denied it (`guard-exec:claude`). A driver that crashed leaves a run lock whose owner process is gone; the next `loopctl run` on the same host takes it over (`lock_taken_over` in the event log), and `lock steal` (stale locks) is a write for the registered conductor or the human.

### Scope notes added in the final review

- **`.claude` protection is anchored.** A path is guardrail-protected as `.claude/...` only inside `~/.claude` or inside a git tree (a directory with `.git` above it). A stray `.claude` folder in `/tmp` is just a folder; the same name inside any checkout is protected.
- **Landings are checked, not only names.** `tar`, `unzip`, `cp -r`, `rsync`, `mv`, `patch` and `git apply` are checked against the members of the archive or tree when it exists at hook time, and against the destination directory. Contents that do not exist yet (a tarball downloaded in the same command) are invisible.
- **`git fetch` into a protected ref** (`git fetch origin main:main`) is `guard-git:fetch-protected`.
- **Working directory is tracked** across `cd`, `pushd`, `env -C`, `git -C`, `make -C`, `npm --prefix` and `find -execdir`, so a relative path is resolved where it will really land. A `cd` to a dynamic directory makes later relative targets unknown, and unknown is asked or denied, not allowed.

## Settings templates (section 7.2)

`.claude/templates/settings/` (generated by `wiring.py --write`; the project's own settings are never overwritten by an upgrade): `interactive.settings.json` (allow template with named scripts, ask rules, the deny block, sandbox), `headless-child.settings.json` (the driver's `--settings`: `dontAsk`, explicit allow list, no project MCP servers, `Workflow` denied, sandbox, the kit hooks), `critic.settings.json`, `deny-baseline.json`. See the README next to them. `wiring.py --sandbox-snippet` prints the sandbox lines with the absolute git-dir paths of this checkout, for a linked worktree.

### The v1 hooks and their removal

Version 2.0.0 wires the guards above in `.claude/settings.json` and deletes the v1 shell hooks `guard-git.sh` and `guard-main-edit.sh`. They were weak: `sh -c`, `eval`, `env`, an absolute path to git, an alias and `xargs` get a push past them, `gh pr merge` and `gh api .../merge` are not matched, a missing `python3` means allow, and `.claude/**` is editable on main. The v2 wiring closes every one of these by construction (`shellparse` and `g_git` see the unwrapped command; `g_exec.DANGER_CMD_RE` and `g_git` cover the merges; the generated command exits 2 when `python3` is missing; `g_file` protects `.claude/**`), and `tests/guards/corpus/adversarial6.json` holds a deny row for each bypass. The doctor row `guards.v1-residue` is the regression guard: a warning while an older install has not switched yet, an error when the v2 hooks are wired (or the install is a 2.x one) and a v1 hook is still wired or still on disk. An upgrade from 1.x does it in one reviewed commit: merge the hooks block, delete the two v1 scripts, then `wiring.py --check`, `doctor.py`, `run.py --selftest --record`, and `guards.v1-residue` passing (`/migrate` leaves `settings.json` to you, see [../packaging.md](../packaging.md)).

## What the guards cannot see

Stated plainly, so nobody relies on a claim the code does not make. Each of these is covered by the boundary (B2 no credentials, B3 sandbox and `denyWrite`, B4 CODEOWNERS and rulesets), not by a guard:

- **Arbitrary code.** A program the agent writes and runs can do anything the OS user can. The guards read shell, inline interpreter code and scripts the agent wrote or changed (untracked or modified); they do not execute or model them. A Python or Node file that is tracked and unchanged from the trusted base (the run's `baseSha`, else the merge-base with the protected branch) is trusted as reviewed code. Obfuscation that does not use `exec`/`eval` with a decode (for example a path rebuilt from computed arithmetic in a script file) is not caught.
- **Imports.** For an agent-authored Python script, the local modules it imports are followed (transitively, up to 10 files) with the run-state check only. Node requires, `node -r ./preload.js` and `python -c "import sitecustomize"` style preloads, and a module found through `sys.path` or a computed name are not followed.
- **Run-time values.** A variable set outside the command, `$1` in a script body, the output of a program, a glob in command position: reported as dynamic and denied for subagents, and for deploy-class verbs for everyone. A main session may still run `$(which tool) status`.
- **Programs the table does not name.** The infra verb table, the write-target table and the interpreter list are explicit. A deploy CLI the table has never heard of is not denied by name (a known tool's unknown verb is). Add tools in `g_infra.py`.
- **Tools other than the guarded ones.** The manifest guards Bash, Edit, Write, MultiEdit, NotebookEdit, `mcp__*`, Agent, Task and Workflow. A subagent's `tools:` allowlist in its frontmatter is what limits it to Read, Grep, Glob, WebSearch and WebFetch besides those.
- **Pip and other installers.** `pip install` runs `setup.py` of source distributions; only the JavaScript installers have an `--ignore-scripts` rule.
- **Git operations that change files by switching branches or merging** a branch that contains guardrail changes. H3/H6 review of that branch is the control.
- **Anything after the hook decision.** A symlink swapped between the check and the use (TOCTOU) is not caught.
- **Hooks that never load.** A headless context where settings hooks are not applied, a `--settings '{"disableAllHooks":true}'` child, an agent that launched `claude` anyway: the liveness canary, managed settings (`allowManagedHooksOnly`) and B2/B3 are the answers (section 5.8).

## False positives

The guards fail closed, so they have false positives; the deny names its rule so the cost of one is a clear message. The false-positive corpus (`tests/guards/corpus/allow.json`, 160 ordinary commands) and the hello-loop fixture's own commands must stay allowed. The usual ways out: run the command yourself with `!` in the prompt, raise `git_autonomy`, put a project path in `main_edit_allow`, or split a command the parser refuses (`parse-limit`). Do not edit the guards to get past one; that is an H6 change.

## Tests

`tests/guards/` (stdlib `unittest`, offline, about 10 s): the bypass corpus (`corpus/*.json`, more than a thousand rows, each `{caller, command or path, expected decision, rule id}`), the false-positive corpus, the injection corpus, shell parser tests, the hook protocol through the real `run.py` (also on Python 3.9), wiring, telemetry. `tests/test_evals.py` and `evals/` hold the eval cases (section 15 items 3 and 4); `python3 tools/evals/run_local.py` grades recorded transcripts. Live evals are pending-human.
