# PV Wiki

PV Wiki turns a read-only PostgreSQL product catalogue into cited,
reader-facing Wiki.js pages. n8n owns schedules, execution history, bounded
retries for idempotent or lease-safe calls, and notifications. A separate
`pv-wiki-worker` performs one bounded product cycle at a time and owns durable
queue retry/backoff state:

```text
PostgreSQL catalogue (read only)
  → bounded Exa Search and Contents
  ↔ user-configured OpenAI-compatible research actions
  → strict local decision validation
  → Wiki.js GraphQL API
```

The included Hermes skill is an installation and operations runbook. Hermes
may inspect an authorized VPS, reuse or install n8n, deploy the worker, import
inactive workflows, and run acceptance checks. It is not the recurring worker
and must not create a Hermes cron job.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| Hermes skill | One-time discovery, installation, upgrade, repair, and removal guidance |
| n8n | Schedule triggers, execution history, bounded retry-safe calls, and system/batch-level alerts |
| PV Wiki worker | Product queue/backoff, bounded multi-round research, source validation, and Wiki.js updates |
| Exa | Bounded public-web discovery and content extraction; never writes Wiki.js |
| AI provider | Proposes either a final decision or a bounded evidence-gap search; never writes Wiki.js |

The model, API base URL, and API key are all operator supplied. The first
release supports OpenAI-compatible Chat Completions and fixes the endpoint to
`{AI_BASE_URL}/chat/completions`.

## Database boundaries

There are three independent data roles:

- the existing product catalogue PostgreSQL database is queried with a
  read-only transaction;
- n8n uses its own PostgreSQL database/user for workflows, encrypted
  credentials, and executions;
- Wiki.js owns and connects to its own application database.

PV Wiki never creates tables in the product catalogue or the Wiki.js database.
It writes Wiki.js content only through the restricted GraphQL API token. Its
local SQLite file stores orchestration state, leases, evidence URLs, and audit
metadata; it does not replace any of the three application databases.

## What it does

- Reads the fixed `public.products` shape through an explicitly configured
  PostgreSQL transport and a read-only account.
- Maintains durable and resumable leases in SQLite.
- Runs one initial research pass and at most two AI-requested supplemental
  passes inside one `/run-one` call. Defaults cap the product at three AI
  actions, seven basic search queries, five unique extract URLs, and a
  20-unit search admission budget. Before each call it reserves one normalized
  unit per Search query or two units per Extract batch; Exa's reported dollar
  cost is also retained in audit metadata. No new
  research action starts after the 600-second deadline.
- Uses Exa as the sole bounded search and extraction provider. A retrieval
  comparison supporting that decision is recorded in
  [`docs/search-provider-benchmark-2026-07-26.md`](docs/search-provider-benchmark-2026-07-26.md).
- Calls a user-selected OpenAI-compatible model with bounded public discovery
  hints and extracts so it can identify the manufacturer and product type.
  The model may return only a final proposal or one of six fixed evidence gaps
  with one or two locally validated, exact-model-bound supplemental queries.
- Lets the model classify matching generic hardware such as screws, bolts,
  nuts, and washers as out of scope, without using `family_code` for routing.
  The local gate requires an exact contiguous quote tying the complete product
  identity to an explicit hardware type. If an energy-product page merely
  mentions an accessory bolt, the classification is rejected. A second local
  gate also rejects any `publish` proposal whose model or public category
  itself identifies generic hardware.
- Uses `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` as an optional source-domain
  override and fast path, not as a complete manufacturer registry.
- When no mapping matches, automatically verifies only an HTTPS manufacturer
  host whose name is consistent with the AI-discovered manufacturer and whose
  extracted body contains both that manufacturer and the complete product
  model. A second independent HTTPS extract must corroborate the identity, and
  every published fact must have exact quotes from both domains. A failed check
  becomes `source_unverified` and cannot publish.
- Requires the complete catalogue product name in publication evidence as well
  as an exact proposed-model match.
- Allows multi-model series datasheets into analysis, while requiring every
  published fact to use a target-model-only span with no sibling/revision.
- Keeps database IDs, family codes, lease tokens, and secrets out of the model
  prompt.
- Validates exact source URLs, source trust, confidence, conflicts, public
  category, five unique facts, and short exact model/label/value evidence spans
  before a page can publish.
- Upserts only the `PV-WIKI-AUTO` section and preserves human-authored text.
- Builds a normal catalogue homepage with brand/category entry points, totals,
  per-brand counts, and recently updated products.
- Creates new Wiki.js pages as private, unpublished drafts by default.
- Records valid non-publish outcomes in SQLite and retries them automatically;
  it does not create a per-product AI issue or manual-review queue.
- Persists every Search, Extract, and AI action before execution. A current
  attempt cannot replay an action slot, and any unresolved `started` or
  `uncertain` action blocks later calls while its action-specific provider and
  wire-contract scope is unchanged. Search-provider and AI scopes are independent, and
  legacy rows without a reliable scope block fail-closed. The queue records
  `research_uncertain` instead of inventing a content conclusion; completed
  calls may be repeated after a crash because response bodies are deliberately
  not stored.

It does not mirror full datasheets, expose an arbitrary command endpoint, mount
the Docker socket, enable n8n Execute Command, or silently accept low-confidence
AI output.

## Deploy with n8n

The production bundle is in [`deploy/n8n`](deploy/n8n/README.md). It includes:

- a dedicated n8n PostgreSQL service;
- persistent n8n and PV Wiki volumes;
- an internal-only authenticated worker;
- an optional Caddy HTTPS overlay;
- two secret-free, inactive workflow templates:
  - `PV Wiki - Product Cycle` starts on the existing monthly quota cadence
    and serially processes products until the queue is empty or every
    configured key has exhausted its credits; a daily non-quota-waking
    catalogue refresh recovers partial/failed source scans, and an hourly
    trigger resumes due/backoff work without rerunning the catalogue sync;
  - `PV Wiki - Homepage Refresh` daily at 02:35 Asia/Shanghai.

The installation and rollout, rather than recurring product decisions, are
deliberately review-gated:

1. Copy and fill `deploy/n8n/.env.example` and
   `deploy/n8n/worker.env.example`; keep both actual files mode `0600`.
2. Configure `EXA_API_KEYS` (or `EXA_API_KEY`), then select `AI_BASE_URL`,
   `AI_API_KEY`, and `AI_MODEL`. Optionally configure
   `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` with known public-manufacturer domain
   overrides to speed up source verification. AI-discovered manufacturer names
   take precedence over possibly stale internal brand codes.
3. Start the Compose project or attach only the worker to an existing n8n
   network.
4. Import the workflows while inactive.
5. Create one n8n Header Auth credential for the internal worker and attach it
   to the four HTTP Request nodes.
6. Run `pv-wiki doctor --live`, then manually test one private/unpublished
   product at the final path prefix and the homepage.
7. Let the user publish the schedules only after reviewing the output.

After activation, `no_datasheet`, `ambiguous`, `insufficient_identity`,
`out_of_scope`, `source_unverified`, and `research_uncertain` are normal
machine-handled outcomes:
they are silently audited and scheduled for an appropriate retry. They do not
open issues. Notifications are reserved for failures that stop or materially
impair a workflow batch, such as provider, configuration, database, or service
outages. Five consecutive invalid decisions affecting distinct products inside
30 minutes open one batch-level decision circuit before more products are
leased. A recent AI 401/402/403/404 opens a six-hour
provider-configuration circuit, and AI 429 opens a one-hour rate-limit circuit,
so the loop stops before spending search budget on more products through a
known-bad AI path. Invalid AI output on three distinct products within the same
one-hour provider scope opens the same pre-search batch stop. Configuration
rejections and repeated invalid output return a service error after recording
the product outcome, so n8n's bounded retries end in one batch-level alert.
The 429 circuit is expected transient state and returns a clean automatic stop.
If an explicit quota/4xx response follows an earlier completed subrequest, the
known partial credits are audited and the action remains automatically
retryable; only genuinely ambiguous paid requests are replay-suppressed.

n8n remains the outer supervisor: it schedules batches and repeatedly calls
the fixed `/run-one` endpoint. The worker owns the inner evidence feedback
loop, leases, paid-call ledger, source trust, and final Wiki.js permission, so
n8n does not need search, AI, catalogue, or Wiki.js credentials.

The example keeps new pages private and unpublished for the one-time rollout.
After that acceptance, an unattended public catalogue can set
`WIKIJS_NEW_PAGE_PRIVATE=false` and `WIKIJS_NEW_PAGE_PUBLISHED=true` once at the
deployment level; this is not a per-product approval step.

The worker exposes only:

- `GET /healthz`
- `POST /sync-catalogue`
- `POST /refresh-catalogue`
- `POST /run-one`
- `POST /publish-home`

All POST operations require the separate `PV_WIKI_WORKER_TOKEN`. An overlapping
scheduled `/run-one` stops cleanly with `worker_busy`. Catalogue synchronization
has its own serialized lock and may safely overlap research; source changes
invalidate the leased snapshot and are rescheduled. A shared publication fence
serializes only catalogue writes against the final source check, Wiki mutation,
and durable outcome, preventing a locally applied source revision from being
inserted midway through publication. Homepage publication also uses its own
serialized lock because it targets a different Wiki path. This fence is
process-local: the supported deployment runs exactly one worker replica, and
scheduled mutations must use its authenticated HTTP endpoints rather than a
concurrent direct CLI process. Other same-operation conflicts are rejected.
Exa HTTP 402 responses rotate to the next configured key.
The product workflow stops cleanly only when no product is due or all configured
keys are out of monthly credits; HTTP 429 remains a transient request-rate
limit.

Catalogue sync is intentionally history-preserving. A product absent from a
later PostgreSQL snapshot is not automatically deleted, archived, or removed
from the homepage; destructive retirement needs an explicit future policy so
the worker never erases historical or human-maintained Wiki content by
inference.

## Local CLI

For development or a supervised acceptance test:

```bash
python3 -m pip install -e .
pv-wiki doctor --live
pv-wiki sync-db
pv-wiki run-one --worker-id manual-acceptance
pv-wiki publish-home
```

Copy `.env.example` to an ignored `.env` and export it before running the CLI.
Prefer `PGSSLMODE=verify-full` with `PGSSLROOTCERT` pointing at the mounted
public/private CA. All standard libpq modes must be selected explicitly.
`disable`, `allow`, and `prefer` are supported for a catalogue server that
cannot use TLS, but may send catalogue credentials and data unencrypted; use
them only after the operator accepts that network risk.

The low-level `claim`, `search`, `extract`, and `publish` commands remain
available for supervised diagnosis. n8n should call the fixed HTTP worker
operations instead. A low-level publish decision requires the corresponding
bounded extract JSON through `publish --evidence-file ...`; source bodies are
used in memory for quote verification and are not added to the SQLite audit.

## Safe rollout and migration

Start with 20–50 representative products at the final Wiki.js prefix while
keeping new pages private and unpublished. Use a separate worker state volume
if a distinct staging prefix is required.
Review exact suffix/model matches, official datasheet citations, specification
tables, category/brand navigation, and preserved human content before enabling
public unattended publishing.

Older pages using `HERMES-AUTO` are migrated in place on their next update.
The legacy block and `managed-by-hermes` tag are removed while all text outside
the block remains untouched.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s skills/wikijs-sync-products/tests -v
```

Tests are offline and use mocked HTTP clients and temporary databases.
