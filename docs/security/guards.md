# The guard suite (Guards v2)

Everything on this page is a **guardrail** (report section 7). It catches honest mistakes quickly, with a reason, and it produces telemetry. It is not the boundary: the boundary is B1 to B5 ([README.md](README.md)). A guard stops an agent that is trying to do its job; it does not stop code that is trying to get around it. Where a heuristic cannot see something, this page says so ([What the guards cannot see](#what-the-guards-cannot-see)).

The suite lives in `.claude/scripts/guards/` (Python 3.9, standard library only). It is **staged, not active**: `.claude/settings.v2.json` holds the wiring, the live `.claude/settings.json` and the v1 shell hooks are unchanged until the release stage switches them in one reviewed commit (IMP-13).

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
| conductor (registered session) | + merge to the local protected branch under `delivery: local`, branch delete (`-D` only for `feat/*`, `dead/*`, `blocked/*`), tags (no `-f`), `reset --hard` and `clean` **only in a loop worktree or on a loop branch**, and push per `delivery` and the refspec allowlist |
| tainted conductor at L2 and above | demoted to `unknown` (section 5.3) |

Nobody gets: `--no-verify` (or `-n` on commit and merge), `-c core.*` or `-c alias.*` (every other `-c` key outside a short harmless list is also refused), `git config` writes, `GIT_*` assignments other than author/committer identity and `GIT_TERMINAL_PROMPT`, `update-ref`, `filter-branch`, `replace`, `rebase`, `tag -f`, `push --force|--mirror|--delete|--tags`, a `gh api` method other than GET, `gh release|secret|workflow|repo edit|ruleset|auth`, or `gh pr merge`.

**Registering a session.** Until `loopctl` (Phase 4) exists, nothing registers a conductor. A human who wants the prose loop to commit and merge from the session registers it:

```
! python3 .claude/scripts/guards/run.py register-session --session <session_id> --role conductor
```

The session id is in the canary line SessionStart prints. The same file has `unregister-session`, `set-halt --reason ...` and `clear-halt`. These are human commands: they refuse to run inside a Claude Code session, and guard-exec denies them to agents. Without registration the session works at `git_autonomy` level.

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

Engineers and unknown subagents additionally cannot edit `.gitattributes` (`guard-file:engineer-gitattributes`), `.husky/**` (`engineer-husky`), `.envrc` (`engineer-envrc`), test, lint and CI configuration (`engineer-test-config`), the `scripts` of `package.json` or a new `package.json` with lifecycle scripts (`package-scripts`), or paths listed in `<git-common-dir>/loopctl/locked_paths.json` (`engineer-locked`; loopctl writes that file, shape `{"globs": ["tests/acceptance/**"]}`).

Paths are compared lexically and physically (symlinks resolved), case-folded, with the repository root taken from the **target path**, never from `$CLAUDE_PROJECT_DIR`. A worktree's `.claude/` is protected like the main checkout's.

**guard-main-edit v2** (`guard-main-edit:shared-branch`): on `main`, `master`, the `integration_branch` or a `protected_branches` entry, only `docs/**`, plan files (`plan/**`, `build-plan.json`, `BUILD_PLAN.md`), `README.md`, `CHANGELOG.md`, `GLOSSARY.md`, `LICENSE`, `.gitignore`, `CLAUDE.md` and `.claude/context/**` may be edited, plus the project's `main_edit_allow` globs. There is no allow-list for the rest of `.claude/`. The branch is the branch of the **file's** repository: editing the main checkout from inside a feature worktree is refused, editing the worktree from a cwd on `main` is allowed (D-2).

From Bash the guard looks at: redirections (`>`, `>>`, `&>`, `>|`, fd redirects, redirect-only commands, `exec > file`), `tee`, `cp`, `mv`, `install`, `rsync`, `ln` (symlink and hard link), `rm`, `rmdir`, `chmod`, `chown`, `truncate`, `touch`, `mkdir`, `dd of=`, `sed -i` and `sed ... w file`, `perl -i`, `awk -i inplace`, `yq -i`, `sponge`, editors in ex mode, `curl -o|-O`, `wget`, `tar x`, `unzip`, `patch`, `git apply|am` (the patch is read), `git restore|checkout --|rm|mv|clean`, `git diff --output`, `git archive -o`, `find -delete|-exec rm`. It follows `cd` (also into a directory computed at run time: then a relative guardrail name is refused, `guard-file:dynamic-target`), `ln -s` made earlier in the same command (`guard-file:link-protected`), and scripts the same command writes and then runs. Inline interpreter code that names a protected path and a write call is `guard-file:script-writes-protected`.

### guard-git

See the table above. Rule ids: `push`, `push-delivery`, `push-protected`, `push-refspec`, `push-force`, `push-delete`, `push-mirror`, `push-tags`, `push-url`, `push-tainted`, `readonly-caller`, `no-verify`, `config-core-alias`, `config-override`, `config-write`, `git-env`, `exec-option`, `exec-path`, `pager-exec`, `update-ref`, `filter-branch`, `replace`, `rebase`, `credential`, `tag-force`, `branch-protected`, `branch-delete-protected`, `branch-force-delete`, `merge-protected`, `amend-protected`, `reset-hard-shared`, `reset-hard-outside-loop`, `clean-outside-loop`, `reflog-expire`, `gc`, `bisect-run`, `submodule-foreach`, `update-index-hide`, `hook`, `dynamic-args`, `unknown-subcommand`, `alias`, `never-run` (git gui, daemon, difftool, mergetool, send-email, svn, p4 and the other external-program and server subcommands), and for GitHub `gh-api-write`, `gh-pr-merge`, `gh-release`, `gh-secret`, `gh-workflow`, `gh-repo`, `gh-ruleset`, `gh-auth`, `gh-mutation`. All are `guard-git:<name>`.

The push allowlist (conductor only): `delivery` is `push-branch`, `draft-pr` or `pr-automerge`; the remote is a named remote (not a URL); every refspec resolves to `refs/heads/feat/*` (`HEAD` is resolved to the current branch); no `+`, no empty source, no wildcard, no tag, nothing under `refs/loopctl/` (loopctl pushes those from its own process, which the guards never see); `--force-with-lease` only with an explicit `<feat/ref>:<expected-sha>`. Under the default `delivery: local` nothing is pushed by an agent, and `pr_prepare` prints the commands for the human.

Aliases are resolved from git config: `git pp` where `alias.pp = !git push` is judged as `git push`. An unknown subcommand, a dynamic one, or an alias the guard cannot analyse is denied.

`gh` is allowed for reads (`pr|issue|run|workflow|repo|release|ruleset|label view|list|diff|checks|status`, `gh api` GET, `gh auth status`). Everything else is denied with `gh-mutation`. `curl` and `wget` to `api.github.com` with a non-GET method are `gh-api-write`.

### guard-infra (a typo guard, section 8.2)

A verb classifier over the infrastructure tools, with an **allow list** of verbs per tool; any other verb of a known tool is denied (`unlisted-verb`). Allowed locally: `terraform|tofu|terragrunt fmt|validate|version|graph|providers|init -backend=false`; `cdk ls|doctor|version|init`, and `cdk synth` only when `agent_isolation` or `sandbox_verified` is set in loop.config; `helm lint|template|package|create`; `kubectl kustomize|version --client`; `aws` verbs that start `describe-|list-|get-` (not the secret-bearing ones), `sts get-caller-identity`; `gcloud|az|gsutil|bq` read verbs; `sam validate|build|init|local`, `serverless print|package|offline`; version and help for deploy-shaped CLIs. Rule ids: `mutating-verb` (apply, deploy, destroy, up, install, delete, ...), `local-plan` (plan, synth, preview, diff: they execute provider or program code, CS-4), `unlisted-verb`, `tunnel` (`ssh -L|-R|-D`, `kubectl port-forward|exec`, `aws ssm start-session`, `socat`, `nc -l`, `ngrok`, `cloudflared`, CS-12), `remote-shell` (`ssh|scp|rsync host:` for subagents), `publish` (`npm|pnpm|cargo|twine publish`, `docker push|login`), `db-prod-migrate` and `db-remote` (migrations and clients aimed at a non-local database or `prod`/`staging`), `credential-env` (assigning `AWS_*`, `GITHUB_TOKEN`, `KUBECONFIG` and the like; `test`/`fake`/`localstack` values are allowed), `mcp`.

`npm|pnpm|yarn|bun run <script>`, `npm test|start`, `make <target>`, `just <recipe>`, `turbo|nx run` are **expanded**: the script body is read from `package.json`, the Makefile or the justfile and judged as commands (with `pre*`/`post*` hooks and the install lifecycle scripts), so `npm run deploy` is judged by what `deploy` runs. `yarn cdk deploy` (a binary, not a script) is judged as `cdk deploy`. Tool images in `docker run hashicorp/terraform apply` are judged as the tool.

**MCP** (`PreToolUse` on `mcp__.*`): `mcp__*__run_script` and `mcp__*deploy*` are denied for every caller in a loop session. For other tools the verb in the name is classified: a subagent may call read verbs only; a main session is **asked** for a mutating verb. Any path-like argument of an MCP tool is also judged as an Edit target.

### guard-exec

Denied for every caller: `claude`, `npx @anthropic-ai/claude-code`, the Agent SDK in a command, a script file or inline code (`guard-exec:claude`); `import anthropic` inline (`sdk-inline`); cloud SDKs inline (`sdk-inline`); `sudo|su|doas` (`sudo`); `docker run --privileged`, added capabilities, host namespaces, devices, the Docker socket, `/`, `$HOME` or credential directories mounted (`docker-privileged`, `docker-mount`); pipes into a shell whose content is computed at run time, `curl | sh` (`pipe-to-shell`); `PATH` with a relative or temporary component, `LD_*`, `DYLD_*`, `BASH_ENV`, `NODE_OPTIONS --require` (`path-hijack`, `env-hijack`); inline code that decodes and executes (`obfuscated-code`); inline code that runs a denied command (`inline-command`); `crontab`, `launchctl load`, `systemctl enable` (`persistence`); `npm config set` and friends (`pkg-config-write`); commands naming credential files such as `~/.aws/credentials`, `~/.ssh/id_*`, `.env.production`, or held-out tests (`credential-file`); `dangerouslyDisableSandbox` (`sandbox-escape`); `python -m guards.run`, `run.py register-session|set-halt|clear-halt`, `wiring.py --write` (`guards-cli`); the kit's own libraries or run state from inline code (`kit-internals`); `awk` with `system()` (`awk-system`); a command with a NUL byte (`nul-byte`); a command it cannot parse (`parse-limit`, fail closed).

`loopctl` **write** subcommands (`begin advance fail approve run gate lock acquire|release|steal abort halt resume reconcile --apply migrate-plan --write set ...`; an unknown subcommand counts as a write) are denied to everyone except the registered conductor session (`loopctl-write`, RC-13). The read subcommands are open: `validate status render doctor lock status reconcile --dry-run migrate-plan --check cost --projection evidence --check`.

For subagents only: inline interpreters `python -c`, `node -e`, `ruby -e`, `perl -e`, `php -r`, `deno eval`, `bun -e`, and heredocs to an interpreter (`inline-interpreter`) unless the code is trivial (a few pure standard-library imports and no file, process or network access, or `require('./x.json')`); JavaScript package installs without `--ignore-scripts` (`install-scripts`, section 5.5); `npx` of a package that is not a known local dev tool (`npx-remote`); a command whose name is computed at run time (`opaque-command`; for everyone when its arguments carry a deploy-class verb).

### guard-critic-write

Critics (`code-reviewer`, `design-critic`, `security-reviewer`, plus any role the roster marks as a critic) cannot write. Edit, Write and friends are denied except for the critic's own report: a **new** file under `docs/features/<slug>/reports/` or `/reviews/`, or an existing one whose name contains review, critic, security, verdict, audit or critique (`guard-critic-write:edit`). From Bash: redirects to files (except `/dev/null` and the report path), `tee`, `sed -i`, `cp`, `mv`, `rm`, `touch`, `mkdir`, `ln`, `chmod`, `dd of=`, `curl -o`, `find -delete` (`bash-write`) and package installs (`install`) are denied; scanners and test runners stay available.

### ConfigChange blocker

While a run is active (`loopctl/lock` or `run.json` exists), a change to project settings, local settings or skills is blocked (exit 2, `guard-config-change:settings-during-run`). Policy settings cannot be blocked by a hook (P32).

### Injection flag and taint (section 5.3)

A `PostToolUse` hook on `WebFetch`, `WebSearch`, `mcp__*`, `Read` of dependency directories (`node_modules`, `vendor`, `site-packages`, ...) and `Bash` that fetches (`gh issue|pr|run|api`, `curl`, `wget`, `npm view`, `git fetch`) marks the session, or the subagent, **tainted** (`<git-common-dir>/loopctl/taint/`). The effect: no push, and a tainted conductor at L2 and above falls back to `unknown`, so it loses commit, merge and loopctl rights until a new session starts. The same hook scans the output for text aimed at an agent ("ignore previous instructions", a fake `system:` role, "print your environment variables", a decode-and-run, hidden zero-width text) and, on a match, adds a notice to the context and logs `injection_suspected`. Detection never blocks and can be evaded by anyone who tries; the taint effect is the part that holds.

### Telemetry (section 7.3)

`<git-common-dir>/loopctl/events/<name>.jsonl`, one file per `agent_id` (`a-<agent_id>`) or per session (`s-<session_id>`): `subagent_start` (type, model if exposed, parent session and its kind), `subagent_stop` (hash of the last message, the report path from its JSON trailer), `tool` (name, ok, duration; async), `permission_denied`, `guard` (rule id of every deny), `injection_suspected`. Diagnostic only: no hash chain, no command text, secrets redacted at write time. A `PostToolUse` of the conductor session also re-stamps the lock heartbeat. Nothing here blocks, ever; an unwritable directory is ignored.

### Self-test and the doctor

SessionStart runs the self-test: 15 payloads (11 that must be denied, 4 that must be allowed, so a guard that denies everything is caught too). The result is recorded in `<git-common-dir>/loopctl/selftest.json`. `python3 .claude/scripts/doctor.py` reads it and the settings: `guards.manifest`, `guards.wiring` (active, partial, staged, v1), `guards.on-failure`, `guards.selftest`, `guards.deny-drift` (the effective deny rules against the suggested deny block). A failed self-test caps the effective level at **L0**; unwired guards cap it at L1.

## Settings templates (section 7.2)

`.claude/templates/settings/` (generated by `wiring.py --write`; the project's own settings are never overwritten by an upgrade): `interactive.settings.json` (allow template with named scripts, ask rules, the deny block, sandbox), `headless-child.settings.json` (the driver's `--settings`: `dontAsk`, explicit allow list, no project MCP servers, `Workflow` denied, sandbox, the kit hooks), `critic.settings.json`, `deny-baseline.json`. See the README next to them.

## What the guards cannot see

Stated plainly, so nobody relies on a claim the code does not make. Each of these is covered by the boundary (B2 no credentials, B3 sandbox and `denyWrite`, B4 CODEOWNERS and rulesets), not by a guard:

- **Arbitrary code.** A program the agent writes and runs can do anything the OS user can. The guards read shell, inline interpreter code and scripts the agent wrote or changed (untracked or modified); they do not execute or model them. A Python or Node file that is tracked and unchanged is trusted as reviewed code. Obfuscation that does not use `exec`/`eval` with a decode (for example a path rebuilt from computed arithmetic in a script file) is not caught.
- **Imports.** A script's imported modules are not followed. `node -r ./preload.js` and `python -c "import sitecustomize"` style preloads are not followed.
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
