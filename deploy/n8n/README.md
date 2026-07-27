# n8n deployment

This directory deploys two separate applications:

- n8n, with its own PostgreSQL database and persistent `/home/node/.n8n`
  volume;
- `pv-wiki-worker`, with its own persistent SQLite orchestration state.

The product catalogue PostgreSQL account remains read-only. Wiki.js continues
to own its own database. Neither n8n nor PV Wiki writes tables into either of
those databases.

## New dedicated n8n instance

Run these steps from `deploy/n8n` on the authorized VPS:

1. Confirm Docker Engine, the Compose v2 plugin, Git, DNS control, and a
   backed-up checkout of an exact PV Wiki commit are available. Record that
   commit in the install manifest. Copy `.env.example` to `.env` and
   `worker.env.example` to `worker.env`.
   Set both files to mode `0600`.
2. Generate separate random values for `N8N_DB_PASSWORD`,
   `N8N_ENCRYPTION_KEY`, and `PV_WIKI_WORKER_TOKEN`. Do not reuse API keys.
3. Fill the Exa key plus the user-owned AI,
   Wiki.js, and read-only catalogue settings
   in `worker.env`. `AI_BASE_URL` is an OpenAI-compatible API base path such as
   `https://provider.example/v1`; choose `AI_MODEL` explicitly. Set
   `PV_WIKI_PUBLIC_BRAND_ALIASES_JSON` when an internal catalogue `brand_code`
   needs an operator-approved public manufacturer identity. Configure
   `PV_WIKI_TRUSTED_SOURCE_DOMAINS_JSON` only for narrow trusted hosts, keyed by
   the exact catalogue brand or its configured public alias. AI output cannot
   select or replace either mapping. A trusted-domain entry also requires the
   matching public alias. Without a matching trusted-domain entry,
   the worker may automatically verify an HTTPS manufacturer host when its name
   is consistent with the public manufacturer identity and its extract contains
   both that manufacturer and the complete model. A second independent HTTPS
   extract must corroborate that identity. Specification facts are quoted from
   the verified primary manufacturer datasheet and do not need duplicate quotes
   from the independent source. A failed check is recorded as
   `source_unverified` and retried automatically;
   it does not create a per-product issue or request manual review.
   Optional UTC stop-losses
   `PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT` and
   `PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT` default to `0` (disabled); set them
   before unattended operation if a cross-product credit ceiling is required.
   Each non-zero global limit must be at least
   `PV_WIKI_RESEARCH_MAX_CREDITS`, because the worker reserves that complete
   per-product Exa allowance immediately before paid research. These limits do
   not count AI tokens; retain provider-account AI spend limits and alerts.
   Prefer `PGSSLMODE=verify-full`. For
   `verify-ca` or `verify-full`, set `CATALOGUE_CA_PATH` in `.env` to the
   host's public CA bundle or a private CA PEM; it is mounted read-only and
   exposed to libpq through `PGSSLROOTCERT`. If the catalogue cannot use TLS,
   the user may explicitly select `disable`, `allow`, or `prefer` after
   accepting the network risk; those modes ignore the CA path.
4. Bootstrap n8n on loopback before exposing it publicly:

   ```bash
   docker compose --env-file .env -f compose.yaml -f compose.bootstrap.yaml config --quiet
   docker compose --env-file .env -f compose.yaml -f compose.bootstrap.yaml up -d --build
   ```

   From the operator's computer, create an SSH tunnel:

   ```bash
   ssh -L 5678:127.0.0.1:5678 <user>@<vps>
   ```

   Open `http://localhost:5678`, create the n8n owner account, use a strong
   password, and enable 2FA before enabling any public proxy. This avoids an
   owner-registration race on a new instance.

5. Confirm local n8n readiness and run PV Wiki's non-destructive live doctor:

   ```bash
   curl --fail --silent --show-error "http://127.0.0.1:5678/healthz/readiness"
   docker compose --env-file .env -f compose.yaml exec pv-wiki-worker pv-wiki doctor --live
   ```

6. Import the secret-free workflow templates. Imported workflows remain
   inactive:

   ```bash
   docker compose --env-file .env -f compose.yaml exec -T n8n \
     n8n import:workflow --input=/files/workflows/pv-wiki-product-cycle.json
   docker compose --env-file .env -f compose.yaml exec -T n8n \
     n8n import:workflow --input=/files/workflows/pv-wiki-homepage-refresh.json
   ```

7. In n8n, create one **Header Auth** credential named `PV Wiki Worker`:

   - Header name: `Authorization`
   - Header value: `Bearer <the PV_WIKI_WORKER_TOKEN from worker.env>`

   Attach it to `Sync Catalogue`, `Refresh Catalogue`, `Run One Product`, and
   `Refresh Homepage`. The workflow files intentionally contain no credential
   ID or secret.

8. Before enabling the loop, run one supervised acceptance cycle directly with
   `docker compose --env-file .env -f compose.yaml exec pv-wiki-worker pv-wiki
   run-one --worker-id manual-acceptance`. Keep the final Wiki.js path prefix
   private/unpublished and inspect the exact model match, citations,
   specifications, preserved human text, and worker audit. Use a separate state
   volume if a truly separate staging prefix is required. Then manually run the
   homepage workflow.
9. Point `N8N_DOMAIN` DNS at the VPS. If an HTTPS reverse proxy already exists,
   remove the bootstrap overlay and proxy to `127.0.0.1:N8N_PORT` only after
   the owner account exists. Recreate n8n without the bootstrap environment:

   ```bash
   docker compose --env-file .env -f compose.yaml config --quiet
   docker compose --env-file .env -f compose.yaml up -d --build
   ```

   If ports 80/443 are free and no proxy exists, switch the same Compose
   project to the Caddy overlay:

   ```bash
   docker compose --env-file .env -f compose.yaml -f compose.caddy.yaml config --quiet
   docker compose --env-file .env -f compose.yaml -f compose.caddy.yaml up -d --build
   ```

   After TLS is available, confirm
   `https://<N8N_DOMAIN>/healthz/readiness` before publishing schedules.

10. Only after rollout review, publish the two workflows. The product cycle
    starts at 08:05 Asia/Shanghai on the first day of every month (00:05 UTC),
    then calls `/run-one` serially at most 15 times and does not start a new
    call once 45 minutes has elapsed. A daily 03:17 catalogue recovery refreshes
    source rows without waking quota-paused products, and an hourly
    due-recovery trigger enters at `Initialize Batch`, so scheduled
    product retries and backoff wakeups do not wait for another monthly sync.
    runs daily at 02:35 Asia/Shanghai. For unattended public pages, set
    `WIKIJS_NEW_PAGE_PRIVATE=false` and `WIKIJS_NEW_PAGE_PUBLISHED=true` once in
    `worker.env`; do not add a per-product publication review queue.
11. Run the security audit, configure an n8n Error Workflow for system or
    batch-level failures using the operator's chosen notification channel, and
    record the deployed image tags and workflow IDs. Do not turn normal
    non-publish product outcomes into alerts or issue-tracker tickets:

    ```bash
    docker compose --env-file .env -f compose.yaml exec -T n8n n8n audit
    ```

The worker has no published host port and exposes only fixed authenticated
operations on the Compose network. n8n's Execute Command node stays excluded,
and the Docker socket is never mounted.

Worker `GET /healthz` is public liveness only. `GET /status` requires the same
Bearer Header Auth credential as worker POST operations and reports redacted
queue, circuit, and global-budget readiness without making paid provider
requests.

## Reusing an existing n8n

Do not start the included n8n or `n8n-db` services beside an instance you do
not own. First identify its owner, version, URL, storage, encryption-key backup,
reverse proxy, and Docker network. Export any existing workflows with the same
names before importing these templates.

For an existing Dockerized n8n, select one network already attached to that
container and set its exact name as `N8N_EXTERNAL_NETWORK` in `.env`. Start
only the worker:

```bash
docker compose --env-file .env -f compose.existing-n8n.yaml config --quiet
docker compose --env-file .env -f compose.existing-n8n.yaml up -d --build
```

The existing n8n can then resolve the unchanged workflow URL
`http://pv-wiki-worker:8080`. Do not attach the worker to a database-only
network.

For a host/systemd n8n, bind the worker to host loopback only:

```bash
docker compose --env-file .env -f compose.systemd-n8n.yaml config --quiet
docker compose --env-file .env -f compose.systemd-n8n.yaml up -d --build
```

Before import, change the four HTTP Request node URLs from
`http://pv-wiki-worker:8080/...` to `http://127.0.0.1:8080/...`. This loopback
form is for a host n8n only; `127.0.0.1` inside an n8n container would address
that container itself.

Keep the worker unexposed to the public Internet. Create the Header Auth
credential in the existing n8n, attach it, run a manual private/unpublished
test, and let the user publish the schedules. Never discover n8n by scanning
unrelated hosts; inspect only the user-authorized VPS and supplied URL.

`Sync Catalogue`, `Refresh Catalogue`, and `Refresh Homepage` have bounded
retries because they are idempotent. `Run One Product` deliberately has no
automatic retry: each new `/run-one` call may lease a different product, so an
n8n retry would not retry the same item. Its 45-minute HTTP timeout covers the
maximum supported research plus AI/Wiki tail. An overlapping `/run-one`
returns the clean stop reason `worker_busy`; a failed request stops the current
workflow execution and is handled once by the Error Workflow. Catalogue sync
uses a separate serialized lane, so the monthly refresh
cannot be lost merely because a product batch is still running; a changed
leased source is invalidated and rescheduled. A shared fence covers the final
source check, Wiki mutation, and durable outcome; catalogue writes wait for
that short publication tail instead of racing it. Both catalogue HTTP nodes
therefore have a 15-minute timeout. Homepage publication has its own lane and
targets a different Wiki path. Keep exactly one `pv-wiki-worker` replica and
route all steady-state mutations through its HTTP endpoints; the publication
fence is intentionally process-local and does not support concurrent mutating
CLI commands or horizontal worker scaling. The workflow makes at most 15 calls
and stops starting calls after 45 minutes; one already-running call is allowed
to finish. A successful processed result loops back to `Run One Product`; any
clean `processed=false` response, including `no_due_product`, global
research-budget exhaustion, provider quota, or an open research circuit, stops
the execution. Exa HTTP 402 rotates keys and stops only after all configured
keys are exhausted.

`Refresh Catalogue` is the daily recovery path for incomplete source scans. It
upserts current rows but deliberately does not wake products paused for
provider quota exhaustion. `Sync Catalogue` is reserved for the first-of-month or
explicit new-key start and does wake those waits. Source disappearance is
history-preserving: neither endpoint infers that an absent row should delete,
archive, or remove an existing Wiki page from navigation.

n8n is the outer supervisor only. One `/run-one` call may contain an initial
research pass and up to two AI-requested supplemental passes inside the
worker. Defaults are three AI actions, seven search queries, five unique
extract URLs, a 20-unit admission budget with per-action reservation,
and a 600-second new-action deadline. The worker persists a request fingerprint
before every Search, Extract, and AI action. Any unresolved `started` or
`uncertain` action suppresses later calls while its action-specific
provider/wire scope still matches. Search, Extract, and AI scopes
are independent; unrelated key and timeout changes cannot unlock a possibly
charged request, while unknown legacy scopes block fail-closed. Suppression
becomes the machine outcome `research_uncertain`; explicit non-executed
quota/4xx failures remain retryable. If such a definitive response follows an
earlier completed query or invalid AI response, the known partial work is
audited and may be repeated by a later bounded attempt; it is not treated as
an ambiguous replay. Keep Exa, AI, catalogue, and Wiki.js credentials in
`worker.env`, not in n8n or an AI Agent node.

The optional `PV_WIKI_GLOBAL_DAILY_CREDIT_LIMIT` and
`PV_WIKI_GLOBAL_MONTHLY_CREDIT_LIMIT` are UTC-wide Exa stop-losses across all
products. Zero disables a window. Immediately before paid research, the worker
reserves the full `PV_WIKI_RESEARCH_MAX_CREDITS` allowance. It pauses
fail-closed until the next UTC day/month if remaining headroom cannot cover
that reservation or if any Exa usage is unknown/uncertain; therefore each
enabled global limit must be at least the per-product maximum. The short lease
is audited as `system_paused` and the product's prior queue state is restored.
When both windows are blocked, `resume_at` is the later boundary and
`blocked_windows` lists both periods.
AI tokens/currency are outside these counters and require provider-side account
limits. Eligible new/due work and matured backoff work are selected with an
approximately 4:1 preference so neither class starves when both remain
available.

Valid non-publish results, including `no_datasheet`, `ambiguous`,
`insufficient_identity`, `out_of_scope`, `source_unverified`, and
`research_uncertain`, return a
successful product-cycle response, are stored in the worker audit, and use
automatic queue backoff. They do not create per-product AI issues. Reserve
notifications for a failed workflow execution or a systemic condition that
prevents the batch from progressing. Five consecutive `invalid_decision`
results on distinct products within 30 minutes open the batch decision circuit
before another paid research action. AI 401/402/403/404 or Exa 401/403/404
opens a six-hour provider circuit, either provider's 429 opens a one-hour
rate-limit circuit, and exhaustion of all Exa keys opens through the next UTC
month. Three
distinct products with systemic invalid AI output inside one hour also open a
provider-scoped output circuit. These gates run after local-only handling but
before paid research. While open, `/run-one` audits `system_paused`, restores
the exact prior queue state, and returns a clean `processed=false` response; it
does not record an unrelated product failure or rely on an n8n retry.
Empty-identity handling, content quarantine, and same-source Wiki-only
publication recovery remain available.

After six consecutive content outcomes (`ambiguous`,
`insufficient_identity`, `invalid_decision`, `no_datasheet`, `out_of_scope`, or
`source_unverified`) for the same unchanged source revision, the next queue
visit records `content_quarantined` without another provider call. The product
then waits about one year, while a changed catalogue source hash wakes it
immediately.

For multi-model datasheets, the AI may submit a structured evidence item with
an exact `model_quote` header row and exact parameter `quote` row from the same
Markdown-pipe/TSV table. Publication requires one unique target-model column
and an unambiguous value in that same column. The verified primary manufacturer
datasheet supplies specification facts; an independent source is required only
to corroborate the manufacturer and complete model when automatic domain
verification is used.

After adding or replacing an Exa key, update `EXA_API_KEYS`, then recreate the
worker so it receives the new environment:

```bash
docker compose --env-file .env -f compose.yaml up -d --no-deps --force-recreate pv-wiki-worker
```

Run `Manual / New Key Start` once after the recreated worker is healthy to wake
quota-paused products immediately and validate the replacement. Without that
manual start, their existing first-of-next-UTC-month wake time remains intact
and the hourly trigger resumes them then. Product-level backoff remains stored
by the worker.

## Backups and removal

Back up all three persistent stores:

- `n8n_db_data` for workflows, credentials, and executions;
- `n8n_data` for the n8n encryption material and local assets;
- `pv_wiki_state` for product leases, evidence audit, and publication history.

The included `deploy/backup/pv-wiki-backup` defaults are intentionally specific
to the current VPS layout: a shared n8n using
`/opt/ai-agents/n8n/data/database.sqlite` plus the existing PV Wiki Docker
volume. It is not a generic backup for this directory's dedicated Compose
topology. A dedicated n8n deployment here uses PostgreSQL and must add a
consistent `pg_dump` of `n8n-db` (plus `n8n_data`, worker SQLite, encryption
configuration, and workflow exports), or replace/override the installed backup
service with an equivalent deployment-specific command. Do not run the
SQLite-only defaults and assume the dedicated n8n database was captured.

On a shared n8n, removal means deactivating/exporting/removing only the two PV
Wiki workflows and revoking their Header Auth credential. Do not stop the
shared n8n. On a dedicated stack, stop the exact project shape that was
started; both commands retain volumes:

```bash
# Existing reverse proxy / base stack
docker compose --env-file .env -f compose.yaml stop

# Bundled Caddy stack
docker compose --env-file .env -f compose.yaml -f compose.caddy.yaml stop
```

Deleting volumes/state, deleting Wiki.js pages, or revoking external
credentials are separate destructive actions that require explicit approval.
Published Wiki.js pages are not automatically rolled back, because they may
contain later human edits.
