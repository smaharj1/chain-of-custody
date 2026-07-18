# Getting Started (Plain-Language Guide)

This guide is for people who are **not** deeply technical but want to use this AI engineering team to build software. It assumes no prior knowledge. If you hit a word you don't recognize, check [GLOSSARY.md](../GLOSSARY.md).

> **The one-sentence version:** This kit gives Claude a structured way to act like a whole software team. You describe what you want; the "team" plans it, builds it, and checks its own work. Your job is to describe your idea clearly and make decisions when asked.

---

## What you need first

1. **Claude Code installed.** This is Anthropic's command-line tool for Claude. Install and sign in by following the official guide: <https://docs.anthropic.com/en/docs/claude-code>.
2. **A folder for your project.** It can be empty (brand-new) or an existing codebase.
3. **A little patience with the terminal.** You'll run a few commands. If that's intimidating, you can ask Claude to run most of them for you once it's started.

You do **not** need to know how to code. You *do* need to be able to make product decisions ("yes, users should log in with Google") and answer questions about what you're trying to build.

---

## Step 1 — Put this kit into your project

Copy the `.claude/` folder and the `docs/` folder from this kit into your own project folder. If you're comfortable in the terminal:

```bash
cp -r path/to/this-kit/.claude   your-project/
cp -r path/to/this-kit/docs      your-project/
```

If that's confusing: open your project folder in Claude Code and ask, *"Copy the .claude and docs folders from `<path to this kit>` into this project."* Claude will do it.

---

## Step 2 — Turn your idea into a build plan: `/planner`

When you have an idea you want to build, **this is your starting point.** In Claude Code, type:

> `/planner`

…then describe your idea in as much detail as you can. The Planner will:

1. **Interview you** about what you're building and who it's for.
2. **Make the big decisions with you** — tech stack, how login works, how data is stored. Say *"I'm not technical — recommend and explain simply"* whenever you're unsure. It remembers all of this for you; there are no files you need to fill in.
3. **Write a build plan** — a dependency-ordered checklist of everything to build. The **first item is always a minimal runnable app** (one page, one endpoint, one passing test), so you never have to scaffold anything by hand. Everything else builds on top of it.
4. **Ask for your approval** before a single line is built.

> **Empty folder? That's the expected case.** You do *not* set up a starter project first — the Planner makes "a runnable skeleton" the first thing the build loop creates. (Greenfield scaffolding is the newest part of the kit and the area still being hardened, so that very first item may want a little more of your attention than the rest.)

If you're adding to an **existing** project, the Planner still works — it just won't need that scaffold item.

---

## Step 3 — Build it: `/orchestrate`

Once you've approved the plan, type:

> `/orchestrate`

This is the loop. It works through your plan **one item at a time** — design → build → review → **show you the feature running on your own machine** → next — pausing for your okay at the checkpoints you chose. You watch, approve, and ask for changes. Nothing deploys; everything runs locally so you can actually see each piece.

> **First run only:** `/orchestrate` needs to know how to start and test your app. If it stops and asks, just say *"fill in `.claude/loop.config.md` for my stack"* and Claude will set it up — then run `/orchestrate` again. You also choose how often it pauses (every item vs. every milestone) in that same file.

When you're unsure how to answer any technical question along the way, it's always okay to say: **"I'm not technical — recommend the best option and explain why in simple terms."**

---

## Step 4 — Changes after your app exists

Once you have a working app, you don't always need the full loop:

- **A small fix** ("the button is the wrong color", "fix this typo") → type `/tech-lead`, describe it, done. It has a "quick lane" for exactly this.
- **One self-contained feature** on an app that already exists → just describe it (you start in **PM mode**), and it walks PM → Architect → Tech Lead for that single feature.

Rule of thumb: `/planner` → `/orchestrate` is for building a **whole idea** or a big batch of work; these two lanes are for **incremental changes** afterward.

---

## Step 5 — Seeing it working

The loop already shows you each feature running at its checkpoints — that's the whole point of the local-first design. Any other time, just ask:

> "How do I run this and see the change?"

Claude will run it or tell you the command. Building the code and *seeing it work* are two different things — always confirm it actually runs.

---

## Good habits

- **Describe the problem, not the solution.** Say "users keep losing their work" rather than "add a save button" — the team may find a better answer.
- **When asked to choose and you don't know, ask for a recommendation.** You're never stuck.
- **Keep your `.claude/context/` files updated** as your project grows. They're the team's memory. After a big feature, ask Claude to "update the primers to reflect what we just built."
- **Let the plan be the plan.** The loop builds one item at a time and shows you each one — resist piling on new ideas mid-run. Jot them down; the Planner can fold them in (it revises the plan between items).

---

## When something goes wrong

- **Claude seems confused about your project** → the team's notes on your project are probably thin. Just ask Claude to *"flesh out the project context"* — you don't edit anything yourself.
- **An engineer "escalated"** → it hit a decision it couldn't make alone. Claude will explain; you decide, or switch to `/architect` to rethink.
- **You don't understand a question** → ask Claude to explain it "like I'm not a developer." That always works.

---

## Where to go next

- [GLOSSARY.md](../GLOSSARY.md) — every unfamiliar word, explained
- [choosing-your-stack.md](choosing-your-stack.md) — what technology to pick if you don't know
- [README.md](../README.md) — the full reference once you're comfortable
- `examples/` — a complete filled-in example project to model yours on
