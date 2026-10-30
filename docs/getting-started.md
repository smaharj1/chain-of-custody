# Getting Started (Plain-Language Guide)

This guide is for people who aren't deeply technical but want to use Chain of Custody, an AI engineering team for Claude Code, to build software. It assumes no prior knowledge. If you hit a word you don't recognize, check [GLOSSARY.md](../GLOSSARY.md).

> **The one-sentence version:** this kit gives Claude a structured way to act like a whole software team. You describe what you want; the team plans it, builds it, and checks its own work. Your job is to describe your idea clearly and make decisions when asked.

## What you need first

1. **Claude Code installed,** version 2.1.302 or newer (`claude --version`). Anthropic's command-line tool for Claude. Install and sign in by following the official guide: <https://docs.anthropic.com/en/docs/claude-code>.
2. **A folder for your project.** Empty (brand-new) or an existing codebase, either works.
3. **A little patience with the terminal.** You'll run a few commands. If that's intimidating, you can ask Claude to run most of them for you once it's started.

You don't need to know how to code. You do need to be able to make product decisions ("yes, users should log in with Google") and answer questions about what you're trying to build.

## Step 1 — Install the kit and set up your project

The kit is a Claude Code plugin. Install it, then set up each project with one command. Start Claude Code in your project folder (type `claude` in a terminal) and type:

```text
/plugin marketplace add smaharj1/chain-of-custody
/plugin install chain-of-custody@chain-of-custody
```

When it asks where to install, pick the project scope (from a shell: `claude plugin install chain-of-custody@chain-of-custody --scope project`). A user-scope install guards every repository on your machine, including ones you never set up for the kit.

Then open Claude Code in your project folder and type:

```text
/chain-of-custody:init
```

It shows what it will write into your project's `.claude/` folder (a description of your project, the loop settings, empty notes the agents fill in, and the safety settings), changing nothing, and prints one command. Run it in a plain terminal (not inside Claude Code), then come back and say it ran. If the project already has a `.claude/` folder, it adds to it and never replaces a setting of yours.

The kit's commands all start with `/chain-of-custody:`. This guide writes them short: `/pm` means `/chain-of-custody:pm`. The exception is init: the short `/init` is Claude Code's own, so always type `/chain-of-custody:init`.

A few commands in these docs are ones you type into a terminal yourself. They write the kit's folder as `$KIT`. `/chain-of-custody:init` prints that folder as `kit: <path>`; type `export KIT=<that path>` in the terminal and the commands work as written. The path includes the plugin's version, so export it again after a plugin update.

> **Used an older version of the kit,** the one you copied into `.claude/`? Install the plugin, then run `/chain-of-custody:migrate`. It moves your project onto the plugin and keeps everything you changed. See [Migrating to v2](migrating-to-v2.md).

## Step 2 — Set it up once: `/bootstrap`

Start Claude Code in your project and type:

> `/bootstrap`

This is an interview for you, run once per project. It detects your stack and tooling (or helps you choose one), writes the stack into `.claude/CLAUDE.md`, proposes the settings for `.claude/loop.config.md`, and generates a short setup script. Then it stops and hands the rest to you, on purpose: creating a GitHub repository, applying labels and rules, and changing the settings that guard the loop are things *you* run, in a plain terminal, after reading what they do. The mode never edits those settings itself and never touches GitHub.

When it finishes it leaves a record of what it did (`.claude/bootstrap-receipt.json`), and `python3 "$KIT/scripts/doctor.py" --level` will say what level of autonomy is safe right now. With nothing attested, that is L1, a supervised local loop. That is where every install starts. Full detail: [bootstrap.md](bootstrap.md). If you skip this step the Planner still proposes the commands it needs (you apply them the same way), but the GitHub side stays unconfigured.

## Step 3 — Turn your idea into a build plan: `/planner`

When you have an idea you want to build, this is your starting point. In Claude Code, type:

> `/planner`

…then describe your idea in as much detail as you can. The Planner will:

1. **Interview you** about what you're building and who it's for.
2. **Make the big decisions with you:** tech stack, how login works, how data is stored. Say *"I'm not technical, recommend and explain simply"* whenever you're unsure. It writes the decisions down, and for `.claude/loop.config.md` (including how to run and test your app) it prepares the change and gives you one command to apply it, because those settings guard the loop and only you change them. You don't work out the values yourself.
3. **Write a build plan,** which is a dependency-ordered checklist of everything to build. The first item is always a minimal runnable app (one page, one endpoint, one passing test), so you never have to scaffold anything yourself. Everything else builds on top of it.
4. **Ask for your approval** before a single line is built.

> **Empty folder? That's the expected case.** You don't set up a starter project first; the Planner makes "a runnable skeleton" the first thing the build loop creates. Greenfield scaffolding is the newest part of the kit and the area still being hardened, so that very first item may want a little more of your attention than the rest.

If you're adding to an existing project the Planner still works. It just won't need that scaffold item.

## Step 4 — Build it: `/orchestrate`

Once you've approved the plan, type:

> `/orchestrate`

This is the loop. It works through your plan one item at a time (design, build, review, then showing you the feature running on your own machine) and pauses for your okay at the checkpoints you chose. You watch, approve, and ask for changes. Nothing deploys; everything runs locally so you can actually see each piece.

> **If it stops at startup:** `/orchestrate` needs to know how to start and test your app. The Planner normally proposes this during planning. If the loop still stops and asks, say *"propose the `.claude/loop.config.md` settings for my stack"*. Claude runs `bootstrap_config.py plan --set key=value ...`, which checks the values and writes a plan. You then run `python3 "$KIT/scripts/bootstrap_config.py" apply .claude/bootstrap/config-plan.json --write` in a plain terminal, review `git diff`, commit, and run `/orchestrate` again (the same command without `--write` only shows the diff; see [bootstrap.md](bootstrap.md)). You also choose how often it pauses (every item vs. every milestone) in that same file.

At a checkpoint, "review" means looking at the screenshot or clicking around the running app, then saying **approve** (keep going), **reject** (explain what's wrong, and the loop un-ships that item and redoes it with your feedback), or **stop**. (With `runner: driver` the item is already merged at its checkpoint, so the choices are approve or stop; a revert is `loopctl abort`.) One more thing worth knowing: finished work sits on your machine until you publish it, and **no agent ever publishes for you** (that is called a "git push"). When you're happy, Claude prints the exact commands and you run them yourself in a terminal. Where it needs *your* decision it prints a line starting with `HUMAN STEP:` followed by the command (approving the plan or an item for merge, lifting a stop); `python3 "$KIT/scripts/loopctl" status --pending-human` lists everything waiting on you. A merge approval is for one commit: if the branch changes afterwards, you approve the new one.

When you're unsure how to answer a technical question along the way, it's always fine to say: **"I'm not technical — recommend the best option and explain why in simple terms."**

## Step 5 — Changes after your app exists

Once you have a working app, you don't always need the full loop:

- **A small fix** ("the button is the wrong color", "fix this typo"): type `/tech-lead`, describe it, done. It has a "quick lane" for exactly this.
- **One self-contained feature** on an app that already exists: just describe it (you start in PM mode) and it walks PM → Architect → Tech Lead for that single feature.

Rule of thumb: `/planner` → `/orchestrate` is for building a whole idea or a big batch of work. These two lanes are for incremental changes afterward. To send either lane out as a draft pull request instead of merging on your machine, see Going further below.

## Step 6 — Seeing it working

The loop already shows you each feature running at its checkpoints, which is the point of the local-first design. Any other time, just ask:

> "How do I run this and see the change?"

Claude will run it or tell you the command. Building the code and *seeing it work* are two different things, so always confirm it actually runs.

## Going further (optional; do these when the basics feel comfortable)

Everything above works with the defaults. The sections below turn on more of the machinery, one step at a time. Each is opt-in, and each says what it needs first.

### Ship a feature as a draft pull request

The config `/chain-of-custody:init` writes sets `git_autonomy: branch+commit+push` and `delivery: push-branch`: when a feature is done the Tech Lead pushes its feature branch (never `main`) and you open the pull request and merge. To have it open a draft pull request with its audit trail inside, set `delivery: draft-pr` in `.claude/loop.config.md` (a reviewed change; these are guard settings), commit your `verify_*` commands to your default branch first, and run `/tech-lead` as usual; CI runs and you merge. To push yourself instead, set `git_autonomy: branch+commit`: the Tech Lead then prints the two commands (`git push ...` and `gh pr create --draft ...`) and **you** run them. Set it up once with [delivery.md](delivery.md); turn on `draft-pr` after the doctor says your rulesets and required checks are in place, because those are what stop any push to `main`.

### Moving up the autonomy levels

Run `python3 "$KIT/scripts/doctor.py" --level L2` (then `L3`, and so on) and do what it says; each step up is a list of things a person has to set up and attest. L1 works out of the box. L2 needs a bot identity, a clean credential audit, a sandbox and an attested deploy surface. L3 adds rulesets the bot cannot bypass, code-owner review, required checks and split CI. The full table, and why it is built this way, is in [security/README.md](security/README.md).

### Start the loop with loopctl

`loopctl` is a program that does the loop's counting, locking and checking so that does not rest on a model's memory. It is off by default (`runner: sequential`). To try it, set `loopctl_tier2: true`, then, from a plain terminal in a dedicated worktree (`git worktree add ../my-project-loop`):

```
python3 "$KIT/scripts/loopctl" validate              # is the plan sound?
python3 "$KIT/scripts/loopctl" approve plan --by you # you approve the plan: a human step
python3 "$KIT/scripts/loopctl" status --pending-human
python3 "$KIT/scripts/loopctl" resume                # clear a HALT: also a human step
```

`approve breaker` lifts the stop after a run of failures, and `approve env` after an environment problem. If loopctl refuses a command it says why (the setting is off, the plan changed, a HALT is set, the lock is held); see the FAQ.

What each gate needs from your plan: every item needs an `ext.g3` entry (its acceptance-test files and stubs, or a reason it is exempt), a test command that prints one line per test naming its criterion id (`unittest -v`, `pytest -v`) or JSON/JUnit output, and `ext.g5.allow` for any test or config file the item may legitimately edit. The conductor runs the checks in this order: environment, stubs, then at verify time integrity, criteria ids, acceptance tests, verify, supply chain and secrets, then the reviews, then the evidence check. `spec/loopctl-gates.md` has the detail.

### Run the loop with the driver

Set `runner: driver` and a `run_budget_usd` (the reviewed change), then, from a plain terminal and never from inside Claude Code:

```
python3 "$KIT/scripts/loopctl" run --dry-run   # shows the plan, the gates, the budget; runs nothing
python3 "$KIT/scripts/loopctl" run
```

The driver starts one headless session per engineer or reviewer. Exit 0 means the plan is done or it stopped on purpose for you; 1 means an item or the whole run is parked or stopped; 3 means it refused to start or was halted. Answer a pause with `loopctl approve checkpoint <item> --by you`; a merge approval for an item waiting on you is `approve item`; a spend stop is `approve cost`; a reset of a blocked item is `approve unblock`. Closing your terminal or pressing Ctrl-C ends the current child and the run (exit 1), leaves your work committed on the item's branch, and releases the lock; the next start picks up from the saved state. A driver that is killed outright (power, SIGKILL) leaves its lock behind, and the next start takes it over. See [the driver spec](../spec/loopctl-driver.md).

### Run it unattended

Unattended (L4) runs the driver while nobody is watching, and makes sure you are told whenever it stops. It refuses to start until the evidence file exists (three clean fixture runs and one real feature) and you have set a spend ceiling, a `notify_command` (a small program of your own that reaches you; not a shell or `curl`), and a scheduled `loopctl unattended watch` every five minutes. Check with `python3 "$KIT/scripts/loopctl" unattended check`; it prints every row. The closing messages are finished, waiting for you, or stopped (with the next command). After a revert, a security block or a stall storm, the install is demoted to L3 until you run `approve demotion`. The setup list and the details are in [unattended.md](unattended.md).

### Run items in parallel

Experimental, and only after the serial driver works for you: set `parallel_mode: worktree` and `max_parallel: 2`, run `loopctl parallel check` to see which items could run side by side (items that declare the files they touch can), and `loopctl run`. If a run is cut short, `loopctl parallel cleanup` shows what is left over; applying it is a human step. See [parallel.md](parallel.md).

### Learn from a milestone

After a milestone, run `/retro` (or `python3 "$KIT/scripts/loopctl" retro`) to see counts of what went well and what repeated, and `loopctl gaps` for things that came up more than once. `loopctl gaps --write` drafts a rule for you to review and prints the commands to propose it; it applies nothing. `loopctl report scan` says when the engineers' reports have been clean for long enough to switch `report_trailers` from `warn` to `block`. See [learning.md](learning.md).

## Good habits

- **Describe the problem, not the solution.** Say "users keep losing their work" instead of "add a save button"; the team may find a better answer.
- **When asked to choose and you don't know, ask for a recommendation.** You're never stuck on a question you can't answer.
- **You don't maintain the `.claude/context/` files.** These are the primers, the team's written memory of your project: your tables, your routes, your conventions. The team writes them and updates them itself as it builds, and you're never expected to author or refresh them. Reading them is a good way to see what the team believes about your project, and correcting something you know is wrong is always welcome. (If the loop says it's pausing more often because a primer is "still a template," that means something genuinely can't be known yet, usually because the code it would describe hasn't been built. It resolves itself as the build progresses.)
- **Let the plan be the plan.** The loop builds one item at a time and shows you each one, so resist piling on new ideas mid-run. Jot them down and the Planner can fold them in; it revises the plan between items.

## When something goes wrong

- **Claude seems confused about your project.** The team's notes on your project (the primers) are probably thin. Ask Claude to *"flesh out the project context"* — you never have to edit them by hand.
- **A command was blocked.** The guards print a rule id (for example `guard-git:push`) and a reason. To see the rule for a command: `python3 "$KIT/scripts/guards/run.py" explain --caller engineer git push origin main`. Pushing and approving are yours to run; see the FAQ.
- **An engineer "escalated".** It hit a decision it couldn't make alone. Claude will explain, then you decide, or switch to `/architect` to rethink.
- **You don't understand a question.** Ask Claude to explain it "like I'm not a developer." That always works.

## Where to go next

- [GLOSSARY.md](../GLOSSARY.md) — every unfamiliar word, explained
- [choosing-your-stack.md](../context/choosing-your-stack.md) — what technology to pick if you don't know
- [faq.md](faq.md) — operational questions (testing, CI, deploying, teams, non-web projects, why a command was refused)
- [bootstrap.md](bootstrap.md), [delivery.md](delivery.md), [unattended.md](unattended.md), [parallel.md](parallel.md), [learning.md](learning.md) — the optional steps above, in full
- [README.md](../README.md) — the full reference once you're comfortable
- `examples/` (in the kit folder) — every config file filled in for a fictional project, to calibrate your own against. It's a writing reference, not a working app; there's no code behind it.
