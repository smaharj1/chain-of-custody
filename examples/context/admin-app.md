## Scope

`apps/admin/` — the internal operator dashboard at http://localhost:5174. Read `shared-frontend.md` first; this file is the admin-app overlay.

## Access Control

- **PlatformAdmin role only.** Clerk session must carry `role: PlatformAdmin` in public metadata; everyone else gets a full-page "not authorized" screen — no partial rendering of admin data.
- The role check is a UX gate only; the API independently enforces PlatformAdmin on every `/api/admin/*` endpoint. Never rely on the frontend check.
- No workspace context here: admin views are platform-wide, unscoped by `workspaceId`.

## Routes

| Route | Page | Backing endpoint |
|---|---|---|
| `/users` | All users table | `GET /api/admin/users` |
| `/workspaces` | All workspaces table | `GET /api/admin/workspaces` |
| `/metrics` | Platform metrics dashboard | `GET /api/admin/metrics` |
| `/billing` | Subscription overview per workspace | `GET /api/admin/workspaces` + Stripe dashboard deep-links |

Layout shell: slim left nav (four routes), topbar with environment badge (local/staging/prod) and user menu.

## Data-Table Conventions

- All tables use the shared `DataTable` component (TanStack Table + shadcn table primitives).
- **Server pagination always** — `?page` + `?pageSize` (default 25) in the query string; never fetch-all-and-paginate-client-side.
- Column filters (text search, status/plan selects) serialize into query params so filtered views are shareable URLs; changing a filter resets to page 1.
- Sort state also lives in query params (`?sort=created_at&dir=desc`).
- Money columns render integer cents via a shared `formatCents` helper; raw cent values are never shown.

## Read-Mostly + Dangerous Actions

- The admin app is read-mostly: browsing, filtering, and metrics are the default; mutations are rare and deliberate.
- Every dangerous action (deactivate user, cancel a workspace's subscription, delete workspace) requires a confirmation dialog that names the target ("Deactivate jane@acme.com?") — generic "Are you sure?" is not acceptable.
- Destructive confirmations for irreversible actions additionally require typing the resource name/slug.
- No bulk mutations in v1 — dangerous actions operate on one row at a time.
- After any mutation, invalidate the affected table's query key; no optimistic updates in the admin app (correctness over snappiness here).
