## System Topology

CloudFront serves both React SPAs and static assets from S3. API Gateway HTTP API proxies to a single Express Lambda. All REST routes are handled by that Lambda. The Lambda connects to RDS PostgreSQL. Socket.IO runs on a separate small ECS Fargate service for real-time (WebSocket connections don't work well with Lambda's request/response model). Clerk handles authentication externally. Stripe handles payments externally.

## Service Map

| Component | Service | Key Config |
|---|---|---|
| CDN | CloudFront | React SPAs + images from S3 origins |
| API routing | API Gateway HTTP API | Proxy to single Lambda |
| Compute | Lambda | Node 20, Express 5 |
| Real-time | ECS Fargate | Socket.IO server, 0.25 vCPU, 512MB |
| Database | RDS PostgreSQL 16 | db.t4g.micro (dev), db.t4g.small (prod) |
| Auth | Clerk | Email/password + Google OAuth, webhook sync |
| Payments | Stripe | Checkout Sessions + Billing (subscriptions) |
| Storage | S3 | SPA assets + user file uploads |
| Email | Resend | Transactional templates (invite, receipt, etc.) |
| IaC | CDK (TypeScript) | Multi-stack |

## Infrastructure Constraints

- Lambda is VPC-attached for RDS access.
- Use Drizzle ORM, not Prisma, to avoid binary dependency.
- Secrets in Secrets Manager, not environment variables.
- Clerk webhook signature must be validated on every webhook event.
- Socket.IO service must be in the same VPC as RDS for real-time query access.

## Auth Model

- Roles: Member, Admin, Owner (workspace-scoped).
- Platform-level: User, PlatformAdmin.
- Users authenticate via Clerk (email/password or Google OAuth).
- Clerk `userId` is the authoritative user ID everywhere.
- Workspace membership is stored in app DB, not Clerk.
- Clerk webhook (`user.created`, `user.updated`) syncs user records.

## Real-time Architecture

- Socket.IO on ECS Fargate.
- Clients connect after authentication; server validates Clerk session token on connect.
- Rooms scoped to `workspace:{workspaceId}` and `board:{boardId}`.
- Events: `task.moved`, `task.created`, `task.updated`, `task.deleted`, `presence.update`.
- REST remains source of truth; Socket.IO signals optimistic UI updates + conflict resolution.

## Performance Targets

- FCP <= 1.5s.
- API p95 <= 200ms for list endpoints, <= 500ms for mutation endpoints.
- Real-time event delivery <= 100ms p95 within a room.
- Image budget <= 200KB WebP for user uploads.

## Decision Log

- Chose Clerk over Auth0 for faster integration and better React hooks.
- Socket.IO on Fargate instead of API Gateway WebSocket because we need room-based broadcasting which APIGW doesn't natively support well.
- Chose Drizzle over Prisma to avoid binary dependency in Lambda.
- Subscriptions via Stripe Billing, not custom billing logic.
- No GraphQL — REST is sufficient for current complexity.

## Out of Scope (v1)

- Native mobile apps.
- Gantt chart view.
- Custom fields on tasks.
- SSO / SAML for enterprise.
- Self-hosted option.
- Audit log for compliance.
