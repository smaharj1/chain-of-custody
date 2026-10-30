<!-- INFRASTRUCTURE PRIMER — Domain context for the Infra Engineer -->
<!-- This file is read by the Infra Engineer at the start of every task. -->
<!-- Fill this in with YOUR project's infrastructure. -->
<!-- If your project has NO infrastructure-as-code, delete this file and delete .claude/agents/infra-engineer.md. -->

## IaC Tool

<!-- What tool defines your infrastructure? Examples: -->
<!-- - AWS CDK (TypeScript) -->
<!-- - Terraform -->
<!-- - Pulumi -->
<!-- - SST / Serverless Framework -->

## Stack / Module Layout

<!-- Where does infrastructure code live, and how is it organized? Example: -->
<!-- - infra/ — CDK app root -->
<!-- - infra/lib/network-stack.ts — VPC, subnets, security groups -->
<!-- - infra/lib/data-stack.ts — database, cache -->
<!-- - infra/lib/api-stack.ts — compute, API gateway, IAM roles -->
<!-- - infra/lib/storage-stack.ts — buckets, CDN -->

## Environments

<!-- List your environments and how they differ. Example: -->
<!-- - dev: smallest instance sizes, short log retention -->
<!-- - prod: larger sizes, long retention, deletion protection on -->
<!-- How is environment selected? (e.g., `cdk deploy --context env=prod`) -->

## Deploy Commands

<!-- How is infrastructure validated and deployed? Example: -->
<!-- - Validate: npx cdk synth -->
<!-- - Preview changes: npx cdk diff --context env=dev -->
<!-- - Deploy: npx cdk deploy --context env=dev -->

## Secrets Management

<!-- Where do secrets live and how do apps read them? Example: -->
<!-- - All secrets in AWS Secrets Manager under myapp/{env}/* -->
<!-- - Apps read at startup and cache in memory -->
<!-- - Never in environment variables or source code -->

## Conventions / Constraints

<!-- List your infrastructure conventions. Examples: -->
<!-- - Every compute unit gets its own least-privilege role -->
<!-- - No public database access -->
<!-- - All storage blocks public access by default -->
<!-- - Resource names prefixed with {project}-{env}- -->

## Canonical Exemplar

<!-- Name one stack/module file that represents the gold standard to follow. -->
<!-- Example: infra/lib/api-stack.ts -->
