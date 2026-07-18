<!-- DATABASE PRIMER — Domain context for the Database Engineer -->
<!-- This file is read by the Database Engineer at the start of every task. -->
<!-- Fill this in with YOUR project's schema, migration tooling, and conventions. -->

## ORM and Tooling

<!-- Describe your ORM and migration tools. Examples: -->
<!-- - ORM: Drizzle ORM / Prisma / SQLAlchemy / TypeORM -->
<!-- - Database: PostgreSQL on RDS / PlanetScale MySQL / SQLite -->
<!-- - Migrations: drizzle-kit generate + migrate / prisma migrate / alembic -->

## Migration Location and Commands

<!-- Where do schema definitions live? What commands generate/apply migrations? -->
<!-- Example:
- Schema files: packages/db/src/schema/
- Generate migration: pnpm db:generate
- Apply migration: pnpm db:migrate
- Drizzle config: packages/db/drizzle.config.ts
-->

## Table Catalog

<!-- List every table with its columns, types, constraints, and relationships. -->
<!-- Keep this updated as the schema evolves. Example:

### users
- `id: text` PK — UUID from auth provider
- `email: text` unique, not null
- `display_name: text` not null
- `role: text` not null, default 'USER', values: USER | ADMIN
- `created_at: timestamp` default now, not null
- `updated_at: timestamp` default now, not null

### tasks
- `id: text` PK via createId()
- `title: text` not null
- `status: text` not null, values: TODO | IN_PROGRESS | DONE
- `assignee_id: text` nullable, FK -> users.id
- `created_at: timestamp` default now, not null
-->

## Conventions

<!-- List your database conventions. Examples: -->
<!-- - Money is integer cents only (price_cents), never floats -->
<!-- - IDs are text PKs via createId(), never auto-increment -->
<!-- - Every table has created_at and updated_at -->
<!-- - Status fields are text with documented allowed values, not DB enums -->
<!-- - Foreign keys always specify onDelete behavior -->

## Relationships

<!-- List entity relationships. Example: -->
<!-- - User <- Task by assignee_id -->
<!-- - Project <- Task by project_id -->
<!-- - User <- Project by owner_id -->
