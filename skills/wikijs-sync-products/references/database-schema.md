# Database boundaries and completion schema

Version 0.4 keeps the existing read-only catalogue and state database, but
replaces the legacy research scheduler with one minimal completion registry.

## Contents

- [Product source contract](#product-source-contract)
- [Database roles](#database-roles)
- [Catalogue snapshot](#catalogue-snapshot)
- [Completion table](#completion-table)
- [Selection and save semantics](#selection-and-save-semantics)
- [Legacy success backfill](#legacy-success-backfill)
- [Legacy tables](#legacy-tables)
- [Backup and retention](#backup-and-retention)

## Product source contract

The supported source remains PostgreSQL table `public.products`. The runtime
issues only `SELECT` statements and starts a read-only transaction.

| Column | Type | Meaning |
| --- | --- | --- |
| `product_id` | varchar, non-null | Stable internal identity and permanent completion key |
| `brand_code` | varchar, nullable | Internal brand hint; not necessarily a public manufacturer name |
| `family_code` | varchar, nullable | Optional local family hint |
| `product_name` | text, nullable | Best available public model/name research seed |
| `unit_of_measure` | varchar, nullable | Catalogue metadata |
| `created_at` | timestamp | Source creation time |
| `updated_at` | timestamp | Incremental refresh watermark |

Never use `product_name` as the idempotency key. Names may be blank, generic,
or repeated. Never assume `brand_code` names the public manufacturer.

Configure the manual catalogue-admin MCP with `PGHOST`, `PGPORT`, `PGDATABASE`,
and an explicit `PGSSLMODE`. Accept only the standard libpq modes `disable`,
`allow`, `prefer`, `require`, `verify-ca`, and `verify-full`. Prefer
`verify-full` with `PGSSLROOTCERT` pointing at a read-only mounted CA. Do not
configure `PGUSER` or `PGPASSWORD`: an explicitly requested interactive refresh
receives a temporary SELECT-only username/password for that one tool call.

## Database roles

Keep four independent data roles:

- the source-catalogue PostgreSQL role is read-only;
- the PV Wiki state role owns the catalogue snapshot and completion table;
- Wiki.js owns its application database;
- Hermes owns no application database and reaches state only through the PV
  Wiki MCP.

Set `PV_WIKI_STATE_DATABASE_URL` to a dedicated production PostgreSQL database.
`PV_WIKI_STATE_PATH` keeps the SQLite-compatible backend available for local
tests and legacy recovery, not for a multi-process production deployment.

PV Wiki never creates tables in the product catalogue or Wiki.js database. It
writes Wiki.js content only through the restricted GraphQL API token.

## Catalogue snapshot

The zero-argument `pv_next_product` reads only the state database and has no
source-database connection path. A separate interactive toolset exposes
`pv_refresh_catalogue(username, password)` only after the user explicitly asks
to update the product list. The username/password are used as explicit psycopg
connection arguments, then released; PV Wiki never writes or returns them.
Hermes/provider history can still retain tool inputs, so use a short-lived
SELECT-only role and revoke it after the call.

The manual tool first completes one full, repeatable-read, read-only scan. It
rejects an empty list or duplicate `product_id`, updates the legacy rollback
rows, and atomically replaces these Hermes-specific tables:

```sql
CREATE TABLE IF NOT EXISTS product_catalogue_snapshot (
    product_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    source_updated_at TEXT,
    payload_json TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_catalogue_snapshot_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    initialized INTEGER NOT NULL CHECK (initialized IN (0, 1)),
    source_records INTEGER NOT NULL CHECK (source_records >= 0),
    checksum TEXT,
    refreshed_at TEXT,
    source TEXT NOT NULL
);
```

The replacement is one state-database transaction: readers see either the old
complete snapshot or the new complete snapshot. A product absent from the new
source scan disappears from the active snapshot without deleting legacy
`products`, attempts, or completions. On upgrade, the snapshot is backfilled
once from the existing `products` table; later legacy upserts do not silently
change it. The recurring research MCP receives no `PG*` source settings.

This removal affects future product selection only. It does not cancel an
already accepted pending publication, which uses the immutable `product_json`
captured in its completion. Publication withdrawal requires a separate,
explicit operator decision.

## Completion table

Opening `HermesCompletionStore` creates this table and pending-publication
index if they do not exist:

```sql
CREATE TABLE IF NOT EXISTS product_research_completions (
    product_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    product_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    publishable INTEGER NOT NULL CHECK (publishable IN (0, 1)),
    completed_at TEXT NOT NULL,
    published_at TEXT,
    wiki_path TEXT,
    wiki_action TEXT,
    CHECK (published_at IS NULL OR publishable = 1)
);

CREATE INDEX IF NOT EXISTS product_research_completions_pending_idx
ON product_research_completions(
    publishable,
    published_at,
    completed_at,
    product_id
);
```

The schema uses canonical JSON text and UTC timestamp text so the same state
contract works with the existing PostgreSQL and SQLite adapters.

| Column | Contract |
| --- | --- |
| `product_id` | Permanent cross-session idempotency key; one completion per catalogue product |
| `source_hash` | Exact catalogue snapshot researched by Hermes; checked only when saving the first completion |
| `product_json` | Canonical catalogue payload captured at completion time |
| `decision_json` | Validated schema-version-`2` result, including bounded citation metadata and exact quotes |
| `evidence_json` | Extracted evidence URLs and SHA-256 hashes only; never full document bodies |
| `outcome` | `publish`, `no_datasheet`, `ambiguous`, `insufficient_identity`, or `out_of_scope` |
| `publishable` | `1` only for the `publish` outcome |
| `completed_at` | UTC time the first decision was accepted |
| `published_at` | UTC time Wiki.js publication was durably acknowledged; null while pending |
| `wiki_path` | Stable Wiki.js path after successful publication |
| `wiki_action` | Last successful Wiki.js upsert action |

Full extracted bodies are supplied to `pv_save_research` only for validation.
Each URL/body/receipt tuple is first verified against the shared Exa-gateway
HMAC key. The bodies are then used in memory to verify URLs and exact evidence
quotes before being reduced to URL plus SHA-256 entries in `evidence_json`.
Receipts are not stored. `decision_json` may retain the short, explicitly cited
exact quotes needed to explain published facts; it does not retain whole source
documents or model prompts.

## Selection and save semantics

The next-product query is deliberately simple. The long-lived MCP process
keeps only the last returned `product_id` in memory and first asks for the next
lexical unfinished id:

```sql
SELECT p.product_id, p.source_hash, p.payload_json
FROM product_catalogue_snapshot AS p
WHERE NOT EXISTS (
    SELECT 1
    FROM product_research_completions AS completed
    WHERE completed.product_id = p.product_id
)
  AND p.product_id > :process_cursor
ORDER BY p.product_id
LIMIT 1;
```

When no row follows the cursor, the same anti-join wraps to its first row
without the cursor predicate. This is an anti-join, not a claim or lease. The
cursor is process-local and is never written to PostgreSQL; restarting the MCP
may revisit the lexical first unfinished product once, then fairness resumes.
This prevents a product with no safely provable public outcome from starving
the rest of the catalogue without inventing a completion, backoff, or retry
schedule. Run one serialized Hermes cron job to avoid parallel research of the
same unfinished row.

`pv_save_research` enforces these steps with a validation snapshot followed by
an atomic write fence:

1. Verify every submitted Exa evidence receipt, including on an idempotent save
   replay.
2. Return the existing completion when `product_id` is already present.
3. Read the active catalogue snapshot and reject an absent product or mismatched
   `source_hash`.
4. Validate schema version `2`, catalogue identity, source authority,
   citations, and exact evidence spans against that snapshot.
5. In the write transaction, reread the active snapshot row and compare
   `source_hash` again so a concurrent change or removal rejects stale research.
6. Insert with `ON CONFLICT (product_id) DO NOTHING` and return the first
   writer's row.

Both publish and non-publish outcomes complete the product permanently.
Changing `source_hash` later does not automatically delete or invalidate its
completion. Re-research requires an explicit operator-approved delete after a
backup; routine catalogue refreshes never do it.

Publication updates only `published_at`, `wiki_path`, and `wiki_action` after
Wiki.js succeeds. A Wiki.js failure leaves a publishable completion pending.
The pending index selects the oldest such row for a publication-only retry.

## Legacy success backfill

Initialization performs an idempotent backfill from the legacy worker:

- select each `products` row with `last_success_at IS NOT NULL` for which no
  completion already exists;
- read the most recent finished `attempts` row whose outcome is `synced`, when
  available;
- preserve its bounded decision URLs, Wiki.js path, and action when available;
- insert outcome `publish`, `publishable=1`, and set both completion and
  publication time to `last_success_at`;
- use `ON CONFLICT (product_id) DO NOTHING` as a concurrent-initialization
  safeguard so a native Hermes completion always wins.

This prevents already published legacy products from re-entering autonomous
research. Missing historical decision details do not weaken the idempotency
marker; a backfilled row still counts as completed and published.

## Legacy tables

Do not delete the version 0.3 tables during upgrade. The Hermes path copies
`products` into its dedicated snapshot once and reads `products`/`attempts` for
successful publication backfill. It does not use:

- product queue states, leases, backoff, or failure streaks;
- attempt ownership or retry policy;
- `research_actions` rounds, request fingerprints, credits, or uncertain
  action state;
- `requeue_events` policy epochs;
- global daily/monthly credit reservations or provider circuits.

Those structures remain available only to the inactive legacy n8n rollback
worker. Their presence must not filter, pause, or budget a Hermes research
session.

## Backup and retention

Back up the complete PV Wiki state database, not only the completion table. A
consistent backup preserves the catalogue payloads referenced by completions
and the legacy data required for rollback.

Treat completion deletion or state restoration as a material operation. A
backup restored to an earlier time forgets later completed products and can
cause legitimate re-research. Record the backup timestamp, deployed commit,
Hermes cron state, and pending-publication count before restoration.

Do not store Exa keys, Wiki.js tokens, PostgreSQL passwords, full extracted
bodies, or Hermes prompts in state-table metadata or database backup labels.
