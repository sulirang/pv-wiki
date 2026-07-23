# Operations

## Runtime ownership

n8n is the only recurring scheduler. `PV Wiki - Product Cycle` starts at
08:05 Asia/Shanghai (00:05 UTC) on the first day of every month, shortly after
Tavily's documented first-day credit reset. It calls the worker serially until
no product is due or every configured Tavily key has exhausted its monthly
plan/pay-as-you-go allowance. A separate hourly trigger enters directly at
`Run One Product`, so due/backoff work resumes without waiting for another
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
mounted.

## Cadence and cost

One product cycle starts with a fixed search/extract pass. The AI may then
return either a final proposal or one of six fixed evidence gaps with one or
two supplemental queries. The default budgets allow at most three AI actions,
seven basic search queries, five unique extract URLs, and a 20-credit Tavily
admission budget. Before a call starts, the worker reserves one credit per
basic Search query or two credits per advanced Extract batch. No new research
action starts after 600 seconds. Configure these with
`PV_WIKI_RESEARCH_MAX_ROUNDS`, `PV_WIKI_RESEARCH_MAX_QUERIES`,
`PV_WIKI_RESEARCH_MAX_CREDITS`, and `PV_WIKI_RESEARCH_MAX_SECONDS`; their
runtime ranges cannot exceed the compiled safety ceilings. A run-one lease must
also cover the research deadline plus the bounded AI repair and Wiki mutation
tail; an incompatible short lease is rejected before a product is leased.

The default workflow immediately starts the next due product after a completed
cycle, consuming the available monthly Tavily credits as quickly as the
bounded inner research loop permits. It stops without an n8n error when the
queue is empty or all keys are out of credits. The Run One HTTP timeout is 45
minutes, covering the maximum allowed 20-minute research admission window plus
the bounded AI-repair and duplicate-create-safe Wiki mutation tail. Set
provider-side Tavily and AI monthly alerts before activating the workflow.

Tavily HTTP 429 is a short request-rate limit and retains bounded Retry-After
handling. HTTP 432 (plan limit) and 433 (pay-as-you-go limit) permanently skip
that key for the current worker call; another configured key is tried
immediately. When every key is exhausted, the active product is scheduled for
the first instant of the next UTC month without increasing its failure count,
and the n8n loop ends. The next monthly catalogue sync, or a manual sync after
installing a new key, wakes quota-paused products before processing resumes. If
an earlier query in the same action completed before the explicit quota
response, its known credits are audited and the action remains retryable after
the wake; only an ambiguous network/provider result is replay-suppressed.

Do not enable Tavily Research or automatically increase URL/token limits. The
AI can propose search text but cannot provide a domain allowlist, grant source
trust, call Wiki.js, or change a budget. No new URL/evidence, a local
validation gap at the round limit, or any exhausted budget ends the attempt as
a normal machine-handled outcome. Record ordinary source conflicts the same
way and let the worker retry them on its normal schedule. They do not create
per-product issues; reserve notification for a systemic condition that
prevents the batch from progressing.

Extracted content is bounded before it reaches the model and the full body is
never returned by the worker HTTP response or saved in SQLite audit details.
The audit stores source URLs, usage counters, selected model name, decision
metadata (including each required supporting quote, capped at 500 characters),
and the Wiki path.

## User-configured AI

The operator owns the provider choice:

```dotenv
AI_BASE_URL=https://provider.example/v1
AI_API_KEY=...
AI_MODEL=...
AI_TIMEOUT_SECONDS=60
AI_MAX_TOKENS=4096
AI_MAX_EVIDENCE_CHARS=80000
```

The worker uses OpenAI-compatible Chat Completions at
`{AI_BASE_URL}/chat/completions`. It rejects non-loopback HTTP unless explicitly
enabled, as well as redirects, oversized responses, ambiguous JSON, and
responses that are not a single JSON object. A trusted private HTTP endpoint requires the explicit
`AI_ALLOW_INSECURE_HTTP=true` opt-in. Provider response bodies and API keys are
not included in errors.

The model is an untrusted proposer. It receives only reader-facing product
fields, bounded Tavily search titles/snippets as discovery hints, and successful
bounded extracts; failed-extract errors are withheld. It discovers the public
manufacturer and product type without receiving or routing on `family_code`.
The runtime strips/overwrites model attempts to
set `schema_version`, `product_id`, or `lease_token`, requires the proposed
model to match the catalogue name, allows series documents containing sibling
models into analysis, and requires a short exact target-model-only
model/label/value span for every unique fact. The optional
`PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` is an explicit override and fast path for
known domains, not a required complete registry. If no entry matches, the local
gate can automatically verify only an HTTPS manufacturer host whose name is
consistent with the AI-discovered manufacturer and whose extracted body
contains both that manufacturer and the complete catalogue model. A second
independent HTTPS extract must corroborate the identity, and every fact needs
exact quotes from both non-community domains. The model cannot grant trust by
itself. A failed check becomes `source_unverified`, is
audited, and receives automatic backoff without a manual escalation. The local
decision validator runs before any Wiki.js mutation.

Changing provider URL or model is an operator configuration change. Run one
private/unpublished product at the final prefix and re-review quality before
continuing a published schedule. Use a separate state volume for a different
staging prefix. Rotate the key independently of all other credentials. A
definitive AI 401/402/403/404 opens a provider-global circuit for six hours,
and AI 429 opens one for an hour. Matching later products stop before Tavily
research instead of repeatedly spending credits against a known-bad AI path;
three distinct products with invalid AI output in one hour also open a
provider-scoped output circuit. Changing endpoint, account key, or model
creates a different circuit scope. Configuration rejection and repeated
invalid-output circuits return HTTP 503 after durably recording the outcome,
so n8n's bounded retries can produce one systemic alert. The 429 circuit
returns a clean stop and relies on automatic backoff.

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
for a read-only catalogue probe and Wiki.js read probe. Doctor validates AI
configuration but intentionally spends no Tavily credits or AI tokens.

## State and retries

The default local state is
`~/.local/state/pv-wiki/state.sqlite3`; the container uses
`/data/state.sqlite3` on the `pv_wiki_state` volume. Back it up. New/changed
catalogue rows become due, claims have finite leases, and expired claims are
audited.

Each attempt has a schema-v7 `research_actions` ledger. Before every Search,
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
Every publication citation must be
in that exact-match set. A series document may contain sibling models, but every
published fact still needs an exact target-model-only span. Ambiguous
multi-model table rows fail closed into an audited non-publish outcome and
automatic retry rather than guessing a column or opening a review issue.
The low-level `publish` command therefore requires `--evidence-file` for a
publish outcome. The fixed n8n `run-one` operation passes the bounded evidence
in memory and does not persist source bodies.

`pv-wiki status` reports aggregate research action/credit counts. With
`--product-id`, it also returns the bounded per-action ledger alongside the
last attempts.

Retry behavior:

- all Tavily keys out of monthly credits: first instant of the next UTC month,
  without increasing the product failure count;
- no datasheet, ambiguity, or insufficient identity: about 30, 90, then 180
  days;
- unresolved paid research delivery (`research_uncertain`): about 30, 90, then
  180 days, with provider replay blocked while the relevant action/provider
  scope remains unchanged;
- source verification failure: about 7, 30, 90, then 365 days;
- matching generic hardware or another out-of-scope item: recheck after 365
  days;
- Tavily, AI, Wiki.js, or another transient service error: about 1, 6, then 24
  hours for the same outcome;
- invalid AI decision contract: about 24 hours, 3 days, 7 days, then 30 days;
- Wiki.js edit conflict: about 24 hours;
- published: recheck after 365 days;
- source changed during a lease: immediately due again.

Inspect queue/audit metadata with `pv-wiki status` and n8n execution history.
Do not delete the state file to force a retry. Let the state-aware schedule run
or correct the source record when catalogue data itself is wrong.

Every valid non-publish result is a normal machine-handled outcome. The worker
stores it in SQLite, returns a successful product-cycle response, and schedules
the next attempt. It does not create an AI issue or a manual-review task.

## Wiki.js visibility and homepage

New pages remain private and unpublished by default. Existing pages retain
their current visibility. Enable public new-page settings only after checking
Wiki.js group permissions and staging output.

The worker replaces only `PV-WIKI-AUTO`. It also recognizes one legacy
`HERMES-AUTO` block and migrates it in place; text outside the block remains
unchanged. A detected edit conflict fails the current attempt instead of
knowingly overwriting a newer human revision.

Homepage refresh reads already successful publications from SQLite. It does no
Tavily or catalogue work. A product remains in homepage counts after a later
source change or temporary retry because the last successful Wiki page still
exists.

## n8n monitoring

Use `/healthz/readiness` for n8n readiness because it includes database
connection/migration state. Use the worker's `/healthz` for its local process
and state schema. Configure an n8n Error Workflow with the operator's chosen
email/chat/incident channel only for a failed workflow execution or a systemic
provider, configuration, database, or service condition. Treat the execution as
one batch-level incident; do not route normal product outcomes to an issue
tracker or notification channel. Five consecutive `invalid_decision` outcomes
inside 30 minutes open the decision circuit before another product is leased;
this converts a likely model/contract regression into one stopped batch rather
than thousands of product alerts.

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
- PV Wiki SQLite state;
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
