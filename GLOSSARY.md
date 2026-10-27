# Glossary

Plain-language definitions of the terms used throughout Chain of Custody. If a word in the README or an agent file is confusing, it's probably here.

## The Team / Workflow

**Agent / Subagent** — A specialized version of Claude with its own instructions and a focused job (e.g., "backend engineer"). The main Claude session can hand work to these and collect their results.

**Mode** — A "hat" the main Claude session wears, switched with a slash command. `/pm`, `/architect`, and `/tech-lead` are modes. Each changes how Claude behaves. Each mode is a skill: a folder `.claude/skills/<mode>/SKILL.md`.

**Slash command** — A command you type starting with `/` (like `/architect`). It tells Claude to switch modes or run a specific routine. The kit's slash commands are its modes, which are skills.

**Maintenance modes** — Modes for setting up and tending an install rather than building a feature. `/bootstrap` takes a copied-in kit to a working install; `/retro` reads a finished milestone and drafts what to learn from it; `/init` and `/migrate` install and upgrade the kit (see Install and Upgrade below). The modes are marked so that Claude does not start them on its own; you type them.

**PM (Product Manager) mode** — Figures out *what* you want to build and *why*, by asking you questions. Writes it down as a requirements document. Doesn't make technical decisions.

**Architect mode** — Decides *how* to build it. Discusses trade-offs with you, then writes a technical design and an API contract.

**Tech Lead mode** — Turns the design into specific assignments ("briefs"), hands them to the engineer agents, and reviews their work.

**Engineer (subagent)** — An agent that actually writes code: backend, database, frontend, or infrastructure.

**Design Critic** — An agent that reviews the Architect's design *before* any code is written, to catch problems early. Runs automatically.

**Security Reviewer** — An agent that audits code for security problems. You run it before deploying; the `/orchestrate` loop also runs it automatically on sensitive items (auth, payments, uploads, user input).

**Brief** — A written assignment the Tech Lead gives an engineer: what to build, which files to read, what "done" looks like.

**Dispatch** — Handing a brief to an engineer agent to start work.

**Escalation** — When an engineer hits a problem it can't solve and hands it back up the chain for a human or the Architect to resolve.

**Telemetry** — Log files recording how work went: `metrics.jsonl` for interactive features (time, retries, gaps) and `loop.jsonl` for `/orchestrate` runs (per-item attempts, verify evidence, checkpoints). Useful for spotting patterns over time. You can ignore both at first.

## The Loop (`/planner` + `/orchestrate`)

**Planner** — The mode (`/planner`) that turns your whole idea into a build plan, interviewing you and making the big decisions with you. "PM for the whole project."

**Orchestrator** — The mode (`/orchestrate`) that executes the build plan item by item — design, build, review, verify, demo — until it's done or a guardrail stops it. Also called "the conductor" or "the loop."

**Build plan** — The durable checklist the Planner writes (`build-plan.json` + a readable `BUILD_PLAN.md` view): every item to build, in dependency order, with acceptance criteria. Everything the loop does comes from it.

**Milestone** — A coherent phase of the build plan (e.g., "M1 Foundation"). The default autonomy setting pauses at milestone boundaries.

**Checkpoint** — A planned pause where the loop shows you the feature *running on your machine* (the auto-demo) and waits for your okay. You can approve, reject ("redo this"), or stop.

**Demo target** — The specific route or endpoint the loop screenshots at a checkpoint, taken from the item's acceptance criteria. It's what the loop shows you to prove the item works.

**Autonomy dial** — The setting (`per-feature` / `per-milestone` / `unattended`) controlling how often the loop pauses for you. Hard-stops fire regardless.

**Hard-stop** — A pause the loop always honors, even unattended: an escalation, a security block, a blown budget, a missing secret, a tripped circuit breaker.

**Circuit breaker** — A guard that stops the run when several items in a row fail verification with no success in between. That pattern usually means something systemic is wrong rather than one bad item.

**Verify gate** — The evidence rule for "done": the tests actually ran and passed (with a non-zero test count), and the feature's specific route/behavior actually worked locally, with captured exit codes to prove it.

**Local-first** — The kit's stance that everything runs and is verified on your machine; deploying to the cloud is a deliberately later milestone.

**Dependency order / DAG** — The build plan's rule for sequencing: an item only starts when everything it depends on is done. (DAG = directed acyclic graph, the shape of a dependency chart with no loops.)

## loopctl: State, Approvals and Gates

**loopctl** — The deterministic half of the loop: one Python program (`python3 .claude/scripts/loopctl`) that owns state changes, counters, caps, the run lock, gate execution, the spend ledger and reconciliation, so those no longer depend on a model counting correctly. The conductor keeps the judgement. It is opt-in; the prose loop is still the default.

**Tier 1 / Tier 2** — Tier 1 loopctl commands (reads, the lock, halt, plan migration) always work. Tier 2 commands change run state and work only when `loopctl_tier2: true` is set in `.claude/loop.config.md` at the base sha.

**State ref** — Where an item's status lives: a git ref (`refs/loopctl/state/<plan>`), not a tracked file. Each write is a commit on that ref, so its history is an audit trail.

**CAS (compare-and-swap)** — A write that says "move this ref from the value I read to this new value" and fails if someone else moved it first. It is how two writers avoid overwriting each other.

**Run lock** — The marker that one run owns the plan on this machine. It is judged dead by its process id and a missing heartbeat, never by age alone.

**Failure class** — Why a step failed, which decides what happens next: `platform` (the API or CLI; retried with backoff), `transient` (a busy port, a stall; retried a couple of times), `env` (the machine is wrong; parked, not retried), `item` (the code is wrong; counts against the item).

**Discard** — When an item has used up its retries, or edited a test that was pinned, the loop throws the attempt away and starts the next one from the integration branch tip with the counters reset. A second discard is a hard stop.

**Park** — Setting an item aside with a reason (`blocked` or `needs_human`). Its `feat/*` branch moves to `blocked/*` and is kept.

**Breaker** — loopctl's circuit breaker: consecutive item failures stop the run, and only `approve breaker` lifts the stop.

**Bypass log** — GitHub's record of when a ruleset was bypassed. `loopctl reconcile --rule-suites` can read a saved copy, so a bypass on a merged item shows up.

**Human step** — An action only a person runs, from a plain terminal: most approvals (`approve plan`, `item`, `env`, `breaker`, `cost`, `demotion`), `resume`, `remote-lock acquire|renew|release|steal`, `claim take|release`, `--apply` on `parallel`, `migrate-plan --force` and `reconcile --live`. The guards deny these to every agent, the conductor included. The conductor prints each as `HUMAN STEP: <command>` and waits; `loopctl status --pending-human` lists what is open. The one table is `.claude/scripts/kitlib/loopctl_table.py`.

**Relay** — The few approvals the conductor may record from your chat reply, because they only acknowledge or narrow what you already approved: a checkpoint answer, a reject or stop of an item, keep or replan of a blocked item.

**Merge approval (bound to a head)** — `loopctl approve item` records the kind of approval, the item, the attempt and the exact commit (the "head") it was given for. If the branch moves afterwards (a CI fix, a rebase), the approval is void and you approve the new head. It attests the item's diff, not what the integration branch becomes after other items have merged.

**Gate (G0 to G11)** — A deterministic check the loop runs on an item, one module each (environment, acceptance criteria, stubs, acceptance tests, mutation testing, test integrity, verify, reviews, supply chain, product checks, secrets, docs). A gate is `on`, `report` (runs and records, never blocks) or `off`, read from the base sha so a branch cannot switch off its own gate.

**Acceptance author** — The agent that writes the acceptance tests from an item's criteria, in a fresh context separate from the engineers. It writes stubs and tests and never the implementation.

**Held-out tests** — Acceptance tests the engineer never sees, kept where its sandbox cannot read them (or run only in CI). They catch code that was written to pass the visible tests.

**Pin (a pinned acceptance test)** — An acceptance test committed before the code and then frozen. Editing one is the clearest sign of gaming the check.

**Red / invalid** — A new test that fails an assertion against the stubs is *red*: it can fail, so a pass later means something. A test that errors on import or on a wrong signature never ran; it is *invalid* and proves nothing.

**Characterization test** — For a refactor: a test of today's behaviour that must pass both before and after the change.

**Reward-hack signature** — Evidence that code was made to pass rather than to be right, such as an edited pinned test. It is the signal for a discard.

**Owner metric** — The one number each gate, rule or primer must move. A part whose owner metric never moves is a candidate for removal at the next model release (see Harness review).

**Evidence gate / evidence file** — `docs/baseline/evidence.json`, committed by a person after three clean fixture runs and one real feature with loopctl. Opt-in features that let the loop run with less supervision (unattended L4/L5, parallel items, mutation testing, product checks, the remote lock) refuse to start until it passes. Also the name of the last gate, which checks that every other gate that is on has passed for this head.

## The Driver, Unattended Runs and Parallel Runs

**Driver** — `loopctl run` with `runner: driver`. A program, not a model, started once by a person from a plain terminal. It does the conductor's mechanical work (select, gate, merge, count, budget) and launches a child session for every piece of judgement. It is the only process that starts `claude`.

**Child session** — A headless `claude -p` session the driver starts for one engineer or reviewer, with its own settings profile, process group and budget.

**Canary** — A liveness check that a guard actually loaded. SessionStart prints a line with a random nonce and writes the matching file; if they do not match, nothing is dispatched. The driver does the same for each child. A separate version canary decides which Claude Code version the kit may trust (`claude_code_tested_max`).

**Compact hook** — A SessionStart hook for the `compact` event. After `/compact` it re-reads the state and gives the session back the thread of the run: the item, stage, attempt, last failure and next action.

**Orphan reaper** — At start the driver ends a child left behind by a driver that died, but only one it can prove is its own, and renames abandoned branches `dead/*` (kept for `orphan_retention_days`).

**Lock takeover** — Taking the run lock of a driver whose process is dead on this machine. A live process, another machine or a conductor's lock is never taken over.

**Checkpoint (driver)** — A pause after a merge, stored in the state. The next start needs `approve checkpoint` (or a choice to stop).

**`awaiting_human`** — The status of an item that only a person may merge: a human-gated path, an infrastructure item, or any `delivery` other than `local`. The driver stops and the closing message says so.

**`reviews` gate record** — The recorded result of the code and security reviewers' verdicts, which the reviews gate (G7) reads.

**Unattended run (L4)** — A driver run with nobody at the keyboard. It refuses to start until every precondition holds (the evidence gate, a spend ceiling, a way to reach you, an external watchdog), and whenever it ends it tells you. Opt-in and experimental.

**No-acknowledge start** — An unattended run never starts and then waits for a reply. It checks everything first and refuses with the reason, or it starts and runs.

**Closing message** — The one message an unattended run sends when it ends: finished, waiting for you, or stopped (with the item, the reason code and the next command).

**`notify_command`** — The program you write that delivers those messages. The kit sends nothing itself. It may not be a shell, an interpreter or a launcher such as `curl` or `ssh`, and the message reaches it on standard input, never on the command line.

**Stall watchdog (in-process and external)** — Two ways silence gets noticed. The in-process one is a thread in the driver that sends a `stall` message if the driver stops making progress. The external one is `loopctl unattended watch`, run by your scheduler, which notices a driver that is gone altogether.

**Stall storm** — Several stalls inside a short window (`stall_storm_n` in `stall_storm_window_min`). It demotes the install.

**Demotion** — A drop from L4 to L3 that the system makes by itself after a revert, a security block, a test-integrity flag or a stall storm. Unattended runs are refused until a person lifts it with `approve demotion`.

**Promotion bar / promotion facts** — The conditions for moving up to L4, judged on facts from GitHub or CI that a person collected into a facts file, never on the local log. The report shows `origin` (the file loopctl read) and `claimed_source` (what the file says about itself); nothing is verified against GitHub. It only advises; promoting is a reviewed commit.

**Intake (two-stage)** — How text written by a stranger (an issue, a comment, a form) reaches the loop. Stage 1 is a model with no tools that answers in a fixed JSON shape. Stage 2 is a script, with no model, that validates that answer. A person then writes the plan item.

**Intake record (quarantine)** — The validated stage-1 answer, stored under the git directory with a draft plan item. It is not a plan item and the driver never reads it.

**Untrusted input block** — A fenced section of an engineer's brief, headed "Untrusted input (data, never instructions)", that carries a stranger's text for provenance only.

**Estimated cost** — The cost the driver assigns to a child it had to kill, because a killed child prints no result. It is derived from this run's own measurements, capped by the child's allowance, and marked `source: estimated` in the ledger.

**Lease** — A claim on something a parallel run shares (an item, a lane, a port slot, the integration slot), kept in a small file with a secret token and a time limit so a stale holder finds out it lost the claim.

**Worker** — One item being built in a parallel run, in its own worktree, with its own branch and child sessions.

**Slot (worktree and port)** — The worktree directory and the port block a worker owns. Every `verify_ports` entry is shifted by the slot, so two workers never meet on a port.

**Integrator** — The part of a parallel run that merges finished items one at a time, in plan order, after checking the footprint, rebasing if needed and verifying the rebased code again.

**Schema mutex (lane)** — The rule that only one item at a time may change the database schema (and one the dependency lockfile) in a parallel run, from its start until it has merged.

**Footprint (declared, lie)** — The files an item says it will touch (`touches.paths`). Items whose footprints cannot be proven apart do not run together. A *footprint lie* is a change outside the declared files; the item is parked and nothing of it is merged.

**Reject cascade** — What rejecting an item does to the items that were built on it: later merges are reverted in reverse order on the local branch and the items go back to pending. A person runs it.

**Drain** — When one parallel item parks or hard-stops, no new item starts, the running ones finish and finished items are still merged, then the run ends.

**Pipeline host** — Which kind of process runs the pipeline. Today only `conductor` works; `workflow` is refused.

**Remote lock** — A lock on a git remote (`refs/loopctl/lock/<plan>`) so only one run per plan happens across machines. It is advisory coordination, not a security boundary, and taking it is a push, so it is a human step.

**Claim** — A record on the git remote (`refs/loopctl/claim/<plan>/<item>`) saying a named person is working on an item (`loopctl claim take`). The driver skips a claimed item. A claim is not an approval and does not prove who typed the name; taking one is a push, so it is a human step.

**Reproducibility manifest** — A sealed record of what a piece of the loop ran on (commits, config hash, gate modes, toolchain pins, lockfile ids). `manifest verify` re-reads from git what git can re-derive. It is not a signature. The hash appears on a `Manifest:` line in the PR's audit block.

**Merge-class (L5 eligibility)** — `loopctl merge-class ITEM` says whether an item would be low-risk enough for auto-merge if auto-merge existed. It merges nothing; `merge_policy: human` is the only value that does anything.

**G10 product checks** — Optional checks (accessibility, performance, PII, observability) that run your own tools, without a shell. Off by default.

## Git (how your work is saved)

**Repository (repo)** — The folder git manages: your project plus its full change history.

**Commit** — A saved snapshot of your project at a point in time, with a message describing the change.

**Branch** — A parallel line of work. The loop builds each item on its own branch (`feat/<name>`), keeping `main` — the primary branch — clean until the item passes.

**Merge** — Folding a branch's commits into another branch. With `delivery: local` (the default) the loop merges each verified item into your *local* `main`; with `draft-pr` the work leaves as a draft pull request and a human merges it.

**Push** — Publishing your local commits to a shared remote (like GitHub). **No agent pushes: that is your job.** The guards block `git push` and `gh pr create` for every agent, the conductor included. Claude prepares the work and prints the commands (for example from `pr_prepare.py`), and you run them yourself in a terminal. If you're unsure how, ask Claude to explain the printed commands, then run them.

## Delivery (pull requests)

**Draft PR (`delivery: draft-pr`)** — The setting under which a feature leaves your machine as a draft pull request with its audit trail inside it. You run the push and the `gh pr create --draft` command; CI runs; you merge.

**Audit block** — The structured record at the top of a PR body (template `.claude/templates/pr-body.md`): feature, lane, shas, design-critic verdict, the verify run, reviews, human-gated paths, CI-fix commits, an engineers table, the acceptance matrix, Doc Delta and Primer Delta, and notes (cost, gates, a `Manifest:` line). The PR body is the durable record, because a squash merge drops commit trailers.

**WIP commit** — A work-in-progress commit the Tech Lead makes after each engineer (`git_autonomy: branch+commit`) so the reviewer sees exactly that engineer's work.

**CI-fix** — A fix commit made after CI rejected the PR. There are at most `max_ci_fix_attempts` (default 2), and one may not edit workflows, tests or lockfiles.

**Human-gated path** — A path a person must read closely before merge: migrations, `.github/`, `.claude/`, lockfiles, test and CI config, deploy files and `always_human_paths`. The audit block must list them.

**Failure class (verify)** — The label `verify.py` puts on a failed run: `config`, `env`, `build`, `lint`, `typecheck`, `tests`, `smoke`, `timeout` or `leak`. `config` and `env` mean the code is not at fault.

**`verify.v1` / `pr_prepare.v1`** — The names of the JSON shapes `verify.py --json` and `pr_prepare.py --json` print, which loopctl and CI read.

**Doc Delta / Primer Delta** — The part of an engineer's report that says what documentation, or what primer, the change makes stale. `NONE` is valid only with a `checked:` line. A Docs Delta gate also checks that a new route, flag or environment variable is documented.

**Slice** — A PR-sized piece of a design (`## Slices`). The Planner turns slices into plan items; slices can merge as stacked PRs.

**Dialect parity** — The rule that the local database is the production engine (not a stand-in such as SQLite), so migrations and queries behave the same locally and in production.

**Plan-ok / preview plan / post-merge plan** — For infrastructure: a person applies the `plan-ok` label to run a *preview plan* of a PR (never applied); after the merge a fresh *post-merge plan* of `main` is made, and that plan is the one a person approves to apply.

**Apply variant (Environment / dispatch)** — The two ways to gate an infrastructure apply: an Environment with required reviewers, or a human-only `workflow_dispatch` run twice (plan, then apply with the approved digest).

**Plan digest** — A hash of a plan's resource and output changes, used so an apply only runs the plan a person approved.

**Source policy** — The check that judges infrastructure code (providers, modules, provisioners) from the base ref before `terraform` runs it, because running a plan executes provider and module code.

**Release automation (H5)** — A tool such as release-please or changesets that turns a merge into a release. A release is a human act at every level; the tool's pull requests are never auto-merged.


## Safety and the Boundary

**Guardrail vs boundary** — A guardrail (a hook, a deny rule, a prose rule) catches honest mistakes and says why. The boundary is what still holds after an agent has escaped every guardrail: B1 to B5 below.

**B1 to B5** — The five parts of the boundary: B1 identity (the agent acts as a bot, not you), B2 credentials (no mutating credential reachable by the agent's OS user), B3 an OS sandbox or container, B4 server-side policy (rulesets, CODEOWNERS, required checks, split CI), B5 the deployment surface (every path from a commit to a deploy is inventoried and gated). See `docs/security/README.md`.

**Doctor** — `python3 .claude/scripts/doctor.py`. It checks the boundary and prints the autonomy level that is safe right now, with the reason for each cap.

**Autonomy level (L0 to L5)** — How much the loop may do unsupervised, from L0 (assistant only) and L1 (a supervised local loop) through L2 (push-branch, with a bot identity and a sandbox) and L3 (draft-PR factory) to L4 (unattended runs) and L5 (low-risk auto-merge, design only). The bot-push half of L2 and L3 is not built; in this release a person pushes. Not to be confused with the autonomy dial, which sets how often the loop pauses. `docs/security/README.md` has the table.

**Effective level / cap** — The autonomy level the doctor allows: the minimum of what you configured and every prerequisite (credentials, sandbox, MCP, B5, platform, evidence, version). Each limit is a *cap*. With nothing attested it is L1, on purpose.

**Attestation** — A statement a person commits in `.claude/loop.config.md` (the deploy surface, the sandbox, the bot identity). Only a human commits one; an agent never edits it.

**Fingerprint** — The hash of every file that can start a deploy. An attestation names it; if any of those files changes, the attestation is void until a person re-attests.

**Bot identity** — The GitHub account or App the agent's pushes use, so it is never you. It is never a bypass actor in a ruleset.

**Bypass actor** — A user or app a ruleset lets skip its rules. The bot must never be one.

**Split CI** — Running untrusted code (a PR's own tests) in a workflow with no secrets, and anything trusted in a separate workflow that checks out only the base ref.

**H6** — The class of configuration only a human changes, through a reviewed commit: caps, gates, delivery, verify commands, attestations. The doctor and loopctl read these keys at the base sha, so a feature branch cannot loosen its own rules.

**Guard** — A hook that inspects a tool call before it runs and may deny it. They live in `.claude/scripts/guards/` and are on by default (`.claude/settings.json` wires `run.py`). The old v1 shell hooks were removed in 2.0.0.

**Rule id** — The name a guard prints with a deny (`guard-git:no-verify`). The test corpus and telemetry use the same ids.

**Caller kind** — Who is making a tool call: `engineer`, `critic`, `conductor` or `unknown`. It decides what git and loopctl commands are allowed. A session is the `conductor` only if registered.

**Taint** — A mark on a session that has read untrusted content (a web page, an issue). A tainted conductor loses its conductor rights at L2 and above, and a tainted session is never trusted with merge or credential-bearing authority.

**HALT** — The kill switch: while the HALT file exists every mutating tool call is denied. A person clears it with `loopctl resume`.

**Corpus** — The table of commands and the decisions the guards must give for them (bypass attempts, ordinary commands that must stay allowed, injections). `guard_replay` is an eval grader: it feeds the commands recorded in an eval transcript to the real guards and checks they decide what the transcript says they decided.

**Untrusted data fence** — A block of backticks or tildes, longer than any run of them in the text, that marks quoted text as data, never instructions. Used for stranger text in briefs, drafts and PRs.


## Environment Terms

**CLI / terminal / command line** — The text window where you type commands (like `claude` or `pnpm dev`) instead of clicking buttons.

**Monorepo** — One repository containing several apps/packages that are developed together (e.g., web app + admin app + API in one repo).

**MCP (Model Context Protocol)** — The plug-in standard that gives Claude extra tools (browser preview, documentation lookup). Some kit features prefer an MCP tool and fall back to text if it's absent.

**Plugin / Skill** — Packaged extensions for Claude Code. A *skill* is a reusable routine Claude can invoke: a folder with a `SKILL.md`. Every kit mode is a skill (`.claude/skills/<mode>/SKILL.md`). A *plugin* bundles skills, agents and hooks so they can be installed in many projects; the kit is available as one (see Plugin install below). The kit does not require any other plugin or skill; the README's Prerequisites section lists what is optional.

## Project Structure

**Primer** — A document in `.claude/context/` that teaches the agents about *your* project (your database tables, your API, your conventions). The agents read these before working, and they write them too, deriving the contents from your code or from the stack decisions made during planning, then keeping them current as they build. You don't fill them in. Reading them tells you what the team believes about your project.

**Feature folder** — A temporary workspace (`docs/features/<name>/`) holding the documents for one feature. Considered "scratch" — disposable once the feature is done.

**Canonical exemplar** — An existing file in your project that represents the "right way" to do something. Engineers copy its style instead of inventing their own. (On a brand-new project, none exist yet — see "Starting from Scratch" in the README.)

**Slug** — A short, hyphenated name for a feature, used in folder names. E.g., the feature "User Notifications" might have the slug `user-notifications`.

## Technical Terms

**Frontend** — The part of the app users see and click (buttons, pages, forms). Runs in the web browser.

**Backend** — The part that runs on a server: handles logic, talks to the database, enforces rules. Users never see it directly.

**API (Application Programming Interface)** — The set of "doors" the frontend uses to talk to the backend. E.g., "create a task" or "get my profile."

**API contract** — A precise written agreement of exactly what each API door expects and returns. The backend builds to it; the frontend relies on it.

**Endpoint** — One specific API door, e.g., `POST /api/tasks` (create a task).

**Database** — Where your app's data is permanently stored (users, orders, etc.), organized into tables.

**Schema** — The structure of your database: what tables exist and what columns (fields) each has.

**Migration** — A script that changes the database structure (e.g., adds a new column) in a safe, repeatable way. "Reversible" means it can be undone.

**ORM (Object-Relational Mapper)** — A library that lets code talk to the database using normal programming instead of raw database language. (Examples: Drizzle, Prisma.)

**Auth (Authentication & Authorization)** — Authentication = "who are you?" (login). Authorization = "what are you allowed to do?" (permissions).

**IaC (Infrastructure as Code)** — Defining your servers, databases, and cloud resources in code files instead of clicking around a web console. (Examples: AWS CDK, Terraform.)

**Tech stack** — The full set of tools and languages your project uses (frontend framework, backend language, database, etc.).

**Lint / Linting** — Automated checking of code for style problems and likely mistakes, without running it.

**Migration / build / deploy** — *Build* = package the code to run. *Deploy* = put it on a live server. *Migration* = update the database structure.

## Install, Bootstrap and the Learning Loop

**Plugin install** — The kit installed through Claude Code's plugin system (`/plugin install`). It gives you the modes, agents, guards, `verify.py` and `pr_prepare.py` in many projects. The driver, unattended and parallel runs need the copy install.

**Copy install** — The kit copied into your project as a `.claude/` folder (from `dist/copy-install/`). It includes the whole loop.

**`KIT_MANIFEST`** — The list of every installed kit file with its SHA-256 and class, so an upgrade can tell your edits from the kit's.

**Base archive** — A copy of an older kit release (`.claude/migrate/v1.2.0.tar`, a `git archive` of the tag) shipped inside the kit, so `/migrate` can do a three-way comparison between the old kit, your files and the new kit without any git history.

**`/init`** — The mode that writes what a plugin cannot ship (the deny block, the sandbox profile, the gitignore lines, your starting config). It plans, shows a diff and a person applies it.

**`/migrate`** — The mode that upgrades an install to a newer kit without overwriting a file you changed. It plans first; a person applies it from a plain terminal.

**Bootstrap receipt** — `.claude/bootstrap-receipt.json`: a chained record of what `/bootstrap` ran and what it only recorded. A record for the reviewer, not a gate.

**Config plan** — The list of `loop.config.md` values `/bootstrap` proposes, checked against the registry. It changes nothing; you apply it.

**Setup script** — `.claude/bootstrap/setup.sh`: a script of the GitHub and file steps that you read and run yourself, in a dry run first. The agent writes it and never runs it.

**Factory labels** — The GitHub labels the intake and approval flow uses (`factory:go`, `factory:feature`, `factory:halt` and others). `label-guard.yml` removes the protected ones when anyone outside `FACTORY_APPROVERS` applies them.

**Retro** — `loopctl retro` and the `/retro` mode: one document of counts about a milestone (flow, failures, rescues, spend, report form, gaps, critic precision). It contains no log text.

**Gap** — Something that repeats across items (a brief gap, a primer that disagrees with the code, a gate finding, an environment failure). `loopctl gaps` finds them.

**Draft (rule skeleton, proposal)** — What `loopctl gaps --write` produces: a rule skeleton for `.claude/rules/` and notes, in a folder of its own, with the commands to open a pull request printed for you to run. A draft changes nothing by itself.

**Report trailer** — The JSON line an engineer ends its final message with. *Hard* problems (a missing or forged report, a trailer that disagrees with the report) fail the item. *Soft* problems (the form) are recorded as a `report_warning` with `report_trailers: warn`, the default, and fail the item with `block`.

**Clean streak** — How many accepted reports in a row had no form problem. Ten means `block` is ready; a person switches it.

**Calibration log** — The record of each critic's blockers and what happened to them (fixed, overruled, false positive).

**Precision** — For a critic: fixed blockers divided by fixed plus overruled plus false positives. Reported once enough outcomes exist.

**Docs Delta gate** — The gate that checks a new route, command-line flag or environment variable is documented or declared in the report. Default mode is `report`.

**Trigger (documented, declared)** — Something added in source that needs documenting. It is *documented* when its name is in an added documentation line, and *declared* when the report's Doc Delta names it in enough words.

**Ablation** — Re-running the eval cases with one harness part switched off to see whether the part still pays for itself.

**Remove candidate** — A gate whose owner metric never moved on enough items and whose ablation shows no effect. A person decides; safety parts are exempt.

**Harness review** — The procedure run at each model release to prune what no longer pays (`docs/harness-review.md`).

## Measurement and Probes

**Baseline run** — One human-run session of the original V1 loop on the `examples/hello-loop/` fixture, recorded as a results file, so every later piece of machinery can be judged by whether it moves a number. Tooling: `docs/baseline/README.md`, `tools/baseline/`.

**Rescue** — A time a human had to step in to get a run moving or to correct it. A *control-flow* rescue is one the machinery could have done (a counter, a lock, an unblock); other rescues are judgement or environment. More than three control-flow rescues per run is the bar for needing loopctl at all.

**Fixture** — A small project (`examples/hello-loop/`) with a plan, primers, held-out checks and an answer key, used to run and measure the loop. It is hand-written, not output of a run.

**Probe** — A short experiment that settles a platform assumption the kit made conservatively (a hook name, a settings key, a CLI flag). Probes are human-run from `tools/probes/`; what each one assumes is in `docs/design/probe-assumptions.md`, and the assumption stands until a result is recorded.

**Scratch repo** — The throwaway repository `make-scratch.sh` builds, outside the kit, in which a probe runs.

## Quality / Process Terms

**Idempotency** — A safety property: doing the same operation twice has the same effect as doing it once. (Important so a double-click doesn't charge a customer twice.)

**N+1 (query problem)** — A common performance bug where code makes one database query per item in a list instead of one query for the whole list. Slow.

**Eager-load** — Fetching related data up front in one query, to avoid the N+1 problem.

**DI (Dependency Injection)** — A code-organization technique where components are handed the things they need rather than creating them. Makes code easier to test and change.

**Concurrency / race condition** — Bugs that appear when two things happen at the same time (e.g., two people buying the last item at once).

**Edge case** — An unusual situation the code must still handle correctly (empty list, huge input, network failure mid-operation).

**Code review** — Checking written code for bugs and quality before accepting it. Here, the `code-reviewer` agent does this after each engineer, in a clean context, and ends with a verdict line that the next step reads. The engineer never reviews its own work, and neither does the Tech Lead.

**Greenfield** — A brand-new project with no existing code. (Opposite: "brownfield," an existing project you're extending.)
