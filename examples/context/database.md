## ORM and Tooling

- ORM: Drizzle ORM.
- Database: PostgreSQL 16 on RDS in prod; SQLite at `./.data/dev.db` for local dev (see `local-dev.md`). Migrations regenerate per-dialect.
- Migrations: `drizzle-kit generate` + `drizzle-kit migrate`.

## Migration Location and Commands

- Schema files: `packages/db/src/schema/`
- Drizzle config: `packages/db/drizzle.config.ts`
- Generate migration: `pnpm --filter @taskflow/db generate`
- Apply migration: `pnpm --filter @taskflow/db migrate`
- API server (`packages/api/`) imports schema via `@taskflow/db/schema` — never runs migrations.

## Table Catalog

### users
- `id: text` PK — Clerk `userId`.
- `email: text` unique, not null.
- `display_name: text` not null.
- `avatar_url: text` nullable.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.
- Synced from Clerk via webhook.

### workspaces
- `id: text` PK via `createId()`.
- `name: text` not null.
- `slug: text` unique, not null.
- `owner_id: text` not null, FK -> users.id.
- `plan: text` not null, values: FREE | PRO | ENTERPRISE. Default FREE.
- `stripe_customer_id: text` unique, nullable.
- `stripe_subscription_id: text` unique, nullable.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.

### workspace_members
- `id: text` PK via `createId()`.
- `workspace_id: text` not null, FK -> workspaces.id, onDelete CASCADE.
- `user_id: text` not null, FK -> users.id, onDelete CASCADE.
- `role: text` not null, values: MEMBER | ADMIN | OWNER. Default MEMBER.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.
- Unique constraint on (workspace_id, user_id).

### boards
- `id: text` PK via `createId()`.
- `workspace_id: text` not null, FK -> workspaces.id, onDelete CASCADE.
- `name: text` not null.
- `position: integer` not null, default 0.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.

### tasks
- `id: text` PK via `createId()`.
- `board_id: text` not null, FK -> boards.id, onDelete CASCADE.
- `workspace_id: text` not null, FK -> workspaces.id, onDelete CASCADE.
- `title: text` not null.
- `description: text` nullable.
- `status: text` not null, values: TODO | IN_PROGRESS | IN_REVIEW | DONE. Default TODO.
- `priority: text` not null, values: LOW | MEDIUM | HIGH | URGENT. Default MEDIUM.
- `assignee_id: text` nullable, FK -> users.id, onDelete SET NULL.
- `due_date: timestamp` nullable.
- `position: integer` not null, default 0.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.

### time_entries
- `id: text` PK via `createId()`.
- `task_id: text` not null, FK -> tasks.id, onDelete CASCADE.
- `user_id: text` not null, FK -> users.id, onDelete CASCADE.
- `workspace_id: text` not null, FK -> workspaces.id, onDelete CASCADE.
- `started_at: timestamp` not null.
- `ended_at: timestamp` nullable. Null = currently running.
- `duration_seconds: integer` nullable. Computed on stop.
- `created_at: timestamp` default now, not null.
- `updated_at: timestamp` default now, not null.

## Conventions

- Money is integer cents only: `priceCents`, `amountCents`.
- IDs are `text` PKs generated via `createId()`, never auto-increment.
- Every table has `created_at` and `updated_at`.
- Status fields are `text` with documented allowed values, not Postgres enum types.
- Foreign keys always specify `onDelete` behavior.
- `workspaceId` is present on all workspace-scoped entities for multi-tenancy queries.

## Relationships

- User <- WorkspaceMember by user_id
- Workspace <- WorkspaceMember by workspace_id
- Workspace <- Board by workspace_id
- Board <- Task by board_id
- Task <- TimeEntry by task_id
- User <- TimeEntry by user_id
- User <- Task by assignee_id
