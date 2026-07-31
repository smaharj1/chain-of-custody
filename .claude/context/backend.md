<!-- BACKEND PRIMER — Domain context for the Backend Engineer -->
<!-- This file is read by the Backend Engineer at the start of every task. -->
<!-- Fill this in with YOUR project's backend architecture, patterns, and conventions. -->

## Runtime and Framework

<!-- Describe your backend runtime and framework. Examples: -->
<!-- - Node 20, Express 5, TypeScript strict -->
<!-- - Python 3.12, FastAPI, Pydantic v2 -->
<!-- - Go 1.22, Chi router -->

## Code Architecture

<!-- Describe the layering pattern your backend follows. Example: -->
<!-- "3-layer architecture: routes/ -> services/ -> repositories/" -->
<!-- For each layer, explain: what it does, what it imports, what it never does. -->

## Auth Middleware

<!-- How is authentication enforced? What middleware exists? -->
<!-- Example: "requireAuth validates JWT. requireAdmin checks role claim." -->

## Endpoint Catalog

<!-- List your API endpoints grouped by auth level. Keep this updated as endpoints are added. -->
<!-- Large existing codebase? You may POINT AT the source of truth instead of enumerating: -->
<!--   "Endpoint catalog: see src/routes/*.ts — conventions below apply to all of them."  -->
<!-- A pointer that stays true beats a list that goes stale. -->
<!-- Example:
Public endpoints:
- GET /api/items — list items
- GET /api/items/:id — item detail

Authenticated endpoints:
- POST /api/items — create item
- PATCH /api/items/:id — update item

Admin endpoints:
- DELETE /api/items/:id — delete item
-->

## Error Handling

<!-- Describe your error handling pattern. Examples: -->
<!-- - Custom error classes (NotFoundError, ConflictError, ValidationError) -->
<!-- - Centralized error handler maps error classes to HTTP status codes -->
<!-- - Services throw domain errors, never HTTP status codes directly -->

## Logging

<!-- Describe your logging conventions. What logger? What format? What to log in each layer? -->

## External Integrations

<!-- Describe any third-party integrations (payment providers, email, storage, etc.) -->
<!-- For each: what library, how it's abstracted, where the code lives. -->

## File Naming

<!-- What's the naming convention? Examples: -->
<!-- - Dot-separated: item.service.ts, item.repository.ts -->
<!-- - Kebab-case: item-service.ts, item-repository.ts -->

## Verification Commands

<!-- The exact lint + test commands the Backend Engineer runs before reporting APPROVED. -->
<!-- Execution-relevant: prove each command by running it once before writing it down -->
<!-- (see primer-protocol.md — a plausible wrong command surfaces later as a mystery failure). Example: -->
<!-- - Lint: pnpm lint -->
<!-- - Test: pnpm test -->

## Canonical Exemplar

<!-- Name one file that represents the "gold standard" for each layer. -->
<!-- Engineers will match this file's patterns. Example: -->
<!-- - Route: routes/items.ts -->
<!-- - Service: services/item.service.ts -->
<!-- - Repository: repositories/item.repository.ts -->
