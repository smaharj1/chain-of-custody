# Git escalation paths

An engineer with only "read-only git" can still escalate if it can write the files git or the toolchain executes later, in the **conductor's** context (report section 5.9).

## The paths

- `.git/hooks/*`
- `.git/config` keys: `core.hooksPath`, `core.fsmonitor`, `core.sshCommand`, `filter.*`, `include.path`, `credential.helper`
- `.gitattributes` filters
- `package.json` scripts: `postinstall`, `prepare`, `pretest`
- `.husky/`
- `.envrc`

## What closes them

- **B3:** the sandbox denies writes to `$(git rev-parse --git-common-dir)/{hooks,config,info}`. The sandbox snippet lists `.git/hooks`, `.git/config` and `.git/info`; for a linked worktree, add the absolute path of the common git directory ([sandbox.md](sandbox.md)).
- The conductor runs git as `git -c core.hooksPath=<kit hooks dir> -c core.fsmonitor=false ...`.
- `loopctl` hashes `.git/config` at run start and refuses to continue on a change (loopctl, later phase).
- The guard-git guardrail blocks `-c core.*`, `-c alias.*`, `git config` writes, `GIT_*` assignments, `--no-verify`, `update-ref`, `filter-branch`, `replace`, `tag -f` and `gh api` with any method other than GET (Phase 2).
- `npm ci --ignore-scripts` by default ([supply-chain.md](supply-chain.md)).
- `.gitattributes`, `.husky/**`, `.envrc` and `package.json` scripts join the test-integrity set.
- Git-native hooks (pre-commit, commit-msg) are conveniences. Every gate that matters is re-checked by the loop at merge time and by CI as a required check.

## The doctor reads hostile repositories safely

The doctor reads repositories an agent can write. Its own git calls therefore run with `core.fsmonitor=false` and `core.hooksPath=/dev/null`, with `GIT_DIR` and the other repository-redirecting variables removed from the environment, so a planted fsmonitor or hook script cannot run while the doctor checks the repo. It reads files only if they are regular files (a named pipe or device in place of `settings.json` is reported, not opened) and caps their size. `cred.git.config` reports the credential-bearing keys above at every config level, including submodule and per-worktree configs, and `config.dirty` warns when `loop.config.md` has uncommitted edits (attestations count only when a human committed them).
