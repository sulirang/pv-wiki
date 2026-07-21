# PV Wiki

PV Wiki turns a read-only PostgreSQL product catalogue into cited,
reader-facing Wiki.js pages. n8n owns schedules, execution history, bounded
retries for idempotent maintenance calls, and notifications. A separate
`pv-wiki-worker` performs one bounded product cycle at a time and owns durable
queue retry/backoff state:

```text
PostgreSQL catalogue (read only)
  → Tavily Search and Extract
  → user-configured OpenAI-compatible model
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
| n8n | Schedule triggers, execution history, bounded idempotent-call retries, and operator-selected alerts |
| PV Wiki worker | Product queue and backoff, Tavily calls, AI proposal, validation, and Wiki.js updates |
| AI provider | Proposes structured facts from bounded evidence; never writes Wiki.js |

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
- Uses no more than three Tavily searches and five extracted URLs per product.
- Calls a user-selected OpenAI-compatible model with bounded public evidence.
- Accepts trusted source status only from the configured domain map for the
  catalogue brand, and requires the complete catalogue product name in the
  extract as well as an exact proposed-model match.
- Rejects bounded extracts that also contain a sibling model/revision; those
  series datasheets require supervised handling.
- Keeps database IDs, family codes, lease tokens, and secrets out of the model
  prompt.
- Validates exact source URLs, source trust, confidence, conflicts, public
  category, five unique facts, and short exact model/label/value evidence spans
  before a page can publish.
- Upserts only the `PV-WIKI-AUTO` section and preserves human-authored text.
- Builds a normal catalogue homepage with brand/category entry points, totals,
  per-brand counts, and recently updated products.
- Creates new Wiki.js pages as private, unpublished drafts by default.

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
  - `PV Wiki - Product Cycle` starts after the monthly Tavily credit refresh
    and serially processes products until the queue is empty or every
    configured key has exhausted its credits;
  - `PV Wiki - Homepage Refresh` daily at 02:35 Asia/Shanghai.

The installation flow is deliberately review-gated:

1. Copy and fill `deploy/n8n/.env.example` and
   `deploy/n8n/worker.env.example`; keep both actual files mode `0600`.
2. Select `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, and the operator-verified
   brand-to-domain map `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON`.
3. Start the Compose project or attach only the worker to an existing n8n
   network.
4. Import the workflows while inactive.
5. Create one n8n Header Auth credential for the internal worker and attach it
   to the three HTTP Request nodes.
6. Run `pv-wiki doctor --live`, then manually test one private/unpublished
   product at the final path prefix and the homepage.
7. Let the user publish the schedules only after reviewing the output.

The worker exposes only:

- `GET /healthz`
- `POST /sync-catalogue`
- `POST /run-one`
- `POST /publish-home`

All POST operations require the separate `PV_WIKI_WORKER_TOKEN`; concurrent
operations are rejected. Tavily HTTP 432/433 responses rotate to the next key.
The product workflow stops cleanly only when no product is due or all configured
keys are out of monthly credits; HTTP 429 remains a transient request-rate
limit.

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
