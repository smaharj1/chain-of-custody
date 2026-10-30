# Migrations: expand, backfill, contract

> Shared kit rule (not a project primer, so it never carries template placeholders). Read by the Architect (designing a change that touches a table), the Planner (slicing it into items) and the Database Engineer (writing the migration). Report: CO-18, §16.1 Phase 0b.

A migration that is safe on an empty local database can still lock a table, lose data, or break the running app in production. The kit's rule is: **never change a column or table in one step that old code still depends on.** Every schema change that touches existing data is split into three stages, and **each stage is its own plan item** with its own review, verification and down-migration.

## The three stages

| Stage | What it does | Safe because | Down-migration |
|---|---|---|---|
| **1. Expand** | Adds the new shape next to the old one: a new nullable column, a new table, a new index (built concurrently where the engine allows). Old code keeps working untouched. Code starts **writing both** shapes. | Purely additive. | Drops what was added. Data loss class `none` or `lossy` (see below). |
| **2. Backfill** | Copies or derives data into the new shape for existing rows. A **separate, idempotent, resumable job**, not part of the migration file. | Runs in batches, can be stopped and restarted, safe to run twice. | Usually none: it only fills columns that stage 1 added. Say so. |
| **3. Contract** | Switches reads to the new shape, adds `NOT NULL` / constraints, then drops the old column or table. Only after stage 2 is verified complete. | Old code no longer reads what is being removed. | **Cannot restore dropped data.** Class `lossy`; human-gated. |

Typical case: renaming `status` to `state`. Expand adds `state` and writes both. Backfill copies `status` into `state` for old rows. Contract moves reads to `state`, makes it `NOT NULL`, drops `status`. A single "rename column" migration is the thing this rule exists to prevent.

## The backfill job (stage 2)

- **Batched**: a fixed batch size (for example 1,000 rows), ordered by primary key, committing per batch, so no long lock and no giant transaction.
- **Idempotent**: re-running it changes nothing it already changed (`WHERE new_col IS NULL`, or an upsert). Running it twice must be harmless.
- **Resumable**: it records its position (last key processed) or derives it from the data, so a stop in the middle restarts where it left off.
- **Verifiable**: it ends with a count that must be zero (rows still unfilled). The item's acceptance names that check.
- **Not inside the migration file.** A migration that backfills a large table holds locks and cannot be retried halfway. The old guidance ("backfill in the migration with batched updates") is withdrawn.

For a table that is small by design (a lookup table of a dozen rows), the Architect may fold stages 1 and 2 and says so in the design, with the row-count bound that makes it safe.

## Declare the data-loss class of every down-migration

Every migration item states, in the plan item and the design's Migration Notes, one of:

- `none`: the down restores the previous schema and loses no data (dropping an empty added column, removing an added index).
- `lossy-recoverable`: the down loses data that still exists elsewhere or can be re-derived (dropping a backfilled column whose source column remains).
- `lossy`: the down destroys data that exists nowhere else (dropping a column or table, narrowing a type, truncating).

The loop **proves** `up -> down -> up` on an ephemeral database for classes `none` and `lossy-recoverable`. A `lossy` down is **human-gated**: the loop does not run it automatically on a shared or durable database, and a checkpoint reject of such an item produces a revert plan for the human instead of executing the down. Contract stages are almost always `lossy`; that is the point of making them separate items a human approves.

## Other rules that follow

- Down migration must exist and work (test it locally), **except** where the class is `lossy` and the item says why no down can exist; then the item's acceptance includes a snapshot or backup note for the human.
- Migrations are numbered or timestamped by the tooling; never renumber a migration that has merged.
- Index creation on a large table uses the engine's non-blocking form (Postgres: `CREATE INDEX CONCURRENTLY`, which cannot run inside a transaction; say so in the item).
- API versioning follows the same logic: add the new field, support both, remove the old after clients move. The design-critic checks both (`.claude/agents/design-critic.md`).
