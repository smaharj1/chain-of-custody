# Your Project Name

<!-- PROJECT_NAME: replace the title above with your project's name -->

<!-- Replace this section with a 2-3 sentence description of what your project does. -->
<!-- Example: "TaskFlow is a SaaS project management platform. Teams create workspaces, manage tasks with kanban boards, and track time. The app has a public-facing web client and an internal admin dashboard." -->

## Tech Stack

<!-- Fill in every layer your project uses. Delete rows that don't apply. Add rows for layers not listed. -->
<!-- Not sure what to pick? See .claude/context/choosing-your-stack.md for recommended defaults by project type. -->

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
<!--   /init      — write the project settings a plugin cannot ship (plugin installs: /chain-of-custody:init) -->
<!--   /migrate   — upgrade a copy install to a newer kit without overwriting your changes -->
Maintenance modes: `/bootstrap`, `/retro`, `/init`, `/migrate`

<!-- Loop engineering (build a whole goal, not one feature at a time): -->
<!--   /planner     — turn a detailed goal into an approved build plan (build-plan.json) -->
<!--   /orchestrate — work through the plan item by item until done, with local auto-demo checkpoints -->
<!-- Config: .claude/loop.config.md   ·   Design: .claude/spec/loop-engineering.md -->
Loop modes: `/planner`, `/orchestrate`

<!-- List the engineer subagents your project actually uses. Default set: database, backend, app, admin-app, infra -->
<!-- Customize: rename "app" to match your frontend (e.g., "web", "mobile", "dashboard") -->
<!-- Delete "infra" if you have no infrastructure-as-code. -->
<!-- Add or remove agents to match your project. See .claude/agents/ for definitions. -->
Engineer subagents: database, backend, app, admin-app, infra

<!-- Read-only reviewers, dispatched on demand rather than per-feature: -->
<!--   security-reviewer — security audit, run before deploys / PRs; auto-dispatched in the /orchestrate loop on sensitive items -->
<!--   design-critic     — reviews the Architect's design before any code is written (used automatically by /architect) -->
<!--   code-reviewer     — independent diff review, in every lane (clean context, parseable verdict) -->
<!-- Started by the loop or a workflow, never by a mode or by hand: -->
<!--   acceptance-author — writes the black-box tests for one plan item in a fresh context (loopctl driver, Tech Lead, Orchestrator) -->
<!--   intake-triage     — reads one piece of untrusted text with every tool denied; the intake workflow or loopctl starts it -->
Specialist agents: security-reviewer, design-critic, code-reviewer, acceptance-author, intake-triage

**Human steps**: no agent pushes, merges a pull request or grants an approval that `loopctl` marks human-only (`approve plan|item|cost|...`, `resume`, `remote-lock`, `claim`). The conductor prints each as `HUMAN STEP: <command>` and waits; `python3 .claude/scripts/loopctl status --pending-human` lists them. A merge approval is bound to one item, attempt and head commit. Delivery is set by `git_autonomy` and `delivery` in `.claude/loop.config.md`; `python3 .claude/scripts/pr_prepare.py prepare` prints the push and PR commands for the person to run (`.claude/context/delivery.md`). Attestations in `.claude/loop.config.md` (deploy surface, sandbox, bot identity) are changed only by a person in a reviewed commit; `python3 .claude/scripts/doctor.py --level` says what blocks the next level.

**Default posture**: in an interactive session with no mode invoked, behave as the PM (per the `pm` skill) — gather requirements through questions; don't design or dispatch until the user switches modes. **Exception:** if this session was launched with a role prompt (a driver child started with `--agent <role>`, or a subagent), follow that prompt and ignore this line. A child session has nobody to interview.

## Key Conventions

<!-- Domain terms (what "member", "workspace", "booking" mean here)? Copy .claude/templates/domain-glossary.md to docs/domain-glossary.md and keep it short. Ambiguous domain words are the most common brief gap. -->
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
