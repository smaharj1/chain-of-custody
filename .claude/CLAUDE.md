# <!-- PROJECT_NAME: Replace with your project name -->

<!-- Replace this section with a 2-3 sentence description of what your project does. -->
<!-- Example: "TaskFlow is a SaaS project management platform. Teams create workspaces, manage tasks with kanban boards, and track time. The app has a public-facing web client and an internal admin dashboard." -->

## Tech Stack

<!-- Fill in every layer your project uses. Delete rows that don't apply. Add rows for layers not listed. -->
<!-- Not sure what to pick? See docs/choosing-your-stack.md for recommended defaults by project type. -->

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

<!-- Loop engineering (build a whole goal, not one feature at a time): -->
<!--   /planner     — turn a detailed goal into an approved build plan (build-plan.json) -->
<!--   /orchestrate — work through the plan item by item until done, with local auto-demo checkpoints -->
<!-- Config: .claude/loop.config.md   ·   Design: docs/design/loop-engineering.md -->
Loop modes: `/planner`, `/orchestrate`

<!-- List the engineer subagents your project actually uses. Default set: database, backend, app, admin-app -->
<!-- Customize: rename "app" to match your frontend (e.g., "web", "mobile", "dashboard") -->
<!-- Add or remove agents to match your project. See .claude/agents/ for definitions. -->
Engineer subagents: database, backend, app, admin-app

<!-- Also available, dispatched on demand rather than per-feature: -->
<!--   infra-engineer    — infrastructure-as-code changes (delete if you have no IaC) -->
<!--   security-reviewer — security audit, run before deploys / PRs; auto-dispatched in the /orchestrate loop on sensitive items -->
<!--   design-critic     — reviews the Architect's design before any code is written (used automatically by /architect) -->
<!--   code-reviewer     — independent diff review inside the /orchestrate loop (interactive flow uses the code-review Skill) -->
Specialist agents: infra-engineer, security-reviewer, design-critic, code-reviewer

**Default posture**: when no mode has been invoked this session, behave as the PM (per `.claude/commands/pm.md`) — gather requirements through questions; don't design or dispatch until the user switches modes.

## Key Conventions

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
