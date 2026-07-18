# Claude Code AI Engineering Team

A plug-and-play multi-agent configuration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that turns a single AI session into a full engineering team — Product Manager, Architect, Tech Lead, and specialist engineers — all coordinated through slash commands and structured workflows.

## What This Is

This is a `.claude/` configuration directory you drop into any project. It gives you:

- **Three operational modes** (`/pm`, `/architect`, `/tech-lead`) that switch Claude's persona and workflow
- **A build loop** (`/planner` + `/orchestrate`) that turns an idea into a dependency-ordered plan and executes it item by item — local-first, with human checkpoints — until done
- **Specialist engineer subagents** (database, backend, frontend, infra, security) that implement code to spec
- **A structured development workflow** with requirements gathering, technical design, design critique, engineer dispatch, code review, and telemetry
- **Quality controls** built in: design-critic catches architectural issues before code is written, code review catches implementation issues after

The system is designed for solo developers or small teams who want Claude to operate like a disciplined engineering org rather than a freeform assistant.

> **New to coding, or not sure what half of these words mean?** Start with **[docs/getting-started.md](docs/getting-started.md)** for a plain-language walkthrough, and keep **[GLOSSARY.md](GLOSSARY.md)** open in another tab. This README is the reference; the getting-started guide is the on-ramp.
>
> **Starting a brand-new project (empty folder)?** Read the [Starting from scratch](#starting-from-scratch) note below first — this kit currently assumes a project already exists, and there's one gap you need to know about before you begin.

## How the Team Works

```
User describes feature
  |
  v
/pm  -->  Gathers requirements via Q&A
  |       Writes docs/features/<slug>/requirements.md
  v
/architect  -->  Brainstorms tradeoffs with user
  |              Writes technical-design.md + api-contract.md
  |              Runs design-critic (catches issues before code)
  v
/tech-lead  -->  Writes per-engineer briefs
  |              Dispatches engineers in dependency order
  |              Runs code review on each engineer's output
  |              Handles escalations and re-dispatch
  v
Engineers (subagents)  -->  Read brief + design + contract
                           Implement matching canonical exemplars
                           Self-verify (lint, test)
                           Return structured report
```

For small tasks (bug fixes, tweaks), skip straight to `/tech-lead` — it has a **quick lane** that dispatches one engineer without the full ceremony.

## Loop Engineering: Building a Whole Idea

The `/pm` → `/architect` → `/tech-lead` modes build **one feature at a time**. The **loop engineering** layer builds a **whole idea**: a **Planner** (`/planner`) turns a detailed goal into a durable, dependency-ordered build plan, and an **Orchestrator** (`/orchestrate`) works through that plan (design → build → review → verify) **until it's done** — with configurable human checkpoints and a **local-first** experience: at each checkpoint it starts your app locally and *shows you the running feature*, no deployment required. **For building a project from an idea, this is the front door.**

<p align="center">
  <img src="docs/assets/loop-engineering-architecture.svg" alt="Loop engineering architecture: a Plan layer, an Orchestrate loop with auto-demo checkpoints, and an Execute layer reusing the existing engineering team" width="820">
</p>

Start with `/planner` to build the plan, then `/orchestrate` to run it. Configure autonomy, budgets, and your local verify/run commands in [`.claude/loop.config.md`](.claude/loop.config.md).

> **Status: V1 (locked design, sequential).** The design note + the adversarial-review rounds behind it are in [docs/design/loop-engineering.md](docs/design/loop-engineering.md). V1 runs items one at a time with local auto-demo checkpoints; parallel execution and a dedicated project-scaffold mode are deferred (see §14 of the design). The PM / Architect / Tech-Lead workflow still works standalone for one-off features.

## Quick Start

### 1. Copy into your project

```bash
# Clone this repo
git clone <this-repo-url> /tmp/ai-team

# Copy the .claude directory and docs templates into your project
cp -r /tmp/ai-team/.claude /path/to/your/project/
cp -r /tmp/ai-team/docs /path/to/your/project/
```

### 2. Fill in your project context

The system needs to know about YOUR project to be effective. Fill in these files:

| File | What to put in it |
|---|---|
| `.claude/CLAUDE.md` | Project description, tech stack, repo structure, conventions |
| `.claude/context/architect.md` | System topology, service map, infra constraints, auth model |
| `.claude/context/backend.md` | Backend framework, endpoint catalog, error handling, logging |
| `.claude/context/database.md` | ORM, schema catalog, migration commands, conventions |
| `.claude/context/shared-frontend.md` | Frontend stack, state management, accessibility rules |
| `.claude/context/app.md` | Main app routes, layout, key interactions |
| `.claude/context/admin-app.md` | Admin dashboard details (delete if no admin app) |
| `.claude/context/infra.md` | IaC tool, stacks, deploy commands (delete if no infrastructure) |

Each file has `<!-- comments -->` explaining what goes in each section. See `examples/` for a fully filled-in fictional project ("TaskFlow") to reference.

> **How long does this take?** If you already know your stack and patterns (you're porting an existing project), maybe an hour total. If you're starting fresh and *don't* yet know what your endpoints, schema, or architecture will be, **don't try to fill these in by hand from a blank page** — you'll be guessing. Fill in `CLAUDE.md` (the stack + a one-line description) and let `/architect` help you establish the rest as you design your first feature. The primers are living documents; they grow as the project does. See [docs/getting-started.md](docs/getting-started.md).

> **Tip:** You don't have to write these files yourself. Open the project in Claude Code and say *"Help me fill in `.claude/CLAUDE.md` for my project — ask me questions."* Claude will interview you and write it. Same for each context file.

### 3. Customize the agent roster

The default agents are:

| Agent | File | Purpose |
|---|---|---|
| **Backend Engineer** | `agents/backend-engineer.md` | API endpoints, services, business logic |
| **Database Engineer** | `agents/database-engineer.md` | Schema changes, migrations |
| **App Engineer** | `agents/app-engineer.md` | Main frontend app UI |
| **Admin App Engineer** | `agents/admin-app-engineer.md` | Admin dashboard UI |
| **Infra Engineer** | `agents/infra-engineer.md` | Infrastructure-as-code |
| **Security Reviewer** | `agents/security-reviewer.md` | Security audit before deploy; auto-dispatched in the loop on sensitive items |
| **Design Critic** | `agents/design-critic.md` | Reviews architecture before code |
| **Code Reviewer** | `agents/code-reviewer.md` | Independent diff review in the `/orchestrate` loop (interactive flow uses the code-review Skill) |

**To rename an agent** (e.g., "app" -> "web"): rename the file and update the `name:` in the frontmatter.

**To add a frontend app**: duplicate `agents/app-engineer.md`, rename it (e.g., `mobile-app-engineer.md`), update the frontmatter, and create a matching context file at `.claude/context/mobile-app.md`.

**To remove an agent**: delete the file. The Tech Lead auto-detects which engineers are needed from the design doc — it won't dispatch agents that don't exist.

### 4. Start using it

```bash
cd /path/to/your/project
claude  # start Claude Code

# To build a whole idea or project (greenfield, or a big batch of work):
> /planner          # interview -> dependency-ordered build-plan.json
> /orchestrate      # runs the plan item by item, local-first, with checkpoints

# For a single feature on an existing project:
> "I want to add user notifications"   # PM mode is the default

# For a small fix:
> /tech-lead
> "Fix the broken pagination on the tasks list page"

# For one-off architecture work:
> /architect
> "Let's design the notification system"
```

## File Structure

```
.claude/
  CLAUDE.md                          # YOUR project description + conventions
  settings.json                      # Claude Code settings (permissions, plugins)
  agents/                            # Engineer subagent definitions
    backend-engineer.md              #   Backend API engineer
    database-engineer.md             #   Database/migration engineer
    app-engineer.md                  #   Main frontend app engineer
    admin-app-engineer.md            #   Admin dashboard engineer
    infra-engineer.md                #   Infrastructure engineer
    security-reviewer.md             #   Security auditor (in-loop gate on sensitive items)
    design-critic.md                 #   Architecture reviewer (pre-code)
    code-reviewer.md                 #   Independent diff reviewer (loop)
  commands/                          # Slash command mode definitions
    pm.md                            #   /pm — requirements gathering
    architect.md                     #   /architect — technical design
    tech-lead.md                     #   /tech-lead — dispatch + review
  context/                           # Domain primers (YOUR project knowledge)
    engineer-protocol.md             #   Shared rules for all engineers (generic)
    architect.md                     #   System architecture primer
    backend.md                       #   Backend patterns primer
    database.md                      #   Schema + migration primer
    shared-frontend.md               #   Shared frontend conventions
    app.md                           #   Main app primer
    admin-app.md                     #   Admin app primer
    infra.md                         #   Infrastructure primer
  metrics.jsonl                      # Telemetry (auto-created, gitignored)
docs/
  features/
    _templates/                      # Templates used by PM, Architect, Tech Lead
      requirements.md                #   PM writes requirements from this
      technical-design.md            #   Architect writes design from this
      api-contract.md                #   Architect writes API contract from this
      brief.md                       #   Tech Lead writes engineer briefs from this
    <feature-slug>/                  # Created per feature (scratch — not long-term)
      requirements.md
      technical-design.md
      api-contract.md
      briefs/<engineer>.md
      reports/<engineer>.md
examples/                            # Filled-in example for reference
  CLAUDE.md                          #   Example: TaskFlow SaaS project
  context/
    architect.md
    backend.md
    database.md
```

## The Three Lanes

### Loop Lane

For building a project from an idea, or working through a large batch of features.

1. `/planner` — interview, big decisions, dependency-ordered `build-plan.json` (first item = runnable baseline)
2. `/orchestrate` — runs the plan item by item (design → build → review → local demo) until done, with checkpoints

### Full Feature Lane

For a single feature on a project that already exists: significant changes, multi-file refactors.

1. `/pm` — gather requirements, write `requirements.md`
2. `/architect` — design the solution, write `technical-design.md` + `api-contract.md`, run design-critic
3. `/tech-lead` — write briefs, dispatch engineers, run code review, wrap up

### Quick Lane

For small, scoped tasks: bug fixes, single-component tweaks, prop renames.

1. `/tech-lead` — describe the task, it dispatches one engineer directly

The Tech Lead will push back if a "quick" task is actually feature-sized.

## Key Concepts

### Primers vs Feature Folders

- **Primers** (`.claude/context/*.md`) are long-lived project knowledge. Keep them updated.
- **Feature folders** (`docs/features/<slug>/`) are scratch workspaces for one feature. After the feature ships, the primers and code are canonical — not the feature folder.

### Brief Gaps Are Feedback

When an engineer can't find information they need, they report it as a "brief gap" instead of guessing. These gaps are the most valuable feedback signal — they tell the Tech Lead what to include in future briefs.

### The Design Critic

Before any code is written, the Architect runs a design-critic subagent that looks for blockers: missing error cases, schema issues, contract ambiguities, security gaps. A bug caught here costs minutes; the same bug caught after implementation costs hours.

### Telemetry

After every feature/task, the Tech Lead appends a JSON line to `.claude/metrics.jsonl` with timing, iteration counts, and brief gaps. Review this periodically to tune your workflow.

## Customization Guide

### Which files to edit (your project context)

- `.claude/CLAUDE.md` — always edit
- `.claude/context/*.md` — always edit (except `engineer-protocol.md`)
- `docs/features/_templates/*.md` — edit if your workflow differs

### Which files to leave alone (generic workflow logic)

- `.claude/agents/*.md` — work as-is for most projects
- `.claude/commands/*.md` — work as-is (the PM/Architect/Tech Lead workflow)
- `.claude/context/engineer-protocol.md` — generic rules for all engineers

### Tuning for your project

- **No admin app?** Delete `agents/admin-app-engineer.md` and `context/admin-app.md`.
- **Multiple frontend apps?** Duplicate `agents/app-engineer.md` and `context/app.md` for each.
- **No infrastructure?** Delete `agents/infra-engineer.md` and `context/infra.md`.
- **Different backend language?** The agents are language-agnostic. Just fill in `context/backend.md` with your patterns.
- **Want a UI designer agent?** Create `agents/ui-designer.md` following the pattern of the existing agents.

## Starting from Scratch

This kit was built to coordinate work on a project that *already exists* — engineers "match the canonical exemplar," an existing file showing the established pattern. On an empty folder there are no exemplars yet, which is exactly why **greenfield starts with `/planner`, not `/pm`**:

1. **`/planner` makes a runnable skeleton the mandatory first item.** Its greenfield rule requires the first plan item to be "a locally-runnable app + test harness + one passing smoke test." `/orchestrate` builds that item first — so you do **not** scaffold the project by hand.
2. **That first item is foundational.** It establishes the patterns (folder layout, error handling, naming) every later item copies, and it's the newest, least-exemplar-supported part of the run — so give it more of your attention than the rest.
3. **After it ships**, the `context/*.md` primers point at those files as the canonical exemplars, and the normal exemplar-matching workflow applies for everything after.

> A dedicated bootstrap mode that hardens this first-item scaffolding experience is on the roadmap. Until then, `/planner` → `/orchestrate` is the path, with extra human attention on item one.

New to all of this? **[docs/getting-started.md](docs/getting-started.md)** walks through the whole thing in plain language.

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- A project to work on (any language, any framework)
- Comfort editing text files and running a couple of terminal commands — or willingness to ask Claude to do it for you. See [docs/getting-started.md](docs/getting-started.md) and [GLOSSARY.md](GLOSSARY.md) if the terminology here is unfamiliar.

## License

MIT
