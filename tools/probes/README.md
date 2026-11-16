# Probes: confirming the platform assumptions v2 was built on

The v2 build ran **no probe** (IMP-7). For every open item in report §17 it assumed the
conservative answer and recorded that in `docs/design/probe-assumptions.md`. This directory is the
harness that lets you confirm or overturn each assumption in minutes instead of hours.

Everything here that starts `claude` is **human-run**. The scripts only prepare; they refuse to run
inside a Claude Code session (`CLAUDECODE` set) and never start `claude` themselves.

## How a probe works

```
tools/probes/make-scratch.sh "$HOME/probes/v1" --probe V1     # prepare (add --dry-run to preview)
export SCRATCH=...  LOG=...                                   # printed by the command above
cd "$SCRATCH" && claude                                       # run the steps in the section below
python3 tools/probes/analyze.py V1 "$LOG"                     # read the answer out of the hook log
```

`make-scratch.sh` builds a git repo **outside** this one, installs `tools/baseline/observe.py` as a
hook-input logger on every hook event (writes `<scratch>.observe.jsonl`), merges the probe's snippet
from `tools/probes/settings/` into `.claude/settings.json`, copies the probe's agents, scripts and
prompt (`PROBE-PROMPT.txt`), and writes `<scratch>.probe.json` (variant, commits to compare against).
Probe hooks (`.claude/probe/probe_hook.py`) write *marker* lines into the same log, so one file tells
the whole story.

Rules for every probe:

1. Open a normal terminal. Run `claude --version` and write the version down; every answer is
   "as of this version". The floor is v2.1.302 (report §14); run on the version you will pin.
2. **One scratch repo per run** (per variant for V9, V18, V23). Never reuse one; the log would mix runs.
3. Run the probe from the scratch directory. Do not open the scratch directory in a session you care
   about: some probes ask the model to try things that are meant to be refused.
4. Each run costs a few cents. Add `--max-budget-usd 1` to any `claude -p` line below if you want a cap.
5. If `claude` reports a settings error naming a hook event, the event name list is wrong for your
   version: rerun `make-scratch.sh` with `--events` and a shorter list, and tell the maintainer
   (`ALL_EVENTS` in `tools/probes/make_scratch.py` is the one constant to fix).
6. Settings keys that the report names but the platform docs may spell differently
   (`onFailure`, `sandbox.failIfUnavailable`, `sandbox.filesystem.denyRead`,
   `CLAUDE_CODE_FORK_SUBAGENT`) are used exactly as the report spells them. A setting that seems to do
   nothing is a finding; record it, then try the platform's own spelling.
7. When the run is over, record the result (below) and delete the scratch repo, its `.observe.jsonl`,
   `.probe.json` and `.remote.git`.

**Recording.** Copy `docs/design/probe-results.template.md` to `docs/design/probe-results.md` and add
one row per probe run: V-item, date, Claude Code version, OS, what you observed, the verdict
(confirms the assumption / overturns it / inconclusive), and the follow-up. If a verdict overturns an
assumption, `docs/design/probe-assumptions.md` says what changes and where in the code.

Closed or moot, no harness: **V2** (rules load in subagents: yes, P21), **V10** (`run_script` code
visibility: moot, cloud MCP is deny-only, P39), **V11** (`--max-budget-usd` covers subagents: yes, as
an estimate, P26).

## Order

Do these first; they gate the most:

| # | Probe | Gates | Time | Needs |
|---|---|---|---|---|
| 1 | [V1](#v1) agent identity at depth 2-3 | roster-aware guards beyond depth 1 | 10 min | interactive + one `-p` |
| 2 | [V5](#v5) driver child inheritance | `runner: driver` | 20 min | `-p` |
| 3 | [V9](#v9) `onFailure: block` forms | fail-closed claims | 30 min | 4 scratch repos |
| 4 | [V18](#v18) fork-mode wait semantics | event-driven dispatch rewrite | 45 min | 3 scratch repos |
| 5 | [V19](#v19) sandbox `failIfUnavailable` | B3 at L2+ | 15 min per OS | each OS |
| 6 | [V22](#v22) `--agents` plus `--agent` in `-p` | driver launch line | 15 min | `-p` |
| 7 | [V24](#v24) per-subagent sandbox | held-out isolation | 15 min | sandbox on |
| 8 | [V27](#v27) GitHub plan features | §8.4 platform table | 30 min | three repos on three plans |

Then, in order: [V3](#v3), [V4](#v4), [V6](#v6), [V7](#v7), [V8](#v8), [V12](#v12), [V13](#v13),
[V14](#v14), [V15](#v15), [V16](#v16), [V17](#v17), [V20](#v20), [V21](#v21), [V23](#v23),
[V25](#v25), [V26](#v26).

---

## V1

**Purpose.** Does hook input carry `agent_id` and `agent_type` for a subagent at depth 2 and 3, for an
agent inside a Workflow, and for a `-p --agent` driver child (P1, §5.7, §7.1)?

**Blocks.** Roster-aware guards beyond depth 1.

**The build assumes.** Agent-blind: a call with `agent_id` but a missing or unknown `agent_type` is
treated as an engineer (restrictive). A call with no `agent_id` and an unregistered `session_id` gets
least privilege whatever `agent_type` says.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v1" --probe V1`

**Steps.**

A. Nested subagents, interactive. `cd "$SCRATCH" && claude`, then paste:

```
Dispatch the subagent probe-outer with the prompt "Follow your instructions." Do nothing else. When it returns, reply with one line: v1 interactive done.
```

B. Driver child (from a normal terminal, not inside a session):

```
cd "$SCRATCH" && claude -p --agent probe-outer --permission-mode dontAsk --allowedTools "Bash(echo probe-v1-*)" "Follow your instructions."
```

(`dontAsk` plus an explicit `--allowedTools`, because `-p` in an untrusted directory does not apply the project's allow rules; see V5.)

C. Workflow agents (optional; needs the workflow-authoring skill). In an interactive session ask Claude
to write a saved workflow named `probe-v1` with one agent that runs the Bash command
`echo probe-v1-workflow`, then to run it. Accept the Workflow opt-in prompt.

**Observe.** `python3 tools/probes/analyze.py V1 "$LOG"` prints, per label (`probe-v1-depth-1` ...),
whether `agent_id` and `agent_type` were present.

| You see | It means |
|---|---|
| depth 1, 2, 3 all show `agent_id` and the right `agent_type` | roster-aware guards can reach depth 3 (a later improvement; the build stays agent-blind until then) |
| depth 2 or 3 shows `agent_id` but no `agent_type` | agent-blind is required, as built |
| a nested depth shows no `agent_id` | a nested subagent looks like its parent session; blocker, tell the maintainer |
| driver child (B): SessionStart and Bash events carry `agent_type`, no `agent_id` | matches P1/RC-16; the registered-session rule is what classifies it |
| Workflow agent (C): note both fields | decides whether Workflow agents need their own rule |

**Record.** Row V1 in the results file, with the per-depth table pasted into "observation".

---

## V5

**Purpose.** What does a `claude -p --agent <role>` child inherit: project hooks, project
`permissions.allow`, the sandbox setting, the parent's environment, and which branch/commit does it
start on (§11.1, §5.8)?

**Blocks.** `runner: driver`.

**The build assumes.** Nothing is inherited. The driver passes `--settings`, `--agents`,
`--allowedTools` and `--permission-mode` explicitly (and `loopctl` creates the worktree).

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v5" --probe V5`. The scratch is on branch
`feature/probe-v5`, one commit ahead of `main`; the manifest records both commits.

**Steps.** Run these **before** ever opening the scratch directory interactively, so it is untrusted,
which is the realistic driver case. `P` below is the prompt:

```
Run these six Bash commands, one per call, in this order: echo probe-v5-start ; git branch --show-current ; git rev-parse --short HEAD ; pwd ; printenv PROBE_V5_ENV ; touch ~/.probe-v5-outside
```

1. Project settings only:
   `cd "$SCRATCH" && PROBE_V5_ENV=parent claude -p --agent probe-role --permission-mode dontAsk "P"`
2. Plus an explicit settings file:
   `... claude -p --agent probe-role --permission-mode dontAsk --settings "$SCRATCH/.claude/probe/headless-settings.json" "P"`
3. Explicit surface only, project settings excluded:
   `... claude -p --agent probe-role --permission-mode dontAsk --setting-sources user --settings "$SCRATCH/.claude/probe/headless-settings.json" --allowedTools "Bash(echo probe-v5-start),Bash(git branch --show-current),Bash(git rev-parse --short HEAD),Bash(pwd),Bash(printenv PROBE_V5_ENV),Bash(touch ~/.probe-v5-outside)" "P"`
4. From a linked worktree:
   `git -C "$SCRATCH" worktree add "$SCRATCH-wt" -b feature/probe-v5-wt main`, then run line 1 from `$SCRATCH-wt`.
5. Trusted case: open `claude` in `$SCRATCH` once, accept the trust dialog, `/exit`, rerun line 1.

Pass `--allowedTools` as one comma-separated argument, as above (a space-separated list would swallow the
prompt). Afterwards delete `~/.probe-v5-outside` if a run created it and `git worktree remove "$SCRATCH-wt"`.

**Observe.** `python3 tools/probes/analyze.py V5 "$LOG"` prints one block per session (in time order,
1 to 5 above): which marker hooks fired, the PermissionRequest count and each command's result.

| Question | Reading |
|---|---|
| Project hooks inherited? | marker `v5-project-hook` present in session 1 |
| `--settings` hooks added on top? | `v5-child-settings-hook` present in session 2 |
| Project `permissions.allow` applied (untrusted `-p`)? | session 1 commands `ok` rather than `no-post`/`failed` (the report says they are not applied untrusted) |
| Sandbox inherited? | `touch ~/.probe-v5-outside` is `failed` where the setting applied, `ok` where it did not (session 3 has no project settings) |
| Environment inherited? | `printenv PROBE_V5_ENV` prints `parent` |
| Where does it branch from? | session 1 prints `feature/probe-v5` and its commit; session 4 prints the worktree's branch |
| Trust matters? | compare session 1 with session 5 |

**Record.** One row; list which of the six facts held. Anything inherited that the build assumed
not inherited is a follow-up in `docs/design/probe-assumptions.md` (V5).

---

## V9

**Purpose.** Does `"onFailure": "block"` on a hook handler make a missing interpreter, a crashing
script and a timeout fail closed, on PreToolUse, ConfigChange and PermissionRequest (P2, RC-30)? Is the
field's name and placement right at all (IMP-16)?

**Blocks.** Every fail-closed claim that rests on the platform rather than on the kit's own scripts.

**The build assumes.** Nothing from the platform: every kit hook entry point fails closed itself
(exit 2 on any exception) and the liveness canary reports non-firing hooks. `onFailure: block` is
wired everywhere but not depended on.

**Prepare.** Four scratch repos, one per row:

```
tools/probes/make-scratch.sh "$HOME/probes/v9-missing" --probe V9 --variant missing-interpreter
tools/probes/make-scratch.sh "$HOME/probes/v9-crash"   --probe V9 --variant crash
tools/probes/make-scratch.sh "$HOME/probes/v9-timeout" --probe V9 --variant timeout
tools/probes/make-scratch.sh "$HOME/probes/v9-control" --probe V9 --variant crash --control
```

The control is the crash variant with `onFailure` removed: it shows the platform default.
Each variant puts the failing hook on PreToolUse (matcher `Bash`), PermissionRequest (matcher `Write`)
and ConfigChange. The timeout variant sets `timeout: 2` on a handler that sleeps 30 s.

**Steps (per scratch repo).** `cd "$SCRATCH" && claude`, then paste:

```
Do these three things in order and report exactly what happened for each, including any error or denial text. Do not retry anything. 1. Run the Bash command: echo probe-v9-bash. 2. Use the Write tool to create the file probe-v9-write.txt with the content x. 3. Wait until I say "config edited", then use the Read tool to read the file probe-v9-secret.txt.
```

If a permission dialog appears for Write, note that it appeared and answer No. When Claude is waiting
at step 3, in a second terminal:

```
printf '{"permissions":{"deny":["Read(./probe-v9-secret.txt)"]}}\n' > "$SCRATCH/.claude/settings.local.json"
```

Then tell Claude `config edited`.

**Observe.** `python3 tools/probes/analyze.py V9 "$LOG"`, plus what you saw on screen.

| Event | Fail-closed looks like | Fail-open looks like |
|---|---|---|
| PreToolUse (Bash) | `echo probe-v9-bash` result `no-post`; transcript shows a denial | result `ok`; the command ran |
| PermissionRequest (Write) | Write denied with no dialog, no file created | the normal dialog still appeared, or the file was written |
| ConfigChange | the Read **succeeds** and prints `secret-marker-v9`: the deny-rule edit was blocked | the Read is denied: the edit took effect despite the failing hook |

Compare each variant with the control. A missing-interpreter row has no marker by construction; its
evidence is the Bash and Write outcomes only.

**Record.** One row per variant and event (nine cells plus the control). Note the exact denial text:
it is what the kit's deny messages should be compared against.

---

## V18

**Purpose.** Fork-mode env names and wait semantics: with fork mode on or off, for a direct child, a
nested spawner and a `-p --agent` driver child, interactive and `-p`, does the parent block until the
child finishes (§11.3, P36, RC-29)? Do `CLAUDE_CODE_FORK_SUBAGENT` and
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` exist under those names and do they change anything?

**Blocks.** §11.3 driver settings and the event-driven dispatch rewrite (§4 G1). Run on the version
you will pin as the floor.

**The build assumes.** Dispatch is event-driven everywhere (a valid report file plus a loopctl state
transition means done, never "the Agent call returned"); the driver sets the fork env explicitly per
runner; it never trusts a blocking return.

**Prepare.** Three scratch repos, one per variant:

```
tools/probes/make-scratch.sh "$HOME/probes/v18-on"    --probe V18 --variant fork-on
tools/probes/make-scratch.sh "$HOME/probes/v18-off"   --probe V18 --variant fork-off
tools/probes/make-scratch.sh "$HOME/probes/v18-nobg"  --probe V18 --variant fork-off-nobg
```

The variants set `CLAUDE_CODE_FORK_SUBAGENT` to `1` or `0`, and (nobg) `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`,
through the settings `env` block. If you suspect `env` is applied too late for these flags, also run the
variant with the variables exported in the shell (`CLAUDE_CODE_FORK_SUBAGENT=0 claude ...`) and note
which form had an effect. The slow child sleeps 20 s (`V18_SLEEP` overrides it).

**Steps (per scratch repo, in this order; the analyzer lists sessions in time order).**

1. Interactive, direct child. `cd "$SCRATCH" && claude`, paste:

```
Dispatch the subagent probe-slow with the prompt "Follow your instructions." As your very next action, with no waiting, run the Bash command: echo probe-v18-parent-after. Then reply with one line: v18 direct done.
```

2. Interactive, nested spawner (new `claude` session), paste:

```
Dispatch the subagent probe-spawner with the prompt "Follow your instructions." Do nothing else. When it returns, reply with one line: v18 nested done.
```

3. `-p`, direct child (`-p` flags: `A` = `--permission-mode dontAsk --allowedTools "Bash(echo probe-v18-*),Bash(sh .claude/probe/slow.sh *)"`): `claude -p A "Dispatch the subagent probe-slow with the prompt \"Follow your instructions.\" As your very next action, with no waiting, run the Bash command: echo probe-v18-parent-after. Then reply with one line: v18 direct done."`
4. `-p`, nested spawner: `claude -p A "Dispatch the subagent probe-spawner with the prompt \"Follow your instructions.\" Do nothing else. When it returns, reply with one line: v18 nested done."`
5. Driver child (`-p --agent`): `claude -p A --agent probe-spawner "Follow your instructions."`

**Observe.** `python3 tools/probes/analyze.py V18 "$LOG"` prints per session when the slow child
started and finished, when the parent's next action ran, and `parent_waited`.

| Reading | Meaning |
|---|---|
| `parent_waited: False`, the next action ran seconds after the dispatch | the parent did not block (fork-style, non-blocking) in that cell |
| `parent_waited: True`, the next action ran after the child finished | the Agent call blocked in that cell |
| fork-on and fork-off give the same result in every cell | the env names do nothing (or `env` is applied too late); try the shell form |
| `-p` result differs from interactive | the report's per-surface split (terminal v2.1.232, `-p` v2.1.302) is real |

**Record.** The 5x3 grid of `parent_waited` values, plus whether the env names changed behavior.

---

## V19

**Purpose.** Does `sandbox.failIfUnavailable: true` make the session refuse to work when the sandbox
cannot start, on macOS, Linux and a devcontainer (P38, B3)?

**Blocks.** B3 at L2 and above.

**The build assumes.** Unknown. The doctor reads the settings and requires a human attestation
`sandbox_verified: <date>`; unknown means L1.

**Prepare (once per OS).** `tools/probes/make-scratch.sh "$HOME/probes/v19" --probe V19`, and the control
`... --probe V19 --control` (the same without `failIfUnavailable`).

**Steps.** In each environment:

1. Healthy sandbox. `cd "$SCRATCH" && claude`, paste the prompt below, note the results, `/exit`.

```
Run these three Bash commands, each in its own tool call, and report the exact result or error of each without retrying. 1. touch probe-v19-inside.txt 2. touch ~/.probe-v19-outside 3. ls -la ~/.probe-v19-outside
```

   Expected: command 1 works, commands 2 and 3 fail (the sandbox blocks writes outside the project).
2. Make the sandbox unavailable and repeat with the real scratch and with the control scratch. One way
   per OS (any other is fine; write down what you did):
   - Linux: start `claude` with a `PATH` that does not contain `bwrap` (and `socat`), or run it where
     unprivileged user namespaces are off.
   - macOS: run `claude` inside an outer sandbox so the nested one cannot start:
     `sandbox-exec -p '(version 1)(allow default)' claude`.
   - Devcontainer: run without extra privileges and with the default seccomp profile.
3. Remove `~/.probe-v19-outside` afterwards if it exists.

**Observe.** `python3 tools/probes/analyze.py V19 "$LOG"` plus the terminal text.

| You see | Meaning |
|---|---|
| `claude` refuses to start, or the first Bash call is refused with a sandbox message (log has no Bash `ok`) | fail-closed: `failIfUnavailable` works on this OS. Copy the message |
| session works and the outside write is `ok` | fail-open: the doctor must not accept `failIfUnavailable: true` as proof; L2+ needs the attestation or a container |
| the control behaves like the real scratch | the key does nothing here |

**Record.** One row per OS and per variant. A pass on one OS says nothing about the others.

---

## V22

**Purpose.** Do `--agents <json>` and `--agent <role>` work together in `-p`, including with
plugin-namespaced agents (`probe-plugin:plugin-role`) and when an inline definition has the same name as
a project agent?

**Blocks.** The driver launch line (§11.1 item 2).

**The build assumes.** The line is built and unit-tested only against the fake `claude`; the first real
run is this probe.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v22" --probe V22`. The scratch has a project
agent `project-role` and a plugin `probe-plugin` (agent `plugin-role`) in `$SCRATCH/probe-plugin`.

**Steps.** From the scratch directory, in a normal terminal, run each line in order. `S` is
`--settings "$SCRATCH/.claude/settings.json" --permission-mode dontAsk`:

```
claude -p S --agent project-role "Follow your instructions."
claude -p S --agents '{"inline-role":{"description":"V22 inline role","prompt":"Run the Bash command `echo probe-v22-inline-role`, then reply done.","tools":["Bash"]}}' --agent inline-role "Follow your instructions."
claude -p S --plugin-dir "$SCRATCH/probe-plugin" --agent probe-plugin:plugin-role "Follow your instructions."
claude -p S --plugin-dir "$SCRATCH/probe-plugin" --agent plugin-role "Follow your instructions."
claude -p S --plugin-dir "$SCRATCH/probe-plugin" --agents '{"inline-role":{"description":"V22 inline role","prompt":"Run the Bash command `echo probe-v22-inline-role`, then reply done.","tools":["Bash"]}}' --agent inline-role "Follow your instructions."
claude -p S --agents '{"project-role":{"description":"override","prompt":"Run the Bash command `echo probe-v22-project-role-INLINE`, then reply done.","tools":["Bash"]}}' --agent project-role "Follow your instructions."
```

Replace `S` with its expansion on each line. Save each line's stdout and exit code.

**Observe.** `python3 tools/probes/analyze.py V22 "$LOG"` lists one row per session in line order.

| Line | A working launch shows |
|---|---|
| 1 | agent_type `project-role`, label `probe-v22-project-role` (the baseline) |
| 2 | agent_type `inline-role`, label `probe-v22-inline-role` |
| 3 | agent_type `probe-plugin:plugin-role` (or `plugin-role`), label `probe-v22-plugin-role` |
| 4 | tells you whether the unprefixed name resolves |
| 5 | an inline role next to `--plugin-dir` |
| 6 | label `probe-v22-project-role-INLINE` = inline wins; `probe-v22-project-role` = the project file wins |

A line that errors writes nothing to the log; its evidence is the exit code and stderr you saved.

**Record.** One row; the working form of the launch line goes in the follow-up, spelled exactly.

---

## V24

**Purpose.** Does a subagent's Bash honor a different sandbox from its parent (RC-6, RC-31, P42)? Can
agent frontmatter relax or tighten the sandbox?

**Blocks.** Held-out test isolation under `in-session` and `sequential` runners.

**The build assumes.** No per-subagent sandbox. Held-out tests are driver-only or CI-only.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v24" --probe V24`. The sandbox denies reads of
`$SCRATCH/heldout`, which holds `secret-v24.txt`. Three agents: `probe-sb-plain` (no sandbox key),
`probe-sb-loose` (frontmatter tries `sandbox: {enabled: false}`) and `probe-sb-strict` (frontmatter tries
to deny reading `README.md`). The frontmatter keys are guesses; "ignored" is the expected answer.

**Steps.** `cd "$SCRATCH" && claude`. First paste (the control: the parent must be denied):

```
Run the Bash command: sh .claude/probe/read-heldout.sh probe-v24-main heldout/secret-v24.txt
```

Then:

```
Dispatch the subagents probe-sb-plain, probe-sb-loose and probe-sb-strict one after another, each with the prompt "Follow your instructions." Do nothing else. Reply with one line: v24 done.
```

**Observe.** `python3 tools/probes/analyze.py V24 "$LOG"` prints `READ` or `DENIED/ERROR` per row.

| main | plain | loose | strict | Meaning |
|---|---|---|---|---|
| DENIED | DENIED | DENIED | READ | the assumption: one shared sandbox; frontmatter sandbox keys ignored |
| DENIED | READ | any | any | subagent Bash is outside the parent's sandbox: held-out isolation in-session is impossible |
| DENIED | DENIED | READ | any | frontmatter can switch the sandbox off: a boundary hole, report it |
| DENIED | DENIED | DENIED | DENIED | frontmatter sandbox keys are honoured: a per-subagent sandbox exists; record the keys |
| READ | | | | the sandbox is not enforcing here; fix the setup and rerun |

**Record.** One row, with the four results.

---

## V27

**Purpose.** Which GitHub plans offer rulesets, required code-owner review, push rulesets and merge
queue on **private** repos (RC-2, P43)? Rows marked `[probe, V27]` in §8.4.

**Blocks.** The §8.4 platform table.

**The build assumes.** Unknown means unavailable: the doctor caps the level unless the human attests the
feature.

**Prepare.** No scratch repo. You need a throwaway **private** repo on each plan you care about (Free,
Pro, Team; an organization repo for Team) and the `gh` CLI signed in as you. Nothing here touches a
repo you care about: each call creates a *disabled* ruleset and deletes it.

**Steps.** For each repo `OWNER/REPO`, first the docs (GitHub Docs, the feature's page, the
"availability" note; write the sentence down), then the API:

```
gh api repos/OWNER/REPO --jq '{private: .private, owner_type: .owner.type}'
gh api repos/OWNER/REPO/branches/main/protection    # a 403 or 404 with an upgrade message = unavailable
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
{"name":"probe-v27-branch","target":"branch","enforcement":"disabled",
 "conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},
 "rules":[{"type":"pull_request","parameters":{"required_approving_review_count":1,
  "dismiss_stale_reviews_on_push":false,"require_code_owner_review":true,
  "require_last_push_approval":false,"required_review_thread_resolution":false}}]}
JSON
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
{"name":"probe-v27-push","target":"push","enforcement":"disabled",
 "rules":[{"type":"file_path_restriction","parameters":{"restricted_file_paths":[".github/**"]}}]}
JSON
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
{"name":"probe-v27-queue","target":"branch","enforcement":"disabled",
 "conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},
 "rules":[{"type":"merge_queue","parameters":{"check_response_timeout_minutes":60,
  "grouping_strategy":"ALLGREEN","max_entries_to_build":5,"max_entries_to_merge":5,
  "merge_method":"SQUASH","min_entries_to_merge":1,"min_entries_to_merge_wait_minutes":5}}]}
JSON
gh api repos/OWNER/REPO/rulesets --jq '.[] | {id, name}'      # then delete each probe ruleset:
gh api -X DELETE repos/OWNER/REPO/rulesets/ID
```

Rule parameter names change; if a call fails with a validation error about parameters rather than a plan
message, fix the body and retry before concluding "unavailable". Once the doctor ships, also run its
platform read on each repo and compare.

**Observe.** `201` = the feature is available on that plan for a private repo; `403`/`404` with an
upgrade or plan message = unavailable. Required code-owner review is the `pull_request` call.

**Record.** A grid: plan x {rulesets, code-owner review, push rulesets, merge queue}.

---

## V3

**Purpose.** Does a Workflow agent accept per-agent `isolation` and `subagent_type` (P12)?
**Blocks.** `pipeline_host: workflow`. **The build assumes.** Not implemented.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v3" --probe V3`.

**Steps.** In an interactive session, ask Claude to load the workflow-authoring skill and tell you which
per-agent options it documents. If neither `isolation` nor `subagent_type` is documented, record
"not documented" and stop. Otherwise ask it to write a saved workflow `probe-v3` with two agents:
one with worktree isolation that runs `pwd` and then `echo probe-v3-isolated`, and one with
`subagent_type` set to a project agent that runs `echo probe-v3-typed`. Run it.

**Observe.** `analyze.py V3` lists the `pwd` each agent saw: a path under `.claude/worktrees` or a temp
directory means isolation worked; the typed agent's events carry that `agent_type`.
**Record.** Row V3, with the option names that worked.

---

## V4

**Purpose.** Does a skill with `context: fork` and `agent:` start clean (P9)? **Blocks.** On-demand
critique skills. **The build assumes.** No kit skill relies on it.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v4" --probe V4`

**Steps.** `claude` in `$SCRATCH`, paste, then in the same session:

```
Remember the codeword CODEWORD-V4-ALPHA. Reply "ok" and nothing else.
```

```
/probe-fork-skill
```

**Observe.** `analyze.py V4`: `probe-v4-clean` = the fork started clean; `probe-v4-saw-codeword` = it
inherited the conversation. **Record.** Row V4.

---

## V6

**Purpose.** Which commit does `isolation: worktree` branch from: the current HEAD or the remote
default branch (D-3)? **Blocks.** Nothing: avoided, the conductor creates worktrees. **The build
assumes.** `isolation: worktree` is not used.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v6" --probe V6`. The scratch has a local bare
`origin`, `main` and `origin/main` at one commit, and the checked-out branch `feature/probe` one
commit ahead.

**Steps.** `claude` in `$SCRATCH`, paste:

```
Dispatch the subagent probe-wt with the prompt "Follow your instructions." Do nothing else. When it returns, reply with one line: v6 done.
```

**Observe.** `analyze.py V6` maps the agent's `git rev-parse --short HEAD` to a recorded commit:
`feature/probe` = from the current HEAD; `main`/`origin/main` = from the default branch.
**Record.** Row V6.

---

## V7

**Purpose.** Does SubagentStart see the dispatch prompt (D-16)? **Blocks.** Brief-path enforcement at
SubagentStart. **The build assumes.** No such enforcement; the report parse and prose enforce it.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v7" --probe V7`

**Steps.** `claude` in `$SCRATCH`, paste:

```
Dispatch the subagent probe-v7 with exactly this prompt: "NONCE-V7-91c4: follow your instructions." Do nothing else. When it returns, reply with one line: v7 done.
```

**Observe.** `analyze.py V7` lists the SubagentStart fields and whether the nonce appears in it (and, for
comparison, in the PreToolUse event for the Agent tool). **Record.** Row V7 with the field list.

---

## V8

**Purpose.** Do SubagentStop or the statusLine input expose model, cost or tokens (P33)? **Blocks.**
Per-subagent cost fields. **The build assumes.** Cost comes from the `-p` JSON per child only.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v8" --probe V8` (also installs a statusLine
command that logs its input).

**Steps.** `claude` in `$SCRATCH`, paste:

```
Dispatch the subagent probe-v7 with exactly this prompt: "NONCE-V8-5be2: follow your instructions." Do nothing else. When it returns, reply with one line: v8 done.
```

Let the status line refresh once or twice (type a character, wait).

**Observe.** `analyze.py V8` lists the cost-like fields per event. **Record.** Row V8, the field paths.

---

## V12

**Purpose.** Does `omitClaudeMd: true` in agent frontmatter keep CLAUDE.md out of a subagent (and does
the default include it)? **Blocks.** Critic frontmatter. **The build assumes.** Critics keep CLAUDE.md.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v12" --probe V12`

**Steps.** `claude` in `$SCRATCH`, paste:

```
Dispatch the subagent probe-keep, wait for it, then dispatch the subagent probe-omit. Use the prompt "Follow your instructions." for both. Do nothing else. Reply with one line: v12 done.
```

**Observe.** `analyze.py V12`: `probe-v12-keep-knows` and `probe-v12-omit-blank` is the documented
behavior. A quality A/B (does a critic do worse without CLAUDE.md) needs the eval harness and is not part
of this probe. **Record.** Row V12.

---

## V13

**Purpose.** Does a plugin's `hooks/hooks.json` fire for plugin agents and project agents, and does
`${CLAUDE_PLUGIN_ROOT}` expand in hook commands and in command text? **Blocks.** Phase 9 (packaging).
**The build assumes.** The plugin ships hooks.json; the liveness canary detects non-firing; `/init` can
also write project-level hooks.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v13" --probe V13`

**Steps.** `cd "$SCRATCH" && claude --plugin-dir "$SCRATCH/probe-plugin13"`, paste:

```
Dispatch the subagent probe13:plugin-agent, wait for it, then dispatch the subagent project-agent. Use the prompt "Follow your instructions." for both. Do nothing else. Reply with one line: v13 agents done.
```

then:

```
/probe13:probe-cmd
```

**Observe.** `analyze.py V13`: which `agent_type`s the plugin hook fired inside; the
`v13-plugin-root-expanded` marker (path = hook expansion works); the `probe-v13-command-root` echo (a
literal `${CLAUDE_PLUGIN_ROOT}` = no expansion in command text). **Record.** Row V13.

---

## V14

**Purpose.** Do hook denials count toward the auto-mode pause (P23)? **Blocks.** Interactive auto-mode
users only. **The build assumes.** Documentation only; `dontAsk` makes it moot for the driver.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v14" --probe V14` (a hook that denies every Bash call).

**Steps.** `cd "$SCRATCH" && claude --permission-mode auto` (or cycle to auto mode with Shift+Tab), paste:

```
Run these four Bash commands, each in its own tool call, and report each result in one line. Do not retry a denied command. Commands: echo probe-v14-1 ; echo probe-v14-2 ; echo probe-v14-3 ; echo probe-v14-4
```

**Observe.** On screen: did auto mode pause or fall back to prompting, and after which denial?
`analyze.py V14` shows how many calls the hook denied. **Record.** Row V14 (count at which it paused).

---

## V15

**Purpose.** Does `claude plugin eval` take a `--model` pin (P29)? **Blocks.** Eval reproducibility.
**The build assumes.** Pin the CLI instead and set the model in the eval's settings.

**Steps.** No scratch repo. In a normal terminal: `claude plugin eval --help` and save the text. If
`--model` is listed, run one trivial eval with `--model <id>` and confirm in the output which model was
used. **Observe.** The help text lists `--model` or it does not; a pinned run names the model it used.
**Record.** Row V15 with the help text's relevant lines.

---

## V16

**Purpose.** A named spawn with agent teams off: teammate or subagent (P34)? **Blocks.** Nothing
(resume is by id). **The build assumes.** Resume by `agent_id` only; documentation only.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v16" --probe V16`

**Steps.** `claude` in `$SCRATCH`, paste:

```
Dispatch the subagent probe-named with the Agent tool and set the name parameter to "probe-alpha". Tell me in one line whether the tool result calls it a teammate or a subagent, and quote the id it returned. Then use SendMessage to send "second message: follow your instructions" to probe-alpha and tell me what happened.
```

**Observe.** `analyze.py V16`: SubagentStart/Stop without TeammateIdle = a subagent; two
`probe-v16-ran` calls = the second message reached it. **Record.** Row V16 with the tool result wording.

---

## V17

**Purpose.** What does `claude --bg` do about pushing and opening PRs (P15)? Until this runs, every
statement that `--bg` "can push or open draft PRs" is UNVERIFIED (§19.7, RC-31). **Blocks.** Nothing
(B1/B4 decide). **The build assumes.** Documentation only.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v17" --probe V17` (a local bare `origin`; no network).

**Steps.** `cd "$SCRATCH" && claude --bg "Create a branch bg-probe, add a file bg.txt, commit it, push the branch to origin, then also try to push to origin main. Report each result."`
Approve or deny prompts as they come; note which ones appeared. Let it finish.

**Observe.** `git --git-dir "$SCRATCH.remote.git" branch -a`: which refs arrived. `analyze.py V17` lists
the push commands and their results. **Record.** Row V17.

---

## V20

**Purpose.** Does SessionStart with source `compact` fire on `/compact`, and can it inject
`additionalContext` the model sees (§10.6, P30)? **Blocks.** §10.6. **The build assumes.** Implemented,
with a fallback: the orchestrate prose re-reads `loopctl status --json` at each stage.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v20" --probe V20`

**Steps.** `claude` in `$SCRATCH`. Paste the first prompt (expect `probe-v20-none`), have a short
conversation so there is something to compact, run `/compact`, then paste the first prompt again.

```
Without reading any file: run the Bash command echo probe-v20-nonce followed by the exact id that comes after the words PROBE-V20-NONCE in your context. If there is no such line in your context, run echo probe-v20-none instead. Run exactly one command.
```

```
/compact
```

**Observe.** `analyze.py V20`: SessionStart sources, whether the inject hook ran on `compact`, the
model's answer before and after (the nonce `V20-7f3a91` after = the context reached it).
**Record.** Row V20.

---

## V21

**Purpose.** Can a managed-settings file be overridden by the same OS user (L3+ requirement for a
separate OS user)? **Blocks.** The L3+ recommendation. **The build assumes.** Overridable: recommend a
separate OS user or a credential-free container.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v21" --probe V21`. It writes
`$SCRATCH/.claude/probe/managed-settings.json` (a deny rule on `echo probe-v21-denied*`,
`allowManagedHooksOnly`, a marker hook) but does **not** install it.

**Steps.** Install it where your OS reads managed settings (macOS:
`/Library/Application Support/ClaudeCode/managed-settings.json`; Linux: `/etc/claude-code/managed-settings.json`;
confirm the path in the docs for your version) with `sudo`, owned by root. Then, as your normal user, in
`$SCRATCH`, try each route and ask Claude to run `echo probe-v21-denied-N` (N = attempt number):

1. baseline: no override (expect denied);
2. add an allow rule for it in `$SCRATCH/.claude/settings.local.json`;
3. `claude --settings '{"permissions":{"allow":["Bash(echo probe-v21-denied*)"]}}'`;
4. `claude --settings '{"disableAllHooks":true}'` (do the hooks still run?);
5. a copy of the file in your user settings with the deny removed;
6. try to edit or delete the managed file as your user without `sudo` (record the error).

Remove the managed file afterwards (`sudo rm`).

**Observe.** `analyze.py V21`: every `probe-v21-denied` call should be `no-post`; any `ok` names the
override route that worked. **Record.** One row per route.

---

## V23

**Purpose.** Can a subagent launch a Workflow, and does an allow rule let a `-p` child launch one
(RC-17)? **Blocks.** Nothing: the kit denies `Workflow` headlessly either way. UNVERIFIED until run.
**The build assumes.** `Workflow` is denied headlessly and to engineers; the doctor fails on a stray allow.

**Prepare.**

```
tools/probes/make-scratch.sh "$HOME/probes/v23-allow"   --probe V23 --variant allow
tools/probes/make-scratch.sh "$HOME/probes/v23-noallow" --probe V23 --variant noallow
```

**Steps.** In each scratch, first create the saved workflow: ask Claude (interactive) to use the
workflow-authoring skill to write and save a workflow named `probe-v23` with one agent that runs
`echo probe-v23-workflow-ran`. Then:

1. subagent: paste

```
Dispatch the subagent probe-wf-caller with the prompt "Follow your instructions." Do nothing else. When it returns, reply with one line: v23 subagent done.
```

2. `-p`: `claude -p --settings "$SCRATCH/.claude/settings.json" --permission-mode dontAsk "Launch the saved workflow named probe-v23 with the Workflow tool, then reply with one line saying whether it started and quoting any error."`

**Observe.** `analyze.py V23`: each Workflow call, whether it came from a subagent, `ok` or `no-post`, and
whether `probe-v23-workflow-ran` appeared. **Record.** Row V23: four cells (allow/noallow x subagent/`-p`).

---

## V25

**Purpose.** Does `worktree.baseRef: "head"` make `isolation: worktree` branch from the current HEAD
(L-45)? UNVERIFIED. **Blocks.** Worktree base resolution notes (§19.7). **The build assumes.** The
conductor creates worktrees itself.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v25" --probe V25` (same repo shape as V6, plus
the setting).

**Steps.** As V6, in the V25 scratch:

```
Dispatch the subagent probe-wt with the prompt "Follow your instructions." Do nothing else. When it returns, reply with one line: v25 done.
```

**Observe.** `analyze.py V25`; compare with the V6 result. **Record.** Row V25.

---

## V26

**Purpose.** Does a project skill named `code-review` override the bundled one (L-45)? UNVERIFIED.
**Blocks.** The naming of the kit's review skill. **The build assumes.** Never use that name.

**Prepare.** `tools/probes/make-scratch.sh "$HOME/probes/v26" --probe V26`

**Steps.** `claude` in `$SCRATCH`, paste:

```
/code-review
```

**Observe.** `analyze.py V26`: `probe-v26-project-skill` = the project skill won. No marker = the
bundled one ran (check the transcript). **Record.** Row V26.
