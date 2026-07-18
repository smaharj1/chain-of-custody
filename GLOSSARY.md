# Glossary

Plain-language definitions of the terms used throughout this kit. If a word in the README or an agent file is confusing, it's probably here.

## The Team / Workflow

**Agent / Subagent** — A specialized version of Claude with its own instructions and a focused job (e.g., "backend engineer"). The main Claude session can hand work to these and collect their results.

**Mode** — A "hat" the main Claude session wears, switched with a slash command. `/pm`, `/architect`, and `/tech-lead` are modes. Each changes how Claude behaves.

**Slash command** — A command you type starting with `/` (like `/architect`). It tells Claude to switch modes or run a specific routine.

**PM (Product Manager) mode** — Figures out *what* you want to build and *why*, by asking you questions. Writes it down as a requirements document. Doesn't make technical decisions.

**Architect mode** — Decides *how* to build it. Discusses trade-offs with you, then writes a technical design and an API contract.

**Tech Lead mode** — Turns the design into specific assignments ("briefs"), hands them to the engineer agents, and reviews their work.

**Engineer (subagent)** — An agent that actually writes code: backend, database, frontend, or infrastructure.

**Design Critic** — An agent that reviews the Architect's design *before* any code is written, to catch problems early. Runs automatically.

**Security Reviewer** — An agent that audits code for security problems. You run it before deploying.

**Brief** — A written assignment the Tech Lead gives an engineer: what to build, which files to read, what "done" looks like.

**Dispatch** — Handing a brief to an engineer agent to start work.

**Escalation** — When an engineer hits a problem it can't solve and hands it back up the chain for a human or the Architect to resolve.

**Telemetry** — A log file (`metrics.jsonl`) recording how each task went (time, retries, gaps). Useful for spotting patterns over time. You can ignore it at first.

## Project Structure

**Primer** — A document in `.claude/context/` that teaches the agents about *your* project (your database tables, your API, your conventions). The agents read these before working. **These are the files you fill in.**

**Feature folder** — A temporary workspace (`docs/features/<name>/`) holding the documents for one feature. Considered "scratch" — disposable once the feature is done.

**Canonical exemplar** — An existing file in your project that represents the "right way" to do something. Engineers copy its style instead of inventing their own. (On a brand-new project, none exist yet — see "Starting from Scratch" in the README.)

**Slug** — A short, hyphenated name for a feature, used in folder names. E.g., the feature "User Notifications" might have the slug `user-notifications`.

## Technical Terms

**Frontend** — The part of the app users see and click (buttons, pages, forms). Runs in the web browser.

**Backend** — The part that runs on a server: handles logic, talks to the database, enforces rules. Users never see it directly.

**API (Application Programming Interface)** — The set of "doors" the frontend uses to talk to the backend. E.g., "create a task" or "get my profile."

**API contract** — A precise written agreement of exactly what each API door expects and returns. The backend builds to it; the frontend relies on it.

**Endpoint** — One specific API door, e.g., `POST /api/tasks` (create a task).

**Database** — Where your app's data is permanently stored (users, orders, etc.), organized into tables.

**Schema** — The structure of your database: what tables exist and what columns (fields) each has.

**Migration** — A script that changes the database structure (e.g., adds a new column) in a safe, repeatable way. "Reversible" means it can be undone.

**ORM (Object-Relational Mapper)** — A library that lets code talk to the database using normal programming instead of raw database language. (Examples: Drizzle, Prisma.)

**Auth (Authentication & Authorization)** — Authentication = "who are you?" (login). Authorization = "what are you allowed to do?" (permissions).

**IaC (Infrastructure as Code)** — Defining your servers, databases, and cloud resources in code files instead of clicking around a web console. (Examples: AWS CDK, Terraform.)

**Tech stack** — The full set of tools and languages your project uses (frontend framework, backend language, database, etc.).

**Lint / Linting** — Automated checking of code for style problems and likely mistakes, without running it.

**Migration / build / deploy** — *Build* = package the code to run. *Deploy* = put it on a live server. *Migration* = update the database structure.

## Quality / Process Terms

**Idempotency** — A safety property: doing the same operation twice has the same effect as doing it once. (Important so a double-click doesn't charge a customer twice.)

**N+1 (query problem)** — A common performance bug where code makes one database query per item in a list instead of one query for the whole list. Slow.

**Eager-load** — Fetching related data up front in one query, to avoid the N+1 problem.

**DI (Dependency Injection)** — A code-organization technique where components are handed the things they need rather than creating them. Makes code easier to test and change.

**Concurrency / race condition** — Bugs that appear when two things happen at the same time (e.g., two people buying the last item at once).

**Edge case** — An unusual situation the code must still handle correctly (empty list, huge input, network failure mid-operation).

**Code review** — Checking written code for bugs and quality before accepting it. Here, the Tech Lead does this automatically after each engineer.

**Greenfield** — A brand-new project with no existing code. (Opposite: "brownfield," an existing project you're extending.)
