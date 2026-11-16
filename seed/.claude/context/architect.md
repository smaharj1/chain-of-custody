<!-- ARCHITECT PRIMER — System-level context for the Architect persona -->
<!-- This file is read by the Architect at every session start. It contains the infrastructure -->
<!-- topology, service map, constraints, and decision log that inform technical design. -->
<!-- Fill this in with YOUR project's architecture. Delete sections that don't apply. -->

## System Topology

<!-- Describe how requests flow through your system. Example: -->
<!-- "CloudFront serves the React SPA. API Gateway proxies to a Lambda running Express. -->
<!-- The Lambda connects to RDS PostgreSQL through RDS Proxy. WebSocket connections go through API Gateway v2." -->

## Service Map

<!-- List every infrastructure component and its role. -->

| Component | Service / Tool | Key Config |
|---|---|---|
| CDN | <!-- e.g., CloudFront, Vercel Edge, Cloudflare --> | <!-- notes --> |
| API routing | <!-- e.g., API Gateway, ALB, nginx --> | <!-- notes --> |
| Compute | <!-- e.g., Lambda, ECS, EC2, Vercel Functions --> | <!-- notes --> |
| Database | <!-- e.g., RDS PostgreSQL, Aurora, PlanetScale --> | <!-- notes --> |
| Auth | <!-- e.g., Cognito, Auth0, Clerk --> | <!-- notes --> |
| Storage | <!-- e.g., S3, R2, GCS --> | <!-- notes --> |
| Email | <!-- e.g., SES, SendGrid, Resend --> | <!-- notes --> |
| IaC | <!-- e.g., CDK, Terraform, Pulumi --> | <!-- notes --> |

## Infrastructure Constraints

<!-- List hard constraints engineers must respect. Examples: -->
<!-- - Connection pooling is mandatory for serverless DB access. -->
<!-- - All auth validation occurs in the application layer, not at the gateway. -->
<!-- - Secrets live in Secrets Manager / Vault, never in environment variables. -->

## Auth Model

<!-- Describe roles, permissions, and how authentication works. -->
<!-- - What roles exist? (e.g., User, Admin, SuperAdmin) -->
<!-- - How do users authenticate? (e.g., email/password, OAuth, SSO) -->
<!-- - How are roles enforced? (e.g., JWT claims, middleware checks) -->

## Real-time Architecture

<!-- If your app has real-time features, describe the pub/sub or WebSocket architecture. -->
<!-- Delete this section if not applicable. -->

## Performance Targets

<!-- List your performance SLAs if any. Examples: -->
<!-- - FCP <= 1.5s, LCP <= 2.5s -->
<!-- - API p95 latency <= 200ms -->
<!-- - WebSocket delivery <= 250ms p95 -->

## Decision Log

<!-- Record key architectural decisions and their rationale. -->
<!-- These prevent re-litigating settled decisions during design. Examples: -->
<!-- - Chose Drizzle over Prisma to avoid binary dependency in Lambda. -->
<!-- - Deferred GraphQL to v2; REST is sufficient for current complexity. -->

## Out of Scope (current version)

<!-- List features explicitly deferred. Prevents scope creep during design. -->
