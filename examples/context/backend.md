## Runtime and Framework

- Node 20 LTS.
- Express 5 as the web framework.
- TypeScript strict mode.
- Single Lambda handles all REST routes.
- Drizzle ORM for database access.
- Pino for structured JSON logging.

## Code Architecture (3-layer with class-based DI)

All API code follows routes -> services -> repositories:
- `routes/` — HTTP concerns only (Zod parsing, calling service methods, response formatting, logging).
- `services/` — Classes with injected repositories. Business logic, throws domain error classes.
- `repositories/` — Classes with injected `db`. Drizzle queries only. Returns raw row types.
- `errors/index.ts` — `AppError`, `NotFoundError`, `ConflictError`, `ValidationError` (domain-only, no HTTP codes).
- DI wiring: middleware creates repos + services per request, attaches to `req.services`.
- File naming: `task.service.ts`, `task.repository.ts` (dot-separated).
- Canonical exemplar: `task.service.ts`, `task.repository.ts`, `routes/tasks.ts`.

## Auth Middleware

- `requireAuth` validates Clerk session token via `@clerk/express`.
- `requireWorkspaceMember` checks membership in the target workspace.
- `requireWorkspaceAdmin` checks admin/owner role in workspace.
- Auth is enforced inside Express middleware, not at API Gateway.

## Endpoint Catalog

Public endpoints:
- `GET /api/health` — health check.

Authenticated endpoints:
- `POST /api/workspaces` — create a workspace.
- `GET /api/workspaces` — list user's workspaces.
- `GET /api/workspaces/:workspaceId/boards` — list boards.
- `POST /api/workspaces/:workspaceId/boards` — create board.
- `GET /api/boards/:boardId/tasks` — list tasks on a board.
- `POST /api/boards/:boardId/tasks` — create task.
- `PATCH /api/tasks/:taskId` — update task (title, status, assignee, etc.).
- `DELETE /api/tasks/:taskId` — delete task.
- `POST /api/tasks/:taskId/time-entries` — start/stop time tracking.
- `GET /api/account/subscription` — current plan + usage.

Admin endpoints (workspace admin/owner):
- `POST /api/workspaces/:workspaceId/invite` — invite member.
- `DELETE /api/workspaces/:workspaceId/members/:userId` — remove member.
- `PATCH /api/workspaces/:workspaceId/members/:userId` — change role.

Platform admin endpoints:
- `GET /api/admin/users` — list all users.
- `GET /api/admin/workspaces` — list all workspaces.
- `GET /api/admin/metrics` — platform metrics.

Webhook endpoints:
- `POST /api/webhooks/clerk` — Clerk webhook (user sync).
- `POST /api/webhooks/stripe` — Stripe webhook (subscription events).

## Error Handling

- Custom error classes: `NotFoundError`, `ConflictError`, `ValidationError`, `ForbiddenError`.
- Services throw domain errors, never HTTP status codes.
- Centralized error handler maps error classes to HTTP codes via `instanceof`.
- Routes validate input with Zod `safeParse()`. On failure, return 400 with formatted error.

## Logging

- Pino logger only. NEVER use `console.log` or any `console.*` method.
- Routes: use `req.log` (Pino child logger with request ID).
- Services: accept logger via constructor, use `this.log`.
- Repositories: do NOT log. Errors propagate up.

## External Integrations

- **Clerk**: user authentication + webhook sync. Library: `@clerk/express`.
- **Stripe**: subscriptions via Stripe Billing. Library: `stripe`. Webhook sig validation required.
- **Resend**: transactional email. Library: `resend`. Templates: invite, receipt, trial-ending.
- **Socket.IO**: real-time events published from service layer after DB writes succeed.

## Canonical Exemplar

- Route: `routes/tasks.ts`
- Service: `services/task.service.ts`
- Repository: `repositories/task.repository.ts`
