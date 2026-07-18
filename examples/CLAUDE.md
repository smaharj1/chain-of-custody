# TaskFlow

TaskFlow is a SaaS project management platform for small teams. Users create workspaces, organize work into projects with kanban boards, assign tasks, track time, and collaborate in real-time. The platform has a public web app for end users and an internal admin dashboard for platform operators.

## Tech Stack

| Layer | Choice |
|---|---|
| Frontend | React 18 + Vite, TypeScript strict, React Router 6 |
| Styling | Tailwind CSS + shadcn/ui components |
| State | TanStack Query for server data, Zustand for UI state |
| Forms | React Hook Form + Zod |
| Real-time | WebSockets via Socket.IO |
| Backend | Node 20, Express 5, TypeScript strict |
| ORM | Drizzle ORM |
| Database | PostgreSQL 16 on RDS |
| Auth | Clerk (email/password + Google OAuth) |
| Payments | Stripe Checkout + Stripe Billing for subscriptions |
| Email | Resend for transactional email |
| IaC | AWS CDK in TypeScript |

## Repo Structure

This is a TypeScript monorepo using pnpm workspaces:
- `apps/web/` — public-facing React SPA (kanban boards, task management, time tracking)
- `apps/admin/` — internal admin dashboard (user management, billing, analytics)
- `packages/api/` — Express API server, deployed as a Lambda
- `packages/db/` — Drizzle schema, migrations, shared types, Zod validators
- `infra/` — CDK stacks and deployment config

## Agents and Modes

Available modes: `/pm`, `/architect`, `/tech-lead`
Engineer subagents: database, backend, app, admin-app

## Key Conventions

- TypeScript strict mode everywhere: frontend and backend must compile with strict types.
- Money values are stored as integer cents only (`priceCents`, `amountCents`), never floats.
- IDs are generated via `createId()` (cuid2), never auto-increment.
- Clerk `userId` is the user primary key everywhere. Do not use email as an ID.
- `workspaceId` is present on all workspace-scoped data models for multi-tenancy.
- Status fields are string unions stored as `text`, not PostgreSQL enums.

## Architecture Patterns

All backend API code follows a strict 3-layer pattern:

- `routes/` — HTTP only. Parse request (Zod), call service, format response. Never imports Drizzle.
- `services/` — Business logic. Classes with injected repositories. Throws domain errors.
- `repositories/` — Data access. Drizzle queries only. Returns raw row types.

DI: Services and repositories are instantiated in Express middleware and attached to `req.services`.

Error handling: Custom error classes (`NotFoundError`, `ConflictError`, `ValidationError`). Centralized error handler maps to HTTP status codes.

File naming: dot-separated — `task.service.ts`, `task.repository.ts`.

## Quick Start

```bash
pnpm install
cp .env.example .env.local  # fill in Clerk + Stripe + DB credentials
pnpm db:migrate              # apply migrations
pnpm dev                     # starts API + web + admin concurrently
```

Local URLs:
- Web app: http://localhost:5173
- Admin: http://localhost:5174
- API: http://localhost:3001

## Full Spec Reference

`docs/tech_spec.md`
