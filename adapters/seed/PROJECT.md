# Your Project Name

<!-- PROJECT_NAME: replace the title above with your project's name -->

<!-- This is the project file for the Codex / Cursor copy of Chain of Custody. The agent reads it at the start of every session
     (`.chain-of-custody/GUIDE.md` says so). Where the kit's files say `CLAUDE.md`, they mean this file. Fill in every section;
     adapt.py never overwrites it. -->

<!-- Replace this section with a 2-3 sentence description of what your project does. -->
<!-- Example: "TaskFlow is a SaaS project management platform. Teams create workspaces, manage tasks with kanban boards, and track time. The app has a public-facing web client and an internal admin dashboard." -->

## Tech Stack

<!-- Fill in every layer your project uses. Delete rows that don't apply. Add rows for layers not listed. -->
<!-- Not sure what to pick? See .chain-of-custody/context/choosing-your-stack.md for recommended defaults by project type. -->

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

## Commands

<!-- How to install, run and check the project. The verify commands themselves are the verify_* keys in
     .chain-of-custody/project/loop.config.md (verify_build, verify_lint, verify_typecheck, verify_tests): the agent runs those
     before it calls work done. List the everyday commands here. Example: -->
<!-- - Install: `pnpm install` -->
<!-- - Dev server: `pnpm dev` (http://localhost:3000) -->
<!-- - Tests: `pnpm test` -->
<!-- - Migrations: `pnpm db:migrate` -->

## Roles and Modes

Modes, named in the prompt ("act as the PM for X"): pm, architect, tech-lead, planner. Each is a file in
`.chain-of-custody/skills/<mode>/SKILL.md`. The orchestrate and retro modes are reference only here.

<!-- List the engineer roles your project actually uses. Default set: database, backend, app, admin-app, infra -->
<!-- Delete "infra" if you have no infrastructure-as-code. The definitions are in .chain-of-custody/agents/. -->
Engineer roles: database, backend, app, admin-app, infra

Reviewer roles: design-critic, code-reviewer, security-reviewer. Run a review in a fresh session that did not write the code.

**Default posture**: when the prompt names no mode, act as the PM (`.chain-of-custody/skills/pm/SKILL.md`): gather requirements
through questions, and do not design or write code until the person names another mode.

**Git and delivery**: `git_autonomy` and `delivery` in `.chain-of-custody/project/loop.config.md`, as committed on the base
branch, say what the agent may do with git. They start at `none` and `local`: the agent asks the person to create the branch,
commit and push. Nothing in Codex or Cursor enforces these values or knows which branches are protected; the push rule is in
`.chain-of-custody/GUIDE.md`. No agent pushes main or a protected branch, force-pushes, or merges a pull request.

## Key Conventions

<!-- Domain terms (what "member", "workspace", "booking" mean here)? Copy .chain-of-custody/templates/domain-glossary.md to docs/domain-glossary.md and keep it short. Ambiguous domain words are the most common brief gap. -->
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

## Full Spec Reference

<!-- If you have a tech spec or architecture doc, point to it here. -->
<!-- Example: `docs/tech_spec.md` -->
