# Getting Started (Plain-Language Guide)

This guide is for people who aren't deeply technical but want to use Chain of Custody, an AI engineering team for Claude Code, to build software. It assumes no prior knowledge. If you hit a word you don't recognize, check [GLOSSARY.md](../GLOSSARY.md).

> **The one-sentence version:** this kit gives Claude a structured way to act like a whole software team. You describe what you want; the team plans it, builds it, and checks its own work. Your job is to describe your idea clearly and make decisions when asked.

## What you need first

1. **Claude Code installed.** Anthropic's command-line tool for Claude. Install and sign in by following the official guide: <https://docs.anthropic.com/en/docs/claude-code>.
2. **A folder for your project.** Empty (brand-new) or an existing codebase, either works.
3. **A little patience with the terminal.** You'll run a few commands. If that's intimidating, you can ask Claude to run most of them for you once it's started.

You don't need to know how to code. You do need to be able to make product decisions ("yes, users should log in with Google") and answer questions about what you're trying to build.

## Step 1 — Put this kit into your project

Everything the agents actually run lives in `.claude/`, so that folder is the one that matters. The `docs/` folder is reading material for you. If you're comfortable in the terminal:

```bash
cp -r path/to/this-kit/.claude   your-project/

mkdir -p your-project/docs
cp path/to/this-kit/docs/choosing-your-stack.md \
   path/to/this-kit/docs/getting-started.md \
   path/to/this-kit/docs/faq.md    your-project/docs/
```

If that's confusing: open your project folder in Claude Code and ask, *"Copy the .claude folder from `<path to this kit>` into this project, plus the docs/choosing-your-stack.md, docs/getting-started.md and docs/faq.md files."* Claude will do it.

(Only the first command is required. The rest are docs you'll want nearby — you can also just keep this kit folder open and read them there.)

> **Already used Claude Code in this project?** Then a `.claude/` folder already exists, and copying over it can clobber your settings. Back it up first (`cp -r .claude .claude.backup`) or ask Claude to *"merge the kit's `.claude` folder into mine without losing my settings."* The README's install notes have the details.

One more note: links in these docs to `GLOSSARY.md`, the README, and `examples/` refer to the kit itself, so keep the kit folder around (or its GitHub page bookmarked) instead of deleting it once you've copied from it.

## Step 2 — Turn your idea into a build plan: `/planner`

When you have an idea you want to build, this is your starting point. In Claude Code, type:

> `/planner`

…then describe your idea in as much detail as you can. The Planner will:

1. **Interview you** about what you're building and who it's for.
2. **Make the big decisions with you:** tech stack, how login works, how data is stored. Say *"I'm not technical, recommend and explain simply"* whenever you're unsure. It writes those decisions into the project's config files for you, including how to run and test your app, so you don't fill anything in by hand.
3. **Write a build plan,** which is a dependency-ordered checklist of everything to build. The first item is always a minimal runnable app (one page, one endpoint, one passing test), so you never have to scaffold anything yourself. Everything else builds on top of it.
4. **Ask for your approval** before a single line is built.

> **Empty folder? That's the expected case.** You don't set up a starter project first; the Planner makes "a runnable skeleton" the first thing the build loop creates. Greenfield scaffolding is the newest part of the kit and the area still being hardened, so that very first item may want a little more of your attention than the rest.

If you're adding to an existing project the Planner still works. It just won't need that scaffold item.

## Step 3 — Build it: `/orchestrate`

Once you've approved the plan, type:

> `/orchestrate`

This is the loop. It works through your plan one item at a time (design, build, review, then showing you the feature running on your own machine) and pauses for your okay at the checkpoints you chose. You watch, approve, and ask for changes. Nothing deploys; everything runs locally so you can actually see each piece.

> **If it stops at startup:** `/orchestrate` needs to know how to start and test your app. The Planner normally fills this in during planning. If the loop still stops and asks, say *"fill in `.claude/loop.config.md` for my stack"* and Claude will set it up, then run `/orchestrate` again. You also choose how often it pauses (every item vs. every milestone) in that same file.

At a checkpoint, "review" means looking at the screenshot or clicking around the running app, then saying **approve** (keep going), **reject** (explain what's wrong, and the loop un-ships that item and redoes it with your feedback), or **stop**. One more thing worth knowing: finished work sits on your machine until you publish it. When you're happy, ask Claude *"push my verified work"* and it will walk you through publishing (called a "git push") step by step.

When you're unsure how to answer a technical question along the way, it's always fine to say: **"I'm not technical — recommend the best option and explain why in simple terms."**

## Step 4 — Changes after your app exists

Once you have a working app, you don't always need the full loop:

- **A small fix** ("the button is the wrong color", "fix this typo"): type `/tech-lead`, describe it, done. It has a "quick lane" for exactly this.
- **One self-contained feature** on an app that already exists: just describe it (you start in PM mode) and it walks PM → Architect → Tech Lead for that single feature.

Rule of thumb: `/planner` → `/orchestrate` is for building a whole idea or a big batch of work. These two lanes are for incremental changes afterward.

## Step 5 — Seeing it working

The loop already shows you each feature running at its checkpoints, which is the point of the local-first design. Any other time, just ask:

> "How do I run this and see the change?"

Claude will run it or tell you the command. Building the code and *seeing it work* are two different things, so always confirm it actually runs.

## Good habits

- **Describe the problem, not the solution.** Say "users keep losing their work" instead of "add a save button"; the team may find a better answer.
- **When asked to choose and you don't know, ask for a recommendation.** You're never stuck on a question you can't answer.
- **You don't maintain the `.claude/context/` files.** These are the primers, the team's written memory of your project: your tables, your routes, your conventions. The team writes them and updates them itself as it builds, and you're never expected to author or refresh them. Reading them is a good way to see what the team believes about your project, and correcting something you know is wrong is always welcome. (If the loop says it's pausing more often because a primer is "still a template," that means something genuinely can't be known yet, usually because the code it would describe hasn't been built. It resolves itself as the build progresses.)
- **Let the plan be the plan.** The loop builds one item at a time and shows you each one, so resist piling on new ideas mid-run. Jot them down and the Planner can fold them in; it revises the plan between items.

## When something goes wrong

- **Claude seems confused about your project.** The team's notes on your project (the primers) are probably thin. Ask Claude to *"flesh out the project context"* — you never have to edit them by hand.
- **An engineer "escalated".** It hit a decision it couldn't make alone. Claude will explain, then you decide, or switch to `/architect` to rethink.
- **You don't understand a question.** Ask Claude to explain it "like I'm not a developer." That always works.

## Where to go next

- [GLOSSARY.md](../GLOSSARY.md) — every unfamiliar word, explained
- [choosing-your-stack.md](choosing-your-stack.md) — what technology to pick if you don't know
- [faq.md](faq.md) — operational questions (testing, CI, deploying, teams, non-web projects)
- [README.md](../README.md) — the full reference once you're comfortable
- `examples/` (in the kit folder) — every config file filled in for a fictional project, to calibrate your own against. It's a writing reference, not a working app; there's no code behind it.
