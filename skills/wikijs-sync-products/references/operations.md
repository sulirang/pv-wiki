# Operations

## Runtime ownership

n8n is the only recurring scheduler. `PV Wiki - Product Cycle` starts at
08:05 Asia/Shanghai (00:05 UTC) on the first day of every month, shortly after
Tavily's documented first-day credit reset. It calls the worker serially until
no product is due or every configured Tavily key has exhausted its monthly
plan/pay-as-you-go allowance. `PV Wiki - Homepage Refresh` runs daily at 02:35
Asia/Shanghai. The worker still processes at most one leased product per call,
so every product attempt remains bounded and resumable. Hermes is not part of
the steady-state runtime.

The worker exposes fixed authenticated HTTP operations and no shell. Keep it
on the n8n Docker network without a published host port. n8n Execute Command
and Local File Trigger remain excluded, and the Docker socket must never be
mounted.

## Cadence and cost

One product cycle uses at most three basic Tavily searches and five advanced
extract URLs. The default workflow immediately starts the next due product
after a completed cycle, consuming the available monthly Tavily credits as
quickly as the serial Search → Extract → AI → Wiki.js path permits. It stops
without an n8n error when the queue is empty or all keys are out of credits.
Set provider-side Tavily and AI monthly alerts before activating the workflow.

Tavily HTTP 429 is a short request-rate limit and retains bounded Retry-After
handling. HTTP 432 (plan limit) and 433 (pay-as-you-go limit) permanently skip
that key for the current worker call; another configured key is tried
immediately. When every key is exhausted, the active product is scheduled for
the first instant of the next UTC month without increasing its failure count,
and the n8n loop ends. The next monthly catalogue sync, or a manual sync after
installing a new key, wakes quota-paused products before processing resumes.

Do not enable Tavily Research or automatically increase URL/token limits.
Escalate a genuinely important source conflict through a supervised run.

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
fields plus successful, bounded Tavily extracts; search snippets and failed
extract errors are withheld. The runtime strips/overwrites model attempts to
set `schema_version`, `product_id`, or `lease_token`, requires the proposed
model to match the catalogue name, allows series documents containing sibling
models into analysis, requires a short exact target-model-only model/label/value
span for every unique fact, and accepts trusted source types only for
the exact `brand_code` mapping in the operator-approved
`PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON`. It then applies the local decision
validator before any Wiki.js mutation.

Changing provider URL or model is an operator configuration change. Run one
private/unpublished product at the final prefix and re-review quality before
continuing a published schedule. Use a separate state volume for a different
staging prefix. Rotate the key independently of all other credentials.

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

Each attempt persistently budgets one Search batch and one Extract batch.
Extract URLs must come from that lease's search candidates. Only URLs that
Tavily successfully extracted with non-empty bounded content, an exact full
model match can become evidence. Every decision citation must be in that
successful set. A series document may contain sibling models, but every
published fact still needs an exact target-model-only span. Ambiguous
multi-model table rows fail closed for supervised handling rather than guessing
a column.
The low-level `publish` command therefore requires `--evidence-file` for a
publish outcome. The fixed n8n `run-one` operation passes the bounded evidence
in memory and does not persist source bodies.

Retry behavior:

- all Tavily keys out of monthly credits: first instant of the next UTC month,
  without increasing the product failure count;
- no datasheet, ambiguity, or insufficient identity: about 30, 90, then 180
  days;
- Tavily, AI, Wiki.js, validation, or other transient error: about 1, 6, then
  24 hours;
- Wiki.js edit conflict: about 24 hours;
- published: recheck after 365 days;
- source changed during a lease: immediately due again.

Inspect queue/audit metadata with `pv-wiki status` and n8n execution history.
Do not delete the state file to force a retry. Use a supervised state-aware
operation or correct the source record.

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
email/chat/incident channel after the corresponding credential is installed.

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
