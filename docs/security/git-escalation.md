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
- `loopctl`'s own git calls ignore the user's global and system git config and switch off hooks, the pager, the editor and external diff drivers (`${CLAUDE_PLUGIN_ROOT}/scripts/loopctl/lgit.py`), so a planted repository config cannot make a loopctl call run code. It does not hash `.git/config` at run start; the sandbox `denyWrite` above is the control.
- The guard-git guardrail blocks `-c core.*`, `-c alias.*`, `git config` writes, `GIT_*` assignments, `--no-verify`, `update-ref`, `filter-branch`, `replace`, `tag -f` and `gh api` with any method other than GET ([guards.md](guards.md)).
- `npm ci --ignore-scripts` by default ([supply-chain.md](supply-chain.md)).
- `.gitattributes`, `.husky/**`, `.envrc` and `package.json` scripts join the test-integrity set.
- Git-native hooks (pre-commit, commit-msg) are conveniences. Every gate that matters is re-checked by the loop at merge time and by CI as a required check.

## The push tier

`git_autonomy: branch+commit+push` lets the main session (the registered conductor, or an unregistered session with no role) push its own feature branch; subagents never push. It does not open an escalation path, because the push is held to one shape ([guards.md](guards.md#guard-git)):

- the tier and `delivery: push-branch|draft-pr` must be set in the working tree **and** in `loop.config.md` at the base sha, so an agent that edits the config in its branch does not raise its own tier (`guard-git:push-config`);
- one existing local lane branch (`feat/`, `quick/`, `fix/`, `chore/` or `docs/`) to the same name on a configured remote, never a protected branch or the remote's default branch, never a URL, a path, a wildcard, a rename, tags, `--mirror`, `--delete`, `-o` push options, `--receive-pack`/`--exec` or `--no-verify`;
- no force, with one exception: `git push --force-with-lease=<lane>/<b>:<sha> origin <lane>/<b>`, a lease that names the pushed branch and the exact sha it expects there (a bare `--force`, `+refspec` or a lease without the sha is refused);
- a branch whose `branch.<b>.merge` names another branch (one created from `origin/main` tracks main) is pushed only as `<b>:<b>`, because a bare `git push origin <b>` would follow the upstream;
- the guard reads git config with its own `HOME` and `XDG_CONFIG_HOME`, and a command that runs git or gh may not set `HOME`, `XDG_CONFIG_HOME`, `PREFIX` or `GH_REPO`, so an agent cannot point them at a global config (aliases, `url.*.insteadOf`, `push.default`) it wrote; the dashed binaries (`git-core/git-push`, `git-lfs`, `git-send-pack`) are checked as `git <sub>`;
- a remote whose config carries a push refspec or `mirror` is refused, so a planted `.git/config` cannot turn a plain push into a mirror (the sandbox `denyWrite` on `.git/config` is the stop that keeps that config unplanted);
- a tainted session does not push;
- `gh pr create` is allowed only as `--draft` under `delivery: draft-pr`; `gh pr ready` and `gh pr merge` stay human.

The server side is the boundary: branch protection or a ruleset on the protected branches, required checks and CODEOWNERS review (B4, [rulesets.md](rulesets.md)). The agent's credential must not be able to push to a protected branch or bypass a ruleset, whatever the guard says.

## The doctor reads hostile repositories safely

The doctor reads repositories an agent can write. Its own git calls therefore run with `core.fsmonitor=false` and `core.hooksPath=/dev/null`, with `GIT_DIR` and the other repository-redirecting variables removed from the environment, so a planted fsmonitor or hook script cannot run while the doctor checks the repo. It reads files only if they are regular files (a named pipe or device in place of `settings.json` is reported, not opened) and caps their size. `cred.git.config` reports the credential-bearing keys above at every config level, including submodule and per-worktree configs, and `config.dirty` warns when `loop.config.md` has uncommitted edits (attestations count only when a human committed them).
