# Claude Code AI Engineering Team

A plug-and-play multi-agent configuration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that turns a single AI session into a full engineering team — Product Manager, Architect, Tech Lead, and specialist engineers — all coordinated through slash commands and structured workflows.

## What This Is

This is a `.claude/` configuration directory you drop into any project. It gives you:

- **Three operational modes** (`/pm`, `/architect`, `/tech-lead`) that switch Claude's persona and workflow
- **A build loop** (`/planner` + `/orchestrate`) that turns an idea into a dependency-ordered plan and executes it item by item — local-first, with human checkpoints — until done
- **Specialist engineer subagents** (database, backend, app, admin-app, infra) that implement code to spec, plus **read-only reviewers** (design-critic, code-reviewer, security-reviewer) that audit without writing code
- **A structured development workflow** with requirements gathering, technical design, design critique, engineer dispatch, code review, and telemetry
- **Quality controls** built in: design-critic catches architectural issues before code is written, code review catches implementation issues after

The system is designed for solo developers or small teams who want Claude to operate like a disciplined engineering org rather than a freeform assistant.

> **New to coding, or not sure what half of these words mean?** Start with **[docs/getting-started.md](docs/getting-started.md)** for a plain-language walkthrough, and keep **[GLOSSARY.md](GLOSSARY.md)** open in another tab. This README is the reference; the getting-started guide is the on-ramp.
>
> **Starting a brand-new project (empty folder)?** Read the [Starting from scratch](#starting-from-scratch) note below first — greenfield goes straight to `/planner` and skips the fill-in step entirely.
>
> **Operational questions** (testing, CI, deploying, teams, non-web projects) live in **[docs/faq.md](docs/faq.md)**.

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
cd /path/to/your/project

# Runtime-required (the modes and the loop read these at run time):
cp -r /tmp/ai-team/.claude .
mkdir -p docs/features docs/design
cp -r /tmp/ai-team/docs/features/_templates docs/features/
cp /tmp/ai-team/docs/design/loop-engineering.md docs/design/
cp /tmp/ai-team/docs/choosing-your-stack.md docs/

# Optional human docs (nice to have in-project; also fine to read from the kit repo):
cp /tmp/ai-team/docs/getting-started.md /tmp/ai-team/docs/faq.md docs/
```

> ⚠️ **Already using Claude Code in this project?** Then `.claude/` already exists, and `cp -r` will merge into it — potentially clobbering your `settings.json` (permissions, hooks), your `CLAUDE.md`, and any agents/commands you've added. **Back up first** (`cp -r .claude .claude.backup`), then merge by hand: this kit's `settings.json` adds two PreToolUse hooks (`guard-git.sh`, `guard-main-edit.sh`) and an empty permission allowlist — fold those into yours rather than replacing it, and **append** the kit's `CLAUDE.md` content to your own.

Then append the kit's runtime artifacts to your project's `.gitignore`:

```gitignore
.claude/metrics.jsonl
.claude/loop.jsonl
.claude/orchestrate.lock
.claude/settings.local.json
.claude/worktrees/
```

(`build-plan.json` and `BUILD_PLAN.md` are the opposite — durable state, **meant to be committed**.)

Links inside the copied docs to `GLOSSARY.md`, this README, and `examples/` refer to the **kit repo** — keep your clone around or bookmark the repo page.

**Which path next?**
- **Existing project** → do step 2 (fill in your project context).
- **Empty folder** → **skip step 2 entirely.** `/planner` pre-bakes those decisions with you and the loop scaffolds the project — go straight to step 4 and type `/planner`. (See [Starting from scratch](#starting-from-scratch).)

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
| `.claude/context/local-dev.md` | **EXECUTION-CRITICAL for the loop** — how to run, test, and reset the app locally (dev command, ports, ephemeral-DB commands, verify-run harness). `/orchestrate` hard-stops if this is still a template. |
| `.claude/loop.config.md` | Loop settings: `verify_tests` / `verify_run` / `db_ephemeral` commands (also execution-critical), autonomy dial, budgets |

Each file has `<!-- comments -->` explaining what goes in each section. For calibration on how much detail to write, see [`examples/`](examples/README.md) — every context file filled in for a fictional project ("TaskFlow"), plus a filled `loop.config.md` and one feature's full artifact chain (`examples/features/csv-export/`).

> **`examples/` is documentation, not a demo.** TaskFlow has no code behind it, and the example feature folder was hand-authored to show the artifacts' shape — it is not the transcript of a real run. The kit does not currently ship a runnable end-to-end demo; see [`examples/README.md`](examples/README.md).

#### Adopting on an existing codebase

For a real codebase, don't fill the primers from memory — the information is in the code. Paste this into Claude Code:

> *"Read this codebase and draft each `.claude/context/` primer: derive the stack, conventions, and architecture patterns from the code; for the endpoint and table catalogs, reference the source-of-truth files rather than enumerating every entry; flag anything ambiguous as a question for me rather than guessing."*

Two scoping rules that keep this tractable on a large repo: **catalogs may point at source-of-truth files** ("Endpoint catalog: see `src/routes/*.ts`") instead of listing every endpoint/table, and partial primers are fine — they're living documents that grow per feature.

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
  KIT_VERSION                        # Which kit release this copy came from (see Updating the kit)
  settings.json                      # Claude Code settings (permissions + the two PreToolUse hooks)
  loop.config.md                     # Loop settings: autonomy, budgets, verify commands, db, git
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
    planner.md                       #   /planner — whole-idea build plan
    orchestrate.md                   #   /orchestrate — run the plan item by item
  context/                           # Domain primers (YOUR project knowledge)
    engineer-protocol.md             #   Shared rules for all engineers (generic)
    architect.md                     #   System architecture primer
    backend.md                       #   Backend patterns primer
    database.md                      #   Schema + migration primer
    shared-frontend.md               #   Shared frontend conventions
    app.md                           #   Main app primer
    admin-app.md                     #   Admin app primer
    infra.md                         #   Infrastructure primer
    local-dev.md                     #   How to run/test/reset the app locally (loop-critical)
  hooks/
    guard-git.sh                     #   PreToolUse(Bash): blocks push/rebase/shared-history git ops
    guard-main-edit.sh               #   PreToolUse(Edit/Write): blocks app-source edits on main
  metrics.jsonl                      # Interactive telemetry (auto-created; gitignore it — see step 1)
  loop.jsonl                         # Loop telemetry (auto-created; gitignore it)
  orchestrate.lock                   # Run lock (auto-created; gitignore it)
build-plan.json                      # Loop plan — canonical state (commit this)
BUILD_PLAN.md                        # Generated human view of the plan (commit this)
docs/
  design/
    loop-engineering.md              # Authoritative loop spec (runtime-required — orchestrate cites it)
  features/
    _templates/                      # Templates used by PM, Architect, Tech Lead, Planner
      requirements.md                #   PM writes requirements from this
      technical-design.md            #   Architect writes design from this
      api-contract.md                #   Architect writes API contract from this
      brief.md                       #   Tech Lead writes engineer briefs from this
      build-plan.md                  #   Planner writes build-plan.json from this
    <feature-slug>/                  # Created per feature (scratch — not long-term)
      requirements.md
      technical-design.md
      api-contract.md
      briefs/<engineer>.md
      reports/<engineer>.md
  faq.md                             # Operational FAQ (testing, CI, deploy, teams, non-web)
examples/                            # Writing reference — filled-in docs for a FICTIONAL
                                     #   project (TaskFlow). No code; not a runnable demo.
  README.md                          #   What this folder is and isn't — read first
  CLAUDE.md
  loop.config.md                     #   Filled loop config
  context/                           #   ALL primers filled: architect, backend, database,
                                     #   shared-frontend, app, admin-app, infra, local-dev
  features/csv-export/               #   One feature's artifact chain (requirements → design
                                     #   → contract → brief → report), hand-authored
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
- **No frontend at all (API service, CLI tool)?** Delete the app agents and `context/shared-frontend.md` — the Tech Lead's Backend→frontend dispatch gate simply never fires, and the api-contract template becomes your interface contract. More in [docs/faq.md](docs/faq.md).
- **Mobile app?** Duplicate `agents/app-engineer.md` and adjust `context/app.md` for React Native etc. Note the loop's auto-demo has no simulator screenshot — mobile demo targets fall back to endpoint/test assertions ([docs/faq.md](docs/faq.md)).
- **Different backend language?** The agents are language-agnostic. Just fill in `context/backend.md` with your patterns.
- **Want a UI designer agent?** Create `agents/ui-designer.md` following the pattern of the existing agents.

## Starting from Scratch

This kit was built to coordinate work on a project that *already exists* — engineers "match the canonical exemplar," an existing file showing the established pattern. On an empty folder there are no exemplars yet, which is exactly why **greenfield starts with `/planner`, not `/pm`**:

1. **`/planner` makes a runnable skeleton the mandatory first item.** Its greenfield rule requires the first plan item to be "a locally-runnable app + test harness + one passing smoke test." `/orchestrate` builds that item first — so you do **not** scaffold the project by hand.
2. **That first item is foundational.** It establishes the patterns (folder layout, error handling, naming) every later item copies, and it's the newest, least-exemplar-supported part of the run — so give it more of your attention than the rest.
3. **After it ships**, the `context/*.md` primers point at those files as the canonical exemplars, and the normal exemplar-matching workflow applies for everything after.

The kit ships a **bootstrap safety floor** so the empty-folder path works without manual setup: `/orchestrate`'s preamble runs `git init` if no repository exists, the Planner fills `.claude/loop.config.md`'s verify commands as a plan output (you don't pre-fill them), and the Tech Lead's engineer-set detection has an explicit scaffold row (the Backend engineer owns the skeleton). The first item also stamps `CLAUDE.md`'s stack table so the loop's autonomy check passes on later runs.

> A dedicated bootstrap *mode* that further hardens this first-item experience remains on the roadmap (design §14). Until then, `/planner` → `/orchestrate` is the path, with extra human attention on item one.

New to all of this? **[docs/getting-started.md](docs/getting-started.md)** walks through the whole thing in plain language.

## Prerequisites

**Required**

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated. Feature floor: agent frontmatter (`.claude/agents/`), PreToolUse hooks with `$CLAUDE_PROJECT_DIR`, and the Skill tool — any recent version has all three.
- `git` — the loop's branch/merge/verify machinery assumes a repository (the orchestrate preamble runs `git init` for you on an empty folder).
- `python3` on PATH — both guard hooks use it. Without it they announce themselves inactive and allow everything (fail-open by design), so the git guardrails silently vanish.
- A project to work on (any language, any framework) — or an empty folder (see [Starting from scratch](#starting-from-scratch)).

**Recommended**

- **context7 MCP** (fresh library docs for engineers). ⚠️ *Install-name trap*: the agent files wire the tool names for a **plugin** install named `context7` (`mcp__plugin_context7_context7__*`). If you install it as a plain MCP server instead, the tools are named `mcp__context7__*` — update the `tools:` line in the eight agent files to match, or the docs capability silently disappears while the engineer protocol still mandates it.
- **superpowers plugin** (`superpowers:brainstorming` — used by `/pm` and `/planner` for exploratory interviews; both fall back gracefully without it).
- **code-review plugin** (`code-review:code-review` — the interactive Tech Lead's review skill; a manual-review fallback is built in).

**Optional (text fallbacks built in)**

- A browser-preview MCP for the loop's auto-demo screenshots; a push-notification capability for checkpoint alerts. Absent either, the loop presents demo routes + HTTP checks as text and uses transcript banners.

## Updating the Kit

Your copy records its release in `.claude/KIT_VERSION`; changes ship in [CHANGELOG.md](CHANGELOG.md). To update:

1. Check your `.claude/KIT_VERSION`, read the CHANGELOG entries since.
2. **Re-copy the never-edit set** from the new kit release — these are kit logic, safe to overwrite: `.claude/agents/*` (unless you added/renamed agents), `.claude/commands/*`, `.claude/context/engineer-protocol.md`, `.claude/hooks/*`, `docs/features/_templates/*`, `docs/design/loop-engineering.md`, and `.claude/KIT_VERSION` itself.
3. **Never overwrite the user-owned set** — these hold *your* project: `.claude/CLAUDE.md`, `.claude/context/*` primers (except engineer-protocol), `.claude/loop.config.md`, `.claude/settings.json`. If a CHANGELOG entry touches one of these (e.g. a new settings hook), apply it as a hand-merge.

## License

MIT — see [LICENSE](LICENSE).
