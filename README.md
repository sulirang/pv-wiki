# PV Wiki

PV Wiki turns a read-only PostgreSQL product catalogue into cited,
reader-facing Wiki.js pages. n8n owns schedules, execution history, bounded
retries for idempotent catalogue/homepage calls, and notifications. A separate
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
| n8n | Schedule triggers, execution history, bounded batches, idempotent-operation retries, and system/batch-level alerts |
| PV Wiki worker | Product queue/backoff, bounded multi-round research, source validation, and Wiki.js updates |
| Exa | Bounded public-web discovery and content extraction; never writes Wiki.js |
| AI provider | Proposes either a final decision or a bounded evidence-gap search; never writes Wiki.js |

The model, API base URL, and API key are all operator supplied. The first
release supports OpenAI-compatible Chat Completions and fixes the endpoint to
`{AI_BASE_URL}/chat/completions`. The optional
`AI_THINKING_MODE=enabled|disabled` provider extension sends
`"thinking":{"type":"..."}` when explicitly configured; it is omitted by
default so other compatible providers retain their native behavior.

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
  Search-result URLs are withheld from the model; only successful Extract URLs
  are visible as citation candidates.
  The model may return only a final proposal or one of six fixed evidence gaps
  with one or two locally validated, exact-model-bound supplemental queries.
- Lets the model classify matching generic hardware such as screws, bolts,
  nuts, and washers as out of scope, without using `family_code` for routing.
  The local gate requires an exact contiguous quote tying the complete product
  identity to an explicit hardware type. If an energy-product page merely
  mentions an accessory bolt, the classification is rejected. A second local
  gate also rejects any `publish` proposal whose model or public category
  itself identifies generic hardware.
- Uses `PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` to map an operator-owned catalogue
  `brand_code` to its public manufacturer name. Raw brand codes are never
  substituted by AI output. `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` separately
  maps that catalogue brand or its exact public alias to narrow trusted hosts.
- When no trusted-domain mapping matches, automatically verifies only an HTTPS
  manufacturer host whose name is consistent with the operator-approved public
  alias when one is configured, or otherwise with the AI-discovered public
  manufacturer. Its extracted body must contain both that manufacturer and the
  complete product model. A second independent HTTPS extract must corroborate
  that manufacturer and complete model. Specification facts themselves are
  quoted from the verified primary manufacturer datasheet; they do not each
  need a duplicate quote from the independent identity source. A failed check
  becomes `source_unverified` and cannot publish.
- Separates a descriptive catalogue name from its public model identity. A
  proposed model must be a complete distinctive model embedded in the name or
  an eligible alphanumeric catalogue code when internal search hints are
  explicitly enabled, and only cited extracts—not unrelated successful
  candidates—must contain that bound model.
- Allows multi-model series datasheets into analysis. A normal prose/target-only
  row still needs one exact model/label/value quote. A Markdown-pipe or TSV
  table may instead provide `model_quote` for the exact model-header row and
  `quote` for the exact parameter row; the runtime accepts it only when the
  target model occurs in one unique header cell and the selected value is
  unambiguous in the same column.
- Keeps family codes, lease tokens, secrets, and unapproved database IDs out of
  the model prompt. Only an explicitly enabled, short ASCII model-shaped
  `product_id` may be promoted to the public model hint.
- Validates exact source URLs, source trust, confidence, conflicts, public
  category, five unique facts, and source-grounded model/label/value evidence
  spans before a page can publish. Ordinary URL/domain constraints accidentally
  included in an AI supplemental query are discarded; obfuscated forms fail
  closed.
- Upserts only the `PV-WIKI-AUTO` section and preserves human-authored text.
- Builds a normal catalogue homepage with brand/category entry points, totals,
  per-brand counts, and recently updated products.
- Creates new Wiki.js pages as private, unpublished drafts by default.
- Records valid non-publish outcomes in SQLite and retries them automatically;
  it does not create a per-product AI issue or manual-review queue.
- Selects eligible `due` and matured `backoff` products with an approximately
  4:1 weighted preference, falling back to the other class when one is empty.
  Six consecutive content failures for one unchanged source revision cause a
  `content_quarantined` annual wait; a catalogue source change wakes the
  product immediately.
- Supports optional UTC-wide research stop-losses through
  `PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT` and
  `PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT`. Zero disables the corresponding
  window; each non-zero limit must be at least the per-product
  `PV_WIKI_RESEARCH_MAX_CREDITS`. Immediately before paid research, the worker
  reserves that full per-product maximum. Insufficient remaining headroom or
  any unknown/uncertain Exa usage in the active window stops fail-closed,
  restores the product's exact prior queue state, and reports its UTC resume
  boundary. These limits count audited Exa Search/Extract units, not AI tokens
  or currency; keep provider-account spend limits enabled for AI.
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
    and makes at most 15 serial product calls per workflow execution, without
    starting a new call after 45 minutes; a daily non-quota-waking
    catalogue refresh recovers partial/failed source scans, and an hourly
    trigger resumes due/backoff work without rerunning the catalogue sync;
  - `PV Wiki - Homepage Refresh` daily at 02:35 Asia/Shanghai.

The installation and rollout, rather than recurring product decisions, are
deliberately review-gated:

1. Copy and fill `deploy/n8n/.env.example` and
   `deploy/n8n/worker.env.example`; keep both actual files mode `0600`.
2. Configure `EXA_API_KEYS` (or `EXA_API_KEY`), then select `AI_BASE_URL`,
   `AI_API_KEY`, and `AI_MODEL`. If the selected provider supports the
   `thinking` request extension and its default consumes the output budget
   before returning JSON, explicitly set `AI_THINKING_MODE=disabled`;
   otherwise leave it empty. Configure
   `PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` for catalogue brands that need an
   operator-approved public manufacturer identity. Optionally configure
   `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON`, keyed by the exact catalogue brand or
   public alias, with narrow trusted hosts. AI output cannot select or replace
   either mapping. A trusted-domain override also requires the corresponding
   public brand alias so the runtime has an operator-approved manufacturer
   identity. Set the optional global daily/monthly credit limits before
   unattended operation; their default `0` values disable those stop-losses.
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
30 minutes open one batch-level decision circuit before more paid research. A
recent AI 401/402/403/404 or Exa 401/403/404 opens a six-hour provider circuit,
either provider's 429 opens a one-hour rate-limit circuit, and exhausted Exa
keys open through the next UTC month. Invalid AI output on three distinct products
within the same one-hour provider scope opens the same pre-search batch stop.
These gates are checked after local-only handling but before paid research.
While open they audit `system_paused`, restore the product's prior queue state,
and return a clean `processed=false` response. Exact resume times are returned
for provider rejection/rate-limit and quota gates; decision/output gates expose
their bounded windows in authenticated status. n8n does not automatically
retry `Run One Product`; a request failure stops that execution for one
batch-level alert.
If an explicit quota/4xx response follows an earlier completed subrequest, the
known partial credits are audited and the action remains automatically
retryable; only genuinely ambiguous paid requests are replay-suppressed.

n8n remains the outer supervisor: it schedules bounded batches and repeatedly
calls the fixed `/run-one` endpoint at most 15 times, stopping before a new
call once 45 minutes has elapsed. `Run One Product` has no automatic retry,
because another call can lease a different queue item. The worker owns the
inner evidence feedback
loop, leases, paid-call ledger, source trust, and final Wiki.js permission, so
n8n does not need search, AI, catalogue, or Wiki.js credentials.

The example keeps new pages private and unpublished for the one-time rollout.
After that acceptance, an unattended public catalogue can set
`WIKIJS_NEW_PAGE_PRIVATE=false` and `WIKIJS_NEW_PAGE_PUBLISHED=true` once at the
deployment level; this is not a per-product approval step.

The worker exposes only:

- `GET /healthz`
- `GET /status`
- `POST /sync-catalogue`
- `POST /refresh-catalogue`
- `POST /run-one`
- `POST /publish-home`

`GET /status` and all POST operations require the separate
`PV_WIKI_WORKER_TOKEN`; only `/healthz` is public. The authenticated status
response reports redacted queue, global-budget, and circuit readiness without
making paid probes. An overlapping
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
Exa HTTP 402 responses rotate to the next configured key. AI and Exa
provider-global rejection/rate-limit circuits, Exa monthly quota, decision and
invalid-output circuits, and the global daily/monthly Exa research budget are
checked after local-only handling but before another paid call. A pause writes
an audit-only `system_paused` attempt and restores the product without changing
its failure count or last outcome. This still allows empty-identity handling,
content quarantine, and same-source Wiki-only publication recovery. The
product workflow also stops cleanly for an open circuit, no due product, or the
n8n 15-call/45-minute batch boundary.

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
