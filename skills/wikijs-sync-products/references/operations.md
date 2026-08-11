# Operations

## Runtime ownership

n8n is the only recurring scheduler. `PV Wiki - Product Cycle` starts at
08:05 Asia/Shanghai (00:05 UTC) on the first day of every month, preserving the
first-day quota cadence. It calls the worker serially until a clean stop or the
batch boundary, with at most 15 `/run-one` calls per workflow execution and no
new call after 45 minutes. A separate hourly trigger enters at
`Initialize Batch`, so due/backoff work resumes without waiting for another
catalogue sync or a person. A daily 03:17 catalogue recovery calls
`Refresh Catalogue`, which retries source ingestion without waking
quota-paused products. `PV Wiki - Homepage Refresh` runs daily at 02:35
Asia/Shanghai. The worker still processes at most one leased product per call,
so every product attempt remains bounded and resumable. Hermes is not part of
the steady-state runtime. Catalogue sync has a separate serialized lock from
product research, so the monthly refresh is not lost if a long product batch
is still active. A changed leased record invalidates that snapshot and is
rescheduled automatically. Catalogue writes share a fence with the final
source check, Wiki mutation, and durable outcome; this closes the publication
race without blocking the paid research phase. Homepage publication has
another serialized lock because it writes a different Wiki path. The supported
topology is exactly one worker replica, with every steady-state mutation routed
through its authenticated HTTP endpoints. Do not horizontally scale the worker
or run direct mutating CLI commands concurrently; the fence is process-local.
Catalogue ingestion is history-preserving: a row absent from a later source
snapshot does not automatically archive/delete its product or remove it from
the homepage. Destructive retirement requires an explicit future policy.

The worker exposes fixed authenticated HTTP operations and no shell. Keep it
on the n8n Docker network without a published host port. n8n Execute Command
and Local File Trigger remain excluded, and the Docker socket must never be
mounted. `GET /healthz` is public liveness only. Authenticated `GET /status`
uses the same Bearer token as the fixed POST operations and returns redacted
queue, circuit, and global-budget readiness without paid probes.

## Cadence and cost

One product cycle starts with a fixed search/extract pass. The AI may then
return either a final proposal or one of six fixed evidence gaps with one or
two supplemental queries. The default budgets allow at most three AI actions,
seven search queries, five unique extract URLs, and a 20-unit Exa
admission budget. Before a call starts, the worker reserves one unit per Search
query or two units per Extract batch. No new research
action starts after 600 seconds. Configure these with
`PV_WIKI_RESEARCH_MAX_ROUNDS`, `PV_WIKI_RESEARCH_MAX_QUERIES`,
`PV_WIKI_RESEARCH_MAX_CREDITS`, and `PV_WIKI_RESEARCH_MAX_SECONDS`; their
runtime ranges cannot exceed the compiled safety ceilings. A run-one lease must
also cover the research deadline plus the bounded AI repair and Wiki mutation
tail; an incompatible short lease is rejected before a product is leased.

After a completed product, the workflow starts another `/run-one` only while
the previous result has `processed=true`, the zero-based run index remains
below 14, and less than 45 minutes has elapsed since batch initialization.
Thus one execution makes at most 15 calls; one already-running request may
finish after the batch time boundary. `Run One Product` has a 45-minute HTTP
timeout and no automatic n8n retry, because a new call may lease a different
product rather than retry the failed one. A request failure ends that workflow
execution for the Error Workflow.

Set optional UTC-wide stop-losses with
`PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT` and
`PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT`; `0` disables the corresponding window.
Each non-zero limit must be at least `PV_WIKI_RESEARCH_MAX_CREDITS`.
Immediately before paid research, the worker reserves that full per-product
maximum; a window pauses when known Exa usage plus the reservation would exceed
its limit. Any unknown or uncertain Exa usage in an enabled window also pauses
fail-closed rather than being invented as a credit value. The leased product
is restored to its exact prior queue class/time, and the clean response
includes `blocked_windows` plus the latest UTC boundary needed when both
windows are blocked. These counters do not price or cap AI tokens; set
provider-account AI spend limits and alerts as a second control before
activation.

HTTP 429 is a short request-rate limit and retains bounded Retry-After
handling. Exa HTTP 402 permanently skips that key for
the current worker call; another configured key is tried
immediately. When every key is exhausted, the active product is scheduled for
the first instant of the next UTC month without increasing its failure count,
and the n8n loop ends. That first response reports
`pause_scope=product_and_provider`; subsequent products are restored with
`pause_scope=system`. The next monthly catalogue sync, or a manual sync after
installing a new key, wakes quota-paused products before processing resumes. If
an earlier query in the same action completed before the explicit quota
response, its known credits are audited and the action remains retryable after
the wake; only an ambiguous network/provider result is replay-suppressed.

Do not enable provider deep-research/agent endpoints or automatically increase
URL/token limits. The
AI can propose search text but cannot provide a domain allowlist, grant source
trust, call Wiki.js, or change a budget. No new URL/evidence, a local
validation gap at the round limit, or any exhausted budget ends the attempt as
a normal machine-handled outcome. The last admitted AI action is final-only;
if the model still requests another search, the worker discards those queries
and records the fixed evidence gap conservatively instead of raising a provider
error. Record ordinary source conflicts the same way and let the worker retry
them on its normal schedule. They do not create
per-product issues; reserve notification for a systemic condition that
prevents the batch from progressing.

Extracted content is bounded before it reaches the model and the full body is
never returned by the worker HTTP response or saved in PostgreSQL audit
details.
The audit stores source URLs, usage counters, selected model name, decision
metadata (including each required supporting quote, capped at 500 characters),
and the Wiki path.

## User-configured AI

The operator owns the provider choice:

```dotenv
AI_BASE_URL=https://provider.example/v1
AI_API_KEY=...
AI_MODEL=...
AI_TIMEOUT_SECONDS=300  # recommended for DeepSeek thinking over full evidence
AI_MAX_TOKENS=4096
AI_THINKING_MODE=  # optional: enabled or disabled
AI_REASONING_EFFORT=  # optional: high or max
AI_MAX_EVIDENCE_CHARS=80000
```

The worker uses OpenAI-compatible Chat Completions at
`{AI_BASE_URL}/chat/completions`. It rejects non-loopback HTTP unless explicitly
enabled, as well as redirects, oversized responses, ambiguous JSON, and
responses that are not a single JSON object. A trusted private HTTP endpoint requires the explicit
`AI_ALLOW_INSECURE_HTTP=true` opt-in. Provider response bodies and API keys are
not included in errors. `AI_THINKING_MODE` is omitted by default for protocol
compatibility. When set to `enabled` or `disabled`, the request includes
`"thinking":{"type":"<mode>"}`; use it only with a provider that documents
that extension. `AI_REASONING_EFFORT` is also omitted by default; when set,
the request includes `reasoning_effort`, with `high` being DeepSeek's shortest
supported effort. Explicitly selecting `disabled` prevents a supported
reasoning-by-default model from spending the bounded output allowance on
reasoning before it returns the required JSON.

For a registered catalogue supplier, the AI receives only the public
manufacturer alias and configured official hostnames. A matching exact-model
HTTPS extract from that registry does not require independent-domain
corroboration, but runtime validation still owns the trust decision. An AI
wall-clock timeout closes the current product as `ai_error` and returns a
normal processed/non-publish response so the n8n batch continues.

The model is an untrusted proposer. It receives only reader-facing product
fields, bounded search-provider titles/snippets as discovery hints, and successful
bounded extracts; failed-extract errors are withheld. It discovers the public
manufacturer and product type without receiving or routing on `family_code`.
The runtime strips/overwrites model attempts to
set `schema_version`, `product_id`, or `lease_token`, requires the proposed
model to match the catalogue name, allows series documents containing sibling
models into analysis, and requires grounded fact evidence. A prose or
target-only row uses one exact model/label/value quote. A Markdown-pipe or TSV
table may instead provide an exact `model_quote` header row and exact parameter
`quote` row; the runtime verifies one unique target-model column and the
selected value in that same column.

The bundled `pv_wiki/suppliers.json` file is the normal operator-owned mapping
from catalogue `brand_code` to public manufacturer/category identity and
role-labelled official hosts. `PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` and
`PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` are deployment overrides. A domain entry
is usable only with the corresponding public alias, and AI output cannot
select or replace either source of operator authority. Registered manufacturers
are searched on their official hosts first; open-web discovery is a bounded
fallback and does not grant trust. If no trusted-domain entry matches, the
local gate can automatically verify only
an HTTPS manufacturer host whose name is consistent with the public
manufacturer and whose extract contains both that manufacturer and the
complete catalogue model. A second independent HTTPS extract must corroborate
the identity. Each specification fact needs its exact quote from the verified
primary manufacturer datasheet, but not a duplicate quote from the independent
identity source. The model cannot grant trust by itself. A failed check becomes
`source_unverified`, is
audited, and receives automatic backoff without a manual escalation. The local
decision validator runs before any Wiki.js mutation.

Changing provider URL or model is an operator configuration change. Run one
private/unpublished product at the final prefix and re-review quality before
continuing a published schedule. Use a separate state volume for a different
staging prefix. Rotate the key independently of all other credentials. A
definitive AI 401/402/403/404 or Exa 401/403/404 opens a provider-global
circuit for six hours, and either provider's 429 opens one for an hour. Exa
402 rotates to another configured key; exhaustion of every key opens a circuit
through the next UTC month. Matching later products stop before
web research instead of repeatedly spending credits against a known-bad path;
three distinct products with invalid AI output in one hour also open a
provider-scoped output circuit. Changing endpoint, account key, or model
creates a different circuit scope. Decision, provider-rejection, rate-limit,
quota, and invalid-output circuits are checked after local-only work and before
paid research. An open circuit finishes the short lease as audit-only
`system_paused`, restores the exact prior queue state, and returns a clean
`processed=false` stop. Provider rejection/quota responses include their exact
resume time; decision/output responses expose their bounded window through
authenticated status. No unrelated product is marked failed and no n8n retry
is needed. Empty-identity handling, content quarantine, and same-source
Wiki-only publication recovery remain available while a research circuit is
open.

## PostgreSQL boundaries and transport

`PGSSLMODE` is mandatory. Prefer `verify-full` with the appropriate trusted CA
and set `PGSSLROOTCERT` to its read-only mounted path so both certificate chain
and hostname are checked. The runtime accepts the six standard libpq modes but
requires one to be selected explicitly. `disable`, `allow`, and `prefer` may
use an unencrypted connection; select one only when the catalogue cannot use
TLS and the operator has accepted that network risk. Blank and unknown modes
are refused before opening a connection.

The product role must remain SELECT-only. Every catalogue read starts a
read-only transaction. n8n uses its own PostgreSQL database/user and Wiki.js
uses its own database; neither shares product tables or credentials.

Run `pv-wiki doctor` for configuration-only checks and `pv-wiki doctor --live`
for a read-only catalogue probe and Wiki.js read probe. Doctor validates AI and
search-provider configuration but intentionally spends no search credits or AI
tokens.

## State and retries

Production state uses the dedicated database configured by
`PV_WIKI_STATE_DATABASE_URL`. The database account must be a non-superuser
dedicated to PV Wiki, and the worker should reach it only over a private Docker
network. Back it up with a consistent `pg_dump`. New/changed catalogue rows
become due, claims have finite leases, and expired claims are audited. A local
SQLite path remains a compatibility option for tests and legacy recovery, not
the production deployment.

Each attempt has a schema-v9 `research_actions` ledger. Before every Search,
Extract, or AI action, the worker stores its round, action name, and request
fingerprint as `started`. A completed action stores only bounded URLs, request
IDs, outcome/gap metadata, physical AI request counts, and known credit
counts—never extract bodies or prompts. An action slot cannot replay within an
attempt. If an earlier attempt has a `started` or `uncertain` action, the
worker compares that prior action's provider/wire scope with the corresponding
current scope before any new provider call. Search, Extract, and AI scopes are
independent; unrelated key or timeout changes do not unlock a possibly charged
request, and unknown legacy scopes block fail-closed. A match records
`research_uncertain`. An explicit quota or definitive 4xx response is `failed`
even after an earlier completed subrequest; known partial credits are audited
and the bounded action remains retryable. Only network/5xx/malformed-response
cases whose execution result is genuinely unknown stay `uncertain`.
Completed calls may be repeated after a worker crash because response bodies
are intentionally not persisted. A validated publish decision whose only
failure was Wiki.js is recovered from the audit and retries the stable Wiki
path without repeating Search, Extract, or AI.

Extract URLs must come from completed search candidates. All non-empty,
bounded successful extracts may be used by AI for manufacturer/type discovery.
Only URLs with an exact full catalogue identity can become publication evidence
or support a durable `out_of_scope` result. Scope exclusion additionally needs
a contiguous quote tying that identity to an explicit generic-hardware type;
accessory mentions and negation fail closed. If the catalogue identity itself
names the hardware, a quote may mention its solar-mounting use only when it
also explicitly states that the item is a nut, bolt, or fastener. The publish
path independently rejects a generic-hardware model or public category.
Every publication citation must be in that exact-match set. A series document
may contain sibling models. Ordinary prose still requires a target-only
model/label/value span; an explicit Markdown/TSV table may use separate
`model_quote` and parameter `quote` rows only when their cell counts match, the
target occurs in one unique header cell, and the selected value is unambiguous
in the same column. Other multi-model rows fail closed into an audited
non-publish outcome rather than guessing a column.
The low-level `publish` command therefore requires `--evidence-file` for a
publish outcome. The fixed n8n `run-one` operation passes the bounded evidence
in memory and does not persist source bodies.

`pv-wiki status` reports aggregate research action/credit counts. With
`--product-id`, it also returns the bounded per-action ledger alongside the
last attempts.

When both fresh `due` items and matured `backoff` items are available,
`lease_next` uses a 4:1 weighted preference and falls back to the other class
when the preferred class is empty. Alternating content outcomes still count
toward one source-revision streak. After six consecutive content failures
(`ambiguous`, `insufficient_identity`, `invalid_decision`, `no_datasheet`,
`out_of_scope`, or `source_unverified`), the next queue visit records
`content_quarantined` without another paid provider call. It is scheduled for
the annual refresh; any catalogue source-hash change resets the relevant
revision history and makes the product immediately due.

Retry behavior:

- all selected-provider keys out of budget: first instant of the next UTC month,
  without increasing the product failure count;
- no datasheet, ambiguity, or insufficient identity: about 30, 90, then 180
  days;
- unresolved paid research delivery (`research_uncertain`): about 30, 90, then
  180 days, with provider replay blocked while the relevant action/provider
  scope remains unchanged;
- source verification failure: about 7, 30, 90, then 365 days;
- matching generic hardware or another out-of-scope item: recheck after 365
  days;
- six consecutive content failures for one source revision:
  `content_quarantined`, recheck after 365 days or immediately after a source
  change;
- search provider, AI, Wiki.js, or another transient service error: about 1, 6, then 24
  hours for the same outcome;
- invalid AI decision contract: about 24 hours, 3 days, 7 days, then 30 days;
- Wiki.js edit conflict: about 24 hours;
- published: recheck after 365 days;
- source changed during a lease: immediately due again.

Inspect queue/audit metadata with `pv-wiki status` and n8n execution history.
Do not delete or truncate state tables to force a retry. Let the state-aware
schedule run or correct the source record when catalogue data itself is wrong.

Every valid non-publish result is a normal machine-handled outcome. The worker
stores it in PostgreSQL, returns a successful product-cycle response, and
schedules the next attempt. It does not create an AI issue or a manual-review
task.

## Wiki.js visibility and homepage

New pages remain private and unpublished by default. Existing pages retain
their current visibility. Enable public new-page settings only after checking
Wiki.js group permissions and staging output.

The worker replaces only `PV-WIKI-AUTO`. It also recognizes one legacy
`HERMES-AUTO` block and migrates it in place; text outside the block remains
unchanged. A detected edit conflict fails the current attempt instead of
knowingly overwriting a newer human revision.

Homepage refresh reads already successful publications from PostgreSQL. It
does no search-provider or catalogue work. A product remains in homepage counts
after a later source change or temporary retry because the last successful Wiki
page still exists.

## n8n monitoring

Use `/healthz/readiness` for n8n readiness because it includes database
connection/migration state. Use the worker's public `/healthz` only for local
process/state-schema liveness. Query authenticated `GET /status` with
`Authorization: Bearer <PV_WIKI_WORKER_TOKEN>` for redacted due counts, global
budget pauses, AI configuration, and circuit readiness. Configure an n8n Error
Workflow with the operator's chosen
email/chat/incident channel only for a failed workflow execution or a systemic
provider, configuration, database, or service condition. Treat the execution as
one batch-level incident; do not route normal product outcomes to an issue
tracker or notification channel. Five consecutive `invalid_decision` outcomes
inside 30 minutes open the decision circuit before another paid research
action; this converts a likely model/contract regression into one cleanly
stopped batch rather than thousands of product alerts.

Run `docker compose --env-file .env -f compose.yaml exec -T n8n n8n audit`
after installation and upgrades (or the discovered instance's equivalent).
Review risky nodes, unused credentials, unprotected webhooks, filesystem
findings, and version findings. The bundled workflows should contain only
Manual/Schedule Trigger, HTTP Request, and the boolean IF node that controls the
serial product loop.

Keep execution pruning enabled. The deployment default retains about 14 days
(`336` hours); adjust this to the organization's audit/retention policy without
retaining source bodies or secrets.

## Backups, upgrades, and rollback

Back up:

- n8n PostgreSQL data;
- `/home/node/.n8n`, including the encryption key/local assets;
- PV Wiki PostgreSQL state;
- current workflow exports;
- the deployment manifest and pinned image/source revisions.

Test restoration, not only backup creation. Losing `N8N_ENCRYPTION_KEY` can
make n8n credentials unreadable even when its PostgreSQL data survives.

Before an upgrade, stop/pause the PV Wiki schedules, export workflows, back up
all three stores, review release notes, change one pinned version, and repeat
manual acceptance. Roll back by restoring the previous image/source revision
and compatible state backup. Do not automatically roll back published Wiki.js
pages because a person may have edited them afterward.

On a shared n8n, uninstall only PV Wiki workflows and their worker credential.
On a dedicated stack, stop containers but retain volumes by default. Volume
deletion, state deletion, page deletion, and external key revocation are
separate destructive operations requiring explicit approval.
