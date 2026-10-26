# Bootstrap: from a copied-in kit to a working install

`/bootstrap` takes a repository, new or old, from "I copied `.claude/` in" to a working install, and leaves a record of what it did. It follows the rule the rest of the kit follows: **the agent prepares, a human applies.** Creating a repository, setting rulesets, applying labels, changing a guard key in `loop.config.md` and enabling CI workflows are all things you run yourself, from a plain terminal, after reading what they will do.

## What you get

```
/bootstrap
   |  detect (read-only)  ->  interview  ->  stack into CLAUDE.md  ->  config PLAN  ->  setup SCRIPT
   v
 you:  sh .claude/bootstrap/setup.sh local   (dry run, read it, then --apply)   edits files; you commit
       sh .claude/bootstrap/setup.sh remote  (dry run, read it, then --apply)   talks to GitHub
   v
/bootstrap again:  doctor, workflow lint, loopctl checks  ->  receipt marked complete
```

Everything bootstrap writes that you review lives in `.claude/bootstrap/` (ignored by git): the config plan and the setup script. The one thing it commits is `.claude/bootstrap-receipt.json`.

## Before you start

- The kit is installed (`.claude/` is in the repository root). Bootstrap does not install it; `/init` and `/migrate` do that in the packaging phase.
- For a new project, a folder with no code is fine. Bootstrap asks you to run `git init -b main` and the first commit yourself, because `git_autonomy` is `none` by default.
- Have `gh` installed and logged in (`gh auth login`) **only for the remote phase, and only in your own terminal**. The agent never has your GitHub credentials.

## Greenfield and existing repositories

| | Greenfield | Existing |
|---|---|---|
| Stack | You choose from `.claude/context/choosing-your-stack.md` with a recommendation | Read from the manifests; you confirm |
| Primers | Written from the decided stack; evidence-dependent parts marked `TODO(primer)` | Derived from the code, following `.claude/context/primer-protocol.md` |
| Verify commands | Provisional: they describe an app that does not exist yet, so `/orchestrate` refuses to start until the first plan item makes them real | Suggested from the manifests, then **proven in the sandbox** with `probe_run.py` (it lists the commands and a digest; you say go) |
| Repository on GitHub | The setup script can create it (`--create-repo`) and make the first push | Already there; the script only sets rules, labels and variables |
| Next mode | `/planner` (the first item is the scaffold) | `/pm` or `/tech-lead` |

## The pieces

| File | What it is | Who runs it |
|---|---|---|
| `.claude/commands/bootstrap.md` | The mode: the interview and the order of steps | you invoke `/bootstrap` |
| `.claude/scripts/bootstrap_detect.py` | Read-only facts: mode hint, stack, suggested verify commands, deploy hosts, release automation, kit and receipt state | the agent |
| `.claude/scripts/bootstrap_config.py` | `plan`: check keys and values against the registry and write a plan. `apply`: edit `loop.config.md` from a plan | the agent plans; **you** apply |
| `.claude/scripts/github/repo_script.py` | Prints the setup script (`.claude/bootstrap/setup.sh`). It only writes text | the agent |
| `.claude/bootstrap/setup.sh` | The script itself: `local` and `remote` phases, a dry run unless `--apply` | **you** |
| `.claude/scripts/github/apply-labels.sh` | The factory labels; the setup script calls it, and you can run it alone | **you** |
| `.claude/scripts/bootstrap_receipt.py` | The receipt | the agent |

All human-run scripts refuse to start when `CLAUDECODE` is set (a Claude Code session). That is a tripwire, not a lock: `env -u CLAUDECODE` removes it, and then the script cannot tell. What actually keeps an agent out is that it holds no GitHub credential, the guards deny `gh` mutations, and you read the dry run before you add `--apply`.

## The config plan

Almost every `loop.config.md` key is H6: it changes caps, gates, delivery, verify commands or an attestation, and only a human changes those. So bootstrap does not edit the file.

```
python3 .claude/scripts/bootstrap_config.py plan --set solo_maintainer=true --set verify_tests="pnpm test"
    -> checks each key against the registry (known, right type, allowed value, evidence gate)
    -> writes .claude/bootstrap/config-plan.json; changes no config

python3 .claude/scripts/bootstrap_config.py apply .claude/bootstrap/config-plan.json            dry run: prints the diff
python3 .claude/scripts/bootstrap_config.py apply .claude/bootstrap/config-plan.json --write    you, in a terminal
```

`apply` does not trust the plan file: it checks every key again, edits only the value on the planned lines (comments and every other line stay), parses the result, and writes only if the planned keys have exactly the planned values and nothing else changed. Keys whose value is a command (`verify_tests`, ...) are flagged: they run later, so read them. Commit the result yourself.

## The setup script

```
sh .claude/bootstrap/setup.sh local            prints; edits nothing
sh .claude/bootstrap/setup.sh local  --apply   applies the config plan, copies the chosen workflows (never over an existing file), installs the launch-app skill
   ... pin each `uses:` to a commit SHA, run python3 .claude/scripts/ci_lint.py, commit ...
sh .claude/bootstrap/setup.sh remote           prints; sends nothing
sh .claude/bootstrap/setup.sh remote --apply   gh repo create + first push (greenfield), rulesets and the squash setting (apply-boundary.sh),
                                               labels (apply-labels.sh), the FACTORY_APPROVERS variable
```

The phases are split because a ruleset has to come after the first push, and a config or workflow edit has to be committed before it. Every value in the script was checked against a strict pattern when it was generated and is shell-quoted; a repository name, branch, login or description cannot become shell syntax. The script's SHA-256 is in the receipt, so you can check that the file you read is the file the agent made (`sha256sum .claude/bootstrap/setup.sh`).

It never creates the bot identity (`docs/security/bot-identity.md`), never sets a secret (run `gh secret set ANTHROPIC_API_KEY` yourself: it prompts, and the key is never in a file), never merges and never deploys. It ends with the list of what is still yours.

## The receipt

`.claude/bootstrap-receipt.json` holds one entry per step: the id, the command, the exit code, the SHA-256 of the output, and whether the agent ran it (`source: ran`) or was told about it (`source: recorded`). Entries are chained: each carries a hash of the one before, so an edit, a removal or a reordering shows up.

```
python3 .claude/scripts/bootstrap_receipt.py show     the summary and the commands still yours to run
python3 .claude/scripts/bootstrap_receipt.py check    validate the file and the chain (exit 1 on any problem)
python3 .claude/scripts/bootstrap_receipt.py status   none | in-progress | complete | invalid
```

It is a record for whoever reviews the pull request that installs the kit. It is not a gate and not proof: someone who can write the file can write a receipt that checks out. What it does catch is a half-finished bootstrap, a step that failed and was never retried, and a setup script that changed after it was recorded.

Rules the writer keeps, because the file lives in a repository an attacker may have shaped:

- The path is fixed. A symlink, pipe, directory or oversized file in its place is refused, and so is a `.claude` that links out of the repository.
- A damaged or hostile receipt is never appended to. `init --force` renames it aside and starts again.
- `run` executes only the kit's own read-only scripts (the doctor, `ci_lint.py`, `bootstrap_detect.py`, `probe_run.py` without `--go`, `guards/wiring.py --check`, and the `doctor`, `validate` and `status` subcommands of loopctl), by exact path, with an allowlist of flags, no shell, a timeout and a scrubbed environment. It cannot run `gh`, `git`, a package manager, `verify.py` or anything in your project.
- Free text is length-limited, stripped of control characters and redacted for secrets before it is stored.

## More than one human

Bootstrap asks whether this is a solo project or a team, and the answer sets three things:

- **Rulesets:** `solo` (one human admin may bypass in pull-request mode only) or `team` (nobody bypasses; releases go through a named team).
- **Approvers:** `factory_approvers` lists the logins whose labels count. `label-guard.yml` removes `factory:*`, `plan-ok` and `preview` labels applied by anyone else, and `intake.yml` checks the same list itself. The setup script sets the matching `FACTORY_APPROVERS` repository variable.
- **CODEOWNERS:** path-scoped review by named people or teams (`.claude/templates/security/CODEOWNERS.template`). It is not the approver list: a label is not path-scoped.

Two people running the loop at once is a different problem. What exists today: the local run lock covers one machine. Between machines, claim a piece of work by hand: put `factory:running` on the tracking issue and assign yourself, and take both off when you stop. A shared lock that does this for you (an advisory ref on the remote, `refs/loopctl/lock/<plan>`) arrives with the loopctl extras as an opt-in feature. It is coordination, not a security boundary: anyone with write access to the repository can move that ref.

## When something goes wrong

| You see | It means | Do |
|---|---|---|
| `refusing to run inside a Claude Code session` | A human-run script was started from the session | Run it from a plain terminal |
| `the existing receipt is not usable` | The file fails validation (edited, truncated, or not ours) | `bootstrap_receipt.py check` names the first problem. Look at the file, then `init --force` |
| `another bootstrap_receipt run holds ...lock` | A run was killed while writing | Delete `.claude/bootstrap-receipt.json.lock` if nothing is running |
| `needs the evidence gate` from `plan` | You asked for an opt-in feature the registry gates | It stays off until `docs/baseline/evidence.json` is complete |
| `is a block value in this file: edit it by hand` | The key is written as a multi-line block, which `apply` will not rewrite | Edit that key yourself |
| The doctor reports L1 | Nothing is attested yet. That is where every install starts | `python3 .claude/scripts/doctor.py --level` lists what blocks the next level |

## Related

- `docs/ops.md`: the intake, dependency-check and smoke workflows, the labels, and the parts that are design only.
- `docs/release-automation.md`: release-please, changesets and the other release tools; why their pull requests are human.
- `docs/delivery.md`: the `delivery` dial and the verify commands bootstrap proposes.
- `docs/security/`: the rulesets, the bot identity and the deploy-surface attestation the setup script points at.
