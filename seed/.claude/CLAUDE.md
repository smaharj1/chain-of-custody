# Your Project Name

<!-- PROJECT_NAME: replace the title above with your project's name -->

<!-- Replace this section with a 2-3 sentence description of what your project does. -->
<!-- Example: "TaskFlow is a SaaS project management platform. Teams create workspaces, manage tasks with kanban boards, and track time. The app has a public-facing web client and an internal admin dashboard." -->

## Tech Stack

<!-- Fill in every layer your project uses. Delete rows that don't apply. Add rows for layers not listed. -->
<!-- Not sure what to pick? See context/choosing-your-stack.md in the kit's folder for recommended defaults by project type. -->

| Layer | Choice |
|---|---|
| Frontend | <!-- e.g., React 18 + Vite, Next.js 14, Vue 3 + Nuxt --> |
| Styling | <!-- e.g., Tailwind CSS, CSS Modules, styled-components --> |
| State | <!-- e.g., TanStack Query + Zustand, Redux Toolkit, Pinia --> |
| Forms | <!-- e.g., React Hook Form + Zod, Formik + Yup --> |
| Backend | <!-- e.g., Node 20 + Express, FastAPI, Rails 7, Go + Chi --> |
| ORM | <!-- e.g., Drizzle, Prisma, SQLAlchemy, TypeORM --> |
| Database | <!-- e.g., PostgreSQL, MySQL, MongoDB, SQLite --> |
| Auth | <!-- e.g., Cognito, Auth0, Clerk, Supabase Auth, NextAuth --> |
| Payments | <!-- e.g., Stripe, PayPal, none --> |
| Real-time | <!-- e.g., WebSockets, SSE, Pusher, AppSync, none --> |
| Email | <!-- e.g., SES, SendGrid, Resend, none --> |
| IaC | <!-- e.g., AWS CDK, Terraform, Pulumi, none --> |

## Repo Structure

<!-- Describe your project layout. Monorepo? Single app? Where does each concern live? -->
<!-- Example:
- `apps/web/` — public-facing React SPA
- `apps/admin/` — internal admin dashboard
- `packages/api/` — Express API server
- `packages/db/` — Drizzle schema, migrations, shared types
- `infra/` — CDK stacks
-->

## Agents and Modes

Available modes: `/pm`, `/architect`, `/tech-lead`

<!-- Maintenance modes are for a person at the keyboard; the loop never starts them: -->
<!--   /bootstrap — take a new or existing repo to a working install (interview, config plan, setup script you run, receipt) -->
<!--   /retro     — retrospective over a milestone's run data; drafts changes, never applies them -->
<!--   /chain-of-custody:init — copy the kit's starter files into this project (changes nothing you already have) -->
<!--   /migrate   — move an old copy install of the kit (.claude/scripts/, KIT_MANIFEST) to the plugin -->
<!-- The kit is the plugin chain-of-custody@chain-of-custody, so each mode is also /chain-of-custody:<mode>. -->
Maintenance modes: `/bootstrap`, `/retro`, `/chain-of-custody:init` (the short `/init` is Claude Code's own), `/migrate`

<!-- Loop engineering (build a whole goal, not one feature at a time): -->
<!--   /planner     — turn a detailed goal into an approved build plan (build-plan.json) -->
<!--   /orchestrate — work through the plan item by item until done, with local auto-demo checkpoints -->
<!-- Config: .claude/loop.config.md   ·   Design: spec/loop-engineering.md in the kit's folder -->
Loop modes: `/planner`, `/orchestrate`

<!-- List the engineer subagents your project actually uses. Default set: database, backend, app, admin-app, infra -->
<!-- Customize: rename "app" to match your frontend (e.g., "web", "mobile", "dashboard") -->
<!-- Delete "infra" if you have no infrastructure-as-code. -->
<!-- Add or remove agents to match your project. The definitions are in the kit's agents/ folder; a file .claude/agents/<name>.md here replaces the kit's agent of that name. -->
Engineer subagents: database, backend, app, admin-app, infra

<!-- Read-only reviewers, dispatched on demand rather than per-feature: -->
<!--   security-reviewer — security audit, run before deploys / PRs; auto-dispatched in the /orchestrate loop on sensitive items -->
<!--   design-critic     — reviews the Architect's design before any code is written (used automatically by /architect) -->
<!--   code-reviewer     — independent diff review, in every lane (clean context, parseable verdict) -->
<!-- Started by the loop or a workflow, never by a mode or by hand: -->
<!--   acceptance-author — writes the black-box tests for one plan item in a fresh context (loopctl driver, Tech Lead, Orchestrator) -->
<!--   intake-triage     — reads one piece of untrusted text with every tool denied; the intake workflow or loopctl starts it -->
Specialist agents: security-reviewer, design-critic, code-reviewer, acceptance-author, intake-triage

**Kit folder**: the kit is a Claude Code plugin, so its scripts are not in this project. The session's first context line, `kit: <path>`, names its folder; the commands below write it as `$KIT`. Name kit files by that full path in what you print for a person (`${CLAUDE_PLUGIN_ROOT}` is not set in the Bash tool). Where the kit's own files write `${CLAUDE_PLUGIN_ROOT}/...`, they mean that same `kit:` folder.

**Human steps**: no agent merges a pull request, pushes to a protected branch, or grants an approval that `loopctl` marks human-only (`approve plan|item|cost|...`, `resume`, `remote-lock`, `claim`). The conductor prints each as `HUMAN STEP: <command>` and waits; `python3 "$KIT/scripts/loopctl" status --pending-human` lists them. A merge approval is bound to one item, attempt and head commit. Delivery is set by `git_autonomy` and `delivery` in `.claude/loop.config.md` (whether an agent may push its own feature branch, or prints the push for the person); `python3 "$KIT/scripts/pr_prepare.py" prepare` prints the commands and who runs each (`$KIT/context/delivery.md`). Attestations in `.claude/loop.config.md` (deploy surface, sandbox, bot identity) are changed only by a person in a reviewed commit; `python3 "$KIT/scripts/doctor.py" --level` says what blocks the next level.

**Default posture**: in an interactive session with no mode invoked, behave as the PM (per the `pm` skill) — gather requirements through questions; don't design or dispatch until the user switches modes. **Exception:** if this session was launched with a role prompt (a driver child started with `--agent <role>`, or a subagent), follow that prompt and ignore this line. A child session has nobody to interview.

## Key Conventions

<!-- Domain terms (what "member", "workspace", "booking" mean here)? Copy templates/domain-glossary.md from the kit's folder to docs/domain-glossary.md and keep it short. Ambiguous domain words are the most common brief gap. -->
<!-- List the coding conventions engineers must follow. Examples: -->
<!-- - TypeScript strict mode everywhere. -->
<!-- - Money values stored as integer cents, never floats. -->
<!-- - IDs are UUIDs / cuid2 / nanoid, never auto-increment. -->
<!-- - Status fields are string unions, not database enums. -->
<!-- - All API responses follow a consistent envelope shape. -->

## Architecture Patterns

<!-- Describe your backend architecture pattern so engineers follow it consistently. -->
<!-- Example: "3-layer architecture: routes/ (HTTP only) -> services/ (business logic) -> repositories/ (data access). DI via constructor injection." -->
<!-- Example: "Next.js App Router with server actions. Data access via Prisma in server components." -->

## Quick Start

<!-- How to run the project locally. Example: -->
<!-- ```bash -->
<!-- pnpm install -->
<!-- pnpm dev -->
<!-- ``` -->

## Full Spec Reference

<!-- If you have a tech spec or architecture doc, point to it here. -->
<!-- Example: `docs/tech_spec.md` -->
