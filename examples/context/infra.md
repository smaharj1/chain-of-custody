## Tooling

- AWS CDK in TypeScript, in `infra/`.
- One CDK app, multiple stacks, parameterized by environment (`dev` | `staging` | `prod`).
- Node 20 runtime everywhere. Region: us-east-1.

## Stacks

| Stack | Resources | Notes |
|---|---|---|
| `TaskflowWebStack` | S3 bucket + CloudFront distribution for `apps/web/` | SPA fallback: 404 -> `/index.html` |
| `TaskflowAdminStack` | S3 bucket + CloudFront distribution for `apps/admin/` | Same shape as web; separate domain |
| `TaskflowApiStack` | API Gateway HTTP API -> single Express Lambda | Lambda is VPC-attached for RDS access |
| `TaskflowRealtimeStack` | ECS Fargate service running Socket.IO | 0.25 vCPU / 512MB, ALB in front, same VPC as RDS |
| `TaskflowDataStack` | RDS PostgreSQL 16 | db.t4g.micro (dev), db.t4g.small (prod); private subnets only |

Cross-stack wiring via CDK exports (VPC, DB security group, secret ARNs) — no hardcoded ARNs.

## Deploy

```bash
pnpm cdk deploy --all -c env=dev       # per-env context flag: dev | staging | prod
```

- Frontends are built (`pnpm build`) before deploy; the CDK asset bundling picks up `apps/*/dist`.
- Database migrations are NOT run by CDK — they're a separate, deliberate step against the target env.
- `cdk diff` before every prod deploy; no exceptions.

## Secrets

- **All secrets live in Secrets Manager** — DB credentials, Clerk secret key, Stripe secret key + webhook signing secret, Resend API key.
- Never in code, never in committed env files, never in plain CDK context or Lambda environment literals. Lambda/Fargate read secret ARNs and resolve at runtime (or via CDK's `Secret.fromSecretNameV2` -> environment injection).
- `.env.local` is for local dev only and is gitignored.
- Clerk/Stripe use separate test-mode keys for dev/staging and live keys only in prod.

## Environment Promotion

- Order: **dev -> staging -> prod**. Nothing reaches prod without having been deployed to staging first.
- Each env is a full copy of all five stacks with its own RDS instance and its own secrets; envs never share a database.
- Promotion is a redeploy of the same commit with the next env's context flag, not a config mutation of a running env.

## Constraints

- Lambda must stay Drizzle-only (no Prisma binary) — see architect decision log.
- Socket.IO cannot move to Lambda/APIGW WebSockets (room broadcasting requirement); it stays on Fargate.
- Clerk and Stripe webhook endpoints must be reachable publicly through API Gateway; signature validation happens in the app layer, not infra.
